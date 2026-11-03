"""文献解读：为入选文献生成结构化中文解读（自动或按需）。

解读依据两种：有 Europe PMC OA 全文（fulltexts.status='ok'）时按全文解读（basis='fulltext'），
否则只依据摘要（basis='abstract'）——提示词会明确禁止编造摘要未提供的细节。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from .. import repo
from ..fulltext import describe as fulltext_describe
from ..llm import LLM, LLMError
from ..settings import Settings
from .score import _context, _variables

log = logging.getLogger("reprohub.interpret")
Logger = Callable[[str], None]

MOCK_BANNER_ABSTRACT = "> ⚠️ **模拟解读**：未调用任何模型，以下内容为从原文摘要抽取的句子，不含评价。配置 API key 后可重新生成。\n\n"
MOCK_BANNER_FULLTEXT = "> ⚠️ **模拟解读**：未调用任何模型，以下内容为从 Europe PMC OA 全文中抽取的句子（已去图表与参考文献），不含评价。配置 API key 后可重新生成。\n\n"
BASIS_LABEL = {"fulltext": "全文（Europe PMC OA 全文，已去图表与参考文献）", "abstract": "摘要"}


def material_for(conn: sqlite3.Connection, it: dict) -> tuple[str, dict | None, str | None]:
    """返回 (basis, 全文记录, 全文文本)。有可用全文才是 'fulltext'。"""
    ft = repo.get_fulltext(conn, it["id"], with_text=True)
    text = (ft or {}).get("text") or ""
    if ft and ft.get("status") == "ok" and text.strip():
        return "fulltext", ft, text
    return "abstract", ft, None


def build_markdown(c: dict, it: dict, *, is_mock: bool, basis: str = "abstract") -> str:
    parts = []
    if is_mock:
        parts.append(MOCK_BANNER_FULLTEXT if basis == "fulltext" else MOCK_BANNER_ABSTRACT)
    parts.append(f"**一句话结论**：{c.get('one_liner', '')}\n")
    if c.get("background"):
        parts.append(f"## 研究背景\n\n{c['background']}\n")
    if c.get("design"):
        parts.append(f"## 研究设计与方法\n\n{c['design']}\n")
    if c.get("findings"):
        parts.append("## 主要发现\n\n" + "\n".join(f"- {f}" for f in c["findings"]) + "\n")
    if c.get("omics_angle") and c["omics_angle"] not in ("不适用", "N/A"):
        parts.append(f"## 多组学视角\n\n{c['omics_angle']}\n")
    if c.get("significance"):
        parts.append(f"## 意义\n\n{c['significance']}\n")
    if c.get("limitations"):
        parts.append("## 局限与待解问题\n\n" + "\n".join(f"- {x}" for x in c["limitations"]) + "\n")
    if c.get("next_questions"):
        parts.append("## 值得跟进\n\n" + "\n".join(f"- {x}" for x in c["next_questions"]) + "\n")
    if c.get("terms"):
        rows = []
        for t in c["terms"]:
            if isinstance(t, dict):
                rows.append(f"- **{t.get('en', '')}** {t.get('zh', '')}：{t.get('note', '')}")
            else:
                rows.append(f"- {t}")
        parts.append("## 关键术语\n\n" + "\n".join(rows) + "\n")
    return "\n".join(parts).strip() + "\n"


def interpret_item(settings: Settings, conn: sqlite3.Connection, llm: LLM, it: dict, *, force: bool = False) -> dict | None:
    basis, ft, material = material_for(conn, it)
    existing = repo.get_interpretation(conn, it["id"])
    # 已有解读不覆盖；但若此前只有摘要、现在拿到了 OA 全文，则用全文重做一次（模拟解读同样重做）
    upgrade_to_fulltext = bool(existing and basis == "fulltext" and (existing.get("basis") or "abstract") != "fulltext")
    if existing and not force and not upgrade_to_fulltext:
        # 已有真实解读不覆盖；已有模拟解读且现在有真实提供商则重做
        if not existing.get("is_mock") or llm.provider_for("interpret").is_mock:
            return existing
    src_cfg = settings.source_cfg(it.get("source_key")) or {}
    variables = _variables(settings, it)
    variables["basis"] = basis
    variables["basis_label"] = BASIS_LABEL[basis]
    variables["fulltext_note"] = fulltext_describe(ft)
    if material:
        variables["summary"] = material
    context = {**_context(it, src_cfg), "basis": basis}
    if material:
        context["summary"] = material
    resp = llm.run("interpret", variables, context=context, ref_type="item", ref_id=it["id"], use_cache=not force)
    content = resp.json()
    md = build_markdown(content, it, is_mock=resp.is_mock, basis=basis)
    repo.save_interpretation(conn, it["id"], content, md, provider=resp.provider, model=resp.model, is_mock=resp.is_mock, effort=resp.effort, basis=basis)
    return repo.get_interpretation(conn, it["id"])


def auto_interpret(settings: Settings, conn: sqlite3.Connection, llm: LLM, *, logger: Logger = log.info) -> dict:
    cfg = settings.selection.get("interpret") or {}
    items = repo.items_needing_interpretation(conn, float(cfg.get("auto_min_score", 72)), list(cfg.get("kinds") or ["paper", "preprint"]), int(cfg.get("max_per_run", 20)))
    out = {"candidates": len(items), "done": 0, "errors": 0, "mock": 0, "fulltext": 0}
    for it in items:
        try:
            r = interpret_item(settings, conn, llm, it)
            out["done"] += 1
            out["mock"] += 1 if (r or {}).get("is_mock") else 0
            out["fulltext"] += 1 if (r or {}).get("basis") == "fulltext" else 0
        except LLMError as e:
            out["errors"] += 1
            logger(f"  ✗ 解读失败 #{it['id']}: {e}")
    logger(f"[解读] 候选 {out['candidates']}，完成 {out['done']}（全文 {out['fulltext']}，模拟 {out['mock']}），失败 {out['errors']}")
    return out

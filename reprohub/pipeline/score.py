"""预筛与评分：规则门槛 → 模型评分（或模拟）→ 偏好加权 → 入选。"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from .. import repo
from ..llm import LLM, LLMError
from ..settings import Settings
from ..textutil import clamp, now_iso, parse_iso, truncate

log = logging.getLogger("reprohub.score")
Logger = Callable[[str], None]


def _topic_names(settings: Settings, topics: list[dict]) -> str:
    tm = settings.topic_map
    return "、".join(f"{tm.get(t['slug'], {}).get('name', t['slug'])}({t['score']:g})" for t in topics[:4])


def _variables(settings: Settings, it: dict) -> dict:
    authors = it.get("authors") or []
    pub = parse_iso(it.get("published_at"))
    return {
        "source_name": it.get("source_name"),
        "tier": it.get("source_tier"),
        "kind": it.get("kind"),
        "title": it.get("title"),
        "authors": ", ".join(authors[:8]) + (" 等" if len(authors) > 8 else ""),
        "journal": it.get("journal"),
        "published": pub.strftime("%Y-%m-%d") if pub else None,
        "doi": it.get("doi"),
        "summary": truncate(it.get("summary") or "（无摘要）", 3500),
        "matched_topics": _topic_names(settings, it.get("topics") or []),
        "preference_labels": "、".join(it.get("preference_labels") or []),
        "topic_slugs": ", ".join(t["slug"] for t in settings.topics),
    }


def _context(it: dict, src_cfg: dict) -> dict:
    extra = it.get("extra") or {}
    return {
        "topic_score": it.get("topic_score"),
        "preference_score": it.get("preference_score"),
        "preference_labels": it.get("preference_labels") or [],
        "tier": it.get("source_tier"),
        "kind": it.get("kind"),
        "summary": it.get("summary") or "",
        "topics": it.get("topics") or [],
        "matched_keywords": extra.get("matched_keywords") or [],
        "in_scope_default": bool(src_cfg.get("in_scope_default")),
    }


def prescreen_item(settings: Settings, conn: sqlite3.Connection, llm: LLM, it: dict) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。"""
    scope = settings.selection.get("scope") or {}
    src_cfg = settings.source_cfg(it.get("source_key")) or {}
    if it.get("in_scope"):
        return True, "rule_in_scope"
    if src_cfg.get("in_scope_default"):
        return True, "source_in_scope_default"
    ambiguous = float(it.get("topic_score") or 0) >= float(scope.get("ambiguous_low", 1))
    provider = llm.provider_for("prescreen")
    if ambiguous and scope.get("prescreen_use_llm", True) and not provider.is_mock:
        try:
            resp = llm.run("prescreen", _variables(settings, it), context=_context(it, src_cfg), ref_type="item", ref_id=it["id"])
            data = resp.json()
            if data.get("in_scope"):
                topics = [t for t in (data.get("topics") or []) if t in settings.topic_map]
                if topics and not it.get("primary_topic"):
                    repo.update_item(conn, it["id"], primary_topic=topics[0])
                return True, "llm_in_scope"
            return False, f"llm_out_of_scope: {data.get('reason', '')}"[:300]
        except LLMError as e:
            return False, f"prescreen_error: {e}"[:300]
    if scope.get("reject_out_of_scope_without_llm", True):
        return False, "out_of_scope_rule"
    return True, "rule_pass_no_reject"


def score_item(settings: Settings, conn: sqlite3.Connection, llm: LLM, it: dict) -> dict:
    sc = settings.selection.get("scoring") or {}
    src_cfg = settings.source_cfg(it.get("source_key")) or {}
    variables = _variables(settings, it)
    context = _context(it, src_cfg)
    resp = llm.run("score", variables, context=context, ref_type="item", ref_id=it["id"])
    data = resp.json()
    llm_score = clamp(float(data.get("score") or 0), 0, 100)
    disagreement = None
    if sc.get("double") and not resp.is_mock:
        resp2 = llm.run("score", variables, context=context, ref_type="item", ref_id=it["id"], temperature=0.4)
        try:
            s2 = clamp(float(resp2.json().get("score") or 0), 0, 100)
            disagreement = abs(llm_score - s2)
            llm_score = (llm_score + s2) / 2
        except (LLMError, ValueError, TypeError):
            pass
    tier = int(it.get("source_tier") or 3)
    pref_boost = min(float(it.get("preference_score") or 0) * float(sc.get("preference_weight", 1.0)), float(sc.get("preference_cap", 25)))
    tier_adj = float((sc.get("tier_adjust") or {}).get(tier, 0))
    final = round(clamp(llm_score + pref_boost + tier_adj, 0, 100), 1)
    threshold = float((sc.get("select_threshold") or {}).get(tier, 65)) + float((sc.get("kind_adjust") or {}).get(it.get("kind"), 0))
    selected = final >= threshold
    fields: dict = {
        "status": "scored",
        "llm_score": round(llm_score, 1),
        "final_score": final,
        "score_dims": data.get("dims") or {},
        "recommend_reason": data.get("recommend_reason") or "",
        "keywords": [str(k) for k in (data.get("keywords") or [])][:8],
        "selected": selected,
        "selected_at": now_iso() if selected else None,
        "llm_provider": resp.provider,
        "llm_model": resp.model,
        "llm_is_mock": resp.is_mock,
        "llm_effort": resp.effort,
        "scored_at": now_iso(),
    }
    if data.get("title_zh"):
        fields["title_zh"] = str(data["title_zh"]).strip()
    if data.get("summary_zh"):
        fields["summary_zh"] = str(data["summary_zh"]).strip()
    llm_topics = [t for t in (data.get("topics") or []) if t in settings.topic_map]
    if llm_topics and (not it.get("primary_topic") or float(it.get("topic_score") or 0) < 3):
        fields["primary_topic"] = llm_topics[0]
    extra = dict(it.get("extra") or {})
    extra["kind_hint"] = data.get("kind_hint")
    extra["threshold"] = threshold
    extra["pref_boost"] = pref_boost
    if disagreement is not None:
        extra["score_disagreement"] = round(disagreement, 1)
        extra["score_disputed"] = disagreement >= float(sc.get("disagreement_flag", 20))
    fields["extra"] = extra
    repo.update_item(conn, it["id"], **fields)
    return {"id": it["id"], "final_score": final, "selected": selected, "is_mock": resp.is_mock}


def process_new(settings: Settings, conn: sqlite3.Connection, llm: LLM, *, limit: int = 300, retry_errors: bool = False, logger: Logger = log.info) -> dict:
    out = {"processed": 0, "rejected": 0, "scored": 0, "selected": 0, "errors": 0, "mock": 0, "selected_ids": []}
    items = repo.items_by_status(conn, "new", limit)
    if retry_errors:
        items += repo.items_by_status(conn, "error", limit)
    for it in items:
        out["processed"] += 1
        ok, reason = prescreen_item(settings, conn, llm, it)
        if not ok:
            repo.update_item(conn, it["id"], status="rejected", reject_reason=reason)
            out["rejected"] += 1
            continue
        try:
            r = score_item(settings, conn, llm, it)
        except LLMError as e:
            repo.update_item(conn, it["id"], status="error", reject_reason=str(e)[:300])
            out["errors"] += 1
            logger(f"  ✗ 评分失败 #{it['id']}: {e}")
            continue
        out["scored"] += 1
        out["mock"] += 1 if r["is_mock"] else 0
        if r["selected"]:
            out["selected"] += 1
            out["selected_ids"].append(it["id"])
    logger(f"[评分] 处理 {out['processed']}：拒绝 {out['rejected']}，评分 {out['scored']}（入选 {out['selected']}，模拟 {out['mock']}），失败 {out['errors']}")
    return out

"""版次：日报（规则编纂）、周报/月报（由日报汇编，模型只写总述与栏目导读）。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from .. import repo
from ..llm import LLM, LLMError
from ..settings import Settings
from ..textutil import parse_iso, truncate

log = logging.getLogger("reprohub.editions")
Logger = Callable[[str], None]

KIND_LABEL = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
KIND_SLUG = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}


def _compact(it: dict) -> dict:
    return {
        "id": it["id"],
        "title": it.get("title"),
        "title_zh": it.get("title_zh"),
        "summary": truncate(it.get("summary") or "", 320),
        "summary_zh": it.get("summary_zh"),
        "url": it.get("url"),
        "doi": it.get("doi"),
        "source_name": it.get("source_name"),
        "source_tier": it.get("source_tier"),
        "kind": it.get("kind"),
        "journal": it.get("journal"),
        "final_score": it.get("final_score"),
        "preference_score": it.get("preference_score"),
        "preference_labels": it.get("preference_labels") or [],
        "primary_topic": it.get("primary_topic"),
        "published_at": it.get("published_at"),
        "recommend_reason": it.get("recommend_reason"),
        "has_interpretation": bool(it.get("has_interpretation")),
        "llm_is_mock": bool(it.get("llm_is_mock")),
    }


def group_by_topic(settings: Settings, items: list[dict], *, per_topic: int, max_items: int) -> list[dict]:
    items = sorted(items, key=lambda x: -(float(x.get("final_score") or 0)))[:max_items]
    buckets: dict[str, list[dict]] = {}
    for it in items:
        buckets.setdefault(it.get("primary_topic") or "_other", []).append(_compact(it))
    tm = settings.topic_map
    sections = []
    for t in settings.topics:
        if t["slug"] in buckets:
            sections.append({"topic": t["slug"], "topic_name": t["name"], "group": t.get("group"), "items": buckets[t["slug"]][:per_topic]})
    if "_other" in buckets:
        sections.append({"topic": "_other", "topic_name": "其他", "group": None, "items": buckets["_other"][:per_topic]})
    for s in sections:
        s["topic_name"] = tm.get(s["topic"], {}).get("name", s["topic_name"])
    return sections


def _stats(items: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    for it in items:
        kinds[it.get("kind") or "other"] = kinds.get(it.get("kind") or "other", 0) + 1
    return {
        "n": len(items),
        "sources": len({it.get("source_key") for it in items}),
        "kinds": kinds,
        "preference_hits": sum(1 for it in items if float(it.get("preference_score") or 0) > 0),
        "mock": sum(1 for it in items if it.get("llm_is_mock")),
        "interpreted": sum(1 for it in items if it.get("has_interpretation")),
    }


def _rules_overview(stats: dict, sections: list[dict], window_label: str) -> str:
    kinds = stats["kinds"]
    kl = "、".join(f"{ {'paper': '文献', 'preprint': '预印本', 'news': '资讯', 'guideline': '指南', 'post': '帖子', 'other': '其他'}.get(k, k)} {v} 条" for k, v in kinds.items())
    top = max(sections, key=lambda s: len(s["items"])) if sections else None
    parts = [f"{window_label}共入选 {stats['n']} 条，来自 {stats['sources']} 个来源（{kl}）。"]
    if top:
        parts.append(f"条目最多的栏目是「{top['topic_name']}」（{len(top['items'])} 条）。")
    if stats["preference_hits"]:
        parts.append(f"其中 {stats['preference_hits']} 条命中重点偏好（早期胚胎转录组/蛋白质组等）。")
    if stats["mock"]:
        parts.append(f"注意：{stats['mock']} 条评分来自模拟模式（未调用模型）。")
    return "".join(parts)


def render_markdown(settings: Settings, *, kind: str, title: str, overview: str | None, sections: list[dict], leads: dict[str, str], is_mock: bool, base_url: str) -> str:
    lines = [f"# {title}", ""]
    if is_mock:
        lines += ["> ⚠️ 模拟模式：总述与导读未调用模型，为规则占位内容。", ""]
    if overview:
        lines += [overview.strip(), ""]
    for s in sections:
        lines.append(f"## {s['topic_name']}")
        if leads.get(s["topic"]):
            lines.append(f"_{leads[s['topic']]}_")
        lines.append("")
        for it in s["items"]:
            t = it.get("title_zh") or it.get("title")
            link = f"{base_url}/items/{it['id']}"
            mock = " ·模拟评分" if it.get("llm_is_mock") else ""
            lines.append(f"- **[{it.get('final_score')}]{mock}** [{t}]({link}) — {it.get('source_name')}")
            if it.get("title_zh") and it.get("title"):
                lines.append(f"  {it['title']}")
            s_zh = it.get("summary_zh") or it.get("summary")
            if s_zh:
                lines.append(f"  {truncate(s_zh, 240)}")
            if it.get("url"):
                lines.append(f"  原文：{it['url']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- daily
def build_daily(settings: Settings, conn: sqlite3.Connection, llm: LLM | None = None, date_key: str | None = None, *, logger: Logger = log.info) -> dict:
    """日报 = 某个本地日历日内入选的条目。当天的日报随流水线持续更新；调度在次日 08:00 定稿前一天。"""
    cfg = (settings.site.get("editions") or {}).get("daily") or {}
    tz = settings.tz
    now_local = settings.now_local()
    d = date.fromisoformat(date_key) if date_key else now_local.date()
    start_local = datetime.combine(d, time(0, 0), tzinfo=tz)
    end_local = min(start_local + timedelta(days=1), now_local)
    start, end = start_local.astimezone(timezone.utc).isoformat(timespec="seconds"), end_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    partial = end_local >= now_local
    # 窗口截至「现在」时不设上界，避免同一秒内刚入选的条目被 < 排除
    items, _ = repo.list_items(conn, selected=True, since=start, until=None if partial else end, date_field="selected_at", order="score", page=1, page_size=800)
    sections = group_by_topic(settings, items, per_topic=int(cfg.get("per_topic", 6)), max_items=int(cfg.get("max_items", 40)))
    stats = _stats(items)
    label = f"{d.isoformat()} 日报" + (f"（截至 {end_local.strftime('%H:%M')}，持续更新）" if partial else "")
    overview = _rules_overview(stats, sections, label)
    title = f"{settings.site_info.get('name_zh', 'ReproHub')} 日报 · {d.isoformat()}"
    content = {"date_key": d.isoformat(), "window": {"start": start, "end": end}, "partial": partial, "stats": stats, "sections": sections, "leads": {}}
    md = render_markdown(settings, kind="daily", title=title, overview=overview, sections=sections, leads={}, is_mock=False, base_url=settings.site_info.get("base_url", ""))
    eid = repo.save_edition(conn, kind="daily", date_key=d.isoformat(), title=title, overview=overview, content=content, markdown=md, provider="rules", model=None, is_mock=False, item_count=stats["n"])
    logger(f"[日报] {d.isoformat()}：{stats['n']} 条，{len(sections)} 个栏目")
    return repo.get_edition_by_id(conn, eid)  # type: ignore[return-value]


# ---------------------------------------------------------- weekly/monthly
def period_window(kind: str, key: str | None, tz) -> tuple[datetime, datetime, str, str]:
    """返回 (start_local, end_local, key, label)。key 为空时取「截至现在」的滚动窗口。"""
    now_local = datetime.now(timezone.utc).astimezone(tz)
    if kind == "weekly":
        if key:
            y, w = key.split("-W")
            start = datetime.combine(date.fromisocalendar(int(y), int(w), 1), time(0, 0), tzinfo=tz)
            end = min(start + timedelta(days=7), now_local)
        else:
            end = now_local
            start = end - timedelta(days=7)
            iso = (end - timedelta(days=1)).isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        label = f"{start.strftime('%Y-%m-%d')} → {end.strftime('%m-%d')}"
    else:
        if key:
            y, m = [int(x) for x in key.split("-")]
            start = datetime(y, m, 1, tzinfo=tz)
            nxt = datetime(y + (m // 12), (m % 12) + 1, 1, tzinfo=tz)
            end = min(nxt, now_local)
        else:
            end = now_local
            start = end - timedelta(days=30)
            key = (end - timedelta(days=1)).strftime("%Y-%m")
        label = f"{start.strftime('%Y-%m-%d')} → {end.strftime('%m-%d')}"
    return start, end, key, label


def build_periodic(kind: str, settings: Settings, conn: sqlite3.Connection, llm: LLM, key: str | None = None, *, logger: Logger = log.info) -> dict:
    assert kind in ("weekly", "monthly")
    cfg = (settings.site.get("editions") or {}).get(kind) or {}
    start_local, end_local, key, label = period_window(kind, key, settings.tz)
    start, end = start_local.astimezone(timezone.utc).isoformat(timespec="seconds"), end_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    # 优先从窗口内日报汇编
    ids: list[int] = []
    for ed in repo.list_editions(conn, "daily", limit=400):
        if start_local.date().isoformat() <= ed["date_key"] <= end_local.date().isoformat():
            full = repo.get_edition_by_id(conn, ed["id"])
            for s in (full or {}).get("content", {}).get("sections", []):
                ids += [it["id"] for it in s["items"]]
    items = repo.get_items_by_ids(conn, list(dict.fromkeys(ids))) if ids else []
    if not items:
        partial = end_local >= settings.now_local() - timedelta(seconds=5)
        items, _ = repo.list_items(conn, selected=True, since=start, until=None if partial else end, date_field="selected_at", order="score", page=1, page_size=1500)
    sections = group_by_topic(settings, items, per_topic=int(cfg.get("per_topic", 5)), max_items=int(cfg.get("max_items", 30)))
    stats = _stats(items)
    kind_label = KIND_LABEL[kind]
    title = f"{settings.site_info.get('name_zh', 'ReproHub')} {kind_label} · {key}"
    overview, leads, provider, model, is_mock = _rules_overview(stats, sections, f"{kind_label}（{label}）"), {}, "rules", None, False
    if sections:
        try:
            resp = llm.run(
                "digest",
                {"kind_label": kind_label, "period_label": label, "sections": sections, "section_slugs": ", ".join(s["topic"] for s in sections)},
                context={"sections": sections},
                ref_type="edition", ref_id=None,
            )
            data = resp.json()
            overview = (data.get("overview") or overview).strip()
            for s in data.get("sections") or []:
                if s.get("topic") and s.get("lead"):
                    leads[s["topic"]] = str(s["lead"]).strip()
            provider, model, is_mock = resp.provider, resp.model, resp.is_mock
        except LLMError as e:
            logger(f"  ✗ {kind_label}总述生成失败，退回规则总述：{e}")
    content = {"date_key": key, "window": {"start": start, "end": end}, "label": label, "stats": stats, "sections": sections, "leads": leads}
    md = render_markdown(settings, kind=kind, title=title, overview=overview, sections=sections, leads=leads, is_mock=is_mock, base_url=settings.site_info.get("base_url", ""))
    eid = repo.save_edition(conn, kind=kind, date_key=key, title=title, overview=overview, content=content, markdown=md, provider=provider, model=model, is_mock=is_mock, item_count=stats["n"])
    logger(f"[{kind_label}] {key}：{stats['n']} 条，{len(sections)} 个栏目{'（模拟总述）' if is_mock else ''}")
    return repo.get_edition_by_id(conn, eid)  # type: ignore[return-value]


def build_weekly(settings, conn, llm, key=None, *, logger: Logger = log.info) -> dict:
    return build_periodic("weekly", settings, conn, llm, key, logger=logger)


def build_monthly(settings, conn, llm, key=None, *, logger: Logger = log.info) -> dict:
    return build_periodic("monthly", settings, conn, llm, key, logger=logger)

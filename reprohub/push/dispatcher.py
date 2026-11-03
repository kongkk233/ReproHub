"""推送分发：按规则把入选条目 / 版次送到已配置渠道，并记录结果。"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from .. import repo
from ..settings import Settings
from ..textutil import truncate
from .channels import PushError, build_channels

log = logging.getLogger("reprohub.push")
Logger = Callable[[str], None]


def _rule_event(rule: dict) -> str:
    """规则触发事件。兼容旧写法 on:（YAML 1.1 会把 on 解析成 True）。"""
    return str(rule.get("event") or rule.get("on") or rule.get(True) or "").strip()


def _active_channels(settings: Settings, keys: list[str]) -> list:
    chans = build_channels(settings)
    out = []
    for k in keys:
        ch = chans.get(k)
        if ch and ch.cfg.get("enabled", False) and ch.configured():
            out.append(ch)
    return out


def items_markdown(settings: Settings, items: list[dict], *, heading: str | None = None) -> str:
    base = settings.site_info.get("base_url", "")
    tm = settings.topic_map
    lines = [f"## {heading}", ""] if heading else []
    for it in items:
        t = it.get("title_zh") or it.get("title")
        mock = "（模拟评分）" if it.get("llm_is_mock") else ""
        topic = tm.get(it.get("primary_topic") or "", {}).get("name", "")
        lines.append(f"- **[{it.get('final_score')}]{mock}** [{t}]({base}/items/{it['id']}) · {it.get('source_name')}" + (f" · {topic}" if topic else ""))
        if it.get("title_zh") and it.get("title"):
            lines.append(f"  {it['title']}")
        if it.get("preference_labels"):
            lines.append(f"  ★ 偏好：{'、'.join(it['preference_labels'])}")
        s = it.get("summary_zh") or it.get("summary")
        if s:
            lines.append(f"  {truncate(s, 260)}")
        if it.get("recommend_reason"):
            lines.append(f"  推荐理由：{truncate(it['recommend_reason'], 200)}")
        lines.append(f"  原文：{it.get('url')}")
        lines.append("")
    return "\n".join(lines).strip()


def _item_matches(rule: dict, it: dict) -> bool:
    if float(it.get("final_score") or 0) < float(rule.get("min_final_score", 0)):
        return False
    if float(it.get("preference_score") or 0) < float(rule.get("min_preference", 0)):
        return False
    topics = rule.get("topics") or []
    if topics:
        slugs = {it.get("primary_topic")} | {t.get("slug") for t in (it.get("topics") or [])}
        if not (slugs & set(topics)):
            return False
    kinds = rule.get("kinds") or []
    if kinds and it.get("kind") not in kinds:
        return False
    tiers = rule.get("journal_tiers") or []
    if tiers and it.get("journal_tier") not in tiers:
        return False
    return True


def push_items(settings: Settings, conn: sqlite3.Connection, item_ids: list[int], *, logger: Logger = log.info) -> dict:
    items = [it for it in repo.get_items_by_ids(conn, item_ids) if not it.get("pushed")]
    out = {"candidates": len(items), "sent": 0, "failed": 0, "by_rule": {}}
    if not items:
        return out
    for rule in (settings.push.get("rules") or []):
        if _rule_event(rule) != "item":
            continue
        matched = [it for it in items if _item_matches(rule, it)]
        if not matched:
            continue
        chans = _active_channels(settings, rule.get("channels") or [])
        if not chans:
            continue
        title = f"{settings.site_info.get('name_zh', 'ReproHub')} · {rule.get('title') or rule.get('key')} · {len(matched)} 条"
        md = items_markdown(settings, matched)
        payload = {"event": "items", "rule": rule.get("key"), "items": [{"id": i["id"], "title": i.get("title"), "title_zh": i.get("title_zh"), "url": i.get("url"), "final_score": i.get("final_score"), "is_mock": bool(i.get("llm_is_mock"))} for i in matched]}
        for ch in chans:
            try:
                ch.send(title, md, payload)
                repo.log_push(conn, rule_key=rule.get("key"), channel_key=ch.key, event="items", ref_type="rule", ref_id=None, status="ok", error=None)
                out["sent"] += 1
                logger(f"[推送] {rule.get('key')} → {ch.key}：{len(matched)} 条")
            except PushError as e:
                repo.log_push(conn, rule_key=rule.get("key"), channel_key=ch.key, event="items", ref_type="rule", ref_id=None, status="error", error=str(e)[:300])
                out["failed"] += 1
                logger(f"[推送] {rule.get('key')} → {ch.key} 失败：{e}")
        out["by_rule"][rule.get("key")] = len(matched)
    for it in items:
        repo.update_item(conn, it["id"], pushed=1)
    return out


def push_edition(settings: Settings, conn: sqlite3.Connection, edition: dict, *, logger: Logger = log.info, force: bool = False) -> dict:
    out = {"sent": 0, "failed": 0}
    if edition.get("pushed") and not force:
        return out
    if not edition.get("item_count"):
        return out
    for rule in (settings.push.get("rules") or []):
        if _rule_event(rule) != "edition":
            continue
        if rule.get("kinds") and edition["kind"] not in rule["kinds"]:
            continue
        chans = _active_channels(settings, rule.get("channels") or [])
        payload = {"event": "edition", "kind": edition["kind"], "date_key": edition["date_key"], "is_mock": bool(edition.get("is_mock")), "url": f"{settings.site_info.get('base_url', '')}/editions/{edition['kind']}/{edition['date_key']}"}
        for ch in chans:
            try:
                ch.send(edition["title"], edition["markdown"], payload)
                repo.log_push(conn, rule_key=rule.get("key"), channel_key=ch.key, event="edition", ref_type="edition", ref_id=edition["id"], status="ok", error=None)
                out["sent"] += 1
                logger(f"[推送] {edition['title']} → {ch.key}")
            except PushError as e:
                repo.log_push(conn, rule_key=rule.get("key"), channel_key=ch.key, event="edition", ref_type="edition", ref_id=edition["id"], status="error", error=str(e)[:300])
                out["failed"] += 1
                logger(f"[推送] {edition['title']} → {ch.key} 失败：{e}")
    conn.execute("UPDATE editions SET pushed=1 WHERE id=?", (edition["id"],))
    return out


def test_channel(settings: Settings, conn: sqlite3.Connection, channel_key: str) -> dict:
    chans = build_channels(settings)
    ch = chans.get(channel_key)
    if not ch:
        return {"ok": False, "error": f"未知渠道 {channel_key}"}
    if not ch.configured():
        return {"ok": False, "error": "渠道未配置（环境变量为空）"}
    try:
        r = ch.send("ReproHub 测试推送", "这是一条来自 ReproHub 的测试消息。\n\n- 渠道：" + channel_key, {"event": "test"})
        repo.log_push(conn, rule_key="test", channel_key=channel_key, event="test", ref_type="test", ref_id=None, status="ok", error=None)
        return {"ok": True, "result": r}
    except PushError as e:
        repo.log_push(conn, rule_key="test", channel_key=channel_key, event="test", ref_type="test", ref_id=None, status="error", error=str(e)[:300])
        return {"ok": False, "error": str(e)}

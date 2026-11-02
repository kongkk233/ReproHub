"""采集：抓取各来源 → 规范化 → 去重 → 入库。"""
from __future__ import annotations

import logging
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import repo
from ..settings import Settings
from ..sources import FetchError, RawItem, get_fetcher
from ..textutil import canonical_url, detect_lang, extract_doi, normalize_doi, now_iso, title_fingerprint, to_iso, truncate, url_hash
from .classify import Classifier

log = logging.getLogger("reprohub.collect")
Logger = Callable[[str], None]


def normalize(raw: RawItem, src_cfg: dict, classifier: Classifier) -> dict | None:
    """RawItem → 入库字典。返回 None 表示被来源级领域过滤丢弃。"""
    url = canonical_url(raw.url)
    title = " ".join((raw.title or "").split()).strip()
    if not url or not title:
        return None
    summary = truncate((raw.summary or "").strip(), 6000)
    doi = normalize_doi(raw.doi) or extract_doi(url)
    kind = raw.kind or src_cfg.get("kind") or "news"
    cls = classifier.classify(title, summary, in_scope_default=bool(src_cfg.get("in_scope_default")))
    if src_cfg.get("scope_filter") and not cls.in_scope:
        return None
    extra = dict(raw.extra or {})
    extra["matched_keywords"] = cls.matched_keywords
    extra["preference_keys"] = cls.preference_keys
    published = raw.published_at
    # 期刊「当期」日期可能是未来的印刷日期，会把条目顶到列表最前；超过 2 天的未来日期视为未知
    if published and published.tzinfo and published > datetime.now(timezone.utc) + timedelta(days=2):
        extra["published_at_original"] = to_iso(published)
        published = None
    return {
        "external_id": raw.external_id,
        "url": url,
        "url_hash": url_hash(url),
        "doi": doi,
        "pmid": raw.pmid,
        "title": title,
        "title_fp": title_fingerprint(title),
        "summary": summary,
        "authors": raw.authors or [],
        "journal": raw.journal,
        "kind": kind,
        "lang": raw.lang or detect_lang(f"{title} {summary}"),
        "published_at": to_iso(published),
        "fetched_at": now_iso(),
        "topics": cls.topics,
        "primary_topic": cls.primary_topic,
        "topic_score": cls.topic_score,
        "in_scope": cls.in_scope,
        "preference_score": cls.preference_score,
        "preference_labels": cls.preference_labels,
        "status": "new",
        "extra": extra,
    }


def ingest_item(conn: sqlite3.Connection, settings: Settings, source_key: str, raw: RawItem, classifier: Classifier | None = None) -> tuple[int | None, str]:
    """单条入库（API 推送 / 演示数据）。返回 (item_id, 'new'|'duplicate'|'dropped')。"""
    classifier = classifier or Classifier(settings)
    src_cfg = settings.source_cfg(source_key) or {"key": source_key, "kind": raw.kind or "other"}
    src = repo.get_source(conn, source_key)
    if not src:
        repo.sync_sources(conn, settings)
        src = repo.get_source(conn, source_key)
    if not src:
        raise ValueError(f"未知来源 {source_key}")
    d = normalize(raw, src_cfg, classifier)
    if d is None:
        return None, "dropped"
    dup = repo.find_duplicate(conn, doi=d["doi"], pmid=d["pmid"], uhash=d["url_hash"], title_fp=d["title_fp"])
    if dup:
        repo.add_mention(conn, dup["id"], src["id"], d["url"])
        return dup["id"], "duplicate"
    return repo.insert_item(conn, src["id"], d), "new"


def collect(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    source_keys: list[str] | None = None,
    force: bool = False,
    since_days: int | None = None,
    logger: Logger = log.info,
) -> dict:
    repo.sync_sources(conn, settings)
    classifier = Classifier(settings)
    now = datetime.now(timezone.utc)
    summary = {"sources": 0, "new": 0, "duplicate": 0, "dropped": 0, "errors": [], "skipped": 0, "per_source": {}}
    for cfg in settings.sources:
        key = cfg.get("key")
        if cfg.get("type") == "push":
            continue
        explicit = bool(source_keys and key in source_keys)
        if source_keys and not explicit:
            continue
        if not cfg.get("enabled", True) and not explicit:
            continue
        src = repo.get_source(conn, key)
        if not src:
            continue
        if not force and not explicit and src.get("next_fetch_at") and src["next_fetch_at"] > now.isoformat(timespec="seconds"):
            summary["skipped"] += 1
            continue
        fetcher = get_fetcher(cfg, settings)
        if fetcher is None:
            continue
        interval = int(cfg.get("interval_minutes", 360))
        params = cfg.get("params") or {}
        days = since_days or params.get("days") or params.get("reldate_days") or 7
        since = now - timedelta(days=int(days))
        summary["sources"] += 1
        logger(f"[采集] {cfg.get('name', key)} …")
        try:
            raws = fetcher.fetch(since)
        except FetchError as e:
            summary["errors"].append({"source": key, "error": str(e)})
            repo.update_source_fetch(conn, src["id"], status="error", error=str(e)[:500], new=0, seen=0, next_minutes=interval)
            logger(f"  ✗ {e}")
            continue
        except Exception as e:  # noqa: BLE001 - 采集器任何异常都不应中断整轮
            err = f"{type(e).__name__}: {e}"
            summary["errors"].append({"source": key, "error": err})
            repo.update_source_fetch(conn, src["id"], status="error", error=err[:500], new=0, seen=0, next_minutes=interval)
            logger(f"  ✗ {err}\n{traceback.format_exc(limit=3)}")
            continue
        new = dup = dropped = 0
        conn.execute("BEGIN")
        try:
            for raw in raws:
                d = normalize(raw, cfg, classifier)
                if d is None:
                    dropped += 1
                    continue
                existing = repo.find_duplicate(conn, doi=d["doi"], pmid=d["pmid"], uhash=d["url_hash"], title_fp=d["title_fp"])
                if existing:
                    repo.add_mention(conn, existing["id"], src["id"], d["url"])
                    # 补全：已有条目缺摘要而新来源有，则补上
                    if not existing.get("summary") and d["summary"]:
                        repo.update_item(conn, existing["id"], summary=d["summary"])
                    dup += 1
                    continue
                repo.insert_item(conn, src["id"], d)
                new += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # 自适应间隔：没有新内容时逐步拉长到 4 倍
        next_minutes = interval if new > 0 else min(interval * 4, max(interval, int((src.get("interval_minutes") or interval) * 1.6)))
        repo.update_source_fetch(conn, src["id"], status="ok", error=None, new=new, seen=len(raws), next_minutes=next_minutes)
        summary["new"] += new
        summary["duplicate"] += dup
        summary["dropped"] += dropped
        summary["per_source"][key] = {"seen": len(raws), "new": new, "duplicate": dup, "dropped": dropped}
        logger(f"  ✓ 抓到 {len(raws)} 条：新增 {new}，重复 {dup}，领域外丢弃 {dropped}")
    return summary

"""数据访问层：所有 SQL 集中于此。返回 dict（JSON 字段已解码）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any, Iterable

from .settings import Settings, get_settings
from .textutil import now_iso, now_utc, parse_iso, title_fingerprint, url_hash as _url_hash

ITEM_JSON_FIELDS = ("authors", "topics", "preference_labels", "keywords", "score_dims", "extra")


def _loads(s: Any, default: Any):
    if s is None or s == "":
        return default
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in ITEM_JSON_FIELDS:
        if f in d:
            d[f] = _loads(d[f], [] if f != "extra" and f != "score_dims" else {})
    if "tags" in d:
        d["tags"] = _loads(d["tags"], [])
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


# ============================================================ sources
def sync_sources(conn: sqlite3.Connection, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    for s in settings.sources:
        params = s.get("params") or {}
        url = params.get("url") or ""
        conn.execute(
            """INSERT INTO sources(key,name,type,tier,kind,enabled,url,interval_minutes)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET name=excluded.name, type=excluded.type, tier=excluded.tier,
                 kind=excluded.kind, enabled=excluded.enabled, url=excluded.url, interval_minutes=excluded.interval_minutes""",
            (s["key"], s.get("name", s["key"]), s.get("type", "rss"), int(s.get("tier", 3)), s.get("kind", "news"),
             1 if s.get("enabled", True) else 0, url, int(s.get("interval_minutes", 360))),
        )


def get_source(conn: sqlite3.Connection, key: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone())


def get_source_by_id(conn: sqlite3.Connection, sid: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    return rows_to_dicts(conn.execute("SELECT * FROM sources ORDER BY tier, name"))


def source_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, COUNT(i.id) AS n_items, SUM(i.selected) AS n_selected,
                  MAX(i.published_at) AS latest
           FROM sources s LEFT JOIN items i ON i.source_id=s.id
           GROUP BY s.id ORDER BY s.tier, s.name"""
    )
    return rows_to_dicts(rows)


def update_source_fetch(conn: sqlite3.Connection, source_id: int, *, status: str, error: str | None,
                        new: int, seen: int, next_minutes: int) -> None:
    nxt = (now_utc() + timedelta(minutes=next_minutes)).isoformat(timespec="seconds")
    conn.execute(
        """UPDATE sources SET last_fetch_at=?, next_fetch_at=?, last_status=?, last_error=?, last_new=?, last_seen=?,
           total_items=total_items+? WHERE id=?""",
        (now_iso(), nxt, status, error, new, seen, new, source_id),
    )


# ============================================================ items
def find_duplicate(conn: sqlite3.Connection, *, doi: str | None, pmid: str | None, uhash: str, title_fp: str | None) -> dict | None:
    if doi:
        r = conn.execute("SELECT * FROM items WHERE doi=? LIMIT 1", (doi,)).fetchone()
        if r:
            return row_to_dict(r)
    if pmid:
        r = conn.execute("SELECT * FROM items WHERE pmid=? LIMIT 1", (pmid,)).fetchone()
        if r:
            return row_to_dict(r)
    r = conn.execute("SELECT * FROM items WHERE url_hash=? LIMIT 1", (uhash,)).fetchone()
    if r:
        return row_to_dict(r)
    if title_fp and len(title_fp) >= 20:
        since = (now_utc() - timedelta(days=90)).isoformat(timespec="seconds")
        r = conn.execute("SELECT * FROM items WHERE title_fp=? AND fetched_at>=? LIMIT 1", (title_fp, since)).fetchone()
        if r:
            return row_to_dict(r)
    return None


def insert_item(conn: sqlite3.Connection, source_id: int, d: dict) -> int:
    """d: 已规范化的条目字典（见 pipeline.collect.normalize）。"""
    cur = conn.execute(
        """INSERT INTO items(source_id, external_id, url, url_hash, doi, pmid, title, title_fp, summary, authors, journal,
                             kind, lang, published_at, fetched_at, topics, primary_topic, topic_score, in_scope,
                             preference_score, preference_labels, status, reject_reason, extra)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id, d.get("external_id"), d["url"], d["url_hash"], d.get("doi"), d.get("pmid"), d["title"],
            d.get("title_fp") or title_fingerprint(d["title"]), d.get("summary") or "", json.dumps(d.get("authors") or [], ensure_ascii=False),
            d.get("journal"), d.get("kind") or "news", d.get("lang") or "en", d.get("published_at"), d.get("fetched_at") or now_iso(),
            json.dumps(d.get("topics") or [], ensure_ascii=False), d.get("primary_topic"), float(d.get("topic_score") or 0),
            1 if d.get("in_scope") else 0, float(d.get("preference_score") or 0),
            json.dumps(d.get("preference_labels") or [], ensure_ascii=False), d.get("status") or "new", d.get("reject_reason"),
            json.dumps(d.get("extra") or {}, ensure_ascii=False),
        ),
    )
    item_id = int(cur.lastrowid)
    add_mention(conn, item_id, source_id, d["url"])
    return item_id


def add_mention(conn: sqlite3.Connection, item_id: int, source_id: int, url: str | None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO item_mentions(item_id, source_id, url, seen_at) VALUES(?,?,?,?)",
        (item_id, source_id, url, now_iso()),
    )
    return cur.rowcount > 0


def update_item(conn: sqlite3.Connection, item_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = 1 if v else 0
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(item_id)
    conn.execute(f"UPDATE items SET {', '.join(cols)} WHERE id=?", vals)


ITEM_COLS = """i.*, s.key AS source_key, s.name AS source_name, s.tier AS source_tier, s.type AS source_type,
                 r.status AS read_status, r.starred, r.read_later, r.rating, r.tags AS tags, r.notes AS notes,
                 (SELECT COUNT(*) FROM item_mentions m WHERE m.item_id=i.id) AS mention_count,
                 (SELECT 1 FROM interpretations x WHERE x.item_id=i.id) AS has_interpretation"""
ITEM_FROM = "FROM items i JOIN sources s ON s.id=i.source_id LEFT JOIN reading r ON r.item_id=i.id"
ITEM_SELECT = f"SELECT {ITEM_COLS} {ITEM_FROM}"


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    return row_to_dict(conn.execute(ITEM_SELECT + " WHERE i.id=?", (item_id,)).fetchone())


def get_items_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = rows_to_dicts(conn.execute(ITEM_SELECT + f" WHERE i.id IN ({q})", ids))
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r["id"], 0))


def list_items(
    conn: sqlite3.Connection,
    *,
    selected: bool | None = None,
    status: str | None = None,
    kind: str | None = None,
    kinds: list[str] | None = None,
    topic: str | None = None,
    source_key: str | None = None,
    tier: int | None = None,
    unread_only: bool = False,
    starred: bool | None = None,
    read_later: bool | None = None,
    read_status: str | None = None,
    min_score: float | None = None,
    min_preference: float | None = None,
    since: str | None = None,
    until: str | None = None,
    date_field: str = "published_at",
    q: str | None = None,
    order: str = "date",
    page: int = 1,
    page_size: int = 30,
    story_id: int | None = None,
) -> tuple[list[dict], int]:
    where, args = [], []
    if selected is not None:
        where.append("i.selected=?"); args.append(1 if selected else 0)
    if status:
        where.append("i.status=?"); args.append(status)
    if kind:
        where.append("i.kind=?"); args.append(kind)
    if kinds:
        where.append(f"i.kind IN ({','.join('?' * len(kinds))})"); args.extend(kinds)
    if topic:
        where.append("(i.primary_topic=? OR i.topics LIKE ?)"); args.extend([topic, f'%"slug": "{topic}"%'])
    if source_key:
        where.append("s.key=?"); args.append(source_key)
    if tier:
        where.append("s.tier=?"); args.append(int(tier))
    if unread_only:
        where.append("(r.status IS NULL OR r.status='unread')")
    if starred is not None:
        where.append("COALESCE(r.starred,0)=?"); args.append(1 if starred else 0)
    if read_later is not None:
        where.append("COALESCE(r.read_later,0)=?"); args.append(1 if read_later else 0)
    if read_status:
        where.append("r.status=?"); args.append(read_status)
    if min_score is not None:
        where.append("COALESCE(i.final_score,0)>=?"); args.append(min_score)
    if min_preference is not None:
        where.append("i.preference_score>=?"); args.append(min_preference)
    if since:
        where.append(f"COALESCE(i.{date_field}, i.fetched_at)>=?"); args.append(since)
    if until:
        where.append(f"COALESCE(i.{date_field}, i.fetched_at)<?"); args.append(until)
    if story_id:
        where.append("i.story_id=?"); args.append(story_id)
    join_fts = ""
    if q:
        q = q.strip()
        if len(q) >= 3:
            join_fts = " JOIN items_fts f ON f.rowid=i.id"
            where.append("items_fts MATCH ?")
            args.append('"' + q.replace('"', '""') + '"')
        else:
            where.append("(i.title LIKE ? OR i.title_zh LIKE ? OR i.summary_zh LIKE ?)")
            args.extend([f"%{q}%"] * 3)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    order_sql = {
        "date": "COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC",
        "score": "COALESCE(i.final_score,0) DESC, COALESCE(i.published_at, i.fetched_at) DESC",
        "fetched": "i.fetched_at DESC, i.id DESC",
        "preference": "i.preference_score DESC, COALESCE(i.final_score,0) DESC",
        "updated": "r.updated_at DESC",
    }.get(order, "COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC")
    base = ITEM_FROM + join_fts + sql_where
    total = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        f"SELECT {ITEM_COLS} {base} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        args + [page_size, offset],
    )
    return rows_to_dicts(rows), int(total)


def items_by_status(conn: sqlite3.Connection, status: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(ITEM_SELECT + " WHERE i.status=? ORDER BY i.id ASC LIMIT ?", (status, limit))
    return rows_to_dicts(rows)


def items_needing_interpretation(conn: sqlite3.Connection, min_score: float, kinds: list[str], limit: int) -> list[dict]:
    q = ",".join("?" * len(kinds))
    rows = conn.execute(
        ITEM_SELECT + f""" WHERE i.selected=1 AND COALESCE(i.final_score,0)>=? AND i.kind IN ({q})
                           AND NOT EXISTS (SELECT 1 FROM interpretations x WHERE x.item_id=i.id AND x.is_mock=0)
                           ORDER BY i.final_score DESC LIMIT ?""",
        [min_score, *kinds, limit],
    )
    return rows_to_dicts(rows)


def delete_items(conn: sqlite3.Connection, ids: list[int]) -> int:
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM items WHERE id IN ({q})", ids)
    return cur.rowcount


def purge_rejected(conn: sqlite3.Connection, days: int) -> int:
    cutoff = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM items WHERE status='rejected' AND fetched_at<? AND id NOT IN (SELECT item_id FROM reading)", (cutoff,))
    return cur.rowcount


def date_groups(conn: sqlite3.Connection, items: list[dict], tz) -> list[dict]:
    """把条目按本地日期分组，供列表页显示。"""
    groups: dict[str, dict] = {}
    for it in items:
        dt = parse_iso(it.get("published_at")) or parse_iso(it.get("fetched_at"))
        key = dt.astimezone(tz).strftime("%Y-%m-%d") if dt else "未知日期"
        g = groups.setdefault(key, {"date": key, "items": []})
        g["items"].append(it)
    return list(groups.values())


def counts(conn: sqlite3.Connection) -> dict:
    c = {}
    c["items"] = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    c["selected"] = conn.execute("SELECT COUNT(*) FROM items WHERE selected=1").fetchone()[0]
    c["new"] = conn.execute("SELECT COUNT(*) FROM items WHERE status='new'").fetchone()[0]
    c["rejected"] = conn.execute("SELECT COUNT(*) FROM items WHERE status='rejected'").fetchone()[0]
    c["scored"] = conn.execute("SELECT COUNT(*) FROM items WHERE status='scored'").fetchone()[0]
    c["interpretations"] = conn.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0]
    c["stories"] = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    c["editions"] = conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
    c["unread_selected"] = conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN reading r ON r.item_id=i.id WHERE i.selected=1 AND (r.status IS NULL OR r.status='unread')"
    ).fetchone()[0]
    c["mock_scored"] = conn.execute("SELECT COUNT(*) FROM items WHERE status='scored' AND llm_is_mock=1").fetchone()[0]
    c["mock_interpretations"] = conn.execute("SELECT COUNT(*) FROM interpretations WHERE is_mock=1").fetchone()[0]
    c["cache_entries"] = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    return c


def topic_counts(conn: sqlite3.Connection, selected_only: bool = True, days: int | None = None) -> dict[str, int]:
    where = ["primary_topic IS NOT NULL"]
    args: list[Any] = []
    if selected_only:
        where.append("selected=1")
    if days:
        where.append("COALESCE(published_at, fetched_at)>=?")
        args.append((now_utc() - timedelta(days=days)).isoformat(timespec="seconds"))
    rows = conn.execute(f"SELECT primary_topic, COUNT(*) FROM items WHERE {' AND '.join(where)} GROUP BY primary_topic", args)
    return {r[0]: r[1] for r in rows}


# ============================================================ interpretations
def get_interpretation(conn: sqlite3.Connection, item_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM interpretations WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content"] = _loads(d.pop("content_json"), {})
    return d


def save_interpretation(conn: sqlite3.Connection, item_id: int, content: dict, markdown: str, *, provider: str, model: str, is_mock: bool, effort: str | None = None) -> None:
    conn.execute(
        """INSERT INTO interpretations(item_id, content_json, markdown, provider, model, is_mock, effort, created_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET content_json=excluded.content_json, markdown=excluded.markdown,
             provider=excluded.provider, model=excluded.model, is_mock=excluded.is_mock, effort=excluded.effort, created_at=excluded.created_at""",
        (item_id, json.dumps(content, ensure_ascii=False), markdown, provider, model, 1 if is_mock else 0, effort, now_iso()),
    )


# ============================================================ stories
def list_stories(conn: sqlite3.Connection, *, limit: int = 20, days: int | None = None, min_items: int = 1, order: str = "heat") -> list[dict]:
    where, args = ["item_count>=?"], [min_items]
    if days:
        where.append("last_seen>=?")
        args.append((now_utc() - timedelta(days=days)).isoformat(timespec="seconds"))
    order_sql = "heat DESC, last_seen DESC" if order == "heat" else "last_seen DESC"
    rows = conn.execute(f"SELECT * FROM stories WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?", args + [limit])
    return rows_to_dicts(rows)


def get_story(conn: sqlite3.Connection, story_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone())


def story_items(conn: sqlite3.Connection, story_id: int) -> list[dict]:
    rows = conn.execute(ITEM_SELECT + " WHERE i.story_id=? ORDER BY COALESCE(i.final_score,0) DESC, i.published_at DESC", (story_id,))
    return rows_to_dicts(rows)


def story_sources(conn: sqlite3.Connection, story_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT DISTINCT s.key, s.name, s.tier FROM items i JOIN item_mentions m ON m.item_id=i.id
           JOIN sources s ON s.id=m.source_id WHERE i.story_id=? ORDER BY s.tier""",
        (story_id,),
    )
    return rows_to_dicts(rows)


def create_story(conn: sqlite3.Connection, *, title: str, summary: str | None, first_seen: str, last_seen: str, primary_topic: str | None, best_item_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO stories(title, summary, first_seen, last_seen, heat, item_count, source_count, primary_topic, best_item_id, updated_at)
           VALUES(?,?,?,?,0,0,0,?,?,?)""",
        (title, summary, first_seen, last_seen, primary_topic, best_item_id, now_iso()),
    )
    return int(cur.lastrowid)


def update_story(conn: sqlite3.Connection, story_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE stories SET {cols} WHERE id=?", [*fields.values(), story_id])


# ============================================================ editions
def save_edition(conn: sqlite3.Connection, *, kind: str, date_key: str, title: str, overview: str | None, content: dict, markdown: str,
                 provider: str | None, model: str | None, is_mock: bool, item_count: int, effort: str | None = None) -> int:
    conn.execute(
        """INSERT INTO editions(kind, date_key, title, overview, content_json, markdown, provider, model, is_mock, effort, item_count, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(kind, date_key) DO UPDATE SET title=excluded.title, overview=excluded.overview, content_json=excluded.content_json,
             markdown=excluded.markdown, provider=excluded.provider, model=excluded.model, is_mock=excluded.is_mock, effort=excluded.effort,
             item_count=excluded.item_count, created_at=excluded.created_at, pushed=0""",
        (kind, date_key, title, overview, json.dumps(content, ensure_ascii=False), markdown, provider, model, 1 if is_mock else 0, effort, item_count, now_iso()),
    )
    return int(conn.execute("SELECT id FROM editions WHERE kind=? AND date_key=?", (kind, date_key)).fetchone()[0])


def get_edition(conn: sqlite3.Connection, kind: str, date_key: str) -> dict | None:
    r = conn.execute("SELECT * FROM editions WHERE kind=? AND date_key=?", (kind, date_key)).fetchone()
    return _edition(r)


def get_edition_by_id(conn: sqlite3.Connection, eid: int) -> dict | None:
    return _edition(conn.execute("SELECT * FROM editions WHERE id=?", (eid,)).fetchone())


def _edition(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    d["content"] = _loads(d.pop("content_json"), {})
    return d


def list_editions(conn: sqlite3.Connection, kind: str | None = None, limit: int = 60) -> list[dict]:
    if kind:
        rows = conn.execute("SELECT id, kind, date_key, title, overview, is_mock, item_count, created_at FROM editions WHERE kind=? ORDER BY date_key DESC LIMIT ?", (kind, limit))
    else:
        rows = conn.execute("SELECT id, kind, date_key, title, overview, is_mock, item_count, created_at FROM editions ORDER BY created_at DESC LIMIT ?", (limit,))
    return rows_to_dicts(rows)


def latest_edition(conn: sqlite3.Connection, kind: str) -> dict | None:
    return _edition(conn.execute("SELECT * FROM editions WHERE kind=? ORDER BY date_key DESC LIMIT 1", (kind,)).fetchone())


# ============================================================ reading
def get_reading(conn: sqlite3.Connection, item_id: int) -> dict:
    r = conn.execute("SELECT * FROM reading WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        return {"item_id": item_id, "status": "unread", "starred": 0, "read_later": 0, "rating": None, "notes": "", "tags": [], "read_at": None, "updated_at": None}
    return row_to_dict(r)  # type: ignore[return-value]


def set_reading(conn: sqlite3.Connection, item_id: int, **fields: Any) -> dict:
    cur = get_reading(conn, item_id)
    cur.update({k: v for k, v in fields.items() if v is not None})
    if fields.get("rating") == 0:  # 0 表示清除评分
        cur["rating"] = None
    if fields.get("status") == "read" and not cur.get("read_at"):
        cur["read_at"] = now_iso()
    if fields.get("status") == "unread":
        cur["read_at"] = None
    conn.execute(
        """INSERT INTO reading(item_id, status, starred, read_later, rating, notes, tags, read_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, starred=excluded.starred, read_later=excluded.read_later,
             rating=excluded.rating, notes=excluded.notes, tags=excluded.tags, read_at=excluded.read_at, updated_at=excluded.updated_at""",
        (item_id, cur.get("status", "unread"), 1 if cur.get("starred") else 0, 1 if cur.get("read_later") else 0, cur.get("rating"),
         cur.get("notes") or "", json.dumps(cur.get("tags") or [], ensure_ascii=False), cur.get("read_at"), now_iso()),
    )
    return get_reading(conn, item_id)


def reading_stats(conn: sqlite3.Connection) -> dict:
    s = {}
    s["read"] = conn.execute("SELECT COUNT(*) FROM reading WHERE status='read'").fetchone()[0]
    s["reading"] = conn.execute("SELECT COUNT(*) FROM reading WHERE status='reading'").fetchone()[0]
    s["starred"] = conn.execute("SELECT COUNT(*) FROM reading WHERE starred=1").fetchone()[0]
    s["read_later"] = conn.execute("SELECT COUNT(*) FROM reading WHERE read_later=1").fetchone()[0]
    s["notes"] = conn.execute("SELECT COUNT(*) FROM reading WHERE notes<>''").fetchone()[0]
    week = (now_utc() - timedelta(days=7)).isoformat(timespec="seconds")
    s["read_7d"] = conn.execute("SELECT COUNT(*) FROM reading WHERE status='read' AND read_at>=?", (week,)).fetchone()[0]
    tags: dict[str, int] = {}
    for (t,) in conn.execute("SELECT tags FROM reading WHERE tags<>'[]'"):
        for tag in _loads(t, []):
            tags[tag] = tags.get(tag, 0) + 1
    s["tags"] = dict(sorted(tags.items(), key=lambda kv: -kv[1]))
    return s


def reading_export(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, i.title, i.title_zh, i.url, i.doi, i.pmid, i.journal, i.published_at, i.kind FROM reading r JOIN items i ON i.id=r.item_id
           WHERE r.status<>'unread' OR r.starred=1 OR r.read_later=1 OR r.notes<>'' OR r.tags<>'[]' ORDER BY r.updated_at DESC"""
    )
    return rows_to_dicts(rows)


# ============================================================ collections
def list_collections(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id=c.id) AS n
           FROM collections c ORDER BY c.name"""
    )
    return rows_to_dicts(rows)


def get_collection(conn: sqlite3.Connection, cid: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone())


def create_collection(conn: sqlite3.Connection, name: str, description: str = "") -> int:
    conn.execute("INSERT OR IGNORE INTO collections(name, description, created_at) VALUES(?,?,?)", (name.strip(), description, now_iso()))
    return int(conn.execute("SELECT id FROM collections WHERE name=?", (name.strip(),)).fetchone()[0])


def delete_collection(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("DELETE FROM collections WHERE id=?", (cid,))


def add_to_collection(conn: sqlite3.Connection, cid: int, item_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO collection_items(collection_id, item_id, added_at) VALUES(?,?,?)", (cid, item_id, now_iso()))


def remove_from_collection(conn: sqlite3.Connection, cid: int, item_id: int) -> None:
    conn.execute("DELETE FROM collection_items WHERE collection_id=? AND item_id=?", (cid, item_id))


def collection_items(conn: sqlite3.Connection, cid: int) -> list[dict]:
    rows = conn.execute(ITEM_SELECT + " JOIN collection_items ci ON ci.item_id=i.id WHERE ci.collection_id=? ORDER BY ci.added_at DESC", (cid,))
    return rows_to_dicts(rows)


def item_collections(conn: sqlite3.Connection, item_id: int) -> list[int]:
    return [r[0] for r in conn.execute("SELECT collection_id FROM collection_items WHERE item_id=?", (item_id,))]


# ============================================================ logs
def log_push(conn: sqlite3.Connection, *, rule_key: str | None, channel_key: str, event: str, ref_type: str, ref_id: int | None, status: str, error: str | None) -> None:
    conn.execute(
        "INSERT INTO pushes(rule_key, channel_key, event, ref_type, ref_id, status, error, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (rule_key, channel_key, event, ref_type, ref_id, status, error, now_iso()),
    )


def recent_pushes(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return rows_to_dicts(conn.execute("SELECT * FROM pushes ORDER BY id DESC LIMIT ?", (limit,)))


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs(kind, started_at, status) VALUES(?,?, 'running')", (kind, now_iso()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, summary: dict, log: str = "") -> None:
    conn.execute("UPDATE runs SET finished_at=?, status=?, summary=?, log=? WHERE id=?", (now_iso(), status, json.dumps(summary, ensure_ascii=False), log[-20000:], run_id))


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["summary"] = _loads(d["summary"], {})
        out.append(d)
    return out


def log_llm_call(conn: sqlite3.Connection, *, task: str, provider: str, model: str, is_mock: bool, prompt_tokens: int | None, completion_tokens: int | None,
                 latency_ms: int | None, ok: bool, error: str | None, ref_type: str | None = None, ref_id: int | None = None,
                 effort: str | None = None, reasoning_tokens: int | None = None, cached: bool = False) -> None:
    conn.execute(
        """INSERT INTO llm_calls(task, provider, model, is_mock, effort, prompt_tokens, completion_tokens, reasoning_tokens, latency_ms, cached, ok, error, ref_type, ref_id, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task, provider, model, 1 if is_mock else 0, effort, prompt_tokens, completion_tokens, reasoning_tokens, latency_ms, 1 if cached else 0, 1 if ok else 0, error, ref_type, ref_id, now_iso()),
    )


# ============================================================ llm cache
def cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    r = conn.execute("SELECT * FROM llm_cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    conn.execute("UPDATE llm_cache SET hits=hits+1, last_hit_at=? WHERE key=?", (now_iso(), key))
    return dict(r)


def cache_put(conn: sqlite3.Connection, key: str, *, task: str, provider: str, model: str, effort: str | None, text: str, prompt_tokens: int | None, completion_tokens: int | None) -> None:
    conn.execute(
        """INSERT INTO llm_cache(key, task, provider, model, effort, text, prompt_tokens, completion_tokens, hits, created_at)
           VALUES(?,?,?,?,?,?,?,?,0,?)
           ON CONFLICT(key) DO UPDATE SET provider=excluded.provider, model=excluded.model, effort=excluded.effort, text=excluded.text,
             prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens, created_at=excluded.created_at""",
        (key, task, provider, model, effort, text, prompt_tokens, completion_tokens, now_iso()),
    )


def cache_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT task, COUNT(*) AS n, SUM(hits) AS hits, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) AS tokens FROM llm_cache GROUP BY task").fetchall()
    by_task = {r["task"]: {"entries": r["n"], "hits": r["hits"] or 0, "tokens_saved": (r["hits"] or 0) and int(r["tokens"] or 0) * (r["hits"] or 0) // max(1, r["n"])} for r in rows}
    return {"entries": sum(v["entries"] for v in by_task.values()), "hits": sum(v["hits"] for v in by_task.values()), "by_task": by_task}


def cache_clear(conn: sqlite3.Connection, task: str | None = None) -> int:
    cur = conn.execute("DELETE FROM llm_cache WHERE task=?", (task,)) if task else conn.execute("DELETE FROM llm_cache")
    return cur.rowcount


def items_mock_scored(conn: sqlite3.Connection, *, days: int, limit: int) -> list[dict]:
    """仍是模拟评分的条目，已入选、偏好高、新发表的优先——有真实模型时用于升级。"""
    since = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        ITEM_SELECT + """ WHERE i.status IN ('scored','error') AND i.llm_is_mock=1 AND COALESCE(i.published_at, i.fetched_at)>=?
                          ORDER BY i.selected DESC, i.preference_score DESC, COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?""",
        (since, limit),
    )
    return rows_to_dicts(rows)


def llm_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT provider, model, is_mock, effort, COUNT(*) AS n, SUM(ok) AS ok, SUM(cached) AS cached, SUM(COALESCE(prompt_tokens,0)) AS pt,
                  SUM(COALESCE(completion_tokens,0)) AS ct, SUM(COALESCE(reasoning_tokens,0)) AS rt, AVG(CASE WHEN cached=0 THEN latency_ms END) AS lat
           FROM llm_calls GROUP BY provider, model, is_mock, effort ORDER BY n DESC"""
    )
    recent = rows_to_dicts(conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 20"))
    return {"by_provider": rows_to_dicts(rows), "total": conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], "recent": recent}


# ============================================================ kv
def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

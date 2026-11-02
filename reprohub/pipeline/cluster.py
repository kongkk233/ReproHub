"""事件聚类与热度：把近期相关条目归为同一事件（story），按独立来源计热度。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import repo
from ..llm import LLM, LLMError
from ..settings import Settings
from ..textutil import jaccard, now_iso, parse_iso, tokenize, truncate

log = logging.getLogger("reprohub.cluster")
Logger = Callable[[str], None]


def _tokens(it: dict) -> tuple[set[str], set[str]]:
    title = f"{it.get('title') or ''} {it.get('title_zh') or ''}"
    return tokenize(title), tokenize(f"{title} {it.get('summary') or ''}")


def _sim(a: tuple[set, set], b: tuple[set, set]) -> float:
    return max(jaccard(a[0], b[0]), 0.85 * jaccard(a[1], b[1]))


def _llm_same(llm: LLM, a: dict, b: dict, sim: float, same_thr: float) -> bool:
    try:
        resp = llm.run(
            "cluster",
            {
                "a_title": a.get("title"), "a_summary": truncate(a.get("summary") or "", 600), "a_source": a.get("source_name"), "a_date": (a.get("published_at") or "")[:10],
                "b_title": b.get("title"), "b_summary": truncate(b.get("summary") or "", 600), "b_source": b.get("source_name"), "b_date": (b.get("published_at") or "")[:10],
            },
            context={"jaccard": sim, "jaccard_same": same_thr},
            ref_type="item", ref_id=a.get("id"),
        )
        rel = (resp.json() or {}).get("relation")
        return rel in ("same", "followup")
    except LLMError:
        return False


def compute_heat(conn: sqlite3.Connection, story_id: int, cfg: dict, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    window_h = float(cfg.get("heat_window_hours", 48))
    half = float(cfg.get("heat_half_life_hours", 24))
    tw = {int(k): float(v) for k, v in (cfg.get("tier_weight") or {1: 3, 2: 2, 3: 1}).items()}
    rows = conn.execute(
        """SELECT m.source_id, s.tier, MIN(COALESCE(i.published_at, m.seen_at)) AS t, MAX(COALESCE(i.final_score,0)) AS best
           FROM items i JOIN item_mentions m ON m.item_id=i.id JOIN sources s ON s.id=m.source_id
           WHERE i.story_id=? GROUP BY m.source_id""",
        (story_id,),
    ).fetchall()
    heat = 0.0
    best = 0.0
    for r in rows:
        t = parse_iso(r["t"])
        best = max(best, float(r["best"] or 0))
        if not t:
            continue
        age_h = max(0.0, (now - t).total_seconds() / 3600)
        if age_h > window_h * 4:
            continue
        w = tw.get(int(r["tier"]), 1.0) * (0.5 ** (age_h / half))
        heat += w
    return round(heat * 10 + best / 20, 1)


def _refresh_story(conn: sqlite3.Connection, story_id: int, cfg: dict) -> None:
    items = repo.story_items(conn, story_id)
    if not items:
        conn.execute("DELETE FROM stories WHERE id=?", (story_id,))
        return
    best = max(items, key=lambda x: (float(x.get("final_score") or 0), x.get("published_at") or ""))
    dates = [x.get("published_at") or x.get("fetched_at") for x in items]
    sources = {r["key"] for r in repo.story_sources(conn, story_id)}
    repo.update_story(
        conn, story_id,
        title=best.get("title_zh") or best.get("title"),
        summary=truncate(best.get("summary_zh") or best.get("summary") or "", 400),
        first_seen=min(dates), last_seen=max(dates),
        item_count=len(items), source_count=len(sources),
        primary_topic=best.get("primary_topic"), best_item_id=best["id"],
        heat=compute_heat(conn, story_id, cfg),
    )


def cluster_recent(settings: Settings, conn: sqlite3.Connection, llm: LLM, *, logger: Logger = log.info) -> dict:
    cfg = settings.selection.get("cluster") or {}
    window_days = int(cfg.get("window_days", 14))
    same_thr = float(cfg.get("jaccard_same", 0.45))
    amb_thr = float(cfg.get("jaccard_ambiguous", 0.28))
    use_llm = bool(cfg.get("use_llm_for_ambiguous", True)) and not llm.provider_for("cluster").is_mock
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(timespec="seconds")
    rows, _ = repo.list_items(conn, status="scored", since=since, date_field="fetched_at", order="date", page=1, page_size=2000)
    candidates = [r for r in rows if r.get("selected") or float(r.get("final_score") or 0) >= 50]
    assigned = [r for r in candidates if r.get("story_id")]
    unassigned = [r for r in candidates if not r.get("story_id")]
    # 现有事件的代表 token（取其中每条）
    story_members: dict[int, list[tuple[dict, tuple[set, set]]]] = {}
    for r in assigned:
        story_members.setdefault(int(r["story_id"]), []).append((r, _tokens(r)))
    created = attached = 0
    touched: set[int] = set()
    pending: list[tuple[dict, tuple[set, set]]] = []
    for r in unassigned:
        tk = _tokens(r)
        best_sid, best_sim, best_item = None, 0.0, None
        for sid, members in story_members.items():
            for m, mtk in members:
                s = _sim(tk, mtk)
                if s > best_sim:
                    best_sid, best_sim, best_item = sid, s, m
        if best_sid and (best_sim >= same_thr or (best_sim >= amb_thr and use_llm and best_item and _llm_same(llm, r, best_item, best_sim, same_thr))):
            repo.update_item(conn, r["id"], story_id=best_sid)
            story_members[best_sid].append((r, tk))
            touched.add(best_sid)
            attached += 1
            continue
        # 与本轮其它未分配条目比较
        matched = None
        for p, ptk in pending:
            s = _sim(tk, ptk)
            if s >= same_thr or (s >= amb_thr and use_llm and _llm_same(llm, r, p, s, same_thr)):
                matched = p
                break
        if matched and matched.get("story_id"):
            sid = int(matched["story_id"])
            repo.update_item(conn, r["id"], story_id=sid)
            story_members.setdefault(sid, []).append((r, tk))
            touched.add(sid)
            attached += 1
        elif matched:
            sid = repo.create_story(conn, title=matched.get("title_zh") or matched["title"], summary=None, first_seen=matched.get("published_at") or matched["fetched_at"],
                                    last_seen=r.get("published_at") or r["fetched_at"], primary_topic=matched.get("primary_topic"), best_item_id=matched["id"])
            repo.update_item(conn, matched["id"], story_id=sid)
            repo.update_item(conn, r["id"], story_id=sid)
            matched["story_id"] = sid
            r["story_id"] = sid
            story_members[sid] = [(matched, _tokens(matched)), (r, tk)]
            touched.add(sid)
            created += 1
        else:
            pending.append((r, tk))
    # 剩余单条也各自成为事件（热度低，但保证每条可进入事件视图）
    for p, ptk in pending:
        if p.get("story_id"):
            continue
        sid = repo.create_story(conn, title=p.get("title_zh") or p["title"], summary=None, first_seen=p.get("published_at") or p["fetched_at"],
                                last_seen=p.get("published_at") or p["fetched_at"], primary_topic=p.get("primary_topic"), best_item_id=p["id"])
        repo.update_item(conn, p["id"], story_id=sid)
        touched.add(sid)
        created += 1
    # 刷新热度（窗口内所有事件，因为衰减随时间变化）
    for (sid,) in conn.execute("SELECT id FROM stories WHERE last_seen>=?", (since,)).fetchall():
        _refresh_story(conn, int(sid), cfg)
    logger(f"[聚类] 候选 {len(candidates)}：新事件 {created}，并入已有 {attached}，刷新 {len(touched)}")
    return {"candidates": len(candidates), "created": created, "attached": attached}

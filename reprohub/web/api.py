"""JSON API v1：只读接口公开；阅读/集合/入库/管理需要本机或口令。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import __version__, repo
from ..db import get_conn
from ..llm import LLM, LLMError, llm_status
from ..pipeline.classify import Classifier
from ..pipeline.collect import ingest_item
from ..pipeline.interpret import interpret_item
from ..pipeline.runner import run_pipeline
from ..push import channel_status
from ..sources import RawItem
from ..textutil import now_utc, parse_date_loose
from .common import is_owner, require_owner

router = APIRouter(tags=["v1"])


def _s(request: Request):
    return request.app.state.settings


def _public_item(it: dict, *, full: bool = False) -> dict:
    keys = [
        "id", "title", "title_zh", "url", "doi", "pmid", "journal", "kind", "lang", "published_at", "fetched_at", "authors",
        "primary_topic", "topics", "topic_score", "preference_score", "preference_labels", "llm_score", "final_score", "score_dims",
        "recommend_reason", "keywords", "selected", "story_id", "llm_provider", "llm_model", "source_key", "source_name", "source_tier",
        "mention_count", "has_interpretation",
    ]
    d = {k: it.get(k) for k in keys}
    d["is_mock"] = bool(it.get("llm_is_mock"))
    d["summary_zh"] = it.get("summary_zh")
    d["summary"] = it.get("summary") if full else (it.get("summary") or "")[:500]
    d["has_interpretation"] = bool(it.get("has_interpretation"))
    return d


@router.get("/status")
def status(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    st = llm_status(settings)
    return {
        "name": settings.site_info.get("name"),
        "version": __version__,
        "time": now_utc().isoformat(timespec="seconds"),
        "llm": st,
        "mock_notice": "is_mock=true 的评分/解读/总述由规则占位生成，未调用任何模型" if st.get("mode") == "mock" else None,
        "counts": repo.counts(conn),
        "push_channels": channel_status(settings),
        "last_runs": repo.recent_runs(conn, 3),
    }


@router.get("/topics")
def topics(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    c = repo.topic_counts(conn, selected_only=True)
    return {"groups": settings.topic_groups, "topics": [{"slug": t["slug"], "name": t["name"], "name_en": t.get("name_en"), "group": t.get("group"), "selected_count": c.get(t["slug"], 0)} for t in settings.topics]}


@router.get("/items")
def items(request: Request, selected: int | None = None, kind: str | None = None, topic: str | None = None, source: str | None = None, q: str | None = None,
          min_score: float | None = None, min_preference: float | None = None, days: int | None = None, order: str = "date", page: int = 1, page_size: int = 30, status: str | None = "scored"):
    conn = get_conn(_s(request))
    since = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds") if days else None
    rows, total = repo.list_items(conn, selected=(bool(selected) if selected is not None else None), status=status or None, kind=kind, topic=topic, source_key=source, q=q,
                                  min_score=min_score, min_preference=min_preference, since=since, order=order, page=max(1, page), page_size=min(200, max(1, page_size)))
    return {"total": total, "page": page, "page_size": page_size, "items": [_public_item(r) for r in rows]}


@router.get("/items/{item_id}")
def item(request: Request, item_id: int):
    conn = get_conn(_s(request))
    it = repo.get_item(conn, item_id)
    if not it:
        raise HTTPException(404)
    d = _public_item(it, full=True)
    interp = repo.get_interpretation(conn, item_id)
    d["interpretation"] = {"content": interp["content"], "markdown": interp["markdown"], "provider": interp["provider"], "model": interp["model"], "is_mock": bool(interp["is_mock"]), "created_at": interp["created_at"]} if interp else None
    if is_owner(request):
        d["reading"] = repo.get_reading(conn, item_id)
    return d


@router.post("/items/{item_id}/interpret")
def item_interpret(request: Request, item_id: int, force: bool = True, _=Depends(require_owner)):
    settings = _s(request)
    conn = get_conn(settings)
    it = repo.get_item(conn, item_id)
    if not it:
        raise HTTPException(404)
    try:
        r = interpret_item(settings, conn, LLM(settings, conn), it, force=force)
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "is_mock": bool(r and r.get("is_mock")), "provider": r and r.get("provider"), "markdown": r and r.get("markdown")}


@router.get("/search")
def search(request: Request, q: str, page: int = 1, page_size: int = 30):
    conn = get_conn(_s(request))
    rows, total = repo.list_items(conn, q=q, page=page, page_size=min(100, page_size))
    return {"total": total, "items": [_public_item(r) for r in rows]}


@router.get("/stories")
def stories(request: Request, days: int = 14, limit: int = 30):
    conn = get_conn(_s(request))
    out = []
    for s in repo.list_stories(conn, limit=min(100, limit), days=days):
        s["sources"] = repo.story_sources(conn, s["id"])
        out.append(s)
    return {"stories": out}


@router.get("/stories/{story_id}")
def story(request: Request, story_id: int):
    conn = get_conn(_s(request))
    s = repo.get_story(conn, story_id)
    if not s:
        raise HTTPException(404)
    s["items"] = [_public_item(x) for x in repo.story_items(conn, story_id)]
    s["sources"] = repo.story_sources(conn, story_id)
    return s


@router.get("/editions")
def editions(request: Request, kind: str | None = None, limit: int = 30):
    conn = get_conn(_s(request))
    return {"editions": repo.list_editions(conn, kind, limit=min(200, limit))}


@router.get("/editions/{kind}/{key}")
def edition(request: Request, kind: str, key: str):
    conn = get_conn(_s(request))
    ed = repo.get_edition(conn, kind, key)
    if not ed:
        raise HTTPException(404)
    ed["is_mock"] = bool(ed["is_mock"])
    return ed


@router.get("/sources")
def sources(request: Request):
    conn = get_conn(_s(request))
    return {"sources": repo.source_stats(conn)}


# ---------------------------------------------------------------- reading
class ReadingUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(unread|reading|read)$")
    starred: bool | None = None
    read_later: bool | None = None
    rating: int | None = Field(None, ge=0, le=5)
    notes: str | None = None
    tags: list[str] | None = None


@router.get("/reading/{item_id}")
def reading_get(request: Request, item_id: int, _=Depends(require_owner)):
    return repo.get_reading(get_conn(_s(request)), item_id)


@router.post("/reading/{item_id}")
def reading_set(request: Request, item_id: int, body: ReadingUpdate, _=Depends(require_owner)):
    conn = get_conn(_s(request))
    if not repo.get_item(conn, item_id):
        raise HTTPException(404)
    fields = body.model_dump(exclude_none=True)
    if "tags" in fields:
        fields["tags"] = [t.strip() for t in fields["tags"] if t and t.strip()][:20]
    return repo.set_reading(conn, item_id, **fields)


@router.get("/reading")
def reading_list(request: Request, _=Depends(require_owner)):
    return {"stats": repo.reading_stats(get_conn(_s(request))), "items": repo.reading_export(get_conn(_s(request)))}


@router.post("/reading/import")
def reading_import(request: Request, rows: list[dict] = Body(...), _=Depends(require_owner)):
    conn = get_conn(_s(request))
    n = 0
    for r in rows:
        iid = r.get("item_id")
        if not iid and r.get("doi"):
            row = conn.execute("SELECT id FROM items WHERE doi=?", (r["doi"].lower(),)).fetchone()
            iid = row[0] if row else None
        if not iid and r.get("url"):
            row = conn.execute("SELECT id FROM items WHERE url=?", (r["url"],)).fetchone()
            iid = row[0] if row else None
        if not iid or not repo.get_item(conn, int(iid)):
            continue
        repo.set_reading(conn, int(iid), status=r.get("status"), starred=r.get("starred"), read_later=r.get("read_later"), rating=r.get("rating"), notes=r.get("notes"), tags=r.get("tags"))
        n += 1
    return {"imported": n}


# ---------------------------------------------------------------- collections
@router.get("/collections")
def collections(request: Request):
    return {"collections": repo.list_collections(get_conn(_s(request)))}


@router.post("/collections")
def collection_create(request: Request, name: str = Body(..., embed=True), description: str = Body("", embed=True), _=Depends(require_owner)):
    conn = get_conn(_s(request))
    return {"id": repo.create_collection(conn, name, description)}


@router.get("/collections/{cid}")
def collection_get(request: Request, cid: int):
    conn = get_conn(_s(request))
    c = repo.get_collection(conn, cid)
    if not c:
        raise HTTPException(404)
    c["items"] = [_public_item(x) for x in repo.collection_items(conn, cid)]
    return c


@router.post("/collections/{cid}/items/{item_id}")
def collection_add(request: Request, cid: int, item_id: int, _=Depends(require_owner)):
    conn = get_conn(_s(request))
    if not repo.get_collection(conn, cid) or not repo.get_item(conn, item_id):
        raise HTTPException(404)
    repo.add_to_collection(conn, cid, item_id)
    return {"ok": True}


@router.delete("/collections/{cid}/items/{item_id}")
def collection_remove(request: Request, cid: int, item_id: int, _=Depends(require_owner)):
    conn = get_conn(_s(request))
    repo.remove_from_collection(conn, cid, item_id)
    return {"ok": True}


# ---------------------------------------------------------------- ingest
class IngestItem(BaseModel):
    url: str
    title: str
    summary: str = ""
    source: str = "ingest"
    doi: str | None = None
    pmid: str | None = None
    authors: list[str] = []
    journal: str | None = None
    published_at: str | None = None
    kind: str | None = None
    external_id: str | None = None
    extra: dict[str, Any] = {}


@router.post("/ingest")
def ingest(request: Request, items: list[IngestItem]):
    settings = _s(request)
    token = settings.env("INGEST_TOKEN")
    if not token:
        raise HTTPException(403, "INGEST_TOKEN 未设置，推送入库接口关闭")
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {token}" and request.headers.get("x-ingest-token") != token:
        raise HTTPException(401, "令牌错误")
    conn = get_conn(settings)
    classifier = Classifier(settings)
    results = []
    for it in items[:200]:
        raw = RawItem(url=it.url, title=it.title, summary=it.summary, external_id=it.external_id, doi=it.doi, pmid=it.pmid, authors=it.authors, journal=it.journal,
                      published_at=parse_date_loose(it.published_at), kind=it.kind, extra=it.extra)
        try:
            iid, st = ingest_item(conn, settings, it.source, raw, classifier)
        except ValueError as e:
            results.append({"url": it.url, "status": "error", "error": str(e)})
            continue
        results.append({"url": it.url, "status": st, "id": iid})
    return {"results": results, "note": "新条目状态为 new，需运行 process（或等待调度）完成评分"}


# ---------------------------------------------------------------- admin
@router.post("/admin/run")
def admin_run(request: Request, steps: dict = Body(default={}), _=Depends(require_owner)):
    settings = _s(request)
    conn = get_conn(settings)
    kw = {k: bool(steps.get(k.replace("do_", ""), True)) for k in ("do_collect", "do_process", "do_interpret", "do_cluster", "do_daily", "do_push")}
    lines: list[str] = []
    summary = run_pipeline(settings, conn, force=bool(steps.get("force")), logger=lines.append, **kw)
    return {"summary": summary, "log": lines[-200:]}

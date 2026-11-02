"""HTML 页面路由。"""
from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from .. import repo
from ..db import get_conn
from ..llm import EFFORT_LEVELS, LLM, LLMError, clear_overrides, llm_status, set_overrides, test_llm
from ..pipeline.cluster import cluster_recent
from ..pipeline.collect import collect
from ..pipeline.editions import build_daily, build_monthly, build_weekly
from ..pipeline.interpret import auto_interpret, interpret_item
from ..pipeline.runner import mock_banner, run_pipeline
from ..pipeline.score import process_new, upgrade_mock_scores
from ..push import channel_status, test_channel
from ..push.dispatcher import push_edition
from ..settings import get_settings
from ..textutil import now_utc
from .common import COOKIE, EDITION_LABEL, KIND_LABEL, ctx, is_owner, login_cookie_value, qp_int, require_owner

router = APIRouter()


def _t(request: Request):
    return request.app.state.templates


def _s(request: Request):
    return request.app.state.settings


def _render(request: Request, name: str, status_code: int = 200, **kw) -> HTMLResponse:
    return _t(request).TemplateResponse(request=request, name=name, context=ctx(request, **kw), status_code=status_code)


def _not_found(request: Request) -> HTMLResponse:
    return _render(request, "404.html", status_code=404, title="未找到")


# ---------------------------------------------------------------- lists
def _list_filters(request: Request) -> dict:
    q = request.query_params
    f = {
        "topic": q.get("topic") or None,
        "kind": q.get("kind") or None,
        "source_key": q.get("source") or None,
        "tier": int(q["tier"]) if q.get("tier", "").isdigit() else None,
        "unread_only": q.get("unread") in ("1", "true"),
        "q": q.get("q") or None,
        "order": q.get("order") or "date",
        "page": qp_int(request, "page", 1),
    }
    if q.get("min_score", "").replace(".", "", 1).isdigit():
        f["min_score"] = float(q["min_score"])
    if q.get("days", "").isdigit():
        f["since"] = (now_utc() - timedelta(days=int(q["days"]))).isoformat(timespec="seconds")
    return f


def _list_page(request: Request, *, title: str, template: str = "list.html", **fixed):
    settings = _s(request)
    conn = get_conn(settings)
    f = _list_filters(request)
    f.update(fixed)
    page_size = int(settings.site_info.get("page_size", 30))
    items, total = repo.list_items(conn, page_size=page_size, **f)
    groups = repo.date_groups(conn, items, settings.tz)
    pages = max(1, (total + page_size - 1) // page_size)
    return _render(request, template, title=title, items=items, groups=groups, total=total, page=f["page"], pages=pages, filters=f)


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    f = _list_filters(request)
    if settings.site_info.get("home_selected_only", True):
        f["selected"] = True
    else:
        f["status"] = "scored"
    page_size = int(settings.site_info.get("page_size", 30))
    items, total = repo.list_items(conn, page_size=page_size, **f)
    groups = repo.date_groups(conn, items, settings.tz)
    stories = repo.list_stories(conn, limit=5, days=7, min_items=1)
    stories = [s for s in stories if s["source_count"] >= 2 or s["item_count"] >= 2][:5] or stories[:3]
    pages = max(1, (total + page_size - 1) // page_size)
    counts = repo.counts(conn)
    latest_daily = repo.latest_edition(conn, "daily")
    return _render(request, "index.html", title="精选", items=items, groups=groups, total=total, page=f["page"], pages=pages, filters=f, stories=stories, counts=counts, latest_daily=latest_daily)


@router.get("/all", response_class=HTMLResponse)
def all_items(request: Request):
    return _list_page(request, title="全部动态", status="scored")


@router.get("/papers", response_class=HTMLResponse)
def papers(request: Request):
    return _list_page(request, title="文献与预印本", status="scored", kinds=["paper", "preprint", "guideline"])


@router.get("/news", response_class=HTMLResponse)
def news(request: Request):
    return _list_page(request, title="资讯与动态", status="scored", kinds=["news", "post", "other"])


@router.get("/search", response_class=HTMLResponse)
def search(request: Request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return _render(request, "list.html", title="搜索", items=[], groups=[], total=0, page=1, pages=1, filters={"q": ""})
    return _list_page(request, title=f"搜索：{q}", q=q)


@router.get("/topics", response_class=HTMLResponse)
def topics_page(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    c_all = repo.topic_counts(conn, selected_only=True)
    c_7 = repo.topic_counts(conn, selected_only=True, days=7)
    return _render(request, "topics.html", title="主题", counts=c_all, counts7=c_7)


@router.get("/topics/{slug}", response_class=HTMLResponse)
def topic_page(request: Request, slug: str):
    settings = _s(request)
    t = settings.topic_map.get(slug)
    if not t:
        return _not_found(request)
    return _list_page(request, title=t["name"], status="scored", topic=slug)


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    return _render(request, "sources.html", title="来源", sources=repo.source_stats(conn), cfg={s["key"]: s for s in settings.sources})


@router.get("/sources/{key}", response_class=HTMLResponse)
def source_page(request: Request, key: str):
    settings = _s(request)
    conn = get_conn(settings)
    src = repo.get_source(conn, key)
    if not src:
        return _not_found(request)
    return _list_page(request, title=src["name"], source_key=key)


# ---------------------------------------------------------------- item
@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int):
    settings = _s(request)
    conn = get_conn(settings)
    it = repo.get_item(conn, item_id)
    if not it:
        return _not_found(request)
    interp = repo.get_interpretation(conn, item_id)
    story = repo.get_story(conn, it["story_id"]) if it.get("story_id") else None
    related = [x for x in repo.story_items(conn, it["story_id"]) if x["id"] != item_id] if it.get("story_id") else []
    mentions = conn.execute("SELECT m.url, m.seen_at, s.name, s.key FROM item_mentions m JOIN sources s ON s.id=m.source_id WHERE m.item_id=? ORDER BY m.seen_at", (item_id,)).fetchall()
    colls = repo.list_collections(conn)
    in_colls = set(repo.item_collections(conn, item_id))
    reading = repo.get_reading(conn, item_id)
    st = llm_status(settings, conn)
    return _render(request, "item.html", title=it.get("title_zh") or it["title"], it=it, interp=interp, story=story, related=related, mentions=[dict(m) for m in mentions], collections=colls, in_collections=in_colls, reading=reading, llm=st)


@router.post("/items/{item_id}/interpret")
def item_interpret(request: Request, item_id: int, _=Depends(require_owner)):
    settings = _s(request)
    conn = get_conn(settings)
    it = repo.get_item(conn, item_id)
    if not it:
        return JSONResponse({"error": "not found"}, status_code=404)
    llm = LLM(settings, conn)
    try:
        interpret_item(settings, conn, llm, it, force=True)
    except LLMError as e:
        return _render(request, "error.html", title="解读失败", message=str(e))
    return RedirectResponse(f"/items/{item_id}#interpretation", status_code=303)


# ---------------------------------------------------------------- stories
@router.get("/stories", response_class=HTMLResponse)
def stories_page(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    days = qp_int(request, "days", 14, 1, 365)
    stories = repo.list_stories(conn, limit=60, days=days, min_items=1)
    for s in stories:
        s["sources"] = repo.story_sources(conn, s["id"])
    return _render(request, "stories.html", title="热点事件", stories=stories, days=days)


@router.get("/story/{story_id}", response_class=HTMLResponse)
def story_page(request: Request, story_id: int):
    settings = _s(request)
    conn = get_conn(settings)
    s = repo.get_story(conn, story_id)
    if not s:
        return _not_found(request)
    items = repo.story_items(conn, story_id)
    return _render(request, "story.html", title=s["title"], story=s, items=items, sources=repo.story_sources(conn, story_id))


# ---------------------------------------------------------------- editions
@router.get("/editions", response_class=HTMLResponse)
def editions_page(request: Request):
    conn = get_conn(_s(request))
    kind = request.query_params.get("kind") or None
    eds = repo.list_editions(conn, kind, limit=120)
    return _render(request, "editions.html", title="日报 · 周报 · 月报", editions=eds, kind=kind)


@router.get("/editions/{kind}/{key}", response_class=HTMLResponse)
def edition_page(request: Request, kind: str, key: str):
    conn = get_conn(_s(request))
    ed = repo.get_edition(conn, kind, key)
    if not ed:
        return _not_found(request)
    ids = [it["id"] for s in ed["content"].get("sections", []) for it in s["items"]]
    full = {x["id"]: x for x in repo.get_items_by_ids(conn, ids)}
    return _render(request, "edition.html", title=ed["title"], ed=ed, full=full)


@router.get("/editions/{kind}/{key}.md")
def edition_markdown(request: Request, kind: str, key: str):
    conn = get_conn(_s(request))
    ed = repo.get_edition(conn, kind, key)
    if not ed:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(ed["markdown"], media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------- library
@router.get("/library", response_class=HTMLResponse)
def library(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    tab = request.query_params.get("tab", "later")
    page = qp_int(request, "page", 1)
    filters: dict = {"order": "updated", "page": page, "page_size": 50}
    if tab == "later":
        filters["read_later"] = True
    elif tab == "starred":
        filters["starred"] = True
    elif tab == "reading":
        filters["read_status"] = "reading"
    elif tab == "read":
        filters["read_status"] = "read"
    elif tab == "notes":
        filters["read_status"] = None
    tag = request.query_params.get("tag")
    items: list[dict] = []
    total = 0
    if tab == "notes":
        rows = conn.execute("SELECT item_id FROM reading WHERE notes<>'' ORDER BY updated_at DESC LIMIT 200").fetchall()
        items = repo.get_items_by_ids(conn, [r[0] for r in rows])
        total = len(items)
    elif tab == "tag" and tag:
        rows = conn.execute("SELECT item_id FROM reading WHERE tags LIKE ? ORDER BY updated_at DESC LIMIT 200", (f'%"{tag}"%',)).fetchall()
        items = repo.get_items_by_ids(conn, [r[0] for r in rows])
        total = len(items)
    elif tab != "collections":
        items, total = repo.list_items(conn, **filters)
    stats = repo.reading_stats(conn)
    colls = repo.list_collections(conn)
    return _render(request, "library.html", title="阅读管理", tab=tab, tag=tag, items=items, total=total, stats=stats, collections=colls, page=page)


@router.get("/library/export.{fmt}")
def library_export(request: Request, fmt: str, _=Depends(require_owner)):
    conn = get_conn(_s(request))
    rows = repo.reading_export(conn)
    stamp = now_utc().strftime("%Y%m%d")
    if fmt == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=reprohub-reading-{stamp}.json"})
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["item_id", "status", "starred", "read_later", "rating", "tags", "notes", "title", "title_zh", "url", "doi", "pmid", "journal", "published_at", "kind", "read_at", "updated_at"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["tags"] = ";".join(r.get("tags") or [])
            w.writerow(r)
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=reprohub-reading-{stamp}.csv"})
    if fmt == "md":
        lines = [f"# ReproHub 阅读记录 {stamp}", ""]
        for r in rows:
            flags = []
            if r.get("starred"):
                flags.append("★")
            if r.get("read_later"):
                flags.append("稍后读")
            flags.append({"unread": "未读", "reading": "在读", "read": "已读"}.get(r.get("status"), ""))
            lines.append(f"- [{r.get('title_zh') or r.get('title')}]({r.get('url')}) · {' '.join(flags)}" + (f" · 评分 {r['rating']}" if r.get("rating") else ""))
            if r.get("tags"):
                lines.append(f"  标签：{'、'.join(r['tags'])}")
            if r.get("notes"):
                lines.append("  > " + r["notes"].replace("\n", "\n  > "))
        return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=reprohub-reading-{stamp}.md"})
    if fmt == "bib":
        out = []
        for r in rows:
            key = (r.get("doi") or f"item{r['item_id']}").replace("/", "_")
            fields = [f"  title = {{{r.get('title')}}}"]
            if r.get("journal"):
                fields.append(f"  journal = {{{r['journal']}}}")
            if r.get("published_at"):
                fields.append(f"  year = {{{r['published_at'][:4]}}}")
            if r.get("doi"):
                fields.append(f"  doi = {{{r['doi']}}}")
            if r.get("url"):
                fields.append(f"  url = {{{r['url']}}}")
            out.append(f"@article{{{key},\n" + ",\n".join(fields) + "\n}")
        return PlainTextResponse("\n\n".join(out), media_type="application/x-bibtex; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=reprohub-reading-{stamp}.bib"})
    return PlainTextResponse("unsupported", status_code=400)


@router.get("/collections/{cid}", response_class=HTMLResponse)
def collection_page(request: Request, cid: int):
    conn = get_conn(_s(request))
    c = repo.get_collection(conn, cid)
    if not c:
        return _not_found(request)
    return _render(request, "collection.html", title=c["name"], collection=c, items=repo.collection_items(conn, cid))


@router.post("/collections")
def collection_create(request: Request, name: str = Form(...), description: str = Form(""), _=Depends(require_owner)):
    conn = get_conn(_s(request))
    cid = repo.create_collection(conn, name, description)
    return RedirectResponse(f"/collections/{cid}", status_code=303)


@router.post("/collections/{cid}/delete")
def collection_delete(request: Request, cid: int, _=Depends(require_owner)):
    conn = get_conn(_s(request))
    repo.delete_collection(conn, cid)
    return RedirectResponse("/library?tab=collections", status_code=303)


# ---------------------------------------------------------------- static pages
@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    settings = _s(request)
    conn = get_conn(settings)
    return _render(request, "about.html", title="关于", counts=repo.counts(conn), llm=llm_status(settings, conn), sources=repo.list_sources(conn), preferences=settings.preferences.get("rules") or [], selection=settings.selection)


@router.get("/agent", response_class=HTMLResponse)
def agent(request: Request):
    return _render(request, "agent.html", title="接入：RSS / API / Agent")


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt(request: Request):
    settings = _s(request)
    info = settings.site_info
    base = info.get("base_url", "")
    lines = [
        f"# {info.get('name')} — {info.get('name_zh')}",
        "",
        f"> {info.get('tagline')}",
        "",
        info.get("description", "").strip(),
        "",
        "## 入口",
        f"- 精选: {base}/",
        f"- JSON API: {base}/api/v1/items?selected=1  （OpenAPI: {base}/openapi.json）",
        f"- RSS: {base}/feed.xml  全部 {base}/feed/all.xml  文献 {base}/feed/papers.xml  日报 {base}/feed/editions.xml",
        f"- 热点事件: {base}/api/v1/stories",
        f"- 日报/周报/月报: {base}/api/v1/editions",
        f"- 状态与 LLM 模式: {base}/api/v1/status  （is_mock=true 表示该结果由规则占位生成，未调用模型）",
        "",
        "## 主题",
    ] + [f"- {t['slug']}: {t['name']} ({t.get('name_en', '')})" for t in settings.topics]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render(request, "login.html", title="登录")


@router.post("/login")
def login(request: Request, password: str = Form("")):
    settings = _s(request)
    pw = settings.env("ADMIN_PASSWORD")
    if not pw:
        return RedirectResponse("/admin", status_code=303)
    if password != pw:
        return _render(request, "login.html", title="登录", error="口令错误")
    resp = RedirectResponse(request.query_params.get("next") or "/admin", status_code=303)
    resp.set_cookie(COOKIE, login_cookie_value(settings), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@router.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- admin
@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    settings = _s(request)
    if not is_owner(request):
        return RedirectResponse("/login?next=/admin", status_code=303)
    conn = get_conn(settings)
    return _render(
        request, "admin.html", title="后台",
        counts=repo.counts(conn), llm=llm_status(settings, conn), banner=mock_banner(settings, conn), sources=repo.source_stats(conn), effort_levels=list(EFFORT_LEVELS),
        llm_test_result=request.query_params.get("llm_test"),
        runs=repo.recent_runs(conn, 15), pushes=repo.recent_pushes(conn, 30), channels=channel_status(settings), llm_stats=repo.llm_stats(conn), cache=repo.cache_stats(conn),
        rules=settings.push.get("rules") or [], message=request.query_params.get("msg"),
    )


@router.post("/admin/llm")
def admin_llm(request: Request, action: str = Form(...), provider: str = Form(""), model: str = Form(""), effort: str = Form(""), _=Depends(require_owner)):
    """后台表单：切换 / 测试 / 清除 LLM 提供商、模型与思考强度。"""
    from urllib.parse import quote

    settings = _s(request)
    conn = get_conn(settings)
    if action == "set":
        cur = set_overrides(conn, provider=provider, model=model, effort=effort, replace=True)
        msg = f"运行时覆盖已保存：{cur or '（空，使用配置默认）'}"
    elif action == "clear":
        clear_overrides(conn)
        msg = "已清除运行时覆盖"
    else:
        r = test_llm(settings, conn, provider=provider or None, model=model or None, effort=effort or None)
        if r.get("ok"):
            msg = (f"{'模拟' if r.get('is_mock') else '真实'}调用 {r['provider']}/{r['model']} 强度={r.get('effort')}：{r['latency_ms']} ms，"
                   f"tokens in/out/reasoning={r.get('prompt_tokens')}/{r.get('completion_tokens')}/{r.get('reasoning_tokens')}，附加字段 {r.get('request_extra')}\n回复：{r['content'][:200]}")
        else:
            msg = f"✗ {r.get('provider')}/{r.get('model')}：{r.get('error')}"
    return RedirectResponse("/admin?msg=" + quote(msg[-1500:]) + "#llm", status_code=303)


@router.post("/admin/run")
def admin_run(request: Request, action: str = Form(...), key: str = Form(""), _=Depends(require_owner)):
    settings = _s(request)
    conn = get_conn(settings)
    llm = LLM(settings, conn)
    msgs: list[str] = []
    logger = msgs.append
    try:
        if action == "pipeline":
            run_pipeline(settings, conn, logger=logger)
        elif action == "collect":
            collect(settings, conn, force=True, source_keys=[key] if key else None, logger=logger)
        elif action == "process":
            process_new(settings, conn, llm, retry_errors=True, logger=logger)
        elif action == "upgrade":
            r = upgrade_mock_scores(settings, conn, llm, limit=int(key) if key.isdigit() else None, logger=logger)
            if not r["processed"]:
                msgs.append("没有需要升级的模拟评分（或当前仍是模拟模式）")
        elif action == "cache-clear":
            msgs.append(f"已清除 {repo.cache_clear(conn)} 条模型输出缓存")
        elif action == "interpret":
            auto_interpret(settings, conn, llm, logger=logger)
        elif action == "cluster":
            cluster_recent(settings, conn, llm, logger=logger)
        elif action == "daily":
            ed = build_daily(settings, conn, llm, key or None, logger=logger)
            push_edition(settings, conn, ed, logger=logger)
        elif action == "weekly":
            ed = build_weekly(settings, conn, llm, key or None, logger=logger)
            push_edition(settings, conn, ed, logger=logger)
        elif action == "monthly":
            ed = build_monthly(settings, conn, llm, key or None, logger=logger)
            push_edition(settings, conn, ed, logger=logger)
        elif action == "push-test":
            r = test_channel(settings, conn, key)
            msgs.append(f"推送测试 {key}: {r}")
        elif action == "llm-clear":
            clear_overrides(conn)
            msgs.append("已清除 LLM 运行时覆盖，回到 models.yaml / 环境变量的设置")
        else:
            msgs.append(f"未知操作 {action}")
    except Exception as e:  # noqa: BLE001
        msgs.append(f"✗ {type(e).__name__}: {e}")
    from urllib.parse import quote

    return RedirectResponse("/admin?msg=" + quote("\n".join(msgs)[-1500:]), status_code=303)

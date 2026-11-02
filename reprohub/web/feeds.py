"""RSS 2.0 输出。"""
from __future__ import annotations

from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import repo
from ..db import get_conn
from ..textutil import now_utc, parse_iso

router = APIRouter()


def _rss(title: str, link: str, desc: str, entries: list[dict]) -> Response:
    items_xml = []
    for e in entries:
        dt = parse_iso(e.get("date")) or now_utc()
        items_xml.append(
            "<item>"
            f"<title>{escape(e['title'])}</title>"
            f"<link>{escape(e['link'])}</link>"
            f"<guid isPermaLink=\"false\">{escape(e['guid'])}</guid>"
            f"<pubDate>{format_datetime(dt)}</pubDate>"
            + (f"<category>{escape(e['category'])}</category>" if e.get("category") else "")
            + f"<description>{escape(e['description'])}</description>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        f"<title>{escape(title)}</title><link>{escape(link)}</link><description>{escape(desc)}</description>"
        f"<lastBuildDate>{format_datetime(now_utc())}</lastBuildDate>"
        + "".join(items_xml)
        + "</channel></rss>"
    )
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def _entry(settings, it: dict) -> dict:
    base = settings.site_info.get("base_url", "")
    mock = "【模拟评分】" if it.get("llm_is_mock") else ""
    t = it.get("title_zh") or it["title"]
    desc_parts = [f"{mock}评分 {it.get('final_score')} · {it.get('source_name')}" + (f" · {it.get('journal')}" if it.get("journal") else "")]
    if it.get("title_zh"):
        desc_parts.append(it["title"])
    if it.get("preference_labels"):
        desc_parts.append("偏好：" + "、".join(it["preference_labels"]))
    desc_parts.append(it.get("summary_zh") or (it.get("summary") or "")[:800])
    if it.get("recommend_reason"):
        desc_parts.append("推荐理由：" + it["recommend_reason"])
    desc_parts.append(f"原文：{it.get('url')}")
    return {
        "title": f"[{it.get('final_score')}] {t}",
        "link": f"{base}/items/{it['id']}",
        "guid": f"reprohub-item-{it['id']}",
        "date": it.get("selected_at") or it.get("published_at") or it.get("fetched_at"),
        "category": settings.topic_map.get(it.get("primary_topic") or "", {}).get("name"),
        "description": "\n\n".join(p for p in desc_parts if p),
    }


def _items_feed(request: Request, title: str, **filters) -> Response:
    settings = request.app.state.settings
    conn = get_conn(settings)
    rows, _ = repo.list_items(conn, page=1, page_size=60, **filters)
    base = settings.site_info.get("base_url", "")
    return _rss(f"{settings.site_info.get('name_zh')} · {title}", base, settings.site_info.get("tagline", ""), [_entry(settings, r) for r in rows])


@router.get("/feed.xml")
def feed_selected(request: Request):
    return _items_feed(request, "精选", selected=True, order="fetched")


@router.get("/feed/all.xml")
def feed_all(request: Request):
    return _items_feed(request, "全部", status="scored", order="fetched")


@router.get("/feed/papers.xml")
def feed_papers(request: Request):
    return _items_feed(request, "文献", selected=True, kinds=["paper", "preprint", "guideline"], order="fetched")


@router.get("/feed/news.xml")
def feed_news(request: Request):
    return _items_feed(request, "资讯", selected=True, kinds=["news", "post", "other"], order="fetched")


@router.get("/feed/preference.xml")
def feed_pref(request: Request):
    return _items_feed(request, "重点偏好", status="scored", min_preference=1, order="fetched")


@router.get("/feed/topic/{slug}.xml")
def feed_topic(request: Request, slug: str):
    settings = request.app.state.settings
    name = settings.topic_map.get(slug, {}).get("name", slug)
    return _items_feed(request, name, selected=True, topic=slug, order="fetched")


@router.get("/feed/editions.xml")
def feed_editions(request: Request):
    return _editions_feed(request, None)


@router.get("/feed/editions/{kind}.xml")
def feed_editions_kind(request: Request, kind: str):
    return _editions_feed(request, kind)


def _editions_feed(request: Request, kind: str | None) -> Response:
    settings = request.app.state.settings
    conn = get_conn(settings)
    base = settings.site_info.get("base_url", "")
    entries = []
    for ed in repo.list_editions(conn, kind, limit=40):
        full = repo.get_edition_by_id(conn, ed["id"]) or ed
        entries.append({
            "title": ("【模拟总述】" if ed.get("is_mock") else "") + ed["title"],
            "link": f"{base}/editions/{ed['kind']}/{ed['date_key']}",
            "guid": f"reprohub-edition-{ed['id']}",
            "date": ed["created_at"],
            "category": {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(ed["kind"]),
            "description": full.get("markdown", ed.get("overview") or ""),
        })
    return _rss(f"{settings.site_info.get('name_zh')} · 版次", base, settings.site_info.get("tagline", ""), entries)

"""RSS / Atom 采集（feedparser）。"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

import feedparser

from ..textutil import extract_doi, normalize_doi, strip_html
from .base import BaseFetcher, FetchError, RawItem, http_get


def _entry_date(e) -> datetime | None:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(k)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


class RSSFetcher(BaseFetcher):
    type = "rss"

    def fetch(self, since: datetime) -> list[RawItem]:
        url = (self.params.get("url") or "").strip()
        if not url or url.startswith("/") or "${" in url:
            raise FetchError("RSS 地址未配置（可能依赖未设置的环境变量，如 RSSHUB_BASE）")
        r = http_get(url, self.settings, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
        feed = feedparser.parse(r.content)
        if feed.get("bozo") and not feed.entries:
            raise FetchError(f"RSS 解析失败：{feed.get('bozo_exception')}")
        feed_title = strip_html((feed.feed or {}).get("title") or "") or self.cfg.get("name")
        out: list[RawItem] = []
        for e in feed.entries:
            link = (e.get("link") or "").strip()
            title = strip_html(e.get("title") or "")
            if not link or not title:
                continue
            pub = _entry_date(e)
            if pub and since and pub < since:
                continue
            summary = ""
            if e.get("content"):
                summary = strip_html(e["content"][0].get("value") or "")
            if not summary:
                summary = strip_html(e.get("summary") or e.get("description") or "")
            doi = normalize_doi(e.get("prism_doi") or e.get("dc_identifier") or "") or extract_doi(e.get("id") or "") or extract_doi(link)
            authors = [strip_html(a.get("name") or "") for a in (e.get("authors") or []) if a.get("name")]
            if not authors and e.get("author"):
                authors = [strip_html(e.get("author"))]
            tags = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
            out.append(
                RawItem(
                    url=link,
                    title=title,
                    summary=summary,
                    external_id=e.get("id") or link,
                    doi=doi,
                    authors=authors,
                    journal=feed_title,
                    published_at=pub,
                    extra={"tags": tags[:10]} if tags else {},
                )
            )
        return out

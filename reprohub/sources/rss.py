"""RSS / Atom 采集（feedparser）。

params:
  url: 订阅地址（可含 ${ENV}）
  journal: 显式刊名。订阅标题不是刊名时用（Science First Release → Science），决定期刊等级匹配
  aggregate: true  多刊聚合源（nature.com 主题源等）：条目刊名未知，留空待 PubMed/Crossref 合并时补全
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone

import feedparser

from ..textutil import extract_doi, normalize_doi, strip_html
from .base import BaseFetcher, FetchError, RawItem, http_get

# nature.com 全家族文章页 /articles/<id> 的 DOI 恒为 10.1038/<id>（s41586-…、d41586-…、nature12373、nmeth.2019 皆然）；主题源不给 DOI，靠这个补
_NATURE_ARTICLE_RE = re.compile(r"nature\.com/articles/([a-z0-9][a-z0-9.\-]*[a-z0-9])", re.I)


def _entry_date(e) -> datetime | None:
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(k)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def _split_authors(authors: list[str]) -> list[str]:
    """Cell Press / AAAS 把全部作者塞进一个 dc:creator：'A B, C D, E F'。只在每段都像「名 姓」时才拆，避免拆散 'Smith, J.'。"""
    if len(authors) != 1 or ", " not in authors[0]:
        return authors
    parts = [p.strip() for p in authors[0].split(",") if p.strip()]
    if len(parts) >= 2 and all(" " in p for p in parts):
        return parts
    return authors


def _strip_title_suffix(title: str, journal: str | None) -> str:
    """去掉 'Title | Science' 这类订阅附加的刊名后缀。"""
    if journal:
        title = re.sub(rf"\s*\|\s*{re.escape(journal)}\s*$", "", title, flags=re.I)
    return title.strip()


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
        explicit = (self.params.get("journal") or "").strip() or None
        journal = explicit or (None if self.params.get("aggregate") else feed_title)
        out: list[RawItem] = []
        for e in feed.entries:
            link = (e.get("link") or "").strip()
            title = _strip_title_suffix(strip_html(e.get("title") or ""), explicit or feed_title)
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
            if not doi:
                m = _NATURE_ARTICLE_RE.search(link)
                doi = f"10.1038/{m.group(1).lower()}" if m else None
            authors = _split_authors([strip_html(a.get("name") or "") for a in (e.get("authors") or []) if a.get("name")])
            if not authors and e.get("author"):
                authors = _split_authors([strip_html(e.get("author"))])
            tags = [t.get("term") for t in (e.get("tags") or []) if t.get("term")]
            out.append(
                RawItem(
                    url=link,
                    title=title,
                    summary=summary,
                    external_id=e.get("id") or link,
                    doi=doi,
                    authors=authors,
                    journal=journal,
                    published_at=pub,
                    extra={"tags": tags[:10]} if tags else {},
                )
            )
        return out

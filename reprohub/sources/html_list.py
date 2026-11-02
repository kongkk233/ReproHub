"""通用 HTML 列表页采集：抓取页面中的链接（按 URL 正则过滤）。适合学会/机构新闻列表。"""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..textutil import strip_html
from .base import BaseFetcher, FetchError, RawItem, http_get


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            self._href = d.get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = strip_html(" ".join(self._buf))
            if text:
                self.links.append((self._href, text))
            self._href = None
            self._buf = []


class HTMLListFetcher(BaseFetcher):
    type = "html_list"

    def fetch(self, since: datetime) -> list[RawItem]:
        url = (self.params.get("url") or "").strip()
        if not url:
            raise FetchError("html_list 未配置 url")
        inc = re.compile(self.params["include_url_regex"]) if self.params.get("include_url_regex") else None
        exc = re.compile(self.params["exclude_url_regex"]) if self.params.get("exclude_url_regex") else None
        min_len = int(self.params.get("min_title_len", 15))
        r = http_get(url, self.settings, headers={"Accept": "text/html,*/*"})
        parser = _LinkParser()
        parser.feed(r.text)
        out: list[RawItem] = []
        seen: set[str] = set()
        for href, text in parser.links:
            full = urljoin(url, href)
            if full in seen or full.rstrip("/") == url.rstrip("/"):
                continue
            if inc and not inc.search(full):
                continue
            if exc and exc.search(full):
                continue
            if len(text) < min_len:
                continue
            seen.add(full)
            out.append(RawItem(url=full, title=text, summary="", external_id=full, journal=self.cfg.get("name")))
        return out[: int(self.params.get("max_items", 60))]

"""通用 JSON API 采集：用点路径映射字段。

params:
  url: https://example.org/api/posts
  headers: { Authorization: "Bearer ${TOKEN}" }
  items_path: data.items          # 列表所在路径（留空=根即列表）
  fields:
    title: title
    url: link
    summary: description
    published: created_at
    id: id
    authors: author.name          # 可为字符串或列表
    doi: doi
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..textutil import normalize_doi, parse_date_loose, strip_html
from .base import BaseFetcher, FetchError, RawItem, http_get


def dig(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class JSONAPIFetcher(BaseFetcher):
    type = "jsonapi"

    def fetch(self, since: datetime) -> list[RawItem]:
        url = (self.params.get("url") or "").strip()
        if not url or "${" in url:
            raise FetchError("JSON API 地址未配置")
        r = http_get(url, self.settings, headers=self.params.get("headers") or None)
        data = r.json()
        rows = dig(data, self.params.get("items_path"))
        if not isinstance(rows, list):
            raise FetchError("items_path 未指向列表")
        f = self.params.get("fields") or {}
        out: list[RawItem] = []
        for row in rows:
            title = strip_html(str(dig(row, f.get("title", "title")) or ""))
            link = str(dig(row, f.get("url", "url")) or "")
            if not title or not link:
                continue
            authors = dig(row, f.get("authors")) if f.get("authors") else None
            if isinstance(authors, str):
                authors = [authors]
            elif isinstance(authors, list):
                authors = [str(a.get("name") if isinstance(a, dict) else a) for a in authors]
            else:
                authors = []
            pub_raw = dig(row, f.get("published")) if f.get("published") else None
            out.append(
                RawItem(
                    url=link,
                    title=title,
                    summary=strip_html(str(dig(row, f.get("summary", "summary")) or "")),
                    external_id=str(dig(row, f.get("id", "id")) or link),
                    doi=normalize_doi(str(dig(row, f["doi"]) or "")) if f.get("doi") else None,
                    authors=authors,
                    journal=self.cfg.get("name"),
                    published_at=parse_date_loose(str(pub_raw)) if pub_raw else None,
                )
            )
        return out

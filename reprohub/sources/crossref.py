"""Crossref：按期刊 ISSN 拉取最新上线文章。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from ..textutil import normalize_doi, strip_html
from .base import BaseFetcher, FetchError, RawItem, http_get, log

API = "https://api.crossref.org/journals/{issn}/works"


def _date(item: dict) -> datetime | None:
    for k in ("published-online", "published-print", "created", "issued"):
        parts = ((item.get(k) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y, m, d = (list(parts) + [1, 1])[:3]
            try:
                return datetime(int(y), int(m or 1), int(d or 1), tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


class CrossrefFetcher(BaseFetcher):
    type = "crossref"

    def fetch(self, since: datetime) -> list[RawItem]:
        days = int(self.params.get("days", 7))
        rows = int(self.params.get("rows", 50))
        frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        email = self.settings.env("NCBI_EMAIL", "")
        out: list[RawItem] = []
        for j in self.params.get("journals") or []:
            issn = j.get("issn")
            if not issn:
                continue
            params = {"filter": f"from-created-date:{frm}", "rows": rows, "sort": "created", "order": "desc"}
            if email:
                params["mailto"] = email
            try:
                r = http_get(API.format(issn=issn), self.settings, params=params, timeout=60)
            except FetchError as e:
                log.warning("Crossref %s(%s) 失败：%s", j.get("name"), issn, e)
                continue
            for it in (r.json().get("message") or {}).get("items") or []:
                doi = normalize_doi(it.get("DOI"))
                title = strip_html(" ".join(it.get("title") or [])).strip()
                if not doi or not title:
                    continue
                authors = []
                for a in it.get("author") or []:
                    nm = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
                    if nm:
                        authors.append(nm)
                out.append(
                    RawItem(
                        url=f"https://doi.org/{doi}",
                        title=title,
                        summary=strip_html(it.get("abstract") or ""),
                        external_id=doi,
                        doi=doi,
                        authors=authors,
                        journal=" ".join(it.get("container-title") or []) or j.get("name"),
                        published_at=_date(it),
                        kind="paper",
                        extra={"crossref_type": it.get("type"), "issn": issn},
                    )
                )
            time.sleep(0.8)
        return out

"""bioRxiv / medRxiv 官方 API（按日期区间翻页，可按分类过滤）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..textutil import normalize_doi, strip_html
from .base import BaseFetcher, RawItem, http_get

API = "https://api.biorxiv.org/details"


class BioRxivFetcher(BaseFetcher):
    type = "biorxiv"

    def fetch(self, since: datetime) -> list[RawItem]:
        server = self.params.get("server", "biorxiv")
        days = int(self.params.get("days", 5))
        max_pages = int(self.params.get("max_pages", 5))
        cats = {c.lower() for c in (self.params.get("categories") or [])}
        frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out: list[RawItem] = []
        seen: set[str] = set()
        cursor = 0
        for _ in range(max_pages):
            r = http_get(f"{API}/{server}/{frm}/{to}/{cursor}", self.settings, timeout=60)
            data = r.json()
            coll = data.get("collection") or []
            for p in coll:
                cat = (p.get("category") or "").lower()
                if cats and cat not in cats:
                    continue
                doi = normalize_doi(p.get("doi"))
                if not doi or doi in seen:
                    continue
                seen.add(doi)
                ver = p.get("version") or "1"
                try:
                    pub = datetime.strptime(p.get("date", "")[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    pub = None
                authors = [a.strip() for a in (p.get("authors") or "").split(";") if a.strip()]
                out.append(
                    RawItem(
                        url=f"https://www.{server}.org/content/{doi}v{ver}",
                        title=strip_html(p.get("title") or ""),
                        summary=strip_html(p.get("abstract") or ""),
                        external_id=doi,
                        doi=doi,
                        authors=authors,
                        journal=server,
                        published_at=pub,
                        kind="preprint",
                        extra={"category": p.get("category"), "version": ver, "published_doi": p.get("published") if p.get("published") not in (None, "NA") else None},
                    )
                )
            msgs = data.get("messages") or [{}]
            count = int(msgs[0].get("count") or len(coll) or 0)
            if count < 100:
                break
            cursor += count
        return out

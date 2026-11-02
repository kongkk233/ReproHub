"""Europe PMC REST（含预印本 SRC:PPR）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..textutil import normalize_doi, strip_html
from .base import BaseFetcher, RawItem, http_get

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class EuropePMCFetcher(BaseFetcher):
    type = "europepmc"

    def fetch(self, since: datetime) -> list[RawItem]:
        days = int(self.params.get("days", 7))
        frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        q = " ".join((self.params.get("query") or "").split())
        query = f"({q}) AND (FIRST_PDATE:[{frm} TO {to}])"
        r = http_get(API, self.settings, params={"query": query, "format": "json", "pageSize": int(self.params.get("page_size", 100)), "sort": "P_PDATE_D desc", "resultType": "core"}, timeout=60)
        out: list[RawItem] = []
        for res in (r.json().get("resultList") or {}).get("result") or []:
            doi = normalize_doi(res.get("doi"))
            src = res.get("source") or ""
            rid = res.get("id") or ""
            url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{src}/{rid}"
            authors = [a.strip() for a in (res.get("authorString") or "").rstrip(".").split(",") if a.strip()]
            journal = res.get("journalTitle") or (res.get("bookOrReportDetails") or {}).get("publisher") or ("预印本" if src == "PPR" else None)
            date = res.get("firstPublicationDate") or res.get("electronicPublicationDate")
            pub = None
            if date:
                try:
                    pub = datetime.strptime(date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    pub = None
            out.append(
                RawItem(
                    url=url,
                    title=strip_html(res.get("title") or "").rstrip("."),
                    summary=strip_html(res.get("abstractText") or ""),
                    external_id=f"{src}:{rid}",
                    doi=doi,
                    pmid=res.get("pmid"),
                    authors=authors,
                    journal=journal,
                    published_at=pub,
                    kind="preprint" if src == "PPR" else "paper",
                    extra={"europepmc_source": src, "pub_types": (res.get("pubTypeList") or {}).get("pubType") or []},
                )
            )
        return out

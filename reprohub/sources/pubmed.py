"""PubMed E-utilities：esearch（按日期）+ efetch（XML）。"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..textutil import normalize_doi, strip_html
from .base import BaseFetcher, RawItem, http_get

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _text(el) -> str:
    return strip_html("".join(el.itertext())) if el is not None else ""


def _ymd(el) -> datetime | None:
    if el is None:
        return None
    try:
        return datetime(int(el.findtext("Year")), int(el.findtext("Month")), int(el.findtext("Day")), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _pubdate(article, record=None) -> datetime | None:
    """发表日期：电子出版日期 > PubMed 收录日期 > 期刊当期日期。

    当期 PubDate 是封面日期，经常在未来（提前登记的下一期），只作最后兜底。
    """
    dt = _ymd(article.find(".//ArticleDate"))
    if dt:
        return dt
    if record is not None:
        for status in ("pubmed", "entrez"):
            dt = _ymd(record.find(f".//PubmedData/History/PubMedPubDate[@PubStatus='{status}']"))
            if dt:
                return dt
    pd = article.find(".//Journal/JournalIssue/PubDate")
    if pd is None:
        return None
    y = pd.findtext("Year")
    m = pd.findtext("Month") or "1"
    d = pd.findtext("Day") or "1"
    if not y:
        md = pd.findtext("MedlineDate") or ""
        y = md[:4]
    try:
        mi = int(m) if m.isdigit() else _MONTHS.get(m[:3].lower(), 1)
        return datetime(int(y), mi, int(d), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class PubMedFetcher(BaseFetcher):
    type = "pubmed"

    def _common(self) -> dict:
        p = {"tool": "reprohub", "email": self.settings.env("NCBI_EMAIL", "")}
        key = self.settings.env("NCBI_API_KEY", "")
        if key:
            p["api_key"] = key
        return p

    def _sleep(self):
        time.sleep(0.12 if self.settings.env("NCBI_API_KEY") else 0.36)

    def fetch(self, since: datetime) -> list[RawItem]:
        query = " ".join((self.params.get("query") or "").split())
        reldate = int(self.params.get("reldate_days", 7))
        retmax = int(self.params.get("retmax", 100))
        r = http_get(
            f"{EUTILS}/esearch.fcgi",
            self.settings,
            params={**self._common(), "db": "pubmed", "term": query, "reldate": reldate, "datetype": "edat", "retmax": retmax, "retmode": "json", "sort": "date"},
        )
        ids = (r.json().get("esearchresult") or {}).get("idlist") or []
        items: list[RawItem] = []
        for i in range(0, len(ids), 100):
            self._sleep()
            batch = ids[i : i + 100]
            rr = http_get(f"{EUTILS}/efetch.fcgi", self.settings, params={**self._common(), "db": "pubmed", "id": ",".join(batch), "retmode": "xml"}, timeout=60)
            items.extend(self._parse(rr.content))
        return items

    def _parse(self, xml_bytes: bytes) -> list[RawItem]:
        out: list[RawItem] = []
        root = ET.fromstring(xml_bytes)
        for pa in root.findall(".//PubmedArticle"):
            mc = pa.find("MedlineCitation")
            if mc is None:
                continue
            pmid = (mc.findtext("PMID") or "").strip()
            art = mc.find("Article")
            if art is None or not pmid:
                continue
            title = _text(art.find("ArticleTitle")).rstrip(".")
            abs_parts = []
            for at in art.findall(".//Abstract/AbstractText"):
                label = at.get("Label")
                txt = _text(at)
                abs_parts.append(f"{label}: {txt}" if label else txt)
            summary = " ".join(abs_parts).strip()
            journal = art.findtext("Journal/ISOAbbreviation") or art.findtext("Journal/Title")
            issn = art.findtext("Journal/ISSN") or None
            doi = None
            for el in art.findall("ELocationID"):
                if el.get("EIdType") == "doi":
                    doi = normalize_doi(el.text)
            if not doi:
                for aid in pa.findall(".//ArticleIdList/ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi = normalize_doi(aid.text)
            authors = []
            for au in art.findall("AuthorList/Author"):
                coll = au.findtext("CollectiveName")
                if coll:
                    authors.append(coll.strip())
                    continue
                ln, ini = au.findtext("LastName"), au.findtext("Initials")
                if ln:
                    authors.append(f"{ln} {ini}".strip())
            pub_types = [pt.text for pt in art.findall("PublicationTypeList/PublicationType") if pt.text]
            kind = "paper"
            if any("Guideline" in p or "Consensus" in p for p in pub_types):
                kind = "guideline"
            out.append(
                RawItem(
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    title=title,
                    summary=summary,
                    external_id=pmid,
                    doi=doi,
                    pmid=pmid,
                    authors=authors,
                    journal=journal,
                    published_at=_pubdate(art, pa),
                    kind=kind,
                    extra={"pub_types": pub_types, "issn": issn},
                )
            )
        return out

"""采集器注册表。"""
from __future__ import annotations

from ..settings import Settings
from .base import BaseFetcher, FetchError, RawItem
from .biorxiv import BioRxivFetcher
from .crossref import CrossrefFetcher
from .europepmc import EuropePMCFetcher
from .html_list import HTMLListFetcher
from .jsonapi import JSONAPIFetcher
from .pubmed import PubMedFetcher
from .rss import RSSFetcher

FETCHERS: dict[str, type[BaseFetcher]] = {
    "rss": RSSFetcher,
    "pubmed": PubMedFetcher,
    "europepmc": EuropePMCFetcher,
    "biorxiv": BioRxivFetcher,
    "crossref": CrossrefFetcher,
    "jsonapi": JSONAPIFetcher,
    "html_list": HTMLListFetcher,
}


def get_fetcher(cfg: dict, settings: Settings) -> BaseFetcher | None:
    """type=push 的来源没有采集器（由 API 推送入库）。"""
    cls = FETCHERS.get(cfg.get("type", ""))
    return cls(cfg, settings) if cls else None


__all__ = ["FETCHERS", "BaseFetcher", "FetchError", "RawItem", "get_fetcher"]

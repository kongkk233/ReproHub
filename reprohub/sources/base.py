"""采集器基类与 HTTP 工具。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .. import __version__
from ..settings import Settings

log = logging.getLogger("reprohub.sources")


@dataclass
class RawItem:
    url: str
    title: str
    summary: str = ""
    external_id: str | None = None
    doi: str | None = None
    pmid: str | None = None
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    published_at: datetime | None = None
    kind: str | None = None
    lang: str | None = None
    extra: dict = field(default_factory=dict)


class FetchError(Exception):
    pass


def user_agent(settings: Settings) -> str:
    email = settings.env("NCBI_EMAIL", "")
    base = f"ReproHub/{__version__} (+https://github.com/reprohub; literature aggregator"
    return base + (f"; mailto:{email})" if email else ")")


def http_get(url: str, settings: Settings, *, params: dict | None = None, headers: dict | None = None, timeout: float = 40.0, retries: int = 2) -> httpx.Response:
    h = {"User-Agent": user_agent(settings), "Accept": "*/*"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                r = client.get(url, params=params, headers=h)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code} {url}")
            return r
        except httpx.HTTPError as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"请求失败 {url}: {last}")


class BaseFetcher:
    type = "base"

    def __init__(self, cfg: dict, settings: Settings):
        self.cfg = cfg
        self.key = cfg.get("key", "?")
        self.params = cfg.get("params") or {}
        self.settings = settings

    def fetch(self, since: datetime) -> list[RawItem]:  # pragma: no cover - interface
        raise NotImplementedError

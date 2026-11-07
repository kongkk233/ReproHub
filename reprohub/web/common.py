"""Web 公共工具：模板环境、鉴权、格式化。"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path

import markdown as md_lib
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import __version__
from ..db import get_conn
from ..journals import JournalRegistry
from ..llm import llm_status
from ..settings import Settings, get_settings
from ..textutil import parse_iso, truncate

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "rh_auth"

KIND_LABEL = {"paper": "文献", "preprint": "预印本", "news": "资讯", "guideline": "指南", "post": "帖子", "other": "其他"}
TIER_LABEL = {1: "一手", 2: "机构", 3: "媒体"}
EDITION_LABEL = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
READ_LABEL = {"unread": "未读", "reading": "在读", "read": "已读"}


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    html = md_lib.markdown(text, extensions=["extra", "sane_lists"], output_format="html")
    return Markup(html)


def fmt_dt(iso: str | None, tz, fmt: str = "%Y-%m-%d") -> str:
    dt = parse_iso(iso)
    return dt.astimezone(tz).strftime(fmt) if dt else ""


def rel_date(iso: str | None, tz) -> str:
    dt = parse_iso(iso)
    if not dt:
        return "未知"
    d = dt.astimezone(tz).date()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    delta = (today - d).days
    wd = "一二三四五六日"[d.weekday()]
    if delta == 0:
        return f"今天 {d.month}月{d.day}日 周{wd}"
    if delta == 1:
        return f"昨天 {d.month}月{d.day}日 周{wd}"
    if d.year == today.year:
        return f"{d.month}月{d.day}日 周{wd}"
    return f"{d.year}年{d.month}月{d.day}日"


COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _sign(settings: Settings, payload: str) -> str:
    # 密钥同时绑定口令：改口令即作废所有旧会话
    key = f"{settings.secret_key}:{settings.env('ADMIN_PASSWORD')}".encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(settings: Settings, now: float | None = None) -> str:
    exp = int((now or time.time()) + COOKIE_MAX_AGE)
    return f"{exp}.{_sign(settings, str(exp))}"


def verify_session_token(settings: Settings, token: str, now: float | None = None) -> bool:
    exp, _, sig = (token or "").partition(".")
    if not exp.isdigit() or not sig:
        return False
    if int(exp) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(settings, exp))


def is_loopback(request: Request) -> bool:
    """真正的本机直连。经反向代理转发的请求（带代理头）一律不算本机。"""
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def is_owner(request: Request, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    pw = settings.env("ADMIN_PASSWORD")
    if not pw:
        return is_loopback(request)
    header = request.headers.get("x-admin-token", "")
    if header and hmac.compare_digest(header, pw):
        return True
    return verify_session_token(settings, request.cookies.get(COOKIE, ""))


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "") or request.url.scheme
    return proto.split(",")[0].strip() == "https"


def require_owner(request: Request) -> None:
    if not is_owner(request):
        raise HTTPException(status_code=401, detail="需要管理员口令（ADMIN_PASSWORD）或从本机访问")


def login_cookie_value(settings: Settings) -> str:
    return make_session_token(settings)


def build_templates(settings: Settings) -> Jinja2Templates:
    t = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = t.env
    env.globals.update(
        site=settings.site_info,
        version=__version__,
        topics=settings.topics,
        topic_map=settings.topic_map,
        topic_groups=settings.topic_groups,
        KIND_LABEL=KIND_LABEL,
        TIER_LABEL=TIER_LABEL,
        EDITION_LABEL=EDITION_LABEL,
        READ_LABEL=READ_LABEL,
        md=render_markdown,
        truncate=truncate,
        fmt_dt=lambda iso, fmt="%Y-%m-%d": fmt_dt(iso, settings.tz, fmt),
        rel_date=lambda iso: rel_date(iso, settings.tz),
        topic_name=lambda slug: settings.topic_map.get(slug or "", {}).get("name", slug or "未分类"),
        llm_status=lambda: llm_status(settings, get_conn(settings)),
        tier_label=lambda t: JournalRegistry(settings).tier_info(t).get("label", ""),
        TOP_TIERS=JournalRegistry(settings).top_tiers,
        IF_SOURCE=(settings.journals.get("meta") or {}).get("if_source", ""),
    )
    return t


def ctx(request: Request, **kw) -> dict:
    d = {"request": request, "owner": is_owner(request), "path": request.url.path, "query": dict(request.query_params)}
    d.update(kw)
    return d


def qp_int(request: Request, key: str, default: int, lo: int = 1, hi: int = 10_000) -> int:
    try:
        v = int(request.query_params.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))

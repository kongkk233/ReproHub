"""FastAPI 应用工厂。uvicorn reprohub.web.app:create_app --factory"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__, repo
from ..db import get_conn
from ..settings import Settings, get_settings
from . import api, feeds, views
from .common import STATIC_DIR, build_templates

log = logging.getLogger("reprohub.web")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    info = settings.site_info
    app = FastAPI(
        title=f"{info.get('name', 'ReproHub')} API",
        version=__version__,
        description=info.get("description", ""),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.templates = build_templates(settings)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    conn = get_conn(settings)
    repo.sync_sources(conn, settings)
    app.include_router(views.router)
    app.include_router(api.router, prefix="/api/v1")
    app.include_router(feeds.router)

    @app.on_event("startup")
    def _startup() -> None:
        if str(settings.env("REPROHUB_SCHEDULER", "")).lower() in ("1", "true", "yes"):
            from ..scheduler import start_background

            start_background(settings)
            log.info("后台调度已启动")

    return app

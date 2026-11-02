"""进程内调度：定时采集/处理、按版次时间生成日报周报月报并推送。"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import repo
from .db import get_conn
from .llm import LLM
from .pipeline.cluster import cluster_recent
from .pipeline.editions import build_daily, build_monthly, build_weekly
from .pipeline.runner import run_pipeline
from .push.dispatcher import push_edition
from .settings import Settings

log = logging.getLogger("reprohub.scheduler")


class Scheduler:
    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.site.get("scheduler") or {}
        self.collect_every = int(cfg.get("collect_every_minutes", 60))
        self.cluster_every = int(cfg.get("cluster_every_minutes", 120))
        self.last_collect: datetime | None = None
        self.last_cluster: datetime | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def _due_edition(self, kind: str, now: datetime) -> str | None:
        """返回应生成的版次 key（本地时间已到且未生成），否则 None。
        日报在 08:00 定稿「昨天」；周报在周一定稿「上一 ISO 周」；月报在 1 日定稿「上个月」。"""
        cfg = (self.settings.site.get("editions") or {}).get(kind) or {}
        hh, mm = [int(x) for x in str(cfg.get("time", "08:00")).split(":")]
        if (now.hour, now.minute) < (hh, mm):
            return None
        if kind == "daily":
            key = (now - timedelta(days=1)).date().isoformat()
        elif kind == "weekly":
            if now.isoweekday() != int(cfg.get("weekday", 1)):
                return None
            iso = (now - timedelta(days=1)).isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:
            if now.day != int(cfg.get("day", 1)):
                return None
            key = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        conn = get_conn(self.settings)
        if repo.kv_get(conn, f"edition_built:{kind}:{key}"):
            return None
        return key

    def tick(self) -> None:
        now = self.settings.now_local()
        conn = get_conn(self.settings)
        try:
            if self.last_collect is None or now - self.last_collect >= timedelta(minutes=self.collect_every):
                self.last_collect = now
                run_pipeline(self.settings, conn, do_cluster=False, do_daily=False, do_push=True, logger=log.info)
            if self.last_cluster is None or now - self.last_cluster >= timedelta(minutes=self.cluster_every):
                self.last_cluster = now
                cluster_recent(self.settings, conn, LLM(self.settings, conn), logger=log.info)
            llm = LLM(self.settings, conn)
            for kind, builder in (("daily", build_daily), ("weekly", build_weekly), ("monthly", build_monthly)):
                key = self._due_edition(kind, now)
                if not key:
                    continue
                ed = builder(self.settings, conn, llm, key, logger=log.info)
                push_edition(self.settings, conn, ed, logger=log.info)
                repo.kv_set(conn, f"edition_built:{kind}:{key}", now.isoformat())
        except Exception:  # noqa: BLE001
            log.error("调度 tick 失败：%s", traceback.format_exc())

    def run_forever(self, interval_seconds: int = 30) -> None:
        log.info("调度启动：采集每 %d 分钟，聚类每 %d 分钟", self.collect_every, self.cluster_every)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(interval_seconds)

    def stop(self) -> None:
        self._stop.set()


_thread: threading.Thread | None = None


def start_background(settings: Settings) -> threading.Thread:
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    sched = Scheduler(settings)
    _thread = threading.Thread(target=sched.run_forever, name="reprohub-scheduler", daemon=True)
    _thread.start()
    return _thread

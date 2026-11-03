"""流水线编排：采集 → 预筛/评分 → 解读 → 聚类 → 日报 → 推送。"""
from __future__ import annotations

import logging
import sqlite3
import traceback
from typing import Callable

from .. import repo
from ..llm import LLM, llm_status
from ..settings import Settings
from .cluster import cluster_recent
from .collect import collect
from .editions import build_daily
from .interpret import auto_interpret
from .score import process_new

log = logging.getLogger("reprohub.runner")
Logger = Callable[[str], None]


def mock_banner(settings: Settings, conn=None) -> str | None:
    st = llm_status(settings, conn)
    if st.get("mode") == "mock":
        return f"⚠ LLM 模拟模式（{st.get('reason')}）：评分 / 解读 / 周报月报总述均为规则占位内容，未调用任何模型；界面与 API 会标注 is_mock。"
    if st.get("mode") == "error":
        return f"✗ LLM 配置错误：{st.get('reason')}"
    return None


def run_pipeline(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    do_collect: bool = True,
    do_process: bool = True,
    do_fulltext: bool = True,
    do_interpret: bool = True,
    do_cluster: bool = True,
    do_daily: bool = True,
    do_push: bool = True,
    force: bool = False,
    source_keys: list[str] | None = None,
    logger: Logger = log.info,
) -> dict:
    run_id = repo.start_run(conn, "pipeline")
    lines: list[str] = []

    def _log(msg: str) -> None:
        lines.append(msg)
        logger(msg)

    banner = mock_banner(settings, conn)
    if banner:
        _log(banner)
    llm = LLM(settings, conn)
    st = llm_status(settings, conn)
    _log(f"LLM：{st.get('mode')} {st.get('key')}/{st.get('model')} 强度={st.get('effort')}")
    summary: dict = {"llm": st.get("mode"), "llm_provider": st.get("key"), "llm_model": st.get("model"), "llm_effort": st.get("effort")}
    status = "ok"
    try:
        if do_collect:
            summary["collect"] = collect(settings, conn, source_keys=source_keys, force=force, logger=_log)
        if do_process:
            summary["process"] = process_new(settings, conn, llm, limit=int(settings.env("REPROHUB_PROCESS_LIMIT", "1000")), retry_errors=True, logger=_log)
        if do_fulltext:
            from ..fulltext import fetch_fulltexts  # 延迟导入，避免与 sources 循环

            summary["fulltext"] = fetch_fulltexts(settings, conn, logger=_log)
        if do_interpret:
            summary["interpret"] = auto_interpret(settings, conn, llm, logger=_log)
        if do_cluster:
            summary["cluster"] = cluster_recent(settings, conn, llm, logger=_log)
        if do_daily:
            ed = build_daily(settings, conn, llm, logger=_log)
            summary["daily"] = {"id": ed["id"], "date_key": ed["date_key"], "items": ed["item_count"]}
        if do_push:
            from ..push.dispatcher import push_edition, push_items  # 延迟导入避免循环

            ids = (summary.get("process") or {}).get("selected_ids") or []
            summary["push_items"] = push_items(settings, conn, ids, logger=_log) if ids else {"sent": 0}
            if do_daily and summary.get("daily"):
                ed = repo.get_edition_by_id(conn, summary["daily"]["id"])
                summary["push_edition"] = push_edition(settings, conn, ed, logger=_log) if ed else {"sent": 0}
        days = int((settings.selection.get("housekeeping") or {}).get("purge_rejected_after_days", 7))
        summary["purged"] = repo.purge_rejected(conn, days)
    except Exception as e:  # noqa: BLE001
        status = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        _log(traceback.format_exc())
    finally:
        repo.finish_run(conn, run_id, status, summary, "\n".join(lines))
    return summary

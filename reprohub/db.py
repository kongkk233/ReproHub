"""SQLite 连接与迁移。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .settings import Settings, get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_local = threading.local()


def connect(settings: Settings | None = None, path: str | Path | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    db_path = Path(path) if path else settings.db_path
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    # 轻量版本化迁移：新增列用 try/except 兼容旧库
    for stmt in (
        "ALTER TABLE items ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE stories ADD COLUMN source_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE items ADD COLUMN llm_effort TEXT",
        "ALTER TABLE interpretations ADD COLUMN effort TEXT",
        "ALTER TABLE editions ADD COLUMN effort TEXT",
        "ALTER TABLE llm_calls ADD COLUMN effort TEXT",
        "ALTER TABLE llm_calls ADD COLUMN reasoning_tokens INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN cached INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE items ADD COLUMN journal_key TEXT",
        "ALTER TABLE items ADD COLUMN journal_tier TEXT",
        "ALTER TABLE items ADD COLUMN journal_if REAL",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # 依赖新增列的索引放在迁移之后
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_journal_tier ON items(journal_tier)")


def get_conn(settings: Settings | None = None) -> sqlite3.Connection:
    """每线程一个连接（Web 与调度线程共用进程）。"""
    settings = settings or get_settings()
    conn = getattr(_local, "conn", None)
    key = str(settings.db_path)
    if conn is None or getattr(_local, "key", None) != key:
        conn = connect(settings)
        migrate(conn)
        _local.conn = conn
        _local.key = key
    return conn


def fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM items_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False

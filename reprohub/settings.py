"""配置加载：.env + config/*.yaml，支持 ${ENV_VAR} 展开。"""
from __future__ import annotations

import functools
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
_ENV_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取：不覆盖已存在的环境变量。"""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def expand_env(obj: Any) -> Any:
    """递归把字符串中的 ${VAR} 替换为环境变量值（缺失则为空串）。"""
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [expand_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    return obj


class Settings:
    def __init__(self, config_dir: Path | str | None = None, data_dir: Path | str | None = None):
        load_dotenv()
        self.config_dir = Path(config_dir or os.environ.get("REPROHUB_CONFIG_DIR") or ROOT / "config")
        self.data_dir = Path(data_dir or os.environ.get("REPROHUB_DATA_DIR") or ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "reprohub.db"

    # ---- yaml -------------------------------------------------------------
    def load(self, name: str) -> dict:
        p = self.config_dir / name
        if not p.exists():
            return {}
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return expand_env(data)

    @functools.cached_property
    def site(self) -> dict:
        return self.load("site.yaml")

    @functools.cached_property
    def taxonomy(self) -> dict:
        return self.load("taxonomy.yaml")

    @functools.cached_property
    def preferences(self) -> dict:
        return self.load("preferences.yaml")

    @functools.cached_property
    def sources(self) -> list[dict]:
        return self.load("sources.yaml").get("sources", [])

    @functools.cached_property
    def selection(self) -> dict:
        return self.load("selection.yaml")

    @functools.cached_property
    def push(self) -> dict:
        return self.load("push.yaml")

    @functools.cached_property
    def models(self) -> dict:
        return self.load("models.yaml")

    def prompt(self, task: str) -> str:
        p = self.config_dir / "prompts" / f"{task}.md"
        return p.read_text(encoding="utf-8")

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def env(key: str, default: str = "") -> str:
        return os.environ.get(key, default) or default

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.site.get("site", {}).get("timezone", "Asia/Shanghai"))
        except Exception:
            return ZoneInfo("UTC")

    def now_local(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.tz)

    @property
    def site_info(self) -> dict:
        return self.site.get("site", {})

    @property
    def topics(self) -> list[dict]:
        return self.taxonomy.get("topics", [])

    @property
    def topic_map(self) -> dict[str, dict]:
        return {t["slug"]: t for t in self.topics}

    @property
    def topic_groups(self) -> list[dict]:
        return self.taxonomy.get("groups", [])

    def source_cfg(self, key: str) -> dict | None:
        for s in self.sources:
            if s.get("key") == key:
                return s
        return None


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings(config_dir=None, data_dir=None) -> Settings:
    """测试或 CLI 切换目录时使用。"""
    global _settings
    _settings = Settings(config_dir=config_dir, data_dir=data_dir)
    return _settings

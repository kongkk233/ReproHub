"""测试夹具：临时数据目录 + 强制模拟模式，不触网。"""
from __future__ import annotations

import os

import pytest

from reprohub import repo
from reprohub.db import connect, migrate
from reprohub.settings import reset_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("REPROHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPROHUB_NO_DOTENV", "1")
    for k in ("DEEPSEEK_API_KEY", "OPUS_API_KEY", "X666_API_KEY", "QWEN_API_KEY", "ZHIPU_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY", "LLM_MODEL", "LLM_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_ALLOW_MOCK_FALLBACK", "true")
    s = reset_settings(data_dir=tmp_path / "data")
    yield s
    reset_settings()


@pytest.fixture()
def conn(settings):
    c = connect(settings)
    migrate(c)
    repo.sync_sources(c, settings)
    yield c
    c.close()


@pytest.fixture()
def seeded(settings, conn):
    from reprohub.demo import seed

    n = seed(settings, conn)
    assert n >= 10
    return conn


@pytest.fixture()
def no_network(monkeypatch):
    """任何 httpx 请求都应失败，保证测试不触网。"""
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx.Client, "send", _boom)
    yield


os.environ.setdefault("PYTHONWARNINGS", "ignore")

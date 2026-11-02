import json

import httpx
import pytest

from reprohub import repo
from reprohub.llm import LLM, LLMError, LLMResponse, build_provider, llm_status
from reprohub.llm.mock import MockProvider
from reprohub.llm.openai_compat import OpenAICompatProvider


def test_missing_key_falls_back_to_labelled_mock(settings):
    st = llm_status(settings)
    assert st["mode"] == "mock"
    assert "DEEPSEEK_API_KEY" in st["reason"]
    assert st["is_mock"] is True


def test_missing_key_can_fail_hard(settings, monkeypatch):
    monkeypatch.setenv("LLM_ALLOW_MOCK_FALLBACK", "false")
    with pytest.raises(LLMError):
        build_provider(settings, "deepseek")


def test_explicit_mock_provider(settings, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert isinstance(build_provider(settings, "mock"), MockProvider)


def test_live_provider_when_key_present(settings, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    p = build_provider(settings, "deepseek")
    assert isinstance(p, OpenAICompatProvider)
    assert p.is_mock is False and p.model == "deepseek-chat" and p.base_url == "https://api.deepseek.com"


def test_per_step_model_override(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    llm = LLM(settings)
    assert llm.provider_for("score").model == "deepseek-chat"


def test_lenient_json_parsing():
    r = LLMResponse(text='```json\n{"a": 1}\n```', provider="x", model="y")
    assert r.json() == {"a": 1}
    r = LLMResponse(text='Sure! {"a": 2} hope it helps', provider="x", model="y")
    assert r.json() == {"a": 2}
    with pytest.raises(LLMError):
        LLMResponse(text="no json here", provider="x", model="y").json()


def test_mock_run_logs_call_with_is_mock(settings, conn):
    llm = LLM(settings, conn)
    resp = llm.run("score", {"source_name": "s", "tier": 1, "kind": "paper", "title": "t", "authors": "", "journal": "", "published": "", "summary": "x" * 500,
                             "matched_topics": "", "preference_labels": "", "topic_slugs": ""},
                   context={"topic_score": 6, "tier": 1, "kind": "paper", "summary": "x" * 500, "preference_score": 0}, ref_type="item", ref_id=1)
    assert resp.is_mock
    data = resp.json()
    assert 30 <= data["score"] <= 66
    assert data["title_zh"] is None  # 模拟模式不伪造翻译
    assert "模拟" in data["recommend_reason"]
    st = repo.llm_stats(conn)
    assert st["total"] == 1 and st["by_provider"][0]["is_mock"] == 1


def test_openai_compat_uses_transport_and_reports_live(monkeypatch):
    calls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["url"] = str(request.url)
        calls["auth"] = request.headers.get("authorization")
        body = json.loads(request.content)
        calls["json_mode"] = body.get("response_format")
        return httpx.Response(200, json={"model": "deepseek-chat", "choices": [{"message": {"content": '{"score": 77}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class PatchedClient(real_client):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", PatchedClient)
    p = OpenAICompatProvider("deepseek", "DeepSeek", "https://api.deepseek.com", "sk-abc", "deepseek-chat")
    resp = p.complete([{"role": "user", "content": "hi"}], json_mode=True)
    assert resp.is_mock is False and resp.json()["score"] == 77
    assert resp.prompt_tokens == 10 and calls["auth"] == "Bearer sk-abc"
    assert calls["url"].endswith("/chat/completions") and calls["json_mode"] == {"type": "json_object"}


def test_openai_compat_4xx_raises_without_retry(monkeypatch):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(401, text="bad key")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class PatchedClient(real_client):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", PatchedClient)
    p = OpenAICompatProvider("deepseek", "DeepSeek", "https://api.deepseek.com", "sk-abc", "deepseek-chat", max_retries=3)
    with pytest.raises(LLMError):
        p.complete([{"role": "user", "content": "hi"}])
    assert n["calls"] == 1

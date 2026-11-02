"""提供商 / 模型 / 思考强度的解析优先级与映射。"""
import httpx
import json

from reprohub.llm import LLM, clear_overrides, llm_status, parse_model_ref, resolve_choice, set_overrides, set_process_overrides, test_llm
from reprohub.llm.openai_compat import OpenAICompatProvider, reasoning_body

KEYS = {"deepseek", "opus", "x666", "qwen"}


def test_parse_model_ref_shorthand():
    assert parse_model_ref("x666/grok-4.7", KEYS) == ("x666", "grok-4.7")
    assert parse_model_ref("grok-4.7", KEYS) == (None, "grok-4.7")
    assert parse_model_ref("deepseek-ai/DeepSeek-V3", KEYS) == (None, "deepseek-ai/DeepSeek-V3")  # 前缀不是提供商
    assert parse_model_ref("", KEYS) == (None, None)


def test_defaults_and_step_effort(settings):
    c = resolve_choice(settings, None)
    assert c.provider == "deepseek" and c.model is None and c.effort == "low"
    assert resolve_choice(settings, "prescreen").effort == "off"      # YAML 裸 off 已处理
    assert resolve_choice(settings, "interpret").effort == "medium"


def test_env_then_step_then_override_then_explicit(settings, conn, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "opus")
    monkeypatch.setenv("LLM_EFFORT", "high")
    c = resolve_choice(settings, "score")
    assert (c.provider, c.effort, c.sources["provider"], c.sources["effort"]) == ("opus", "low", "env", "step")  # step 的 effort 优先于 env
    monkeypatch.setenv("LLM_MODEL", "x666/grok-4.6")
    c = resolve_choice(settings, "score")
    assert (c.provider, c.model) == ("x666", "grok-4.6")  # 同层里 LLM_MODEL 的前缀比 LLM_PROVIDER 更具体，优先
    set_overrides(conn, provider="deepseek", effort="high")
    c = resolve_choice(settings, "score", overrides={"provider": "deepseek", "effort": "high"})
    assert (c.provider, c.model, c.effort, c.sources["effort"]) == ("deepseek", None, "high", "override")  # 模型 grok 不属于 deepseek，被丢弃
    c = resolve_choice(settings, "score", overrides={"provider": "deepseek"}, explicit={"model": "opus/gemini-3-flash", "effort": "off"})
    assert (c.provider, c.model, c.effort) == ("opus", "gemini-3-flash", "off")
    clear_overrides(conn)


def test_process_overrides_merge_over_kv(settings, conn):
    set_overrides(conn, provider="x666")
    set_process_overrides(effort="high")
    try:
        llm = LLM(settings, conn)
        c = llm.choice_for("score")
        assert (c.provider, c.effort) == ("x666", "high")
    finally:
        set_process_overrides(effort=None)
        clear_overrides(conn)


def test_reasoning_body_mapping():
    assert reasoning_body({"style": "openai"}, "high") == {"reasoning_effort": "high"}
    assert reasoning_body({"style": "openai"}, "off") == {}
    assert reasoning_body({"style": "openai", "off": {"thinking": {"type": "disabled"}}}, "off") == {"thinking": {"type": "disabled"}}
    assert reasoning_body({"style": "openai", False: {"thinking": {"type": "disabled"}}}, "off") == {"thinking": {"type": "disabled"}}  # YAML 裸 off
    assert reasoning_body({"style": "none"}, "high") == {}
    lv = {"style": "custom", "levels": {"low": {"enable_thinking": True, "thinking_budget": 1024}}}
    assert reasoning_body(lv, "low") == {"enable_thinking": True, "thinking_budget": 1024}
    assert reasoning_body(lv, "medium") == {}


def test_provider_body_and_reasoning_tokens(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 42}}})

    real = httpx.Client

    class Patched(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", Patched)
    p = OpenAICompatProvider("deepseek", "DeepSeek", "https://api.deepseek.com", "k", "deepseek-flash", reasoning={"style": "openai", "off": {"thinking": {"type": "disabled"}}})
    r = p.complete([{"role": "user", "content": "x"}], json_mode=True, effort="medium")
    assert seen["body"]["reasoning_effort"] == "medium" and r.reasoning_tokens == 42 and r.effort == "medium"
    p.complete([{"role": "user", "content": "x"}], effort="off")
    assert "reasoning_effort" not in seen["body"] and seen["body"]["thinking"] == {"type": "disabled"}


def test_status_lists_providers_and_effort(settings, conn, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    st = llm_status(settings, conn)
    assert st["mode"] == "live" and st["effort"] == "low"
    keys = {p["key"] for p in st["providers"]}
    assert {"deepseek", "opus", "x666", "mock"} <= keys
    assert next(p for p in st["providers"] if p["key"] == "opus")["configured"] is False
    assert st["steps"]["prescreen"]["effort"] == "off"


def test_test_llm_in_mock_mode_is_labelled(settings, conn):
    r = test_llm(settings, conn, provider="mock")
    assert r["ok"] and r["is_mock"] and r["effort"] is None

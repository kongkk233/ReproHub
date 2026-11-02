"""LLM 门面：按步骤选择提供商、渲染提示词、记录调用；缺 key 时显式进入模拟模式。"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .. import repo
from ..settings import Settings, get_settings
from .base import LLMError, LLMProvider, LLMResponse
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

log = logging.getLogger("reprohub.llm")

SYSTEM_PROMPT = (
    "你是生殖医学、辅助生殖与胚胎多组学领域的专业编辑与研究助理。"
    "严格依据给定材料作答，不编造数据；要求输出 JSON 时只输出一个合法 JSON 对象，不加任何解释或 Markdown 围栏。"
)

_warned: set[str] = set()


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def resolve_provider_key(settings: Settings, step: str | None = None) -> tuple[str, str | None, dict]:
    """返回 (provider_key, model_override, step_cfg)。"""
    steps = (settings.models.get("steps") or {})
    step_cfg = dict(steps.get(step or "", {}) or {})
    key = (step_cfg.get("provider") or "").strip() or settings.env("LLM_PROVIDER") or settings.site.get("llm", {}).get("default_provider", "deepseek")
    model = (step_cfg.get("model") or "").strip() or None
    return key.lower(), model, step_cfg


def build_provider(settings: Settings, key: str, model_override: str | None = None) -> LLMProvider:
    if key == "mock":
        return MockProvider("LLM_PROVIDER=mock")
    llm_cfg = settings.site.get("llm", {})
    providers = llm_cfg.get("providers", {})
    cfg = providers.get(key)
    allow_fallback = _truthy(settings.env("LLM_ALLOW_MOCK_FALLBACK", "true"))
    if not cfg:
        reason = f"未知提供商 {key!r}（可选：{', '.join(providers)}, mock）"
        if allow_fallback:
            _warn(reason)
            return MockProvider(reason)
        raise LLMError(reason)
    base_url = cfg.get("base_url") or settings.env(cfg.get("base_url_env", ""))
    api_key_env = cfg.get("api_key_env") or ""
    api_key = settings.env(api_key_env) if api_key_env else ""
    model = model_override or cfg.get("model") or settings.env(cfg.get("model_env", ""))
    missing = []
    if not base_url:
        missing.append(cfg.get("base_url_env") or "base_url")
    if not model:
        missing.append(cfg.get("model_env") or "model")
    if not api_key and not cfg.get("no_key_required"):
        missing.append(api_key_env or "api_key")
    if missing:
        reason = f"提供商 {key} 缺少配置：{', '.join(missing)}"
        if allow_fallback:
            _warn(reason)
            return MockProvider(reason)
        raise LLMError(reason + "（LLM_ALLOW_MOCK_FALLBACK=false，拒绝进入模拟模式）")
    return OpenAICompatProvider(
        key=key,
        label=cfg.get("label", key),
        base_url=base_url,
        api_key=api_key,
        model=model,
        supports_json_mode=bool(cfg.get("supports_json_mode", True)),
        timeout=float(llm_cfg.get("request_timeout_seconds", 90)),
        max_retries=int(llm_cfg.get("max_retries", 3)),
    )


def _warn(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        log.warning("进入模拟模式：%s。评分/解读/总述将是规则占位内容，并在界面与 API 中标注 is_mock=true。", reason)


def llm_status(settings: Settings | None = None) -> dict:
    """供 CLI 横幅、页脚、/api/v1/status 使用。"""
    settings = settings or get_settings()
    key, model, _ = resolve_provider_key(settings)
    try:
        p = build_provider(settings, key, model)
    except LLMError as e:
        return {"mode": "error", "provider": key, "model": model, "reason": str(e), "is_mock": False}
    d = p.describe()
    d["mode"] = "mock" if p.is_mock else "live"
    d["requested_provider"] = key
    steps = {}
    for step in (settings.models.get("steps") or {}):
        sk, sm, _ = resolve_provider_key(settings, step)
        try:
            sp = build_provider(settings, sk, sm)
            steps[step] = {"provider": sp.key, "model": sp.model, "is_mock": sp.is_mock}
        except LLMError as e:
            steps[step] = {"provider": sk, "error": str(e)}
    d["steps"] = steps
    return d


class LLM:
    """按步骤取提供商、渲染提示词并调用。所有调用写入 llm_calls。"""

    def __init__(self, settings: Settings | None = None, conn: sqlite3.Connection | None = None):
        self.settings = settings or get_settings()
        self.conn = conn
        self._providers: dict[str, LLMProvider] = {}
        self._env = Environment(loader=FileSystemLoader(str(self.settings.config_dir / "prompts")), autoescape=False, trim_blocks=True, lstrip_blocks=True)

    def provider_for(self, step: str) -> LLMProvider:
        key, model, _ = resolve_provider_key(self.settings, step)
        cache_key = f"{key}:{model or ''}"
        if cache_key not in self._providers:
            self._providers[cache_key] = build_provider(self.settings, key, model)
        return self._providers[cache_key]

    @property
    def any_live(self) -> bool:
        return any(not self.provider_for(s).is_mock for s in ("prescreen", "score", "interpret", "cluster", "digest"))

    def render(self, task: str, variables: dict) -> str:
        return self._env.get_template(f"{task}.md").render(**variables)

    def run(
        self,
        task: str,
        variables: dict,
        *,
        context: dict | None = None,
        json_mode: bool = True,
        ref_type: str | None = None,
        ref_id: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        provider = self.provider_for(task)
        _, _, step_cfg = resolve_provider_key(self.settings, task)
        prompt = self.render(task, variables)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        temp = temperature if temperature is not None else float(step_cfg.get("temperature", 0.2))
        max_tokens = step_cfg.get("max_tokens")
        try:
            resp = provider.complete(messages, json_mode=json_mode, temperature=temp, max_tokens=max_tokens, task=task, context=context)
        except LLMError as e:
            self._log(task, provider, None, ok=False, error=str(e), ref_type=ref_type, ref_id=ref_id)
            raise
        self._log(task, provider, resp, ok=True, error=None, ref_type=ref_type, ref_id=ref_id)
        return resp

    def _log(self, task: str, provider: LLMProvider, resp: LLMResponse | None, *, ok: bool, error: str | None, ref_type, ref_id) -> None:
        if self.conn is None:
            return
        try:
            repo.log_llm_call(
                self.conn,
                task=task,
                provider=provider.key,
                model=(resp.model if resp else provider.model),
                is_mock=provider.is_mock,
                prompt_tokens=resp.prompt_tokens if resp else None,
                completion_tokens=resp.completion_tokens if resp else None,
                latency_ms=resp.latency_ms if resp else None,
                ok=ok,
                error=error,
                ref_type=ref_type,
                ref_id=ref_id,
            )
        except sqlite3.Error:
            pass


__all__ = ["LLM", "LLMError", "LLMProvider", "LLMResponse", "MockProvider", "OpenAICompatProvider", "build_provider", "llm_status", "resolve_provider_key", "SYSTEM_PROMPT"]

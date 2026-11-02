"""LLM 门面：解析「提供商 / 模型 / 思考强度」、渲染提示词、调用并记录；缺 key 时显式进入模拟模式。

解析优先级（高 → 低）：
  1. 调用点显式参数（LLM.run(provider=, model=, effort=)）
  2. 运行时覆盖（`reprohub llm set` / 后台表单 / POST /api/v1/llm/override，存于 kv 表）
  3. 步骤配置 config/models.yaml steps.<step>
  4. 环境变量 LLM_PROVIDER / LLM_MODEL / LLM_EFFORT
  5. site.yaml llm.default_provider / default_effort 与提供商默认模型
model 一律支持 "provider/model" 简写。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .. import repo
from ..settings import Settings, get_settings
from .base import EFFORT_LEVELS, LLMError, LLMProvider, LLMResponse
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider, reasoning_body

log = logging.getLogger("reprohub.llm")

SYSTEM_PROMPT = (
    "你是生殖医学、辅助生殖与胚胎多组学领域的专业编辑与研究助理。"
    "严格依据给定材料作答，不编造数据；要求输出 JSON 时只输出一个合法 JSON 对象，不加任何解释或 Markdown 围栏。"
)
OVERRIDE_KEY = "llm_override"
STEPS = ("prescreen", "score", "interpret", "cluster", "digest")

_warned: set[str] = set()


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ============================================================ 解析
@dataclass
class LLMChoice:
    provider: str
    model: str | None
    effort: str
    sources: dict = field(default_factory=dict)  # 每个字段来自哪一层

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "effort": self.effort, "sources": self.sources}


def parse_model_ref(ref: str | None, provider_keys: set[str]) -> tuple[str | None, str | None]:
    """'x666/grok-4.7' -> ('x666', 'grok-4.7')；'grok-4.7' -> (None, 'grok-4.7')。
    前缀只有在是已配置的提供商 key 时才当作提供商（避免误切 'deepseek-ai/DeepSeek-V3' 这类模型名）。"""
    ref = (ref or "").strip()
    if not ref:
        return None, None
    if "/" in ref:
        head, tail = ref.split("/", 1)
        if head in provider_keys or head == "mock":
            return head, tail or None
    return None, ref


def normalize_effort(v: Any, default: str = "low") -> str:
    if isinstance(v, bool):  # YAML 1.1 会把裸 off/on 解析成布尔
        v = "off" if not v else "high"
    v = str(v or "").strip().lower()
    return v if v in EFFORT_LEVELS else default


_process_overrides: dict = {}


def set_process_overrides(**kw: str | None) -> dict:
    """本进程内的临时覆盖（CLI --provider/--model/--effort），优先于 kv 里的运行时覆盖。"""
    for k, v in kw.items():
        if v:
            _process_overrides[k] = str(v).strip()
        else:
            _process_overrides.pop(k, None)
    return dict(_process_overrides)


def load_overrides(conn: sqlite3.Connection | None) -> dict:
    """kv 里的运行时覆盖（不含本进程的 CLI 标志——它们是更高的一层，见 resolve_choice）。"""
    if conn is None:
        return {}
    try:
        raw = repo.kv_get(conn, OVERRIDE_KEY)
        return json.loads(raw) if raw else {}
    except (sqlite3.Error, ValueError):
        return {}


def set_overrides(conn: sqlite3.Connection, *, provider: str | None = None, model: str | None = None, effort: str | None = None, replace: bool = False) -> dict:
    cur = {} if replace else load_overrides(conn)
    for k, v in (("provider", provider), ("model", model), ("effort", effort)):
        if v is None:
            continue
        v = str(v).strip()
        if v:
            cur[k] = v
        else:
            cur.pop(k, None)
    repo.kv_set(conn, OVERRIDE_KEY, json.dumps(cur, ensure_ascii=False))
    return cur


def clear_overrides(conn: sqlite3.Connection) -> None:
    repo.kv_set(conn, OVERRIDE_KEY, "{}")


def resolve_choice(settings: Settings, step: str | None = None, *, overrides: dict | None = None, explicit: dict | None = None) -> LLMChoice:
    llm_cfg = settings.site.get("llm") or {}
    keys = set((llm_cfg.get("providers") or {}).keys())
    step_cfg = dict(((settings.models.get("steps") or {}).get(step or "", {}) or {}))
    layers: list[tuple[str, dict]] = [
        ("explicit", explicit or {}),
        ("cli", dict(_process_overrides)),
        ("override", overrides or {}),
        ("step", step_cfg),
        ("env", {"provider": settings.env("LLM_PROVIDER"), "model": settings.env("LLM_MODEL"), "effort": settings.env("LLM_EFFORT")}),
        ("default", {"provider": llm_cfg.get("default_provider", "deepseek"), "effort": llm_cfg.get("default_effort", "low")}),
    ]
    parsed = []
    for name, cfg in layers:
        p = (cfg.get("provider") or "").strip() or None
        mp, m = parse_model_ref(cfg.get("model"), keys)
        e = cfg.get("effort")
        if isinstance(e, bool):
            e = "off" if not e else "high"
        # 同一层里 model 的 "provider/" 前缀比裸 provider 更具体，优先
        parsed.append((name, mp or p, m, (str(e).strip() if e not in (None, "") else None)))
    sources: dict = {}
    provider = None
    for name, p, _, _ in parsed:
        if p:
            provider, sources["provider"] = p.lower(), name
            break
    provider = provider or "deepseek"
    model = None
    for name, p, m, _ in parsed:
        # 模型只在「该层没指定提供商」或「指定的提供商就是选中的」时才有效，避免张冠李戴
        if m and (p is None or p.lower() == provider):
            model, sources["model"] = m, name
            break
    effort = None
    for name, _, _, e in parsed:
        if e:
            effort, sources["effort"] = normalize_effort(e, llm_cfg.get("default_effort", "low")), name
            break
    return LLMChoice(provider=provider, model=model, effort=effort or normalize_effort(llm_cfg.get("default_effort"), "low"), sources=sources)


def resolve_provider_key(settings: Settings, step: str | None = None) -> tuple[str, str | None, dict]:
    """兼容旧接口：返回 (provider_key, model_override, step_cfg)。"""
    c = resolve_choice(settings, step)
    return c.provider, c.model, dict(((settings.models.get("steps") or {}).get(step or "", {}) or {}))


# ============================================================ 构建
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
        reasoning=cfg.get("reasoning") or {"style": "openai"},
        rate_limit=cfg.get("rate_limit"),
        timeout=float(llm_cfg.get("request_timeout_seconds", 120)),
        max_retries=int(llm_cfg.get("max_retries", 3)),
    )


def _warn(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        log.warning("进入模拟模式：%s。评分/解读/总述将是规则占位内容，并在界面与 API 中标注 is_mock=true。", reason)


def provider_catalog(settings: Settings) -> list[dict]:
    """供界面/CLI 展示：每个提供商是否已配置 key、默认模型、已知模型列表、是否支持强度。"""
    out = []
    for key, cfg in (settings.site.get("llm", {}).get("providers") or {}).items():
        env = cfg.get("api_key_env") or ""
        configured = bool(cfg.get("no_key_required")) or bool(env and settings.env(env))
        if cfg.get("base_url_env") and not settings.env(cfg["base_url_env"]):
            configured = False
        out.append({
            "key": key,
            "label": cfg.get("label", key),
            "configured": configured,
            "api_key_env": env,
            "model": cfg.get("model") or settings.env(cfg.get("model_env", "")),
            "models": list(cfg.get("models") or []),
            "effort_style": (cfg.get("reasoning") or {}).get("style", "openai"),
        })
    out.append({"key": "mock", "label": "模拟模式（不调用模型）", "configured": True, "api_key_env": "", "model": "mock-rules", "models": [], "effort_style": "none"})
    return out


def llm_status(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """供 CLI 横幅、页脚、/api/v1/status、后台使用。"""
    settings = settings or get_settings()
    overrides = load_overrides(conn)
    choice = resolve_choice(settings, None, overrides=overrides)
    try:
        p = build_provider(settings, choice.provider, choice.model)
    except LLMError as e:
        return {"mode": "error", "provider": choice.provider, "model": choice.model, "effort": choice.effort, "reason": str(e), "is_mock": False, "overrides": overrides}
    d = p.describe()
    d["mode"] = "mock" if p.is_mock else "live"
    d["requested_provider"] = choice.provider
    d["effort"] = choice.effort if p.supports_effort() else None
    d["sources"] = choice.sources
    d["overrides"] = overrides
    d["effort_levels"] = list((settings.site.get("llm") or {}).get("effort_levels") or EFFORT_LEVELS)
    steps = {}
    for step in STEPS:
        c = resolve_choice(settings, step, overrides=overrides)
        try:
            sp = build_provider(settings, c.provider, c.model)
            steps[step] = {"provider": sp.key, "model": sp.model, "effort": c.effort if sp.supports_effort() else None, "is_mock": sp.is_mock, "sources": c.sources}
        except LLMError as e:
            steps[step] = {"provider": c.provider, "model": c.model, "effort": c.effort, "error": str(e)}
    d["steps"] = steps
    d["providers"] = provider_catalog(settings)
    return d


# ============================================================ 门面
class LLM:
    """按步骤解析提供商/模型/强度、渲染提示词并调用。所有调用写入 llm_calls。"""

    def __init__(self, settings: Settings | None = None, conn: sqlite3.Connection | None = None, overrides: dict | None = None):
        self.settings = settings or get_settings()
        self.conn = conn
        self.overrides = overrides if overrides is not None else load_overrides(conn)
        self._providers: dict[str, LLMProvider] = {}
        self._env = Environment(loader=FileSystemLoader(str(self.settings.config_dir / "prompts")), autoescape=False, trim_blocks=True, lstrip_blocks=True)

    def choice_for(self, step: str, **explicit) -> LLMChoice:
        return resolve_choice(self.settings, step, overrides=self.overrides, explicit={k: v for k, v in explicit.items() if v})

    def provider_for(self, step: str, **explicit) -> LLMProvider:
        c = self.choice_for(step, **explicit)
        cache_key = f"{c.provider}:{c.model or ''}"
        if cache_key not in self._providers:
            self._providers[cache_key] = build_provider(self.settings, c.provider, c.model)
        return self._providers[cache_key]

    @property
    def any_live(self) -> bool:
        return any(not self.provider_for(s).is_mock for s in STEPS)

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
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        use_cache: bool = True,
    ) -> LLMResponse:
        """渲染提示词并调用。真实模型的输出按 (task, 提示词) 哈希缓存：同一篇文章不重复花 token；
        use_cache=False（如「重新生成」）绕过缓存但仍会写入。模拟输出永不缓存。"""
        choice = self.choice_for(task, provider=provider, model=model, effort=effort)
        prov = self.provider_for(task, provider=provider, model=model, effort=effort)
        step_cfg = dict(((self.settings.models.get("steps") or {}).get(task, {}) or {}))
        prompt = self.render(task, variables)
        cache_key = hashlib.sha1(f"{task}\n{int(json_mode)}\n{SYSTEM_PROMPT}\n{prompt}".encode("utf-8")).hexdigest()
        if use_cache and self.conn is not None and not prov.is_mock:
            hit = repo.cache_get(self.conn, cache_key)
            if hit:
                resp = LLMResponse(text=hit["text"], provider=hit["provider"], model=hit["model"], is_mock=False, effort=hit.get("effort"),
                                   prompt_tokens=0, completion_tokens=0, latency_ms=0, cached=True)
                self._log(task, prov, resp, effort=hit.get("effort"), ok=True, error=None, ref_type=ref_type, ref_id=ref_id, cached=True)
                return resp
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        temp = temperature if temperature is not None else float(step_cfg.get("temperature", 0.2))
        max_tokens = step_cfg.get("max_tokens")
        try:
            resp = prov.complete(messages, json_mode=json_mode, temperature=temp, max_tokens=max_tokens, effort=choice.effort, task=task, context=context)
        except LLMError as e:
            self._log(task, prov, None, effort=choice.effort, ok=False, error=str(e), ref_type=ref_type, ref_id=ref_id)
            raise
        self._log(task, prov, resp, effort=choice.effort, ok=True, error=None, ref_type=ref_type, ref_id=ref_id)
        if self.conn is not None and not prov.is_mock and resp.text:
            try:
                repo.cache_put(self.conn, cache_key, task=task, provider=resp.provider, model=resp.model, effort=resp.effort, text=resp.text,
                               prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens)
            except sqlite3.Error:
                pass
        return resp

    def _log(self, task: str, provider: LLMProvider, resp: LLMResponse | None, *, effort: str | None, ok: bool, error: str | None, ref_type, ref_id, cached: bool = False) -> None:
        if self.conn is None:
            return
        try:
            repo.log_llm_call(
                self.conn,
                task=task,
                provider=provider.key,
                model=(resp.model if resp else provider.model),
                is_mock=provider.is_mock,
                effort=(effort if provider.supports_effort() else None),
                prompt_tokens=resp.prompt_tokens if resp else None,
                completion_tokens=resp.completion_tokens if resp else None,
                reasoning_tokens=resp.reasoning_tokens if resp else None,
                latency_ms=resp.latency_ms if resp else None,
                ok=ok,
                error=error,
                ref_type=ref_type,
                ref_id=ref_id,
                cached=cached,
            )
        except sqlite3.Error:
            pass


def test_llm(settings: Settings, conn: sqlite3.Connection | None = None, *, provider: str | None = None, model: str | None = None, effort: str | None = None, prompt: str | None = None) -> dict:
    """真实调用一次（小提示词），返回延迟 / tokens / 推理 tokens / 内容；模拟模式如实标注。"""
    choice = resolve_choice(settings, None, overrides=load_overrides(conn), explicit={"provider": provider, "model": model, "effort": effort})
    try:
        prov = build_provider(settings, choice.provider, choice.model)
    except LLMError as e:
        return {"ok": False, "provider": choice.provider, "model": choice.model, "effort": choice.effort, "error": str(e)}
    q = prompt or "一个 IVF 周期取卵 12 枚、正常受精 7 枚、囊胚 3 枚。只输出 JSON：{\"blastocyst_rate_percent\": 数值}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": q}]
    t0 = time.monotonic()
    try:
        resp = prov.complete(messages, json_mode=True, temperature=0.2, effort=choice.effort, task="test", context={})
    except LLMError as e:
        if conn is not None:
            repo.log_llm_call(conn, task="test", provider=prov.key, model=prov.model, is_mock=prov.is_mock, effort=choice.effort, prompt_tokens=None, completion_tokens=None, reasoning_tokens=None, latency_ms=int((time.monotonic() - t0) * 1000), ok=False, error=str(e))
        return {"ok": False, "provider": prov.key, "model": prov.model, "effort": choice.effort, "error": str(e), "is_mock": prov.is_mock}
    if conn is not None:
        repo.log_llm_call(conn, task="test", provider=prov.key, model=resp.model, is_mock=prov.is_mock, effort=resp.effort, prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens, reasoning_tokens=resp.reasoning_tokens, latency_ms=resp.latency_ms, ok=True, error=None)
    try:
        parsed = resp.json()
    except LLMError:
        parsed = None
    return {
        "ok": True, "is_mock": prov.is_mock, "provider": prov.key, "label": prov.label, "model": resp.model, "effort": resp.effort,
        "latency_ms": resp.latency_ms, "prompt_tokens": resp.prompt_tokens, "completion_tokens": resp.completion_tokens, "reasoning_tokens": resp.reasoning_tokens,
        "content": resp.text[:300], "parsed": parsed, "request_extra": reasoning_body(getattr(prov, "reasoning", None), choice.effort) if not prov.is_mock else {},
    }


__all__ = [
    "LLM", "LLMChoice", "LLMError", "LLMProvider", "LLMResponse", "MockProvider", "OpenAICompatProvider", "EFFORT_LEVELS", "STEPS", "SYSTEM_PROMPT",
    "build_provider", "clear_overrides", "llm_status", "load_overrides", "parse_model_ref", "provider_catalog", "resolve_choice", "resolve_provider_key", "set_overrides", "set_process_overrides", "test_llm",
]

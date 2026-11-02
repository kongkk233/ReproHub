"""OpenAI 兼容 Chat Completions 客户端：DeepSeek / Gemini、Grok 等网关 / 通义 / 智谱 / Moonshot / OpenAI / Ollama / 自定义端点。"""
from __future__ import annotations

import copy
import time

import httpx

from .base import LLMError, LLMProvider, LLMResponse


def reasoning_body(reasoning: dict | None, effort: str | None) -> dict:
    """把统一的思考强度翻译成该提供商的请求体字段。

    style=openai : low/medium/high -> reasoning_effort；off -> reasoning.off（无则不带参数）
    style=custom : reasoning.levels[effort] 原样合并
    style=none   : 忽略
    """
    r = reasoning or {}
    style = r.get("style", "openai")
    if not effort or style == "none":
        return {}
    if style == "custom":
        levels = r.get("levels") or {}
        # 裸 off 在 YAML 1.1 中是布尔 False，这里兼容两种写法
        return copy.deepcopy(levels.get(effort) or (levels.get(False) if effort == "off" else None) or {})
    if effort == "off":
        return copy.deepcopy(r.get("off") or r.get(False) or {})
    return {"reasoning_effort": effort}


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        key: str,
        label: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        supports_json_mode: bool = True,
        reasoning: dict | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
    ):
        self.key = key
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.supports_json_mode = supports_json_mode
        self.reasoning = reasoning or {"style": "openai"}
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.is_mock = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def build_body(self, messages, *, json_mode=False, temperature=0.2, max_tokens=None, effort=None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if json_mode and self.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(reasoning_body(self.reasoning, effort))
        return body

    def complete(self, messages, *, json_mode=False, temperature=0.2, max_tokens=None, effort=None, task=None, context=None) -> LLMResponse:
        body = self.build_body(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens, effort=effort)
        url = f"{self.base_url}/chat/completions"
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            t0 = time.monotonic()
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, headers=self._headers(), json=body)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 4xx（鉴权、参数）不重试
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}") from None
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                if choice.get("finish_reason") == "length":
                    # 思考型模型的 max_tokens 往往同时覆盖推理与正文；截断的 JSON 没有意义，直接报清楚
                    raise LLMError(
                        f"输出被 max_tokens 截断（finish_reason=length，completion_tokens={usage.get('completion_tokens')}）："
                        "请调高 config/models.yaml 中该步骤的 max_tokens，或把思考强度降为 low/off"
                    ) from None
                details = usage.get("completion_tokens_details") or {}
                return LLMResponse(
                    text=text,
                    provider=self.key,
                    model=data.get("model") or self.model,
                    is_mock=False,
                    effort=effort if self.supports_effort() else None,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    reasoning_tokens=details.get("reasoning_tokens"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    raw=data,
                )
            except (httpx.HTTPError, LLMError) as e:
                last_err = e
                msg = str(e)
                if (msg.startswith("HTTP 4") and not msg.startswith("HTTP 429")) or msg.startswith("输出被"):
                    break
                time.sleep(min(8, 1.5 * (2**attempt)))
        raise LLMError(f"{self.label} 调用失败：{last_err}")

    def ping(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(f"{self.base_url}/models", headers=self._headers())
            if r.status_code == 200:
                return True, f"{self.label} 可达，模型 {self.model}"
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        except httpx.HTTPError as e:
            return False, str(e)

    def list_models(self) -> list[str]:
        """从 /models 拉取可用模型 id（网关通常支持）；失败返回空列表。"""
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(f"{self.base_url}/models", headers=self._headers())
            if r.status_code != 200:
                return []
            return sorted(str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id"))
        except (httpx.HTTPError, ValueError):
            return []

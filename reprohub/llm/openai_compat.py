"""OpenAI 兼容 Chat Completions 客户端：DeepSeek / 通义 / 智谱 / Moonshot / OpenAI / Ollama / 自定义端点。"""
from __future__ import annotations

import time

import httpx

from .base import LLMError, LLMProvider, LLMResponse


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
        timeout: float = 90.0,
        max_retries: int = 3,
    ):
        self.key = key
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.supports_json_mode = supports_json_mode
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.is_mock = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def complete(self, messages, *, json_mode=False, temperature=0.2, max_tokens=None, task=None, context=None) -> LLMResponse:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if json_mode and self.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
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
                return LLMResponse(
                    text=text,
                    provider=self.key,
                    model=data.get("model") or self.model,
                    is_mock=False,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    raw=data,
                )
            except (httpx.HTTPError, LLMError) as e:
                last_err = e
                msg = str(e)
                if msg.startswith("HTTP 4") and not msg.startswith("HTTP 429"):
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

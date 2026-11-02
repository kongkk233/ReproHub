"""LLM 提供商抽象。所有响应都带 is_mock 标记，调用方必须把它一路带到数据库与界面。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)

# 思考强度统一词汇；各提供商在 site.yaml -> llm.providers.*.reasoning 中声明映射
EFFORT_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    is_mock: bool = False
    effort: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)

    def json(self) -> Any:
        """宽松解析：去掉 ``` 围栏，退而取第一个 { 到最后一个 }。"""
        txt = (self.text or "").strip()
        txt = _FENCE_RE.sub("", txt).strip()
        try:
            return json.loads(txt)
        except ValueError:
            pass
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(txt[start : end + 1])
            except ValueError:
                pass
        raise LLMError(f"模型未返回合法 JSON：{txt[:200]!r}")


class LLMProvider:
    key: str = "base"
    label: str = "base"
    model: str = ""
    is_mock: bool = False
    reasoning: dict = {}

    def complete(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        effort: str | None = None,
        task: str | None = None,
        context: dict | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    def ping(self) -> tuple[bool, str]:
        return True, "ok"

    def supports_effort(self) -> bool:
        return (self.reasoning or {}).get("style", "none") != "none"

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "model": self.model, "is_mock": self.is_mock, "supports_effort": self.supports_effort()}

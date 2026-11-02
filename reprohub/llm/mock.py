"""模拟提供商：没有 API key 时的占位实现。

原则：
- 绝不假装调用过模型。所有输出 is_mock=True，文本以「模拟」标注；
- 不编造翻译或评价。中文标题/摘要留空（界面回退显示原文），评分来自可解释的规则；
- 解读只做「从摘要中抽取句子」，不生成评价性内容。
"""
from __future__ import annotations

import json
import re

from ..textutil import clamp, first_sentences, truncate
from .base import LLMProvider, LLMResponse

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_METHOD_WORDS = re.compile(r"\b(we|using|used|performed|conducted|analy[sz]ed|sequenc|profil|cohort|randomi[sz]ed|trial|retrospective|prospective|measured|compared|mass spectrometry|RNA-seq|single-cell|proteom|transcriptom|n\s*=\s*\d+|participants|patients|embryos|oocytes|mice|samples)\b", re.I)
_FINDING_WORDS = re.compile(r"\b(found|show|shows|showed|reveal|revealed|demonstrat|identified|increased|decreased|reduced|higher|lower|associated|correlat|significant|improved|predict|suggest|indicate|conclude|conclusion)\w*\b", re.I)
_OMICS_WORDS = re.compile(r"\b(transcriptom\w*|proteom\w*|RNA-seq|scRNA-seq|single-cell|mass spectrometry|epigenom\w*|methylom\w*|metabolom\w*|lipidom\w*|multi-?omics?|chromatin|ATAC-seq|Hi-C|CUT&Tag|translatom\w*)\b", re.I)

MOCK_NOTICE = "模拟模式：未调用任何模型，以下为规则生成的占位内容。设置 DEEPSEEK_API_KEY（或其他提供商）后重新运行即可获得真实结果。"


class MockProvider(LLMProvider):
    key = "mock"
    label = "模拟模式（未调用模型）"
    model = "mock-rules"
    is_mock = True

    def __init__(self, reason: str = "LLM_PROVIDER=mock"):
        self.reason = reason

    # ------------------------------------------------------------------ api
    def complete(self, messages, *, json_mode=False, temperature=0.2, max_tokens=None, task=None, context=None) -> LLMResponse:
        ctx = context or {}
        fn = {
            "prescreen": self._prescreen,
            "score": self._score,
            "interpret": self._interpret,
            "cluster": self._cluster,
            "digest": self._digest,
        }.get(task or "", self._generic)
        out = fn(ctx)
        text = json.dumps(out, ensure_ascii=False) if json_mode else (out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
        return LLMResponse(text=text, provider=self.key, model=self.model, is_mock=True, latency_ms=0)

    def ping(self) -> tuple[bool, str]:
        return True, f"模拟模式（{self.reason}）"

    def describe(self) -> dict:
        d = super().describe()
        d["reason"] = self.reason
        return d

    # ------------------------------------------------------------ generators
    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]

    def _generic(self, ctx: dict):
        return {"mock": True, "notice": MOCK_NOTICE}

    def _prescreen(self, ctx: dict) -> dict:
        topic_score = float(ctx.get("topic_score") or 0)
        in_scope = topic_score >= 1 or bool(ctx.get("in_scope_default"))
        return {
            "in_scope": in_scope,
            "confidence": 0.5,
            "reason": f"模拟预筛：规则主题分 {topic_score:g}（未调用模型）",
            "topics": [t.get("slug") for t in (ctx.get("topics") or [])][:3],
            "mock": True,
        }

    def _score(self, ctx: dict) -> dict:
        topic_score = float(ctx.get("topic_score") or 0)
        pref = float(ctx.get("preference_score") or 0)
        tier = int(ctx.get("tier") or 3)
        kind = ctx.get("kind") or "news"
        summary = ctx.get("summary") or ""
        # 刻意保守：模拟分落在 30-66，只有主题命中很强的条目才过入选线；
        # 偏好加权由评分步骤统一叠加，这里不重复计入
        score = 40.0
        score += min(16.0, topic_score * 2.5)
        score += {1: 4, 2: 2, 3: 0}.get(tier, 0)
        score += {"paper": 3, "preprint": 2, "guideline": 3, "news": -2, "post": -4, "other": -5}.get(kind, 0)
        if len(summary) > 400:
            score += 2
        elif len(summary) < 80:
            score -= 6
        score = round(clamp(score, 30, 66), 1)
        relevance = round(clamp(40 + topic_score * 8, 30, 95))
        dims = {"novelty": round(score), "rigor": round(clamp(score - 5, 20, 90)), "impact": round(score), "relevance": relevance, "translation": round(clamp(score - 8, 20, 90))}
        labels = ctx.get("preference_labels") or []
        topics = [t.get("slug") for t in (ctx.get("topics") or [])][:3]
        reason = f"【模拟评分】规则生成：主题命中分 {topic_score:g}，来源等级 {tier}，类型 {kind}" + (f"；命中偏好 {'、'.join(labels)}（另行加权 +{pref:g}）" if pref else "") + "。未调用模型，不含质量判断。"
        return {
            "title_zh": None,
            "summary_zh": None,
            "score": score,
            "dims": dims,
            "recommend_reason": reason,
            "keywords": (ctx.get("matched_keywords") or [])[:6],
            "topics": topics,
            "kind_hint": kind,
            "mock": True,
        }

    def _interpret(self, ctx: dict) -> dict:
        summary = ctx.get("summary") or ""
        sents = self._sentences(summary)
        design = [s for s in sents if _METHOD_WORDS.search(s)][:3]
        findings = [s for s in sents if _FINDING_WORDS.search(s)][:4]
        omics = sorted({m.group(0).lower() for m in _OMICS_WORDS.finditer(summary)})
        terms = [{"en": kw, "zh": "", "note": "（模拟模式不提供释义）"} for kw in (ctx.get("matched_keywords") or [])[:6]]
        return {
            "one_liner": "【模拟解读】未调用模型；以下内容均为从原文摘要中抽取的句子，不含评价。",
            "background": first_sentences(summary, 2, 500) or "原文未提供摘要。",
            "design": " ".join(design) if design else "原文未提供（或模拟模式未能从摘要中识别方法句）。",
            "findings": findings or ["原文未提供（或模拟模式未能从摘要中识别结果句）。"],
            "omics_angle": ("摘要中提及的组学/技术词：" + "、".join(omics)) if omics else "不适用（摘要未提及组学相关词）",
            "significance": "模拟模式不生成评价。" + MOCK_NOTICE,
            "limitations": ["模拟模式未评估局限性。"],
            "next_questions": [],
            "terms": terms,
            "reading_time_min": max(2, len(summary) // 900 + 2),
            "mock": True,
        }

    def _cluster(self, ctx: dict) -> dict:
        j = float(ctx.get("jaccard") or 0)
        same = float(ctx.get("jaccard_same") or 0.45)
        rel = "same" if j >= same else "unrelated"
        return {"relation": rel, "confidence": 0.4, "reason": f"模拟判定：标题/摘要 Jaccard={j:.2f}（未调用模型）", "mock": True}

    def _digest(self, ctx: dict) -> dict:
        sections = ctx.get("sections") or []
        n = sum(len(s.get("items") or []) for s in sections)
        lines = [f"【模拟总述】本期共 {n} 条入选内容，分布于 {len(sections)} 个栏目。未调用模型，以下仅为统计性描述："]
        for s in sections:
            its = s.get("items") or []
            if its:
                top = its[0]
                lines.append(f"· {s.get('topic_name')}：{len(its)} 条，最高分 {top.get('final_score')}（{truncate(top.get('title_zh') or top.get('title') or '', 60)}）")
        leads = []
        for s in sections:
            its = s.get("items") or []
            lead = f"{len(its)} 条" + (f"；首推：{truncate(its[0].get('title_zh') or its[0].get('title') or '', 50)}" if its else "")
            leads.append({"topic": s.get("topic"), "lead": "【模拟】" + lead})
        return {"overview": "\n".join(lines), "sections": leads, "mock": True}

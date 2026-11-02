"""规则分类：主题匹配（领域门槛）与重点偏好（加权）。不依赖模型，始终运行。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..settings import Settings
from ..textutil import _CJK_RE, normalize_text

# 横切主题：只有在没有其它主题命中时才作为主主题
SECONDARY_TOPICS = {"omics-methods", "ai-compute", "animal-models"}


def _compile(keywords: list[str]) -> tuple[re.Pattern | None, list[str]]:
    en = [k.strip() for k in keywords if k and not _CJK_RE.search(k)]
    zh = [k.strip() for k in keywords if k and _CJK_RE.search(k)]
    pat = None
    if en:
        alts = sorted({re.escape(k.lower()) for k in en}, key=len, reverse=True)
        pat = re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])", re.I)
    return pat, zh


def _hits(pat: re.Pattern | None, zh: list[str], text: str) -> set[str]:
    found: set[str] = set()
    if pat:
        for m in pat.finditer(text):
            found.add(m.group(0).lower())
    for k in zh:
        if k in text:
            found.add(k)
    return found


@dataclass
class Classification:
    topics: list[dict] = field(default_factory=list)  # [{slug, score, hits}]
    primary_topic: str | None = None
    topic_score: float = 0.0
    in_scope: bool = False
    preference_score: float = 0.0
    preference_labels: list[str] = field(default_factory=list)
    preference_keys: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "topics": self.topics,
            "primary_topic": self.primary_topic,
            "topic_score": self.topic_score,
            "in_scope": self.in_scope,
            "preference_score": self.preference_score,
            "preference_labels": self.preference_labels,
            "preference_keys": self.preference_keys,
            "matched_keywords": self.matched_keywords,
        }


class Classifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.topics = settings.topics
        self.min_topic_score = float((settings.selection.get("scope") or {}).get("min_topic_score", 2))
        self._topic_pats: list[tuple[dict, re.Pattern | None, list[str]]] = []
        for t in self.topics:
            pat, zh = _compile(t.get("keywords") or [])
            self._topic_pats.append((t, pat, zh))
        self._rules: list[tuple[dict, list[tuple[re.Pattern | None, list[str]]]]] = []
        for r in (settings.preferences.get("rules") or []):
            groups = [_compile(g.get("any_of") or []) for g in (r.get("all_of") or [])]
            self._rules.append((r, groups))

    # ------------------------------------------------------------------
    def match_topics(self, title: str, summary: str) -> list[dict]:
        t_norm = normalize_text(title)
        s_norm = normalize_text(summary)
        out = []
        for t, pat, zh in self._topic_pats:
            th = _hits(pat, zh, t_norm)
            sh = _hits(pat, zh, s_norm)
            if not th and not sh:
                continue
            score = (len(th) * 3 + min(len(sh), 12)) * float(t.get("weight", 1.0))
            out.append({"slug": t["slug"], "score": round(score, 2), "hits": sorted(th | sh)[:12]})
        out.sort(key=lambda d: -d["score"])
        return out

    def preference(self, title: str, summary: str) -> tuple[float, list[str], list[str]]:
        text_all = normalize_text(f"{title}\n{summary}")
        text_title = normalize_text(title)
        score, labels, keys = 0.0, [], []
        for r, groups in self._rules:
            text = text_title if r.get("title_only") else text_all
            if groups and all(_hits(pat, zh, text) for pat, zh in groups):
                score += float(r.get("weight", 0))
                labels.append(r.get("label", r.get("key")))
                keys.append(r.get("key"))
        return score, labels, keys

    def classify(self, title: str, summary: str, *, in_scope_default: bool = False) -> Classification:
        topics = self.match_topics(title, summary)
        c = Classification(topics=topics)
        core = [t for t in topics if t["slug"] not in SECONDARY_TOPICS]
        if topics:
            c.topic_score = topics[0]["score"]
            primary = next((t for t in core if t["score"] >= self.min_topic_score), None)
            c.primary_topic = (primary or topics[0])["slug"]
        # 领域门槛：横切主题（组学方法/AI/动物模型）单独命中不算在领域内
        core_score = core[0]["score"] if core else 0.0
        c.in_scope = bool(in_scope_default) or core_score >= self.min_topic_score
        c.preference_score, c.preference_labels, c.preference_keys = self.preference(title, summary)
        kws: list[str] = []
        for t in topics[:3]:
            for h in t["hits"]:
                if h not in kws:
                    kws.append(h)
        c.matched_keywords = kws[:10]
        return c

    def quick_in_scope(self, title: str, summary: str) -> bool:
        return self.classify(title, summary).in_scope

    def topic_name(self, slug: str | None) -> str:
        if not slug:
            return "未分类"
        return self.settings.topic_map.get(slug, {}).get("name", slug)

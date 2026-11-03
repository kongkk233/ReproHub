"""期刊注册表：期刊名/ISSN → 等级、影响因子；回填条目；不调模型地重算最终分。"""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from . import repo
from .settings import Settings
from .textutil import clamp, now_iso

_PUNCT_RE = re.compile(r"[\.\,\:\;\'\"’‘“”\-–—/]+")
_WS_RE = re.compile(r"\s+")
_ISSN_RE = re.compile(r"^(\d{4})-?(\d{3}[\dXx])$")


def norm_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.strip().lower().replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def norm_issn(issn: str | None) -> str | None:
    if not issn:
        return None
    m = _ISSN_RE.match(issn.strip())
    return f"{m.group(1)}-{m.group(2).upper()}" if m else None


class JournalRegistry:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None):
        self.settings = settings
        cfg = settings.journals
        self.meta = cfg.get("meta") or {}
        self.tiers: dict[str, dict] = cfg.get("tiers") or {}
        self.top_tiers: list[str] = list(cfg.get("top_tiers") or ["cns", "cns-sub", "general-top", "field-top"])
        self.entries: dict[str, dict] = {}
        self._by_name: dict[str, str] = {}
        self._by_issn: dict[str, str] = {}
        self._patterns = [(re.compile(p["match"]), p["tier"]) for p in (cfg.get("patterns") or []) if p.get("match")]
        for j in cfg.get("journals") or []:
            self._add(dict(j), source="yaml")
        if conn is not None:
            try:
                for r in conn.execute("SELECT * FROM journals"):
                    d = dict(r)
                    self._add({"key": d["key"], "name": d["name"], "aliases": (d.get("aliases") or "").split("|") if d.get("aliases") else [], "issn": (d.get("issn") or "").split("|") if d.get("issn") else [],
                               "tier": d.get("tier") or None, "if": d.get("if_value"), "if_year": d.get("if_year")}, source=d.get("source") or "import")
            except sqlite3.OperationalError:
                pass

    def _add(self, j: dict, *, source: str) -> None:
        key = j.get("key") or re.sub(r"[^a-z0-9]+", "-", norm_name(j.get("name"))).strip("-")
        if not key:
            return
        j["key"] = key
        j["source"] = source
        prev = self.entries.get(key)
        if prev:  # 导入覆盖 yaml：保留 yaml 里有而导入缺的字段
            j = {**prev, **{k: v for k, v in j.items() if v not in (None, "", [])}}
        if not j.get("tier"):
            j["tier"] = "standard"
        self.entries[key] = j
        for alias in [j.get("name"), *(j.get("aliases") or [])]:
            n = norm_name(alias)
            if n:
                self._by_name[n] = key
        for issn in j.get("issn") or []:
            n = norm_issn(str(issn))
            if n:
                self._by_issn[n] = key

    # ------------------------------------------------------------------
    def tier_info(self, tier: str | None) -> dict:
        t = dict(self.tiers.get(tier or "unknown") or self.tiers.get("unknown") or {"label": "", "boost": 0, "rank": 9})
        t["key"] = tier or "unknown"
        return t

    def resolve(self, name: str | None, issn: str | None = None) -> dict:
        """返回 {key, name, tier, if, if_year, label, boost, rank, matched_by}；未命中 tier=unknown。"""
        key = None
        matched = None
        n_issn = norm_issn(issn)
        if n_issn and n_issn in self._by_issn:
            key, matched = self._by_issn[n_issn], "issn"
        if not key:
            n = norm_name(name)
            if n in self._by_name:
                key, matched = self._by_name[n], "alias"
        if key:
            e = self.entries[key]
            t = self.tier_info(e.get("tier"))
            return {"key": key, "name": e.get("name"), "tier": t["key"], "if": e.get("if"), "if_year": e.get("if_year") or self.meta.get("if_year"), "label": t["label"], "boost": t["boost"], "rank": t["rank"], "matched_by": matched}
        for pat, tier in self._patterns:
            if name and pat.search(name.strip()):
                t = self.tier_info(tier)
                return {"key": None, "name": name, "tier": t["key"], "if": None, "if_year": None, "label": t["label"], "boost": t["boost"], "rank": t["rank"], "matched_by": "pattern"}
        t = self.tier_info("unknown")
        return {"key": None, "name": name, "tier": "unknown", "if": None, "if_year": None, "label": "", "boost": 0, "rank": t["rank"], "matched_by": None}

    def item_fields(self, name: str | None, issn: str | None = None) -> dict:
        r = self.resolve(name, issn)
        return {"journal_key": r["key"], "journal_tier": r["tier"], "journal_if": r["if"]}

    def boost_for(self, tier: str | None) -> float:
        if not (self.settings.selection.get("scoring") or {}).get("journal_boost_enabled", True):
            return 0.0
        cap = float((self.settings.selection.get("scoring") or {}).get("journal_boost_cap", 12))
        return clamp(float(self.tier_info(tier).get("boost", 0)), -cap, cap)


# ============================================================ 维护操作
def import_csv(conn: sqlite3.Connection, path: str | Path, *, source: str = "import") -> int:
    """CSV 列：name, issn（可多值用 | 分隔）, if, tier（可空）, year（可空）, aliases（可空，| 分隔）, key（可空）。"""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            key = (row.get("key") or "").strip() or re.sub(r"[^a-z0-9]+", "-", norm_name(name)).strip("-")
            issn = "|".join(x for x in (norm_issn(s) for s in (row.get("issn") or "").split("|")) if x)
            try:
                if_value = float(row["if"]) if row.get("if") not in (None, "") else None
            except ValueError:
                if_value = None
            conn.execute(
                """INSERT INTO journals(key, name, aliases, issn, tier, if_value, if_year, source, updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET name=excluded.name, aliases=excluded.aliases, issn=excluded.issn, tier=COALESCE(NULLIF(excluded.tier,''), journals.tier),
                     if_value=COALESCE(excluded.if_value, journals.if_value), if_year=COALESCE(excluded.if_year, journals.if_year), source=excluded.source, updated_at=excluded.updated_at""",
                (key, name, (row.get("aliases") or "").strip(), issn, (row.get("tier") or "").strip(), if_value, (row.get("year") or "").strip() or None, source, now_iso()),
            )
            n += 1
    return n


def annotate_items(conn: sqlite3.Connection, settings: Settings, *, only_missing: bool = False) -> dict:
    """用注册表回填所有条目的 journal_key / journal_tier / journal_if（不调模型）。"""
    reg = JournalRegistry(settings, conn)
    where = "WHERE journal_tier IS NULL" if only_missing else ""
    rows = conn.execute(f"SELECT id, journal, extra FROM items {where}").fetchall()
    out = {"items": len(rows), "matched": 0, "pattern": 0, "unknown": 0, "by_tier": {}}
    import json

    conn.execute("BEGIN")
    try:
        for r in rows:
            issn = None
            try:
                issn = (json.loads(r["extra"] or "{}") or {}).get("issn")
            except ValueError:
                pass
            res = reg.resolve(r["journal"], issn)
            conn.execute("UPDATE items SET journal_key=?, journal_tier=?, journal_if=? WHERE id=?", (res["key"], res["tier"], res["if"], r["id"]))
            out["by_tier"][res["tier"]] = out["by_tier"].get(res["tier"], 0) + 1
            if res["matched_by"] in ("issn", "alias"):
                out["matched"] += 1
            elif res["matched_by"] == "pattern":
                out["pattern"] += 1
            else:
                out["unknown"] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def unknown_journals(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT journal, COUNT(*) n FROM items WHERE journal_tier='unknown' AND journal IS NOT NULL GROUP BY journal ORDER BY n DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def rescore_finals(conn: sqlite3.Connection, settings: Settings) -> dict:
    """只用已存的模型分 + 偏好 + 来源等级 + 期刊等级重算最终分与入选，不调模型。"""
    reg = JournalRegistry(settings, conn)
    sc = settings.selection.get("scoring") or {}
    rows = conn.execute(
        """SELECT i.id, i.llm_score, i.preference_score, i.kind, i.selected, i.selected_at, i.journal_tier, i.extra, s.tier AS source_tier
           FROM items i JOIN sources s ON s.id=i.source_id WHERE i.status='scored' AND i.llm_score IS NOT NULL"""
    ).fetchall()
    out = {"items": len(rows), "changed_selected": 0, "newly_selected": 0, "dropped": 0}
    import json

    conn.execute("BEGIN")
    try:
        for r in rows:
            tier = int(r["source_tier"] or 3)
            pref_boost = min(float(r["preference_score"] or 0) * float(sc.get("preference_weight", 1.0)), float(sc.get("preference_cap", 25)))
            tier_adj = float((sc.get("tier_adjust") or {}).get(tier, 0))
            jboost = reg.boost_for(r["journal_tier"])
            final = round(clamp(float(r["llm_score"]) + pref_boost + tier_adj + jboost, 0, 100), 1)
            threshold = float((sc.get("select_threshold") or {}).get(tier, 65)) + float((sc.get("kind_adjust") or {}).get(r["kind"], 0))
            selected = 1 if final >= threshold else 0
            extra = {}
            try:
                extra = json.loads(r["extra"] or "{}") or {}
            except ValueError:
                pass
            extra["threshold"], extra["pref_boost"], extra["journal_boost"] = threshold, pref_boost, jboost
            selected_at = r["selected_at"]
            if selected and not r["selected"]:
                selected_at = now_iso()
                out["newly_selected"] += 1
            elif not selected and r["selected"]:
                out["dropped"] += 1
            if selected != (r["selected"] or 0):
                out["changed_selected"] += 1
            conn.execute("UPDATE items SET final_score=?, selected=?, selected_at=?, extra=? WHERE id=?", (final, selected, selected_at, json.dumps(extra, ensure_ascii=False), r["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def journal_stats(conn: sqlite3.Connection, settings: Settings) -> dict:
    reg = JournalRegistry(settings, conn)
    rows = conn.execute("SELECT journal_tier, COUNT(*) n, SUM(selected) sel FROM items WHERE status='scored' GROUP BY journal_tier").fetchall()
    by_tier = {r["journal_tier"] or "unknown": {"n": r["n"], "selected": r["sel"] or 0, "label": reg.tier_info(r["journal_tier"]).get("label") or (r["journal_tier"] or "unknown")} for r in rows}
    top = conn.execute(
        f"""SELECT COALESCE(journal_key, journal) AS k, MIN(journal) AS journal, journal_tier, MAX(journal_if) AS journal_if, COUNT(*) n, SUM(selected) sel
            FROM items WHERE journal_tier IN ({','.join('?' * len(reg.top_tiers))}) GROUP BY k ORDER BY n DESC LIMIT 40""", reg.top_tiers
    ).fetchall()
    tops = []
    for r in top:
        d = dict(r)
        e = reg.entries.get(d["k"]) if d["k"] else None
        d["journal"] = (e or {}).get("name") or d["journal"]
        tops.append(d)
    return {"by_tier": by_tier, "top_journals": tops, "registry_size": len(reg.entries), "if_source": reg.meta.get("if_source"), "top_tiers": reg.top_tiers}

"""OA 全文获取与抽取（只走 Europe PMC，无需 API key）。

只用 Europe PMC 的两个公开接口：
  1) lite 检索（按 DOI，退而求其次 PMID）确认 PMCID 与 OA 状态；
  2) fullTextXML 取 JATS 全文，且仅当 isOpenAccess / inEPMC 为真时才取。

原则：
- 取不到全文绝不编造：解读回退为「仅摘要解读」（interpretations.basis='abstract'），并记录原因；
- 抽取只做「去图表、去参考文献、按 IMRaD 分节」的正文还原，不改写、不补全；
- 网络层失败不写记录（下次运行重试），非 OA / 无 PMCID 是确定性结论，不再重复请求；
- 每条全文都在 text 顶部注明来源、PMCID 与许可，便于读者核对。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Callable

from . import repo
from .settings import Settings
from .sources.base import FetchError, http_get
from .textutil import normalize_doi

log = logging.getLogger("reprohub.fulltext")
Logger = Callable[[str], None]

LITE_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
DEFAULT_MAX_CHARS = 30000
# 连续多少次网络层失败就中止本轮（Europe PMC 不可达是全局问题，不必逐条重试）
NETWORK_ABORT = 2

# 抓取状态。只有 ok 才是「全文解读」，其余一律回退为「仅摘要解读」并说明原因。
STATUS_LABEL = {
    "ok": "已取得 Europe PMC OA 全文",
    "no_id": "条目没有 DOI / PMID，无法查询 Europe PMC",
    "not_found": "Europe PMC 无此记录",
    "no_pmcid": "Europe PMC 无 PMCID（未收录全文）",
    "non_oa": "Europe PMC 未收录免费全文（非 OA，或该来源不在 Europe PMC 全文范围内，如 bioRxiv 预印本）",
    "error": "抓取失败",
}
# 确定性结论：不再重复请求
FINAL_STATUS = ("no_id", "not_found", "no_pmcid", "non_oa")
# 网络层失败：不落库，下次运行重试
NETWORK_STATUS = "network"

BASIS_LABEL = {"fulltext": "全文解读", "abstract": "仅摘要解读"}

SECTION_ORDER = ("abstract", "introduction", "methods", "results", "discussion", "other")
SECTION_LABEL = {
    "abstract": "Abstract 摘要",
    "introduction": "Introduction 引言",
    "methods": "Methods 方法",
    "results": "Results 结果",
    "discussion": "Discussion 讨论",
    "other": "Other 其他",
}
# 截断时的分配权重：方法与结果优先保留
SECTION_WEIGHT = {"methods": 3.0, "results": 3.0, "abstract": 1.0, "introduction": 0.8, "discussion": 1.2, "other": 0.6}
_TRUNC_MARK = "…（本节已截断）"

_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("methods", re.compile(r"\bmethod|materials and methods|experimental|patients and methods|study design|subjects|participants|data (?:collection|analysis)|statistical analysis|protocol", re.I)),
    ("results", re.compile(r"\bresult|findings|outcomes", re.I)),
    ("discussion", re.compile(r"\bdiscussion|conclusion|limitation|perspective", re.I)),
    ("introduction", re.compile(r"\bintroduction|background", re.I)),
)
# 与解读无关的例行小节：致谢、补充材料、利益声明等，直接不抽
_SKIP_TITLE_RE = re.compile(
    r"acknowledg|supplementar|source data|author contributions?|competing interests?|conflicts? of interest|declaration of interests?|"
    r"funding|financial disclosure|ethics|consent|publisher'?s note|abbreviation|orcid|permissions?|graphical abstract|peer review",
    re.I,
)

# 抽取时跳过的子树：图表、参考文献、公式、许可声明等
_SKIP_TAGS = frozenset(
    """fig table-wrap table supplementary-material ref-list ref fn-group fn graphic media inline-graphic
       disp-formula inline-formula tex-math xref label attrib permissions license funding-group
       author-notes journal-meta""".split()
)
_WS_RE = re.compile(r"\s+")
# 许可：既认 "CC BY 4.0" 也认 "Creative Commons Attribution 4.0 International License"，以及 CC 的 URL
_CC_RE = re.compile(
    r"(?:CC[ -]?BY|Creative Commons Attribution)"
    r"(?:[ -]?(?:NonCommercial|NC))?"
    r"(?:[ -]?(?:NoDerivatives|ND|ShareAlike|SA))?"
    r"(?:[ -]*(?:4\.0|3\.0|2\.5|2\.0|1\.0))?",
    re.I,
)
_CC_URL_RE = re.compile(r"creativecommons\.org/licenses/([a-z\-]+)/(\d\.\d)", re.I)


# ============================================================ 文本抽取
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _text(elem: ET.Element) -> str:
    """元素内可见文本：跳过图表、参考文献、公式等子树，保留段落与各级小标题。"""
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        if _local(e.tag) in _SKIP_TAGS:
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
            if c.tail:
                parts.append(c.tail)

    walk(elem)
    return _norm(" ".join(parts))


def classify_section(title: str) -> str:
    """把节标题归到 IMRaD 五类之一；例行小节返回 skip，认不出的一律算 other。"""
    t = _norm(title)
    if not t:
        return "other"
    if _SKIP_TITLE_RE.search(t):
        return "skip"
    for kind, pat in _SECTION_PATTERNS:
        if pat.search(t):
            return kind
    return "other"


def _cc_label(raw: str) -> str:
    """把命中的许可写法归一成短标签，如 CC BY 4.0 / CC BY-NC-ND 4.0。"""
    t = raw.lower()
    out = "CC BY"
    if "noncommercial" in t or re.search(r"[- ]nc\b", t):
        out += "-NC"
    if "noderivatives" in t or re.search(r"[- ]nd\b", t):
        out += "-ND"
    if "sharealike" in t or re.search(r"[- ]sa\b", t):
        out += "-SA"
    m = re.search(r"\d\.\d", t)
    return out + (f" {m.group(0)}" if m else "")


def _license(root: ET.Element) -> str:
    for e in root.iter():
        if _local(e.tag) != "license":
            continue
        href = e.get("{http://www.w3.org/1999/xlink}href") or e.get("href") or ""
        m = _CC_URL_RE.search(href)
        if m:
            return _cc_label("CC " + m.group(1).replace("-", " ").upper() + " " + m.group(2))
        body = " ".join(_text(p) for p in e.iter() if _local(p.tag) == "license-p")
        m = _CC_RE.search(body)
        if m:
            return _cc_label(m.group(0))
        ltype = _norm(e.get("license-type") or "")
        if ltype:
            return ltype
        if href:
            return href
    return ""


def extract(xml_text: str) -> dict:
    """从 JATS 全文 XML 抽取 {title, license, sections}。sections 为 {abstract|introduction|methods|results|discussion|other: 纯文本}。"""
    root = ET.fromstring(xml_text)
    if _local(root.tag) != "article":
        art = next((e for e in root.iter() if _local(e.tag) == "article"), None)
        if art is not None:
            root = art

    title = ""
    for e in root.iter():
        if _local(e.tag) == "article-title":
            title = _text(e)
            break

    sections: dict[str, str] = {}

    def _add(kind: str, txt: str, head: str = "") -> None:
        if kind == "skip":
            return
        txt = _norm(txt)
        head = _norm(head)
        if head and txt.startswith(head):  # 去掉与章节标题重复的开头（如「Methods Resource availability…」）
            txt = txt[len(head):].lstrip(" .:—-")
        if not txt:
            return
        sections[kind] = f"{sections[kind]}\n\n{txt}" if kind in sections else txt

    for e in root.iter():
        tag = _local(e.tag)
        if tag == "abstract" and _norm(e.get("abstract-type") or "") in ("", "abstract", "structured", "summary", "author-summary"):
            head = next((t for t in e if _local(t.tag) == "title"), None)
            _add("abstract", _text(e), _text(head) if head is not None else "")

    body = next((e for e in root.iter() if _local(e.tag) == "body"), None)
    if body is not None:
        secs = [c for c in body if _local(c.tag) == "sec"]
        for sec in secs:
            head = next((t for t in sec if _local(t.tag) == "title"), None)
            head_txt = _text(head) if head is not None else ""
            _add(classify_section(head_txt), _text(sec), head_txt)
        if not secs:
            _add("other", _text(body))

    return {"title": title, "license": _license(root), "sections": sections}


# ============================================================ 按节截断
def _allocate(sections: dict[str, str], budget: int) -> dict[str, int]:
    """按权重给每节分配字数上限；短节用不完的额度回收给长节。总和 ≤ budget。"""
    keys = [k for k in SECTION_ORDER if sections.get(k)]
    weights = {k: SECTION_WEIGHT.get(k, 1.0) for k in keys}
    caps: dict[str, int] = {}
    remaining = budget
    pending = list(keys)
    while pending:
        total_w = sum(weights[k] for k in pending) or 1.0
        fixed = [k for k in pending if len(sections[k]) <= int(remaining * weights[k] / total_w)]
        if not fixed:
            for k in pending:
                caps[k] = max(0, int(remaining * weights[k] / total_w))
            break
        for k in fixed:
            caps[k] = len(sections[k])
            remaining -= caps[k]
            pending.remove(k)
    return caps


def truncate_sections(sections: dict[str, str], max_chars: int) -> tuple[dict[str, str], list[str]]:
    """把各节正文按权重截断到合计不超过 max_chars（不含标题行）。返回 (截断后的节文本, 被截断的节名)。"""
    caps = _allocate({k: v for k, v in sections.items() if v}, max(200, int(max_chars)))
    out: dict[str, str] = {}
    cut: list[str] = []
    for k in SECTION_ORDER:
        txt = sections.get(k)
        if not txt:
            continue
        cap = caps.get(k, 0)
        if len(txt) <= cap:
            out[k] = txt
        else:
            cut.append(k)
            out[k] = txt[: max(0, cap - len(_TRUNC_MARK))].rstrip() + _TRUNC_MARK if cap > len(_TRUNC_MARK) else txt[:cap]
    return out, cut


def assemble(sections: dict[str, str], *, max_chars: int, header: str = "") -> tuple[str, list[str]]:
    """拼成给模型/页面用的全文文本（含表头），总长不超过 max_chars。返回 (文本, 被截断的节名)。"""
    heads = {k: f"## {SECTION_LABEL[k]}\n\n" for k in SECTION_ORDER if sections.get(k)}
    parts_n = len(heads) + (1 if header else 0)
    overhead = len(header) + sum(len(h) for h in heads.values()) + 2 * max(0, parts_n - 1) + 1  # +1 = 结尾换行
    kept, cut = truncate_sections(sections, max(200, int(max_chars) - overhead))
    parts = [header] if header else []
    for k in SECTION_ORDER:
        if kept.get(k):
            parts.append(f"## {SECTION_LABEL[k]}\n\n{kept[k]}")
    return "\n\n".join(parts).strip() + "\n", cut


def describe(ft: dict | None) -> str:
    """给界面/API 用的一句话：为什么是或不是全文解读。"""
    if not ft:
        return "尚未抓取全文（未入选、分数未达阈值，或还没跑 fulltext 步骤）"
    label = STATUS_LABEL.get(ft.get("status") or "", ft.get("status") or "")
    err = (ft.get("error") or "").strip()
    return f"{label}：{err}" if err else label


# ============================================================ 抓取
def _yes(v) -> bool:
    return str(v).strip().lower() in ("y", "yes", "true", "1")


def lookup(settings: Settings, *, doi: str | None = None, pmid: str | None = None, retries: int = 1) -> dict | None:
    """Europe PMC lite 检索（DOI 优先，其次 PMID）。返回 None 表示无记录。"""
    doi = normalize_doi(doi)
    pmid = (pmid or "").strip() or None
    if doi:
        query = f'DOI:"{doi}"'
    elif pmid:
        query = f"EXT_ID:{pmid} AND SRC:MED"
    else:
        return None
    r = http_get(LITE_API, settings, params={"query": query, "format": "json", "resultType": "lite", "pageSize": 1}, timeout=30, retries=retries)
    try:
        results = (r.json().get("resultList") or {}).get("result") or []
    except ValueError:
        results = []
    if not results:
        return None
    x = results[0]
    return {
        "pmcid": x.get("pmcid") or None,
        "pmid": x.get("pmid") or pmid,
        "doi": normalize_doi(x.get("doi")) or doi,
        "title": x.get("title") or "",
        "source": x.get("source") or "",
        "is_open_access": _yes(x.get("isOpenAccess")),
        "in_epmc": _yes(x.get("inEPMC")),
        "has_pdf": _yes(x.get("hasPDF")),
        "license": x.get("license") or "",
    }


def fetch_jats(settings: Settings, pmcid: str, *, retries: int = 1) -> str:
    url = FULLTEXT_API.format(pmcid=pmcid)
    return http_get(url, settings, headers={"Accept": "application/xml, text/xml"}, timeout=60, retries=retries).text


def fetch_for_item(settings: Settings, conn: sqlite3.Connection, it: dict, *, force: bool = False, max_chars: int | None = None) -> dict:
    """抓取并保存单条文献的 OA 全文。

    返回 {'ok': bool, 'status': str, 'reason': str, 'record': dict|None, 'truncated': [...]}。
    status 为 'network' 时不写库（下次运行重试），其余状态都会留下记录供界面说明原因。
    """
    item_id = int(it["id"])
    cfg = settings.selection.get("interpret") or {}
    max_chars = int(max_chars or cfg.get("fulltext_max_chars") or DEFAULT_MAX_CHARS)

    if not force:
        cur = repo.get_fulltext(conn, item_id)
        if cur and cur.get("status") == "ok":
            return {"ok": True, "status": "ok", "reason": STATUS_LABEL["ok"], "record": cur, "skipped": True, "truncated": []}

    def _fail(status: str, reason: str, pmcid: str | None = None) -> dict:
        cur = repo.get_fulltext(conn, item_id, with_text=True)
        if cur and cur.get("status") == "ok" and (cur.get("text") or "").strip():
            # 重新抓取没成功时保留已有的全文，别把已经抓到的正文抹掉
            return {"ok": True, "status": "ok", "skipped": True, "record": cur, "truncated": [],
                    "reason": f"{STATUS_LABEL['ok']}（本次重抓未成功，沿用上次记录：{STATUS_LABEL.get(status, status)} {reason}）".strip()}
        repo.save_fulltext(conn, item_id, status=status, pmcid=pmcid, source="Europe PMC", error=reason)
        return {"ok": False, "status": status, "reason": f"{STATUS_LABEL.get(status, status)}：{reason}" if reason else STATUS_LABEL.get(status, status), "record": repo.get_fulltext(conn, item_id), "truncated": []}

    if not (it.get("doi") or it.get("pmid")):
        return _fail("no_id", "")
    try:
        hit = lookup(settings, doi=it.get("doi"), pmid=it.get("pmid"))
    except FetchError as e:
        return {"ok": False, "status": NETWORK_STATUS, "reason": f"Europe PMC 查询失败：{e}", "record": None, "truncated": []}
    if not hit:
        return _fail("not_found", f"DOI {it['doi']}" if it.get("doi") else f"PMID {it.get('pmid')}")
    pmcid = hit.get("pmcid")
    if not (hit["is_open_access"] or hit["in_epmc"]):
        return _fail("non_oa", f"isOpenAccess={hit['is_open_access']}, inEPMC={hit['in_epmc']}", pmcid=pmcid)
    if not pmcid:
        return _fail("no_pmcid", "")
    try:
        xml = fetch_jats(settings, pmcid)
    except FetchError as e:
        if getattr(e, "status", None) is None:
            return {"ok": False, "status": NETWORK_STATUS, "reason": f"全文抓取失败：{e}", "record": None, "truncated": []}
        return _fail("error", str(e), pmcid=pmcid)
    try:
        extracted = extract(xml)
    except ET.ParseError as e:
        return _fail("error", f"全文 XML 解析失败：{e}", pmcid=pmcid)

    sections = extracted.get("sections") or {}
    if not any(sections.values()):
        return _fail("error", "全文 XML 中没有可用正文", pmcid=pmcid)
    license_ = extracted.get("license") or hit.get("license") or ""
    header = (
        f"【{extracted.get('title') or hit.get('title') or it.get('title') or ''}】\n"
        f"Europe PMC 开放获取全文（JATS，已去图表与参考文献）· PMCID {pmcid}"
        f" · 许可 {license_ or '未标注'} · 来源 {FULLTEXT_API.format(pmcid=pmcid)}"
    )
    text, cut = assemble(sections, max_chars=max_chars, header=header)
    repo.save_fulltext(conn, item_id, status="ok", pmcid=pmcid, license=license_, source="Europe PMC", chars=len(text), text=text)
    return {"ok": True, "status": "ok", "reason": STATUS_LABEL["ok"], "record": repo.get_fulltext(conn, item_id, with_text=True), "truncated": cut, "skipped": False}


def fetch_fulltexts(settings: Settings, conn: sqlite3.Connection, *, item_id: int | None = None, limit: int | None = None, force: bool = False, logger: Logger = log.info) -> dict:
    """为已入选/高分文献批量抓取 OA 全文。模拟模式照常（Europe PMC 不需要 key）。"""
    cfg = settings.selection.get("interpret") or {}
    out: dict = {"candidates": 0, "ok": 0, "non_oa": 0, "no_pmcid": 0, "not_found": 0, "no_id": 0, "errors": 0, "skipped": 0, "network": 0, "chars": 0, "aborted": False}
    if item_id is None and not cfg.get("fulltext_enabled", True):
        logger("[全文] interpret.fulltext_enabled=false，跳过")
        return out
    if item_id is not None:
        it = repo.get_item(conn, item_id)
        items = [it] if it else []
    else:
        items = repo.items_needing_fulltext(
            conn,
            float(cfg.get("fulltext_min_score", 60)),
            list(cfg.get("kinds") or ["paper", "preprint", "guideline"]),
            int(limit or cfg.get("fulltext_max_per_run", 40)),
            retry_days=int(cfg.get("fulltext_retry_days", 14)),
            ignore_existing=force,
        )
    out["candidates"] = len(items)
    streak = 0
    for it in items:
        r = fetch_for_item(settings, conn, it, force=force)
        if r.get("skipped"):
            out["skipped"] += 1
            continue
        status = r["status"]
        if status == NETWORK_STATUS:
            out["network"] += 1
            streak += 1
            logger(f"  ✗ 全文 #{it['id']}：{r['reason']}")
            if streak >= NETWORK_ABORT:
                out["aborted"] = True
                logger(f"[全文] Europe PMC 连续 {streak} 次无法访问，本轮中止；这些条目未落库，下次运行重试")
                break
            continue
        streak = 0
        if status == "ok":
            rec = r["record"] or {}
            out["ok"] += 1
            out["chars"] += int(rec.get("chars") or 0)
            logger(f"  ✓ 全文 #{it['id']} {rec.get('pmcid')} · {rec.get('chars')} 字符 · 许可 {rec.get('license') or '未标注'}"
                   + (f"（截断：{'、'.join(r['truncated'])}）" if r.get("truncated") else ""))
        elif status == "error":
            out["errors"] += 1
            logger(f"  ✗ 全文 #{it['id']}：{r['reason']}")
        else:
            out[status] = out.get(status, 0) + 1
            logger(f"  · 全文 #{it['id']}：{r['reason']}（回退为仅摘要解读）")
    logger(
        f"[全文] 候选 {out['candidates']}：取得 {out['ok']}（共 {out['chars']} 字符），非 OA {out['non_oa']}，无 PMCID {out['no_pmcid']}，"
        f"未找到 {out['not_found']}，无标识 {out['no_id']}，失败 {out['errors']}"
        + (f"，网络失败 {out['network']}（本轮中止）" if out["aborted"] else "")
    )
    return out

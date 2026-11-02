"""文本处理：规范化、分词、相似度、URL 规范化、时间解析。"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-\+']*")
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "rss", "spm", "from", "ncid", "cmpid"}

STOPWORDS = set(
    """a an the of and or in on for to with by from at as is are was were be been this that these those
    we our its it their into via using use used based study studies analysis results result new novel
    human humans mouse mice cell cells patients patient effect effects role between during after before
    among than within without versus vs over under not no high low can may could should would also both
    which who whom whose where when what how one two three first second large small more most less
    """.split()
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_date_loose(s: str | None) -> datetime | None:
    """尽量解析各种来源的时间字符串。"""
    if not s:
        return None
    s = s.strip()
    dt = parse_iso(s)
    if dt:
        return dt
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y %b %d", "%Y %b", "%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    return None


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return re.sub(r"\s+([.,;:!?)\]}，。；：！？）])", r"\1", text)


def truncate(text: str, n: int = 300) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """去掉追踪参数、片段、尾部斜杠，统一小写 host。"""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in _TRACKING_PARAMS)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return sha1(canonical_url(url))


def extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    m = _DOI_RE.search(text)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;)")
    return doi.lower()


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi or None


def detect_lang(text: str) -> str:
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    return "zh" if cjk >= max(3, len(text) * 0.15) else "en"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return _WS_RE.sub(" ", text).strip()


def title_fingerprint(title: str) -> str:
    """标题指纹：小写、去标点、去停用词后排序；用于去重。"""
    t = normalize_text(title)
    t = re.sub(r"[^a-z0-9一-鿿 ]+", " ", t)
    words = [w for w in t.split() if w not in STOPWORDS and len(w) > 1]
    return " ".join(sorted(words))[:400]


def tokenize(text: str) -> set[str]:
    """英文按词（去停用词），中文按双字；返回集合用于 Jaccard。"""
    t = normalize_text(text)
    tokens: set[str] = set()
    for w in _WORD_RE.findall(t):
        if w not in STOPWORDS and len(w) > 2:
            tokens.add(w)
    cjk = "".join(_CJK_RE.findall(t))
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i : i + 2])
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a) + len(b) - inter)


def first_sentences(text: str, n: int = 2, max_len: int = 400) -> str:
    text = (text or "").strip()
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    out = " ".join(parts[:n]).strip()
    return truncate(out, max_len)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def slugify(text: str) -> str:
    t = normalize_text(text)
    t = re.sub(r"[^a-z0-9一-鿿]+", "-", t).strip("-")
    return t[:80] or "x"

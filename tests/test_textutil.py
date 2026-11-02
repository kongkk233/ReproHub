from datetime import timezone

from reprohub.textutil import (
    canonical_url, detect_lang, extract_doi, jaccard, normalize_doi, parse_date_loose, strip_html, title_fingerprint, tokenize, url_hash,
)


def test_canonical_url_strips_tracking_and_fragment():
    a = canonical_url("https://www.Example.org/path/?utm_source=x&b=2&a=1#frag")
    b = canonical_url("https://example.org/path?a=1&b=2")
    assert a == b
    assert url_hash(a) == url_hash(b)


def test_doi_extraction_and_normalization():
    assert extract_doi("see https://doi.org/10.1093/humrep/deaf001.") == "10.1093/humrep/deaf001"
    assert normalize_doi("DOI: 10.1016/J.FERTNSTERT.2024.01.001") == "10.1016/j.fertnstert.2024.01.001"
    assert normalize_doi("https://dx.doi.org/10.1/abc") == "10.1/abc"
    assert normalize_doi(None) is None


def test_title_fingerprint_ignores_case_punct_and_order():
    a = title_fingerprint("Single-cell proteomics of the human blastocyst: a resource")
    b = title_fingerprint("A Resource: single cell proteomics of the Human Blastocyst!")
    assert a == b


def test_tokenize_handles_zh_and_en():
    t = tokenize("早期胚胎 proteome analysis of blastocysts")
    assert "proteome" in t and "blastocysts" in t
    assert "早期" in t and "胚胎" in t
    assert "the" not in tokenize("the of and")


def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard(set(), {"b"}) == 0.0


def test_detect_lang():
    assert detect_lang("人类早期胚胎蛋白质组研究进展") == "zh"
    assert detect_lang("Human embryo proteome") == "en"


def test_parse_date_loose():
    assert parse_date_loose("2026-11-02").tzinfo is not None
    assert parse_date_loose("2026-11-02T08:00:00Z").year == 2026
    assert parse_date_loose("Mon, 02 Nov 2026 08:00:00 GMT").astimezone(timezone.utc).hour == 8
    assert parse_date_loose("garbage") is None


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"

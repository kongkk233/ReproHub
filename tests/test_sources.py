from datetime import datetime, timezone

from reprohub.sources.crossref import _date
from reprohub.sources.jsonapi import dig
from reprohub.sources.pubmed import PubMedFetcher
from reprohub.sources.rss import RSSFetcher

PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
<PubmedArticle>
 <MedlineCitation Status="MEDLINE" Owner="NLM">
  <PMID Version="1">40000001</PMID>
  <Article PubModel="Print-Electronic">
   <Journal><ISOAbbreviation>Hum Reprod</ISOAbbreviation><Title>Human reproduction</Title>
     <JournalIssue><PubDate><Year>2026</Year><Month>Oct</Month></PubDate></JournalIssue></Journal>
   <ArticleTitle>Proteomic profiling of human <i>blastocysts</i>.</ArticleTitle>
   <ELocationID EIdType="doi" ValidYN="Y">10.1093/humrep/deaf999</ELocationID>
   <Abstract><AbstractText Label="STUDY QUESTION">Does it work?</AbstractText><AbstractText Label="SUMMARY ANSWER">Yes.</AbstractText></Abstract>
   <AuthorList><Author><LastName>Zhang</LastName><Initials>Y</Initials></Author><Author><CollectiveName>Embryo Consortium</CollectiveName></Author></AuthorList>
   <ArticleDate DateType="Electronic"><Year>2026</Year><Month>10</Month><Day>28</Day></ArticleDate>
   <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
  </Article>
 </MedlineCitation>
</PubmedArticle>
</PubmedArticleSet>"""

RSS_XML = b"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Fertility and Sterility</title>
<item><title>Freeze-all &amp; live birth</title><link>https://www.fertstert.org/article/S0015-0282(26)00001-1/fulltext?rss=yes</link>
<description>&lt;p&gt;Cumulative live birth after &lt;b&gt;freeze-all&lt;/b&gt;.&lt;/p&gt;</description><pubDate>Mon, 02 Nov 2026 08:00:00 GMT</pubDate>
<dc:identifier>doi:10.1016/j.fertnstert.2026.01.001</dc:identifier><dc:creator>Doe J</dc:creator></item>
</channel></rss>"""


PUBMED_ISSUE_ONLY_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle>
 <MedlineCitation><PMID>40000002</PMID>
  <Article><Journal><ISOAbbreviation>Zhonghua Yi Xue Yi Chuan Xue Za Zhi</ISOAbbreviation>
   <JournalIssue><PubDate><Year>2026</Year><Month>Nov</Month><Day>10</Day></PubDate></JournalIssue></Journal>
   <ArticleTitle>Predictive value of maternal age for euploid blastocysts</ArticleTitle></Article>
 </MedlineCitation>
 <PubmedData><History>
  <PubMedPubDate PubStatus="received"><Year>2026</Year><Month>8</Month><Day>9</Day></PubMedPubDate>
  <PubMedPubDate PubStatus="pubmed"><Year>2026</Year><Month>11</Month><Day>7</Day></PubMedPubDate>
 </History></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


def test_pubmed_prefers_pubmed_date_over_future_issue_date(settings):
    f = PubMedFetcher({"key": "pubmed-x", "params": {}}, settings)
    it = f._parse(PUBMED_ISSUE_ONLY_XML)[0]
    assert it.published_at == datetime(2026, 11, 7, tzinfo=timezone.utc)


def test_pubmed_xml_parsing(settings):
    f = PubMedFetcher({"key": "pubmed-x", "params": {}}, settings)
    items = f._parse(PUBMED_XML)
    assert len(items) == 1
    it = items[0]
    assert it.pmid == "40000001" and it.doi == "10.1093/humrep/deaf999"
    assert it.title == "Proteomic profiling of human blastocysts"
    assert "STUDY QUESTION: Does it work?" in it.summary
    assert it.authors == ["Zhang Y", "Embryo Consortium"]
    assert it.published_at == datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert it.url.endswith("/40000001/") and it.kind == "paper"


def test_rss_parsing(settings, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, content=RSS_XML)

    real = httpx.Client

    class Patched(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", Patched)
    f = RSSFetcher({"key": "rss-x", "name": "X", "params": {"url": "https://example.org/feed"}}, settings)
    items = f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(items) == 1
    it = items[0]
    assert it.title == "Freeze-all & live birth"
    assert it.doi == "10.1016/j.fertnstert.2026.01.001"
    assert it.summary == "Cumulative live birth after freeze-all."
    assert it.journal == "Fertility and Sterility" and it.authors == ["Doe J"]
    assert it.published_at.hour == 8


def test_rss_requires_url(settings):
    import pytest

    from reprohub.sources import FetchError

    f = RSSFetcher({"key": "rss-x", "params": {"url": "/sciencenet/news"}}, settings)
    with pytest.raises(FetchError):
        f.fetch(datetime.now(timezone.utc))


# Cell Press In Press（RDF）：全部作者挤在一个 dc:creator，DOI 在 dc:identifier，日期在 dc:date
CELL_RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/1.2/basic/">
<channel rdf:about="https://www.cell.com/cell/inpress.rss"><title>Cell</title><link>https://www.cell.com/cell/inpress</link></channel>
<item rdf:about="https://www.cell.com/cell/fulltext/S0092-8674(26)01265-1?rss=yes">
<title>Single-cell multi-omics of human blastocysts</title>
<link>https://www.cell.com/cell/fulltext/S0092-8674(26)01265-1?rss=yes</link>
<description>Integration of transcriptome and proteome of human blastocysts reveals lineage programs.</description>
<dc:creator>Mathilde Poyet, Malte Rühlemann, Eric J. Alm</dc:creator>
<dc:identifier>10.1016/j.cell.2026.10.016</dc:identifier>
<dc:date>2026-11-02</dc:date>
</item></rdf:RDF>""".encode()

# Science First Release（RSS 2.0）：标题带 " | Science" 后缀，DOI 只在链接里，订阅标题不是刊名
SCIENCE_AOP = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel><title>Science First Release</title>
<item><title>Oocyte proteostasis sets the embryo clock | Science</title>
<link>https://www.science.org/doi/10.1126/science.adv9181</link>
<description>Mammalian oocytes maintain a proteome for decades.</description>
<pubDate>Thu, 29 Oct 2026 09:01:00 -0400</pubDate></item>
</channel></rss>"""

# nature.com 主题源：只有标题、链接、日期；刊名未知，DOI 由文章号推出
NATURE_SUBJECT = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Reproductive biology : nature.com subject feeds</title>
<item><title>Myristic acid promotes in vitro maturation of porcine oocytes</title>
<link>https://www.nature.com/articles/s42003-026-11222-8</link>
<pubDate>Thu, 29 Oct 2026 00:00:00 +0000</pubDate></item>
<item><title>Legacy identifier still maps</title><link>https://www.nature.com/articles/nmeth.2019?error=cookies</link></item>
</channel></rss>"""


def _rss(monkeypatch, settings, body: bytes, params: dict):
    import httpx

    real = httpx.Client

    class Patched(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", Patched)
    return RSSFetcher({"key": "rss-x", "name": "X", "params": {"url": "https://example.org/feed", **params}}, settings).fetch(datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_rss_cell_press_splits_authors_and_reads_dc_identifier(settings, monkeypatch):
    (it,) = _rss(monkeypatch, settings, CELL_RDF, {})
    assert it.journal == "Cell" and it.doi == "10.1016/j.cell.2026.10.016"
    assert it.authors == ["Mathilde Poyet", "Malte Rühlemann", "Eric J. Alm"]
    assert it.published_at == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert it.summary.startswith("Integration of transcriptome")


def test_rss_explicit_journal_strips_title_suffix_and_takes_doi_from_link(settings, monkeypatch):
    (it,) = _rss(monkeypatch, settings, SCIENCE_AOP, {"journal": "Science"})
    assert it.title == "Oocyte proteostasis sets the embryo clock"
    assert it.journal == "Science" and it.doi == "10.1126/science.adv9181"


def test_rss_aggregate_feed_leaves_journal_unknown_but_derives_nature_doi(settings, monkeypatch):
    a, b = _rss(monkeypatch, settings, NATURE_SUBJECT, {"aggregate": True})
    assert a.journal is None and b.journal is None
    assert a.doi == "10.1038/s42003-026-11222-8"
    assert b.doi == "10.1038/nmeth.2019"
    # 不标 aggregate 时沿用订阅标题（旧行为；strip_html 会把 " : " 收成 ": "）
    (c, _) = _rss(monkeypatch, settings, NATURE_SUBJECT, {})
    assert c.journal == "Reproductive biology: nature.com subject feeds"


def test_rss_author_split_is_conservative():
    from reprohub.sources.rss import _split_authors

    assert _split_authors(["Smith, J."]) == ["Smith, J."]  # 「姓, 名缩写」不拆
    assert _split_authors(["Jane Doe, PhD"]) == ["Jane Doe, PhD"]
    assert _split_authors(["Jane Doe, John Smith"]) == ["Jane Doe", "John Smith"]
    assert _split_authors(["A", "B"]) == ["A", "B"]  # 多个 dc:creator 原样保留


def test_crossref_date_and_jsonapi_dig():
    assert _date({"published-online": {"date-parts": [[2026, 11]]}}) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert _date({"created": {"date-parts": [[None]]}}) is None
    assert dig({"data": {"items": [{"t": 1}]}}, "data.items.0.t") == 1
    assert dig({"a": 1}, "b.c") is None

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


def test_crossref_date_and_jsonapi_dig():
    assert _date({"published-online": {"date-parts": [[2026, 11]]}}) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert _date({"created": {"date-parts": [[None]]}}) is None
    assert dig({"data": {"items": [{"t": 1}]}}, "data.items.0.t") == 1
    assert dig({"a": 1}, "b.c") is None

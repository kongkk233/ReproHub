"""OA 全文解读：抽取 / 截断 / basis 写入 / 非 OA 与无 PMCID 回退。全部离线。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from reprohub import repo
from reprohub.fulltext import (
    LITE_API,
    _cc_label,
    assemble,
    classify_section,
    extract,
    fetch_for_item,
    fetch_fulltexts,
    lookup,
    truncate_sections,
)
from reprohub.llm import LLM
from reprohub.pipeline.collect import ingest_item
from reprohub.pipeline.interpret import interpret_item
from reprohub.pipeline.score import process_new
from reprohub.sources import RawItem
from reprohub.web.app import create_app

# 一段假的 JATS：含图表（不该抽）、参考文献（不该抽）、结构化摘要与四个正文节
FAKE_JATS = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">
<front><journal-meta><journal-title-group><journal-title>Nature Communications</journal-title></journal-title-group></journal-meta>
<article-meta>
 <title-group><article-title>Single-cell proteomics of human blastocysts</article-title></title-group>
 <abstract><title>Abstract</title><p>We profiled human blastocysts by low-input mass spectrometry.</p></abstract>
 <permissions><copyright-statement>© The Author(s) 2026</copyright-statement>
  <license xlink:href="https://creativecommons.org/licenses/by/4.0/"><license-p><bold>Open Access</bold> This article is licensed under a Creative Commons Attribution 4.0 International License.</license-p></license>
 </permissions>
 <author-notes><corresp>Correspondence must not appear.</corresp></author-notes>
</article-meta></front>
<body>
 <sec><title>Introduction</title><p>Embryo proteomics is limited <xref ref-type="bibr" rid="b1">1</xref> and mostly transcript-based.</p></sec>
 <sec><title>Materials and methods</title><p>We collected 96 blastocysts and used mass spectrometry.</p>
   <sec><title>Statistical analysis</title><p>Two-tailed tests were used.</p></sec></sec>
 <sec><title>Results</title><p>We quantified 4,812 protein groups across stages.</p>
   <fig id="F1"><label>Fig. 1</label><caption><p>FIGURE CAPTION MUST NOT APPEAR.</p></caption></fig>
   <table-wrap><caption><p>TABLE MUST NOT APPEAR.</p></caption></table-wrap></sec>
 <sec><title>Discussion</title><p>Post-transcriptional control shapes the early embryo.</p></sec>
 <sec><title>Acknowledgements</title><p>We thank the reviewers.</p></sec>
</body>
<back><ref-list><ref id="b1"><mixed-citation>Smith et al. Nature 2026.</mixed-citation></ref></ref-list></back>
</article>"""

LITE_OA = {"resultList": {"result": [{
    "id": "40000001", "source": "MED", "pmid": "40000001", "pmcid": "PMC1234567", "doi": "10.1038/s41467-026-00001-1",
    "title": "Single-cell proteomics of human blastocysts", "isOpenAccess": "Y", "inEPMC": "Y", "hasPDF": "Y",
}]}}
LITE_NON_OA = {"resultList": {"result": [{
    "id": "40000002", "source": "MED", "pmid": "40000002", "pmcid": "PMC7654321", "doi": "10.1038/s41586-026-99999-9",
    "title": "Base editing in human embryos", "isOpenAccess": "N", "inEPMC": "N", "hasPDF": "N",
}]}}
LITE_NO_PMCID = {"resultList": {"result": [{
    "id": "40000003", "source": "MED", "pmid": "40000003", "doi": "10.1000/xyz", "title": "Oocyte proteome",
    "isOpenAccess": "Y", "inEPMC": "Y", "hasPDF": "N",
}]}}


_REAL_CLIENT = httpx.Client  # 记住原类：同一测试里反复打补丁时要基于它，而不是基于上一次的补丁


def _mock_europepmc(monkeypatch, *, lite: dict | None, jats: str = FAKE_JATS, jats_status: int = 200):
    """把 Europe PMC 的两个接口都接上假响应。lite=None 表示检索无结果。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "/fullTextXML" in str(request.url):
            return httpx.Response(jats_status, text=jats if jats_status == 200 else "not found")
        assert "europepmc" in str(request.url)
        return httpx.Response(200, json=lite if lite is not None else {"resultList": {}})

    class Patched(_REAL_CLIENT):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", Patched)


def _item(conn, settings, doi="10.1038/s41467-026-00001-1", title="Single-cell proteomics of human blastocysts", pmid=None, selected=True):
    iid, _ = ingest_item(conn, settings, "ingest", RawItem(url=f"https://doi.org/{doi}", title=title, summary="Proteome of human blastocysts by mass spectrometry.", doi=doi, pmid=pmid, kind="paper"))
    repo.update_item(conn, iid, status="scored", selected=1 if selected else 0, final_score=90.0, in_scope=1, kind="paper")
    return repo.get_item(conn, iid)


# ---------------------------------------------------------------- 抽取与截断
def test_extract_jats_sections_license_and_drops_figures_references():
    e = extract(FAKE_JATS)
    s = e["sections"]
    assert e["title"] == "Single-cell proteomics of human blastocysts"
    assert e["license"] == "CC BY 4.0"
    assert set(s) == {"abstract", "introduction", "methods", "results", "discussion"}
    assert s["abstract"] == "We profiled human blastocysts by low-input mass spectrometry."
    assert "4,812 protein groups" in s["results"]
    assert "Statistical analysis" in s["methods"] and "Two-tailed tests" in s["methods"]  # 子节并入 methods
    assert "FIGURE CAPTION" not in json.dumps(s) and "TABLE MUST NOT APPEAR" not in json.dumps(s)
    assert "Smith et al" not in json.dumps(s) and "b1" not in json.dumps(s)  # 参考文献与引用标记都不抽
    assert "Acknowledgements" not in json.dumps(s) and "thank the reviewers" not in json.dumps(s)
    assert "Correspondence must not appear" not in json.dumps(s)


def test_classify_section_and_license_labels():
    assert classify_section("Materials and Methods") == "methods"
    assert classify_section("Results and discussion") == "results"
    assert classify_section("Limitations of the study") == "discussion"
    assert classify_section("Background") == "introduction"
    assert classify_section("Data availability") == "other"
    assert classify_section("Acknowledgements") == "skip"
    assert classify_section("") == "other"
    assert _cc_label("Creative Commons Attribution 4.0 International License") == "CC BY 4.0"
    assert _cc_label("CC BY-NC-ND 3.0") == "CC BY-NC-ND 3.0"


def test_truncate_keeps_methods_and_results_and_stays_within_budget():
    sections = {
        "abstract": "A" * 400,
        "introduction": "I" * 4000,
        "methods": "M" * 20000,
        "results": "R" * 20000,
        "discussion": "D" * 8000,
    }
    kept, cut = truncate_sections(sections, 12000)
    assert "methods" in cut and "results" in cut
    assert sum(len(v) for v in kept.values()) <= 12000
    # 权重相同的方法与结果拿到相同的额度，且明显多于引言
    assert len(kept["methods"]) == len(kept["results"])
    assert len(kept["methods"]) > len(kept["introduction"])
    assert kept["methods"].endswith("…（本节已截断）")

    text, cut2 = assemble(sections, max_chars=5000, header="【标题】\n来源：Europe PMC")
    assert len(text) <= 5000 and cut2
    assert text.startswith("【标题】") and "## Methods 方法" in text and "## Results 结果" in text


def test_truncate_keeps_short_sections_whole():
    sections = {"abstract": "short abstract", "methods": "M" * 5000, "results": "R" * 50}
    kept, cut = truncate_sections(sections, 3000)
    assert kept["abstract"] == "short abstract" and kept["results"] == "R" * 50
    assert cut == ["methods"]


# ---------------------------------------------------------------- Europe PMC 查询
def test_lookup_parses_lite_and_builds_doi_query(settings, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=LITE_OA)

    real = _REAL_CLIENT

    class Patched(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "Client", Patched)
    hit = lookup(settings, doi="https://doi.org/10.1038/S41467-026-00001-1")
    assert hit["pmcid"] == "PMC1234567" and hit["is_open_access"] and hit["in_epmc"]
    assert "DOI" in seen["url"] and LITE_API.split("/")[-1] in seen["url"]
    assert lookup(settings, doi=None, pmid=None) is None  # 没有标识就不查


def test_fetch_for_item_saves_oa_fulltext(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    it = _item(conn, settings)
    r = fetch_for_item(settings, conn, it)
    assert r["ok"] and r["status"] == "ok" and not r["truncated"]
    ft = repo.get_fulltext(conn, it["id"], with_text=True)
    assert ft["pmcid"] == "PMC1234567" and ft["license"] == "CC BY 4.0" and ft["source"] == "Europe PMC"
    assert ft["chars"] == len(ft["text"])
    assert "## Methods 方法" in ft["text"] and "4,812 protein groups" in ft["text"]
    assert "FIGURE CAPTION" not in ft["text"] and "Smith et al" not in ft["text"]
    assert repo.get_item(conn, it["id"])["fulltext_status"] == "ok"
    # 已有全文时默认不再请求（skipped），force 才重抓
    assert fetch_for_item(settings, conn, it)["skipped"] is True


def test_fetch_for_item_truncates_long_fulltext(settings, conn, monkeypatch):
    big = FAKE_JATS.replace("<p>We quantified 4,812 protein groups across stages.</p>", "<p>" + "R" * 40000 + "</p>")
    _mock_europepmc(monkeypatch, lite=LITE_OA, jats=big)
    it = _item(conn, settings)
    r = fetch_for_item(settings, conn, it, max_chars=8000)
    ft = repo.get_fulltext(conn, it["id"], with_text=True)
    assert r["ok"] and ft["chars"] <= 8000 and ft["chars"] == len(ft["text"])
    assert "results" in r["truncated"]
    assert "We collected 96 blastocysts" in ft["text"]  # methods 优先保留
    assert ft["text"].count("…（本节已截断）") == len(r["truncated"])


@pytest.mark.parametrize(
    "lite,status,keyword",
    [
        (LITE_NON_OA, "non_oa", "未收录免费全文"),
        (LITE_NO_PMCID, "no_pmcid", "无 PMCID"),
        (None, "not_found", "无此记录"),
    ],
)
def test_fetch_for_item_records_why_no_fulltext(settings, conn, monkeypatch, lite, status, keyword):
    _mock_europepmc(monkeypatch, lite=lite)
    it = _item(conn, settings, doi="10.1038/s41586-026-99999-9", title="Base editing in human embryos")
    r = fetch_for_item(settings, conn, it)
    assert not r["ok"] and r["status"] == status and keyword in r["reason"]
    ft = repo.get_fulltext(conn, it["id"], with_text=True)
    assert ft["status"] == status and not (ft["text"] or "").strip() and ft["chars"] == 0
    assert repo.get_item(conn, it["id"])["fulltext_status"] == status


def test_refetch_failure_keeps_previous_fulltext(settings, conn, monkeypatch):
    """重新抓取失败时不能把已经抓到的全文抹掉（条目页「重新抓取全文」按钮会走到这里）。"""
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    it = _item(conn, settings)
    assert fetch_for_item(settings, conn, it)["ok"]
    before = repo.get_fulltext(conn, it["id"], with_text=True)
    _mock_europepmc(monkeypatch, lite=LITE_OA, jats_status=404)
    r = fetch_for_item(settings, conn, it, force=True)
    assert r["ok"] and "沿用上次记录" in r["reason"]
    after = repo.get_fulltext(conn, it["id"], with_text=True)
    assert after["status"] == "ok" and after["text"] == before["text"] and after["chars"] == before["chars"]


def test_fetch_for_item_without_identifiers(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    iid, _ = ingest_item(conn, settings, "ingest", RawItem(url="https://example.org/noid", title="Oocyte proteome without identifiers", summary="x", kind="paper"))
    repo.update_item(conn, iid, status="scored", selected=1, final_score=90.0, in_scope=1, kind="paper")
    r = fetch_for_item(settings, conn, repo.get_item(conn, iid))
    assert r["status"] == "no_id" and "DOI" in r["reason"]


def test_fetch_fulltexts_batch_limits_to_selected_and_high_score(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    good = _item(conn, settings)
    low = _item(conn, settings, doi="10.1000/low", title="Oocyte proteome of aged mice by mass spectrometry")
    repo.update_item(conn, low["id"], final_score=40.0)
    not_selected = _item(conn, settings, doi="10.1000/ns", title="Blastocyst proteome without selection")
    repo.update_item(conn, not_selected["id"], selected=0)
    out = fetch_fulltexts(settings, conn, logger=lambda m: None)
    assert out["candidates"] == 1 and out["ok"] == 1
    assert repo.get_fulltext(conn, good["id"])["status"] == "ok"
    assert repo.get_fulltext(conn, low["id"]) is None and repo.get_fulltext(conn, not_selected["id"]) is None
    # 已抓到的条目不会重复进入候选
    assert fetch_fulltexts(settings, conn, logger=lambda m: None)["candidates"] == 0
    # force 时不再看已有记录（CLI --force / 后台「重新抓取」）
    forced = fetch_fulltexts(settings, conn, force=True, limit=1, logger=lambda m: None)
    assert forced["candidates"] == 1 and forced["ok"] == 1
    assert repo.fulltext_stats(conn)["available"] == 1


def test_fetch_fulltexts_aborts_when_europepmc_unreachable(settings, conn, seeded, no_network):
    """网络整体不可达时不要逐条重试：本轮中止，且不落库（下次运行还能重试）。"""
    process_new(settings, conn, LLM(settings, conn), logger=lambda m: None)
    out = fetch_fulltexts(settings, conn, limit=10, logger=lambda m: None)
    assert out["candidates"] >= 2 and out["aborted"] and out["network"] >= 2 and out["ok"] == 0
    assert "ok" not in repo.fulltext_stats(conn)["by_status"]


def test_fetch_fulltexts_can_be_disabled(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    monkeypatch.setitem(settings.selection, "interpret", {"fulltext_enabled": False})
    out = fetch_fulltexts(settings, conn, logger=lambda m: None)
    assert out["candidates"] == 0 and repo.fulltext_stats(conn)["by_status"] == {}


# ---------------------------------------------------------------- 解读依据
def test_interpret_marks_fulltext_basis_and_uses_fulltext_material(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    it = _item(conn, settings)
    fetch_for_item(settings, conn, it)
    llm = LLM(settings, conn)
    r = interpret_item(settings, conn, llm, repo.get_item(conn, it["id"]), force=True)
    assert r["basis"] == "fulltext"
    assert "从 Europe PMC OA 全文中抽取的句子" in r["markdown"]
    assert "Figure 1" not in r["markdown"]  # 图表没有进入材料
    # 提示词里带上了依据说明与全文（正文材料来自 JATS，图表没有进来）
    prompt = llm.render("interpret", _prompt_vars(settings, conn, repo.get_item(conn, it["id"])))
    assert "【解读依据】全文" in prompt and "We collected 96 blastocysts" in prompt and "只能依据摘要作答" not in prompt
    assert "FIGURE CAPTION" not in prompt


def _prompt_vars(settings, conn, it):
    """复刻 interpret_item 的变量（供断言提示词内容）。"""
    from reprohub.fulltext import describe as fulltext_describe
    from reprohub.pipeline.interpret import BASIS_LABEL, material_for
    from reprohub.pipeline.score import _variables

    basis, ft, material = material_for(conn, it)
    v = _variables(settings, it)
    v["basis"] = basis
    v["basis_label"] = BASIS_LABEL[basis]
    v["fulltext_note"] = fulltext_describe(ft)
    if material:
        v["summary"] = material
    return v


def test_interpret_falls_back_to_abstract_and_says_why(settings, conn, monkeypatch):
    _mock_europepmc(monkeypatch, lite=LITE_NON_OA)
    it = _item(conn, settings, doi="10.1038/s41586-026-99999-9", title="Base editing in human embryos")
    fetch_for_item(settings, conn, it)
    llm = LLM(settings, conn)
    r = interpret_item(settings, conn, llm, repo.get_item(conn, it["id"]), force=True)
    assert r["basis"] == "abstract"
    assert "从原文摘要抽取的句子" in r["markdown"]
    assert "本次未取得 OA 全文" in r["markdown"]
    prompt = llm.render("interpret", _prompt_vars(settings, conn, repo.get_item(conn, it["id"])))
    assert "只能依据摘要作答" in prompt and "未收录免费全文" in prompt


def test_interpret_without_fulltext_row_is_abstract(settings, conn):
    it = _item(conn, settings)
    r = interpret_item(settings, conn, LLM(settings, conn), it, force=True)
    assert r["basis"] == "abstract" and repo.get_fulltext(conn, it["id"]) is None


def test_fulltext_upgrades_existing_abstract_interpretation(settings, conn, monkeypatch):
    """此前只有摘要的真实解读，拿到全文后应重做（而不是永久停留在摘要版）。"""
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    it = _item(conn, settings)
    repo.save_interpretation(conn, it["id"], {"one_liner": "摘要版"}, "abstract md", provider="deepseek", model="deepseek-chat", is_mock=False, basis="abstract")
    assert repo.get_interpretation(conn, it["id"])["basis"] == "abstract"
    # 还没有全文时不动它
    assert interpret_item(settings, conn, LLM(settings, conn), repo.get_item(conn, it["id"]))["markdown"] == "abstract md"
    fetch_for_item(settings, conn, repo.get_item(conn, it["id"]))
    assert repo.items_needing_interpretation(conn, 72, ["paper"], 10)  # 重新进入候选，供 auto_interpret 重做
    assert interpret_item(settings, conn, LLM(settings, conn), repo.get_item(conn, it["id"]))["basis"] == "fulltext"


def test_fulltext_upgrades_mock_interpretation_too(settings, conn, monkeypatch):
    """模拟解读同样升级：先按摘要生成，拿到全文后重做，不再停留在摘要版。"""
    it = _item(conn, settings)
    llm = LLM(settings, conn)
    assert interpret_item(settings, conn, llm, it)["basis"] == "abstract"
    _mock_europepmc(monkeypatch, lite=LITE_OA)
    fetch_for_item(settings, conn, repo.get_item(conn, it["id"]))
    r = interpret_item(settings, conn, llm, repo.get_item(conn, it["id"]))
    assert r["basis"] == "fulltext" and r["is_mock"] == 1 and "从 Europe PMC OA 全文中抽取" in r["markdown"]


# ---------------------------------------------------------------- 界面与出口
@pytest.fixture()
def client(settings, seeded):
    process_new(settings, seeded, LLM(settings, seeded), logger=lambda m: None)
    return TestClient(create_app(settings))


def test_item_page_api_and_rss_show_basis(client, settings):
    from reprohub.db import get_conn

    conn = get_conn(settings)
    iid = [x["id"] for x in repo.list_items(conn, selected=True, page_size=5)[0]][0]
    repo.save_fulltext(conn, iid, status="ok", pmcid="PMC1234567", license="CC BY 4.0", source="Europe PMC", chars=120, text="【X】\n## Methods 方法\n\nWe used mass spectrometry.")
    r = client.post(f"/api/v1/items/{iid}/interpret", params={"force": True})
    assert r.status_code == 200
    d = client.get(f"/api/v1/items/{iid}").json()
    assert d["interpretation"]["basis"] == "fulltext" and d["fulltext"]["available"] is True
    assert d["fulltext"]["pmcid"] == "PMC1234567" and d["fulltext"]["license"] == "CC BY 4.0"
    assert d["fulltext_status"] == "ok"
    page = client.get(f"/items/{iid}")
    assert "全文解读" in page.text and "Europe PMC OA 全文" in page.text

    # 非 OA：页面与 API 都要说明原因
    other = [x["id"] for x in repo.list_items(conn, selected=True, page_size=5)[0] if x["id"] != iid][0]
    repo.save_fulltext(conn, other, status="non_oa", pmcid="PMC999", source="Europe PMC", error="isOpenAccess=False, inEPMC=False")
    client.post(f"/api/v1/items/{other}/interpret", params={"force": True})
    d2 = client.get(f"/api/v1/items/{other}").json()
    assert d2["fulltext"]["available"] is False and d2["interpretation"]["basis"] == "abstract"
    assert "未收录免费全文" in d2["fulltext"]["reason"] and "isOpenAccess=False" in d2["fulltext"]["reason"]
    page2 = client.get(f"/items/{other}")
    assert "仅摘要解读" in page2.text and "未收录免费全文" in page2.text
    assert "仅摘要解读（未取得全文）" in client.get("/feed.xml").text

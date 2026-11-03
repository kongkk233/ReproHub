from datetime import datetime, timezone

from reprohub import repo
from reprohub.llm import LLM
from reprohub.pipeline.cluster import cluster_recent
from reprohub.pipeline.collect import ingest_item
from reprohub.pipeline.editions import build_daily, build_weekly
from reprohub.pipeline.interpret import auto_interpret, interpret_item
from reprohub.pipeline.runner import run_pipeline
from reprohub.pipeline.score import process_new
from reprohub.push.dispatcher import push_edition, push_items
from reprohub.sources import RawItem


def test_dedupe_by_doi_and_url(settings, conn):
    raw = RawItem(url="https://example.org/a?utm_source=x", title="Blastocyst proteome study", summary="Human blastocyst proteomics by mass spectrometry.", doi="10.1/ABC")
    iid, st = ingest_item(conn, settings, "demo", raw)
    assert st == "new"
    _, st2 = ingest_item(conn, settings, "ingest", RawItem(url="https://other.org/b", title="Different title", summary="", doi="10.1/abc"))
    assert st2 == "duplicate"
    _, st3 = ingest_item(conn, settings, "ingest", RawItem(url="https://www.example.org/a", title="x", summary=""))
    assert st3 == "duplicate"
    it = repo.get_item(conn, iid)
    assert it["mention_count"] == 2  # demo + ingest（同一来源只计一次）


def test_scope_filter_drops_out_of_scope_for_general_sources(settings, conn):
    raw = RawItem(url="https://example.org/cardio", title="Cardiac fibrosis after infarction", summary="Mouse heart single-cell atlas.")
    # biorxiv-devbio 配置了 scope_filter
    iid, st = ingest_item(conn, settings, "biorxiv-devbio", raw)
    assert st == "dropped" and iid is None


def test_duplicate_merge_fills_missing_fields_and_upgrades_journal_tier(settings, conn):
    doi = "10.1038/s41586-026-00001-1"
    title = "Single-cell proteomic landscape of human preimplantation embryos"
    # 主题源先到：只有标题与链接，刊名未知、无摘要
    iid, st = ingest_item(conn, settings, "rss-nature-reprobio", RawItem(url=f"https://www.nature.com/articles/{doi[8:]}", title=title, summary="", doi=doi))
    assert st == "new"
    before = repo.get_item(conn, iid)
    assert before["journal"] is None and before["journal_tier"] == "unknown" and not before["summary"] and not before["authors"]
    # PubMed 后到：同一 DOI，带摘要、PMID、作者、刊名 → 合并为重复并补全，等级随之定为 CNS
    _, st2 = ingest_item(
        conn, settings, "pubmed-embryo-omics",
        RawItem(url="https://pubmed.ncbi.nlm.nih.gov/40000002/", title=title, summary="Proteome of 1,000 single blastomeres.", doi=doi, pmid="40000002", journal="Nature", authors=["Zhang Y"], extra={"issn": "0028-0836"}),
    )
    assert st2 == "duplicate"
    after = repo.get_item(conn, iid)
    assert after["pmid"] == "40000002" and after["summary"].startswith("Proteome") and after["authors"] == ["Zhang Y"]
    assert after["journal"] == "Nature" and after["journal_tier"] == "cns" and after["journal_key"] == "nature"
    # 刊名存在但定不了级（订阅名）时，也被能定级的来源纠正；已定级的刊名不会被覆盖
    jid, _ = ingest_item(conn, settings, "ingest", RawItem(url="https://example.org/j", title="Oocyte aging proteome across the reproductive lifespan", summary="", journal="Reproductive biology: nature.com subject feeds"))
    assert repo.get_item(conn, jid)["journal_tier"] == "unknown"
    ingest_item(conn, settings, "ingest", RawItem(url="https://example.org/j2", title="Oocyte aging proteome across the reproductive lifespan", summary="", journal="Nat Aging"))
    ingest_item(conn, settings, "ingest", RawItem(url="https://example.org/j3", title="Oocyte aging proteome across the reproductive lifespan", summary="", journal="Cell"))
    fixed = repo.get_item(conn, jid)
    assert fixed["journal"] == "Nat Aging" and fixed["journal_tier"] == "cns-sub"


def test_full_offline_pipeline_marks_everything_mock(settings, seeded):
    conn = seeded
    llm = LLM(settings, conn)
    r = process_new(settings, conn, llm, logger=lambda m: None)
    assert r["scored"] >= 10 and r["mock"] == r["scored"]
    sel, _ = repo.list_items(conn, selected=True, page_size=100)
    assert sel, "领域内文献应有入选"
    assert all(x["llm_is_mock"] for x in sel)
    # 偏好只加权：多组学胚胎文章排第一，但普通 RCT 也入选
    top = repo.list_items(conn, selected=True, order="score", page_size=1)[0][0]
    assert top["primary_topic"] == "early-embryo" and top["preference_score"] > 0
    assert any(x["primary_topic"] == "art-clinical" for x in sel)

    i = auto_interpret(settings, conn, llm, logger=lambda m: None)
    assert i["done"] >= 1 and i["mock"] == i["done"]
    interp = repo.get_interpretation(conn, top["id"])
    assert interp["is_mock"] == 1 and "模拟解读" in interp["markdown"]

    c = cluster_recent(settings, conn, llm, logger=lambda m: None)
    assert c["created"] >= 1
    stories = repo.list_stories(conn, limit=5)
    assert stories and stories[0]["heat"] > 0

    ed = build_daily(settings, conn, llm, logger=lambda m: None)
    assert ed["item_count"] == len(sel) and ed["is_mock"] == 0  # 日报是规则编纂，不算模拟
    assert ed["content"]["partial"] is True
    wk = build_weekly(settings, conn, llm, logger=lambda m: None)
    assert wk["is_mock"] == 1 and "模拟" in wk["overview"]


def test_interpret_on_demand_and_no_overwrite_of_real(settings, seeded):
    conn = seeded
    llm = LLM(settings, conn)
    process_new(settings, conn, llm, logger=lambda m: None)
    it = repo.list_items(conn, selected=True, page_size=1)[0][0]
    r1 = interpret_item(settings, conn, llm, it)
    assert r1["is_mock"] == 1
    # 伪造一条真实解读后，模拟提供商不应覆盖它
    repo.save_interpretation(conn, it["id"], {"one_liner": "real"}, "real md", provider="deepseek", model="deepseek-chat", is_mock=False)
    r2 = interpret_item(settings, conn, llm, it)
    assert r2["is_mock"] == 0 and r2["markdown"] == "real md"


def test_push_rules_and_file_channel(settings, seeded):
    conn = seeded
    llm = LLM(settings, conn)
    r = process_new(settings, conn, llm, logger=lambda m: None)
    out = push_items(settings, conn, r["selected_ids"], logger=lambda m: None)
    assert out["sent"] >= 1 and "embryo-omics-alert" in out["by_rule"]
    files = list((settings.data_dir / "outbox").glob("*.md"))
    assert files and any("embryo-omics-alert" in f.name for f in files)
    # 第二次不重复推送
    assert push_items(settings, conn, r["selected_ids"], logger=lambda m: None)["candidates"] == 0
    ed = build_daily(settings, conn, llm, logger=lambda m: None)
    assert push_edition(settings, conn, ed, logger=lambda m: None)["sent"] == 1
    assert push_edition(settings, conn, repo.get_edition_by_id(conn, ed["id"]), logger=lambda m: None)["sent"] == 0
    assert all(p["status"] == "ok" for p in repo.recent_pushes(conn))


def test_run_pipeline_without_network(settings, seeded, no_network):
    conn = seeded
    s = run_pipeline(settings, conn, do_collect=False, logger=lambda m: None)
    assert "error" not in s
    assert s["llm"] == "mock" and s["process"]["selected"] >= 1 and s["daily"]["items"] >= 1
    runs = repo.recent_runs(conn, 1)
    assert runs[0]["status"] == "ok"


def test_collect_survives_source_errors(settings, conn, no_network):
    from reprohub.pipeline.collect import collect

    s = collect(settings, conn, source_keys=["rss-fertstert"], force=True, logger=lambda m: None)
    assert s["sources"] == 1 and len(s["errors"]) == 1
    src = repo.get_source(conn, "rss-fertstert")
    assert src["last_status"] == "error" and src["last_error"]

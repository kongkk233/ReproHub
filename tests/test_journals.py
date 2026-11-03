"""期刊注册表：等级、影响因子、回填与重算。"""
from reprohub import repo
from reprohub.journals import JournalRegistry, annotate_items, import_csv, norm_issn, norm_name, rescore_finals, unknown_journals
from reprohub.llm import LLM
from reprohub.pipeline.collect import ingest_item
from reprohub.pipeline.score import process_new
from reprohub.sources import RawItem


def test_normalization():
    assert norm_name("The Lancet.") == "lancet"
    assert norm_name("Front Endocrinol (Lausanne)") == norm_name("front endocrinol (lausanne)")
    assert norm_issn("0268-1161") == "0268-1161" and norm_issn("2050084x") == "2050-084X" and norm_issn("bad") is None


def test_resolve_by_issn_alias_pattern_unknown(settings):
    r = JournalRegistry(settings)
    assert r.resolve("Nat Cell Biol")["tier"] == "cns-sub"
    assert r.resolve("Nature")["if"] == 50.5 and r.resolve("Nature")["tier"] == "cns"
    assert r.resolve("Some Journal", "0268-1161")["key"] == "humrep"       # ISSN 优先
    assert r.resolve("Nat Ecol Evol")["matched_by"] == "pattern" and r.resolve("Nat Ecol Evol")["if"] is None
    assert r.resolve("Cells")["tier"] == "low-signal" and r.resolve("Cell Genomics")["tier"] == "cns-sub"
    u = r.resolve("Totally Unknown Journal")
    assert u["tier"] == "unknown" and u["boost"] == 0 and u["if"] is None
    assert r.boost_for("cns") == 12 and r.boost_for("low-signal") == -3 and r.boost_for(None) == 0


def test_ingest_annotates_and_scoring_adds_journal_boost(settings, conn):
    raw = RawItem(url="https://example.org/n1", title="Single-cell proteomics of human blastocysts", summary="We profiled human blastocysts by mass spectrometry.", journal="Nature", doi="10.1038/x1")
    iid, st = ingest_item(conn, settings, "demo", raw)
    it = repo.get_item(conn, iid)
    assert it["journal_tier"] == "cns" and it["journal_if"] == 50.5
    raw2 = RawItem(url="https://example.org/n2", title="Single-cell proteomics of human blastocysts (replica)", summary="We profiled human blastocysts by mass spectrometry.", journal="Int J Mol Sci", doi="10.3390/x2")
    iid2, _ = ingest_item(conn, settings, "demo", raw2)
    process_new(settings, conn, LLM(settings, conn), logger=lambda m: None)
    a, b = repo.get_item(conn, iid), repo.get_item(conn, iid2)
    assert a["extra"]["journal_boost"] == 12 and b["extra"]["journal_boost"] == -3
    assert a["final_score"] - b["final_score"] >= 15 - 1  # 同样内容，CNS 比 MDPI 高 15 分


def test_import_csv_overrides_and_rescore(settings, conn, tmp_path):
    csv_path = tmp_path / "if.csv"
    csv_path.write_text("name,issn,if,tier,year\nTotally Unknown Journal,1234-5678,9.9,field-top,2024\nNature,0028-0836,55.0,,2024\n", encoding="utf-8")
    assert import_csv(conn, csv_path) == 2
    r = JournalRegistry(settings, conn)
    assert r.resolve("Totally Unknown Journal")["tier"] == "field-top" and r.resolve("Totally Unknown Journal")["if"] == 9.9
    assert r.resolve("Nature")["if"] == 55.0 and r.resolve("Nature")["tier"] == "cns"  # 导入覆盖 IF，缺省字段保留 yaml
    ingest_item(conn, settings, "demo", RawItem(url="https://example.org/u1", title="Oocyte maturation in vitro", summary="IVM of human oocytes.", journal="Totally Unknown Journal"))
    process_new(settings, conn, LLM(settings, conn), logger=lambda m: None)
    conn.execute("UPDATE items SET journal_tier='unknown', journal_if=NULL, journal_key=NULL")
    res = annotate_items(conn, settings)
    assert res["matched"] == 1 and res["by_tier"].get("field-top") == 1
    rr = rescore_finals(conn, settings)
    assert rr["items"] == 1 and unknown_journals(conn) == []

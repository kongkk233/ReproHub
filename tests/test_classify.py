from reprohub.pipeline.classify import Classifier


def test_core_topic_in_scope_and_preference(settings):
    c = Classifier(settings)
    r = c.classify(
        "Integrated transcriptome and proteome of human preimplantation embryos",
        "We performed single-cell RNA-seq and low-input mass spectrometry proteomics on human zygotes and blastocysts.",
    )
    assert r.in_scope
    assert r.primary_topic == "early-embryo"
    assert r.preference_score >= 30
    assert "早期胚胎转录组+蛋白质组联合分析" in r.preference_labels


def test_cross_cutting_topic_alone_is_out_of_scope(settings):
    c = Classifier(settings)
    r = c.classify("Deep learning predicts cardiovascular risk from retinal images", "A convolutional neural network trained on fundus photographs of 10,000 patients.")
    assert not r.in_scope
    r2 = c.classify("A new low-input proteomics workflow for mass spectrometry", "We present a sample preparation method for single-cell proteomics of HeLa cells.")
    assert not r2.in_scope


def test_in_scope_default_from_source(settings):
    c = Classifier(settings)
    r = c.classify("Editorial", "Short note.", in_scope_default=True)
    assert r.in_scope and r.primary_topic is None


def test_chinese_keywords(settings):
    c = Classifier(settings)
    r = c.classify("多囊卵巢综合征患者促排卵方案比较", "辅助生殖周期中不同促排卵方案的活产率。")
    assert r.in_scope
    assert r.primary_topic in ("repro-endo", "art-clinical")


def test_preference_is_boost_not_gate(settings):
    c = Classifier(settings)
    r = c.classify("Randomized trial of letrozole versus clomiphene for ovulation induction in PCOS", "Live birth rates were compared in 750 women with polycystic ovary syndrome.")
    assert r.in_scope
    assert r.preference_score == 0

"""离线演示样例：没有网络 / 没有 API key 时也能看到完整界面与流程。

所有样例均为合成文本（不是真实论文），来源标记为 demo，URL 指向 example.org。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .pipeline.classify import Classifier
from .pipeline.collect import ingest_item
from .settings import Settings
from .sources import RawItem

_NOW = datetime.now(timezone.utc)

DEMO_ITEMS: list[dict] = [
    {
        "title": "Integrated single-cell transcriptome and low-input proteome profiling of human preimplantation embryos reveals delayed protein-level zygotic genome activation",
        "summary": "Zygotic genome activation (ZGA) in human embryos has been characterized mainly at the transcript level, and how newly transcribed genes are translated into proteins remains unclear. We performed Smart-seq3 single-cell RNA-seq and ultra-low-input mass spectrometry proteomics on matched human zygotes, 4-cell, 8-cell embryos, morulae and blastocysts (n = 96 embryos). We quantified 4,812 protein groups and found that protein abundance of major ZGA genes lagged transcript induction by roughly one cleavage division. Maternal proteins were degraded in two waves, the second coinciding with compaction. Integrative analysis identified 212 genes with discordant RNA–protein dynamics enriched in chromatin remodelers and translation regulators. These results suggest that post-transcriptional control shapes the earliest proteome of the human embryo and provide a resource for embryo quality research.",
        "journal": "Demo Journal of Reproductive Biology",
        "kind": "paper",
        "authors": ["Demo A", "Demo B", "Demo C"],
        "days_ago": 0.3,
        "doi": "10.0000/demo.embryo.omics.001",
    },
    {
        "title": "Phosphoproteomic landscape of mouse oocyte maturation identifies a CDK1-dependent switch controlling spindle assembly",
        "summary": "Oocyte meiotic maturation requires precise phosphorylation cascades, yet a global phosphoproteome of mammalian oocytes has been lacking because of limited material. Using a microfluidic sample preparation workflow we profiled 1,000 mouse oocytes per stage at germinal vesicle, metaphase I and metaphase II and quantified 9,300 phosphosites. We identified a cluster of spindle-associated proteins whose phosphorylation increased sharply at nuclear envelope breakdown in a CDK1-dependent manner. Inhibiting one candidate kinase delayed spindle assembly and increased aneuploidy. The dataset offers a framework for understanding oocyte quality decline with age.",
        "journal": "Demo Developmental Cell Reports",
        "kind": "paper",
        "authors": ["Demo D", "Demo E"],
        "days_ago": 0.8,
        "doi": "10.0000/demo.oocyte.phospho.002",
    },
    {
        "title": "Randomized trial of freeze-all versus fresh single blastocyst transfer in normo-responders: cumulative live birth at 12 months",
        "summary": "Whether elective freeze-all improves cumulative live birth in women with a normal ovarian response remains debated. In this multicentre randomized controlled trial, 1,208 women undergoing their first IVF/ICSI cycle were allocated to freeze-all or fresh single blastocyst transfer. The primary outcome was cumulative live birth within 12 months. Cumulative live birth rates were 52.1% versus 50.4% (risk ratio 1.03, 95% CI 0.93–1.15). Ovarian hyperstimulation syndrome was less frequent in the freeze-all arm, while time to pregnancy was longer. Freeze-all did not improve cumulative live birth in normo-responders.",
        "journal": "Demo Human Reproduction",
        "kind": "paper",
        "authors": ["Demo F", "Demo G", "Demo H"],
        "days_ago": 1.2,
        "doi": "10.0000/demo.art.rct.003",
    },
    {
        "title": "Non-invasive preimplantation genetic testing using spent culture medium: concordance with trophectoderm biopsy in 640 blastocysts",
        "summary": "Cell-free DNA in spent embryo culture medium (SCM) offers a non-invasive alternative to trophectoderm biopsy for aneuploidy screening. We compared SCM-based niPGT-A with trophectoderm biopsy in 640 blastocysts from 180 couples. Whole-chromosome concordance was 84.7%, with higher concordance for day-6 than day-5 collection. Maternal DNA contamination, assessed by SNP analysis, was detected in 7% of samples. Mosaic calls were more frequent in SCM. The findings support SCM analysis as a screening tool but not yet as a replacement for biopsy.",
        "journal": "Demo Fertility and Sterility",
        "kind": "paper",
        "authors": ["Demo I", "Demo J"],
        "days_ago": 1.9,
        "doi": "10.0000/demo.pgt.ni.004",
    },
    {
        "title": "Single-cell atlas of the human endometrium across the window of implantation in recurrent implantation failure",
        "summary": "Recurrent implantation failure (RIF) may reflect impaired endometrial receptivity. We generated a single-cell RNA-seq atlas of 42 endometrial biopsies from RIF patients and fertile controls sampled across the window of implantation. Decidualizing stromal cells in RIF showed reduced expression of progesterone-responsive genes and a delayed transition to a secretory state, while uterine NK cells displayed an inflammatory signature. A receptivity score derived from the atlas predicted subsequent implantation in an independent cohort. These data nominate stromal decidualization timing as a target in RIF.",
        "journal": "Demo Nature Medicine",
        "kind": "paper",
        "authors": ["Demo K", "Demo L", "Demo M"],
        "days_ago": 2.4,
        "doi": "10.0000/demo.endometrium.005",
    },
    {
        "title": "Deep learning on time-lapse morphokinetics predicts blastocyst euploidy: external validation across four IVF laboratories",
        "summary": "Artificial intelligence models trained on time-lapse embryo videos have been proposed to prioritize euploid blastocysts. We externally validated a convolutional neural network on 5,120 embryos with PGT-A results from four laboratories using different incubators. Area under the curve ranged from 0.66 to 0.71, lower than in the development cohort. Performance was sensitive to culture medium and camera settings. Combining the model with maternal age improved calibration. The model may assist embryo ranking but cannot replace genetic testing.",
        "journal": "Demo Reproductive BioMedicine Online",
        "kind": "paper",
        "authors": ["Demo N", "Demo O"],
        "days_ago": 3.1,
        "doi": "10.0000/demo.ai.timelapse.006",
    },
    {
        "title": "Human blastoids model trophectoderm–hypoblast crosstalk and respond to endometrial organoid co-culture",
        "summary": "Stem cell-derived blastoids provide a tractable model of the human blastocyst. We derived blastoids from naive pluripotent stem cells and profiled them with single-cell multi-omics (RNA and chromatin accessibility). Blastoid trophectoderm and hypoblast lineages matched their blastocyst counterparts, and co-culture with hormonally primed endometrial organoids induced attachment and expression of implantation-associated genes. This platform enables mechanistic studies of early implantation without using human embryos.",
        "journal": "bioRxiv (demo)",
        "kind": "preprint",
        "authors": ["Demo P", "Demo Q"],
        "days_ago": 0.6,
        "doi": "10.0000/demo.blastoid.007",
    },
    {
        "title": "Whole-exome sequencing in 1,200 men with non-obstructive azoospermia identifies recurrent variants in meiotic genes",
        "summary": "The genetic basis of non-obstructive azoospermia (NOA) remains unexplained in most patients. We sequenced exomes from 1,200 men with NOA and 2,000 fertile controls. Rare biallelic variants in genes essential for meiotic recombination and synapsis were enriched in NOA, explaining 9% of cases. Several genes were associated with specific testicular histology, which may inform the likelihood of sperm retrieval by microdissection TESE. Our findings support genetic testing before surgical sperm retrieval.",
        "journal": "Demo American Journal of Human Genetics",
        "kind": "paper",
        "authors": ["Demo R", "Demo S", "Demo T"],
        "days_ago": 4.0,
        "doi": "10.0000/demo.male.exome.008",
    },
    {
        "title": "ESHRE updates guideline on management of premature ovarian insufficiency",
        "summary": "The European Society of Human Reproduction and Embryology has released an updated guideline on premature ovarian insufficiency (POI), covering diagnosis using AMH and FSH thresholds, hormone replacement therapy until the natural age of menopause, bone and cardiovascular health, fertility options including oocyte donation, and psychological support. The update emphasizes shared decision-making and long-term follow-up.",
        "journal": "Demo society news",
        "kind": "guideline",
        "authors": [],
        "days_ago": 2.0,
    },
    {
        "title": "Microplastics detected in follicular fluid correlate with lower oocyte maturation rates in IVF patients",
        "summary": "Environmental microplastics have been detected in human blood and placenta, but their presence in the ovary is less studied. We analysed follicular fluid from 120 women undergoing IVF using Raman spectroscopy and detected microplastic particles in 78% of samples. Particle counts were inversely associated with the proportion of mature oocytes retrieved after adjusting for age and body mass index. No association with fertilization or blastocyst rate was found. These observational data warrant mechanistic follow-up.",
        "journal": "Demo Environment International",
        "kind": "paper",
        "authors": ["Demo U", "Demo V"],
        "days_ago": 5.2,
        "doi": "10.0000/demo.tox.microplastic.009",
    },
    {
        "title": "Fertility benefits startup raises Series B to expand IVF coverage for employers",
        "summary": "A company offering fertility benefits to employers announced a Series B round to expand coverage of IVF, egg freezing and fertility preservation across more markets, citing rising demand for assisted reproduction benefits. The company said the funding would also support partnerships with fertility clinics and the development of digital tools for patients.",
        "journal": "Demo Tech News",
        "kind": "news",
        "authors": [],
        "days_ago": 1.5,
    },
    {
        "title": "In vitro gametogenesis: mouse oocyte-like cells from induced pluripotent stem cells give rise to live offspring with improved efficiency",
        "summary": "Reconstituting oogenesis in vitro from pluripotent stem cells has been achieved in mice but with low efficiency. Here we optimized the differentiation of primordial germ cell-like cells and the reconstituted ovary culture, increasing the yield of metaphase II oocyte-like cells threefold. Transcriptome and DNA methylation profiling showed near-complete erasure and re-establishment of imprinting. Following IVF, 3.2% of transferred embryos developed to term. The work provides a benchmark for evaluating in vitro gametogenesis fidelity.",
        "journal": "Demo Cell Stem Cell",
        "kind": "paper",
        "authors": ["Demo W", "Demo X"],
        "days_ago": 6.5,
        "doi": "10.0000/demo.ivg.010",
    },
]


def seed(settings: Settings, conn: sqlite3.Connection) -> int:
    classifier = Classifier(settings)
    n = 0
    for i, d in enumerate(DEMO_ITEMS, 1):
        raw = RawItem(
            url=f"https://example.org/demo/{i:03d}",
            title=d["title"],
            summary=d["summary"],
            external_id=f"demo-{i:03d}",
            doi=d.get("doi"),
            authors=d.get("authors") or [],
            journal=d.get("journal"),
            published_at=_NOW - timedelta(days=float(d.get("days_ago", 1))),
            kind=d.get("kind"),
            extra={"demo": True},
        )
        _, st = ingest_item(conn, settings, "demo", raw, classifier)
        if st == "new":
            n += 1
    return n

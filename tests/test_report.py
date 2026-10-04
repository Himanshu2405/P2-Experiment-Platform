"""The experiment results pages: the Markdown reader, the read-only export from the tool, and the page built from the design doc, the results and the
conclusion. All offline (a memory store and the fake runner); the real example pages are checked against their own results.json."""
import json
import re

from markupsafe import escape
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from p2.catalog.seed import seed_registry
from p2.report import export, md, page
from p2.services.platform import Platform
from p2.stats.bayes import BayesPlan
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources

REPO = Path(__file__).resolve().parents[1]
EXPERIMENTS = REPO / "experiments"
REG = seed_registry()
AFTER = datetime(2026, 2, 1, tzinfo=timezone.utc)


# ---- the Markdown reader ----------------------------------------------------------------------------------------------------
DOC = """# exp-1: A test

- **Experiment ID:** exp-1
- **PM:** Sam

## Overview

**Change being tested:** Make it green.

**Null hypothesis (TL;DR):** No difference.

## Metrics

### Primary

| Metric | Baseline |
|---|---|
| Conversion rate | 3.7% |
"""


def test_a_document_is_split_into_its_title_header_and_sections():
    doc = md.parse_doc(DOC)
    assert doc.title == "exp-1: A test" and doc.header == {"Experiment ID": "exp-1", "PM": "Sam"}
    assert list(doc.sections) == ["Overview", "Metrics"] and doc.sections["Metrics"].startswith("### Primary")
    assert md.fields(doc.sections["Overview"]) == {"Change being tested": "Make it green.", "Null hypothesis (TL;DR)": "No difference."}
    assert md.table(doc.sections["Metrics"]) == [{"Metric": "Conversion rate", "Baseline": "3.7%"}]
    with pytest.raises(ValueError, match="Title"):
        md.parse_doc("no title here")


def test_markdown_becomes_html_and_nothing_in_a_document_can_inject_markup():
    out = md.to_html("**Bold** and `code` and [a link](https://example.com).\n\n- one\n- two\n\n1. first\n\n### Sub\n\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<strong>Bold</strong>" in out and "<code>code</code>" in out and '<a href="https://example.com">a link</a>' in out
    assert "<ul><li>one</li><li>two</li></ul>" in out and "<ol><li>first</li></ol>" in out and "<h3>Sub</h3>" in out
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in out
    hostile = md.to_html('<script>alert(1)</script> [x](javascript:alert(1))')
    assert "<script>" not in hostile and "&lt;script&gt;" in hostile and 'href="javascript' not in hostile


# ---- the design doc format ---------------------------------------------------------------------------------------------------
def test_every_design_doc_follows_the_template_headings():
    heads = lambda p: re.findall(r"^(#{2,3} .+)$", Path(p).read_text(), flags=re.M)
    template = heads(EXPERIMENTS / "_template" / "design.md")
    assert [h[3:] for h in template if h.startswith("## ")] == page.EDD_SECTIONS
    docs = sorted(EXPERIMENTS.glob("*/design.md"))
    assert len(docs) >= 4
    for doc in docs:
        assert heads(doc) == template, doc


# ---- the export from the tool ------------------------------------------------------------------------------------------------
@pytest.fixture
def plat():
    p = Platform(MemoryStore(APP_TABLES), load_sources(), checker=FakeChecker(), runner=FakeRunner())
    p.bootstrap()
    return p


def analysed(p, eid, bayesian):
    a = p.get_actor("priya")
    p.create_experiment(a, eid, "checkout", "Banner", "More shoppers buy", date(2026, 1, 1), 14)
    conv, refund = REG.get("conversion_rate"), REG.get("refund_rate")
    if bayesian:
        p.save_design(a, eid, BayesPlan(conv, "primary", threshold=0.0005), [BayesPlan(refund, "guardrail", margin=0.25, margin_kind="relative")],
                      ["add_to_cart_rate"])
    else:
        p.save_design(a, eid, MetricPlan(conv, "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"),
                      [MetricPlan(refund, "guardrail", 0.0047, 0.25, "relative", 0.05, 0.8, "one-sided")], ["add_to_cart_rate"])
    p.run_analysis(a, eid, as_of=AFTER)


def test_the_export_holds_the_numbers_the_tool_shows_and_is_read_only(plat):
    analysed(plat, "exp-f", bayesian=False)
    before = {t: len(plat.store.select(t)) for t in ("results", "daily_stats", "segment_results", "experiments", "experiment_items", "audit_log")}
    data = export.export_results(plat, "exp-f", exported_on=date(2026, 10, 4))
    assert {t: len(plat.store.select(t)) for t in before} == before                              # nothing was written
    json.dumps(data, default=export._json_default)                                              # and it is plain JSON
    assert data["experiment"]["method"] == "frequentist" and data["is_final"] and data["sample"] == {"n_control": 450, "n_variant": 450}
    assert data["verdict"]["text"] == "Significant improvement" and "alpha of 0.05" in data["verdict"]["reason"]
    primary, guardrail, secondary = data["metrics"]
    assert (primary["role"], primary["control"], primary["variant"]) == ("primary", 0.10, 0.16) and primary["lift"] == pytest.approx(0.6)
    stored = {r["item_id"]: r for r in plat.latest_results("exp-f")}
    for m in data["metrics"]:                                                                    # the same p-values and intervals the tool stored
        assert m["p_value"] == stored[m["item_id"]]["p_value"] and m["ci_low"] == stored[m["item_id"]]["ci_low"]
    assert guardrail["label"] == "Failed" and secondary["label"] == "Win"                       # a clear secondary lift is a Win
    assert data["checks"]["balance"]["balanced"] and data["checks"]["balance"]["p_value"] == pytest.approx(1.0)
    assert data["checks"]["placebo"]["method"] == "frequentist" and data["checks"]["placebo"]["text"].startswith("The test behaves")
    assert [c["key"] for c in data["charts"]] == ["time", "difference"] and all(c["spec"]["width"] == "container" for c in data["charts"])
    assert data["label_rule"] == "" and data["plan"] == {"alpha": 0.05, "power": 0.8, "sidedness": "two-sided"}


def test_a_bayesian_export_adds_the_chance_to_win_the_risks_and_the_risk_charts(plat):
    analysed(plat, "exp-b", bayesian=True)
    data = export.export_results(plat, "exp-b")
    primary, guardrail, secondary = data["metrics"]
    assert data["experiment"]["method"] == "bayesian" and data["verdict"]["text"] == "Variant is the safer choice"
    assert primary["chance_to_win"] > 0.99 and primary["risk_variant"] < primary["risk_control"] and primary["chance_of_harm"] is None
    assert guardrail["label"] == "Failed" and guardrail["chance_of_harm"] > 0.9 and secondary["label"] == "Win"
    assert [c["key"] for c in data["charts"]] == ["time", "risk", "risk_time", "difference", "chance"]
    assert "Win when the chance the variant is better is 90% or more" in data["label_rule"]
    assert data["plan"]["risk_threshold"] == pytest.approx(0.0005) and data["plan"]["min_days"] == 7


def test_secondary_labels_follow_one_plain_rule():
    from p2.stats.bayes import BayesOutcome
    out = lambda p: BayesOutcome(p, 0, 0, 0, 0, 0, 0, None, None)
    assert [export._label("secondary", True, {}, out(p)) for p in (0.95, 0.90, 0.5, 0.10, 0.05)] == ["Win", "Win", "Neutral", "Loss", "Loss"]
    assert export._label("secondary", False, {"verdict": "Significant improvement"}, None) == "Win"
    assert export._label("secondary", False, {"verdict": "Significant decline"}, None) == "Loss"
    assert export._label("secondary", False, {"verdict": "No significant difference"}, None) == "Neutral"
    assert export._label("guardrail", False, {"verdict": "Passed"}, None) == "Passed"


def test_exporting_an_experiment_with_no_results_says_to_run_it_first(plat):
    plat.create_experiment(plat.get_actor("priya"), "exp-n", "checkout", "n", "h", date(2026, 1, 1), 14)
    with pytest.raises(Exception, match="no results yet|plan"):
        export.export_results(plat, "exp-n")


# ---- the page ----------------------------------------------------------------------------------------------------------------
CONCLUSION = """# exp-f: Conclusion

- **Decision:** Ship
- **DS:** Priya (fictional)
- **Date of report:** 2026-10-04

## Executive summary

The banner works.

## Key findings

- Conversion rose.

## Notes on the results

Nothing odd.

## Recommendation

Ship it.
"""


@pytest.fixture
def folder(plat, tmp_path):
    analysed(plat, "exp-f", bayesian=False)
    (tmp_path / "design.md").write_text((EXPERIMENTS / "demo-banner" / "design.md").read_text())
    (tmp_path / "conclusion.md").write_text(CONCLUSION)
    export.write_results(plat, "exp-f", tmp_path, exported_on=date(2026, 10, 4))
    return tmp_path


def test_the_page_combines_the_design_doc_the_results_and_the_conclusion(folder):
    html = page.render_page(folder)
    assert "<title>exp-f: Banner" in html and "Shoppers who see the free-shipping banner will complete more purchases." in html     # from the tool, the EDD
    assert "Sam Rivera (fictional)" in html and "Priya (fictional)" in html and "450 control / 450 variant" in html
    assert "The banner works." in html and "Ship it." in html and '<span class="chip green">Ship</span>' in html           # from the conclusion
    assert ">10.000%<" in html and ">16.000%<" in html and "+60.0%" in html and "Significant improvement" in html          # the tool's numbers
    assert "Chi-square p = 100.00%" in html and "The test behaves correctly on your data." in html
    assert 'id="chart-time"' in html and 'id="chart-difference"' in html and "vegaEmbed" in html
    for label in ("Audience", "Metrics", "Decision criteria", "Visible changes"):
        assert f"<summary>{label}</summary>" in html


def test_a_page_refuses_an_unfinished_design_doc_or_conclusion(folder):
    (folder / "design.md").write_text((folder / "design.md").read_text().replace("## Kill switch", "## Something else"))
    with pytest.raises(ValueError, match="design doc is missing or has empty sections: Kill switch"):
        page.build_context(folder)
    (folder / "design.md").write_text((EXPERIMENTS / "demo-banner" / "design.md").read_text())
    (folder / "conclusion.md").write_text(CONCLUSION.replace("Ship\n- **DS", "Maybe\n- **DS"))
    with pytest.raises(ValueError, match="Decision must be one of"):
        page.build_context(folder)
    (folder / "conclusion.md").write_text(CONCLUSION.replace("The banner works.", ""))
    with pytest.raises(ValueError, match="conclusion is missing or has empty sections: Executive summary"):
        page.build_context(folder)


def test_text_from_the_documents_cannot_break_the_page(folder):
    (folder / "conclusion.md").write_text(CONCLUSION.replace("The banner works.", "<script>alert('x')</script> Works."))
    html = page.render_page(folder)
    assert "<script>alert" not in html and "&lt;script&gt;alert" in html


def test_the_build_writes_a_page_per_ready_experiment_and_a_list(folder, tmp_path_factory):
    root = tmp_path_factory.mktemp("experiments")
    ready = root / "exp-f"
    ready.mkdir()
    for f in ("design.md", "conclusion.md", "results.json"):
        (ready / f).write_text((folder / f).read_text())
    (root / "exp-draft").mkdir()
    (root / "exp-draft" / "design.md").write_text("# not ready\n")                         # no conclusion or results yet: skipped
    (root / "_template").mkdir()
    written = page.build(root)
    assert sorted(p.relative_to(root).as_posix() for p in written) == ["exp-f/index.html", "index.html"]
    listing = (root / "index.html").read_text()
    assert 'href="exp-f/index.html"' in listing and "Ship" in listing and "exp-draft" not in listing


# ---- the real example pages --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("eid", ["demo-banner", "exp-002", "sep-checkout-1"])
def test_each_example_page_shows_exactly_the_numbers_in_its_results(eid):
    folder = EXPERIMENTS / eid
    data = json.loads((folder / "results.json").read_text())
    html = (folder / "index.html").read_text()
    assert html == page.render_page(folder)                                                  # the committed page is up to date
    primary = data["metrics"][0]
    assert page.fmt_value(primary["type"], primary["control"]) in html and page.fmt_value(primary["type"], primary["variant"]) in html
    assert str(escape(data["verdict"]["text"])) in html and str(escape(data["verdict"]["reason"])) in html
    assert f"{data['sample']['n_control']:,} control / {data['sample']['n_variant']:,} variant" in html
    assert len(re.findall(r'id="chart-', html)) == len(data["charts"])
    assert data["experiment"]["id"] == eid and (folder / "conclusion.md").exists()


def test_the_example_experiments_cover_both_methods_and_three_decisions():
    decisions = {}
    for eid in ("demo-banner", "exp-002", "sep-checkout-1"):
        decisions[eid] = (json.loads((EXPERIMENTS / eid / "results.json").read_text())["experiment"]["method"],
                          md.parse_doc((EXPERIMENTS / eid / "conclusion.md").read_text()).header["Decision"])
    assert decisions == {"demo-banner": ("frequentist", "Ship"), "exp-002": ("frequentist", "Inconclusive"), "sep-checkout-1": ("bayesian", "Iterate")}

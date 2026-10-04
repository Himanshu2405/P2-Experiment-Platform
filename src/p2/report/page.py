"""Build an experiment's results page (index.html) from its three inputs and one fixed template.

  experiments/<id>/design.md      the EDD, written by the PM and DS outside the tool
  experiments/<id>/results.json   the tool's saved results, exported read-only (see p2.report.export)
  experiments/<id>/conclusion.md  the DS's decision, summary and recommendation

python -m p2.report.build [<experiment-id> ...] builds those pages and the list page experiments/index.html.
"""
import html
import json
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from p2.report import md

ROOT = Path(__file__).resolve().parents[3]
REPO = "https://github.com/Himanshu2405/P2-Experiment-Platform"
EDD_SECTIONS = ["Overview", "Problem and opportunity", "Hypothesis", "Evidence", "Risks and dependencies", "Audience", "Metrics", "Analysis plan",
                "Decision criteria", "Kill switch", "Visible changes"]
CONCLUSION_SECTIONS = ["Executive summary", "Recommendation"]            # required; Key findings and Notes on the results are optional
DECISIONS = ("Ship", "Do not ship", "Iterate", "Inconclusive")
GOOD = ("improvement", "safer choice", "passed", "win", "ship")
BAD = ("decline", "failed", "loss", "do not ship")


def tone(text: str) -> str:
    """The colour of a verdict or decision chip: green, red or yellow (grey for 'no meaningful difference' and similar)."""
    t = (text or "").lower()
    if t.startswith("control is the safer"):
        return "red"
    if any(w in t for w in BAD):
        return "red"
    if any(w in t for w in GOOD):
        return "green"
    return "grey" if ("no meaningful" in t or "no significant" in t or "neutral" in t) else "yellow"


def fmt_value(kind: str, x, diff: bool = False) -> str:
    if x is None:
        return ""
    if kind == "rate":
        return f"{x * 100:+.3f} pp" if diff else f"{x * 100:.3f}%"
    return f"{x:+,.3f}" if diff else f"{x:,.3f}"


def fmt_loss(kind: str, x) -> str:
    if x is None:
        return ""
    return f"{x * 100:.3f} pp" if kind == "rate" else f"{x:,.3f}"


def fmt_p(p) -> str:
    return "" if p is None else ("<0.001" if p < 0.001 else f"{p:.3f}")


ROLE_TONE = {"primary": "star", "secondary": "grey", "guardrail": "red"}


def lift_class(m: dict) -> str:
    """pos when the variant moved the good way, neg when it moved the bad way, flat when it barely moved (a lower-is-better metric flips)."""
    lift = m["lift"]
    if lift is None or abs(lift) < 0.005:
        return "flat"
    return "pos" if (lift > 0) == (m["good_direction"] == "higher") else "neg"


def _results_table(data: dict) -> tuple[list[dict], list[dict]]:
    """(columns, rows) of the results table. A column is {name, num} (num: right-aligned numbers); a cell is {text, num?, cls?, tone?, sub?, star?}."""
    bayes = data["experiment"]["method"] == "bayesian"
    head = [("Metric", False), ("Type", False), ("Control", True), ("Variant", True), ("Lift", True)]
    head += ([("Chance to win", True), ("Difference (95% interval)", True), ("Risk if ship", True), ("Risk if keep", True),
              ("Chance of harm", True)] if bayes else [("Difference", True), ("p-value", True), ("95% CI", True)])
    head += [("Verdict", False)]
    rows = []
    for m in data["metrics"]:
        kind = m["type"]
        num = lambda text, **kw: {"text": text, "num": True, **kw}
        cells = [{"text": m["name"], "sub": kind}, {"text": m["role"].capitalize(), "chip": ROLE_TONE[m["role"]], "star": m["role"] == "primary"},
                 num(fmt_value(kind, m["control"])), num(fmt_value(kind, m["variant"])),
                 num("" if m["lift"] is None else f"{m['lift'] * 100:+.1f}%", cls=lift_class(m))]
        guard = m["role"] == "guardrail"
        if bayes:
            has = m.get("chance_to_win") is not None
            cells += [num(f"{m['chance_to_win'] * 100:.1f}%" if has else "", cls="strong"),
                      num(fmt_value(kind, m.get("expected_difference"), True) if has else "",
                          sub2=f"{fmt_value(kind, m['ci_low'], True)} to {fmt_value(kind, m['ci_high'], True)}" if has else ""),
                      num("" if guard or not has else fmt_loss(kind, m["risk_variant"])),
                      num("" if guard or not has else fmt_loss(kind, m["risk_control"])),
                      num(f"{m['chance_of_harm'] * 100:.1f}%" if guard and m.get("chance_of_harm") is not None else "")]
        else:
            level = "" if m.get("ci_level") in (None, 0.95) else f" ({m['ci_level'] * 100:.0f}%)"
            cells += [num(fmt_value(kind, m["difference"], True)), num(fmt_p(m.get("p_value")), cls="strong"),
                      num((f"{fmt_value(kind, m['ci_low'], True)} to {fmt_value(kind, m['ci_high'], True)}{level}") if m.get("ci_low") is not None else "", wrap=True)]
        cells.append({"text": m["label"], "tone": tone(m["label"])})
        rows.append({"cells": cells, "primary": m["role"] == "primary"})
    return [{"name": n, "num": k} for n, k in head], rows


def _read_doc(path: Path, required: list[str], what: str) -> md.Doc:
    doc = md.parse_doc(path.read_text())
    missing = [s for s in required if s not in doc.sections or not doc.sections[s].strip()]
    if missing:
        raise ValueError(f"{path}: the {what} is missing or has empty sections: {', '.join(missing)}")
    return doc


def build_context(folder: Path) -> dict:
    """Read the three inputs of an experiment and shape everything the template shows."""
    data = json.loads((folder / "results.json").read_text())
    edd = _read_doc(folder / "design.md", EDD_SECTIONS, "design doc")
    concl = _read_doc(folder / "conclusion.md", CONCLUSION_SECTIONS, "conclusion")
    decision = concl.header.get("Decision", "")
    if decision not in DECISIONS:
        raise ValueError(f"{folder / 'conclusion.md'}: Decision must be one of {', '.join(DECISIONS)} (found '{decision}')")
    exp, overview = data["experiment"], md.fields(edd.sections["Overview"])
    head, rows = _results_table(data)
    balance, placebo = data["checks"]["balance"], data["checks"]["placebo"]
    sample = data["sample"]
    period = f"{exp['launch_date']} to {exp['end_date']} ({(date.fromisoformat(exp['end_date']) - date.fromisoformat(exp['launch_date'])).days + 1} days)"
    secondary = [m for m in data["metrics"] if m["role"] == "secondary"]
    changes = md.fields(edd.sections["Visible changes"])
    compare = None
    if changes.get("Control") and changes.get("Variant"):             # shown side by side; otherwise the section stays in the design details
        pm_row = next(m for m in data["metrics"] if m["role"] == "primary")
        compare = {"control": md.inline(changes["Control"]), "variant": md.inline(changes["Variant"]), "metric": pm_row["name"],
                   "n_control": f"{sample['n_control']:,}", "n_variant": f"{sample['n_variant']:,}",
                   "v_control": fmt_value(pm_row["type"], pm_row["control"]), "v_variant": fmt_value(pm_row["type"], pm_row["variant"]),
                   "lift": "" if pm_row["lift"] is None else f"{pm_row['lift'] * 100:+.1f}%", "lift_cls": lift_class(pm_row)}
    return {
        "id": exp["id"], "name": exp["name"], "method": exp["method"].capitalize(), "bayesian": exp["method"] == "bayesian",
        "question": overview.get("Product hypothesis (TL;DR)", ""),
        "meta": [("DS", concl.header.get("DS") or edd.header.get("DS", "")), ("PM", edd.header.get("PM", "")),
                 ("Date of report", concl.header.get("Date of report", data["exported_on"])), ("Experiment ID", exp["id"]),
                 ("Period", period), ("Sample", f"{sample['n_control']:,} control / {sample['n_variant']:,} variant"), ("Method", exp["method"].capitalize())],
        "decision": decision, "decision_tone": tone(decision), "verdict": data["verdict"]["text"], "verdict_tone": tone(data["verdict"]["text"]),
        "verdict_reason": data["verdict"]["reason"], "primary_name": data["primary"]["name"], "is_final": data["is_final"],
        "summary": md.to_html(concl.sections["Executive summary"]),
        "links": [("Design doc (EDD)", f"{REPO}/blob/main/experiments/{exp['id']}/design.md"), ("Results data (JSON)", "results.json"),
                  ("Product context", f"{REPO}/blob/main/products/{exp['product']}.md")],
        "background": [(name, md.to_html(edd.sections[name])) for name in ("Overview", "Problem and opportunity", "Hypothesis", "Evidence")],
        "design": [(name, md.to_html(edd.sections[name])) for name in ("Audience", "Metrics", "Analysis plan", "Decision criteria", "Kill switch",
                                                                      "Visible changes", "Risks and dependencies") if not (compare and name == "Visible changes")],
        "compare": compare,
        "table_head": head, "table_rows": rows, "label_rule": data["label_rule"] if secondary else "",
        "legend": ("Chance to win: the chance the variant is better. Risk if ship: the average loss if we ship the variant and it is actually worse. "
                   "Risk if keep: the average loss if we keep control and the variant was actually better. Chance of harm (guardrails): the chance the "
                   "metric got worse by more than the margin.") if exp["method"] == "bayesian" else "",
        "balance": balance, "balance_p": f"{balance['p_value'] * 100:.2f}%", "placebo": placebo,
        "charts": data["charts"], "charts_json": json.dumps({c["key"]: c["spec"] for c in data["charts"]}).replace("</", "<\\/"),
        "findings": [{"lead": lead, "body": md.inline(rest)} for lead, rest in (md.lead_and_rest(b) for b in md.bullets(concl.sections.get("Key findings", "")))],
        "headline": concl.header.get("Headline", ""),
        "notes": md.to_html(concl.sections["Notes on the results"]) if concl.sections.get("Notes on the results") else "",
        "recommendation": md.to_html(concl.sections["Recommendation"]), "exported_on": data["exported_on"],
        "through": data["through_date"],
    }


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)


def render_page(folder: Path) -> str:
    return _env().get_template("page.html").render(**build_context(folder))


def render_index(items: list[dict]) -> str:
    return _env().get_template("index.html").render(items=items)


def build(root: Path | None = None, only: list[str] | None = None) -> list[Path]:
    """Build index.html for every experiment folder that has a design doc, a conclusion and results (or only the named ones), then the list page."""
    root = Path(root) if root else ROOT / "experiments"
    written, items = [], []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        ready = all((folder / f).exists() for f in ("design.md", "conclusion.md", "results.json"))
        if not ready:
            continue
        ctx = build_context(folder)
        items.append({"id": ctx["id"], "name": ctx["name"], "method": ctx["method"], "decision": ctx["decision"], "tone": ctx["decision_tone"],
                      "summary": html.unescape(ctx["question"])})
        if not only or folder.name in only:
            (folder / "index.html").write_text(render_page(folder))
            written.append(folder / "index.html")
    (root / "index.html").write_text(render_index(items))
    written.append(root / "index.html")
    return written


def main(argv: list[str]) -> None:
    for path in build(only=argv or None):
        print("wrote", path)


if __name__ == "__main__":
    main(sys.argv[1:])

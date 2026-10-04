# Experiments

One folder per experiment. Each experiment starts as a design doc (EDD) written by the PM and the DS, outside the tool. The tool then holds the numbers. A results page combines the two with the DS's conclusion.

All names, numbers and links in these documents are fictional. The data is synthetic.

## What lives in an experiment folder

| File | Written by | Purpose |
|---|---|---|
| `design.md` | PM and DS, before launch | The EDD: why we test, what we test, how we judge it |
| `conclusion.md` | DS, after the test | Decision, summary, key findings and recommendation |
| `results.json` | exported from the tool | The numbers, exactly as the tool shows them |
| `index.html` | built | The results page |

## The EDD format

The EDD is Markdown with fixed headings, so a template can read it. Copy `_template/design.md` and fill it in. Rules:

- Keep the heading names and their order. Add text under them, not new headings.
- The top block is `- **Label:** value` lines. Keep the labels.
- Metric tables keep their column names.
- Write plain, short sentences. Say "n/a" instead of deleting a section.
- Under `## Visible changes`, keep the `**Control:**` and `**Variant:**` lines. The page shows them side by side, with the users and the primary metric of each arm under them.

Headings, in order: Overview, Problem and opportunity, Hypothesis, Evidence, Risks and dependencies, Audience, Metrics (Primary, Secondary, Guardrails), Analysis plan, Decision criteria, Kill switch, Visible changes.

## Examples

- `demo-banner`: a frequentist test that wins.
- `sep-checkout-1`: a Bayesian test that is inconclusive.
- `exp-002`: a frequentist test that is inconclusive because it ran with far fewer users than planned.

Product facts used by the EDDs are in `../products/`.

## Building a results page

The tool is not changed. The page builder only reads from it.

```bash
python -m p2.report.export <experiment-id>   # read the saved results from the tool into results.json
python -m p2.report.build                    # build every experiment folder that has design.md, conclusion.md and results.json
```

The page combines three inputs with one fixed template (`src/p2/report/page.html`):

1. `design.md`, the EDD, for the background and the design.
2. `results.json`, the tool's numbers, charts and checks.
3. `conclusion.md`, the DS's decision and recommendation.

The builder stops with a clear message if a heading is missing or the decision is not one of Ship, Do not ship, Iterate or Inconclusive.

## The conclusion file

Markdown with fixed headings, written by the DS:

- Header lines: `- **Decision:**` (Ship, Do not ship, Iterate or Inconclusive), `- **DS:**`, `- **Date of report:**`, and optionally `- **Headline:**` (a few words shown next to the decision, such as "Strong primary win, guardrail clear").
- Sections: `## Executive summary` and `## Recommendation` (required), `## Key findings` and `## Notes on the results` (optional).
- `## Key findings` is a list of bullets. Start each with a bold label ending in a colon, for example `- **Primary metric:** conversion rose 30%.`. The page numbers them and shows the label in bold.

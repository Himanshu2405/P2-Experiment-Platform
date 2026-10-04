# Experiments

One folder per experiment. Each experiment starts as a design doc (EDD) written by the PM and the DS, outside the tool. The tool then holds the numbers. A results page combines the two with the DS's conclusion.

All names, numbers and links in these documents are fictional. The data is synthetic.

## What lives in an experiment folder

| File | Written by | Purpose |
|---|---|---|
| `design.md` | PM and DS, before launch | The EDD: why we test, what we test, how we judge it |
| `conclusion.md` | DS, after the test (not built yet) | Decision, summary and recommendation |
| `results.json` | exported from the tool (not built yet) | The numbers, exactly as the tool shows them |

## The EDD format

The EDD is Markdown with fixed headings, so a template can read it. Copy `_template/design.md` and fill it in. Rules:

- Keep the heading names and their order. Add text under them, not new headings.
- The top block is `- **Label:** value` lines. Keep the labels.
- Metric tables keep their column names.
- Write plain, short sentences. Say "n/a" instead of deleting a section.

Headings, in order: Overview, Problem and opportunity, Hypothesis, Evidence, Risks and dependencies, Audience, Metrics (Primary, Secondary, Guardrails), Analysis plan, Decision criteria, Kill switch, Visible changes.

## Examples

- `demo-banner`: a frequentist test that wins.
- `sep-checkout-1`: a Bayesian test that is inconclusive.
- `exp-002`: a frequentist test that is inconclusive because it ran with far fewer users than planned.

Product facts used by the EDDs are in `../products/`.

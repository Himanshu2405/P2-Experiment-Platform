# <experiment-id>: <experiment name>

- **Experiment ID:** <same id as in the tool>
- **Method:** Frequentist or Bayesian
- **Planned dates:** <first day> to <last day> (<n> days)
- **Product:** <checkout, email or onboarding>
- **PM:** <name>
- **DS:** <name>
- **Eng, Design, QA:** <names>
- **Links:** <PRD, design file, chat channel>

## Overview

**Change being tested:** <one sentence>

**Product hypothesis (TL;DR):** <one sentence>

**Null hypothesis (TL;DR):** <one sentence: no difference in the primary metric>

## Problem and opportunity

**Problem:** <what is wrong today, for whom>

**Opportunity:** <what it is worth if fixed>

**Sizing:** <how the value was estimated, with the numbers>

## Hypothesis

**Testable statement:** If <change>, then <primary metric> will <direction> because <reason>.

## Evidence

- **Related experiments:** <what we learned before>
- **Other evidence:** <research, data, feedback>

## Risks and dependencies

- **Risks:** <what could go wrong, and how it is covered>
- **Dependencies:** <teams or work this relies on>

## Audience

- **Who is in:** <eligibility, countries, account type>
- **Where they enter:** <the screen or event that triggers assignment>
- **Split:** <for example 50 / 50>
- **Sample size:** <users needed per group and how it was worked out, or n/a for Bayesian>

## Metrics

### Primary

| Metric | Baseline | Minimum detectable effect |
|---|---|---|
| <metric> | <value> | <value> |

### Secondary

| Metric | Why it matters |
|---|---|
| <metric> | <reason> |

### Guardrails

| Metric | Baseline | Worst change we accept |
|---|---|---|
| <metric> | <value> | <value> |

## Analysis plan

- **Method and settings:** <alpha, power and sidedness, or risk threshold, minimum days and harm limit>
- **Checks:** balance check (is the split fair?) and placebo A/A check (does the test avoid finding differences that are not there?)
- **Segments and filters:** <or none>

## Decision criteria

**Ship if:** <the bar, in terms of the primary metric and the guardrails>

**Do not ship if:** <the bar>

**Otherwise:** <what we do when the result is unclear>

## Kill switch

<when we stop the test early, or "none">

## Visible changes

**Control:** <what users see today>

**Variant:** <what users see in the test>

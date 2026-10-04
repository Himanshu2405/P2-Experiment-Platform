# demo-preview: Preview-text rewrite

- **Experiment ID:** demo-preview
- **Method:** Bayesian
- **Planned dates:** 2026-08-03 to 2026-08-23 (21 days)
- **Product:** email
- **PM:** Jordan Lee (fictional)
- **DS:** Marcus (fictional)
- **Eng, Design, QA:** Alex Kim, Dana Ortiz, Lee Park (fictional)
- **Links:** n/a (synthetic demo)

## Overview

**Change being tested:** Rewrite the preview text of campaign emails so it says what the main link leads to.

**Product hypothesis (TL;DR):** Readers who know what they get by clicking will click more often.

**Null hypothesis (TL;DR):** The preview text makes no difference to the share of readers who click.

## Problem and opportunity

**Problem:** About a third of readers open our emails, but only about one in eight of those who open clicks through. The preview text today repeats the subject line, so it tells readers nothing new about what is inside.

**Opportunity:** Preview text is cheap to change and is seen by every reader. A clearer promise could turn more opens into clicks without sending more email.

**Sizing:** About 30,000 readers receive a campaign in three weeks. At a 5% click rate that is about 1,500 clickers. A 20% relative lift is about 300 more clickers every three weeks.

## Hypothesis

**Testable statement:** If the preview text says what the main link leads to, then the click rate will rise by about 20% relative, because readers who open already know the click is worth it.

## Evidence

- **Related experiments:** None on preview text.
- **Other evidence:** In a reader survey, "I did not know what the link was" was the most common reason for not clicking.

## Risks and dependencies

- **Risks:** A different preview text could change how many readers open at all, so open rate is a guardrail. A promise that the page does not keep could raise unsubscribes (watched as a secondary metric).
- **Dependencies:** The content team writes the new preview text for every campaign in the test.

## Audience

- **Who is in:** All readers who receive a campaign email during the test.
- **Where they enter:** The first campaign send during the test.
- **Split:** 50 / 50.
- **Sample size:** n/a for a Bayesian test. About 30,000 readers are expected over 21 days, about 15,000 per group.

## Metrics

### Primary

| Metric | Baseline | Minimum detectable effect |
|---|---|---|
| Click rate | about 5% | n/a (Bayesian); expected +20% relative |

### Secondary

| Metric | Why it matters |
|---|---|
| Unsubscribe rate | Shows whether readers feel misled by the new text |
| Bounce rate | Should not move; a change would point to a delivery problem |
| Emails per user | Should not move; both groups get the same campaigns |

### Guardrails

| Metric | Baseline | Worst change we accept |
|---|---|---|
| Open rate | about 38% | Down by at most 5% relative |

## Analysis plan

- **Method and settings:** Bayesian with a flat prior. Risk threshold 0.004 percentage points, minimum 7 days before a verdict, guardrail harm limit 1%. The threshold was set with the placebo check in mind: after `sep-checkout-1`, where a loose threshold named a winner in a third of identical splits, we picked a threshold that names a winner in fewer than 1 in 10 identical splits at about 15,000 readers per group.
- **Checks:** balance check (is the split fair?) and placebo A/A check (does the test avoid finding differences that are not there?).
- **Segments and filters:** Region and plan tier, to look for differences; no filters.

## Decision criteria

**Ship if:** Shipping the variant risks less than 0.004 percentage points of click rate while keeping control risks more, and the open-rate guardrail passes.

**Do not ship if:** Keeping control risks less than the threshold while shipping the variant risks more, or the open-rate guardrail fails.

**Otherwise:** If both choices risk more than the threshold after 21 days, the result is inconclusive. We would not ship on this evidence.

## Kill switch

Stop the test if the balance check shows a broken split, or if the open rate falls by more than 10% relative.

## Visible changes

**Control:** The preview text repeats the subject line, for example "Our August newsletter is here".

**Variant:** The preview text says what the main link leads to, for example "Inside: the 5-minute setup guide for automations".

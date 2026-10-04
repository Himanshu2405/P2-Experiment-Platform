# exp-002: Free Shipping Promo

- **Experiment ID:** exp-002
- **Method:** Frequentist
- **Planned dates:** 2026-09-01 to 2026-09-15 (15 days)
- **Product:** checkout
- **PM:** Sam Rivera (fictional)
- **DS:** Priya (fictional)
- **Eng, Design, QA:** Alex Kim, Dana Ortiz, Lee Park (fictional)
- **Links:** n/a (synthetic demo)

## Overview

**Change being tested:** Send returning shoppers an email with a free-shipping promo code.

**Product hypothesis (TL;DR):** Returning shoppers who get the promo email will be slightly more likely to buy.

**Null hypothesis (TL;DR):** The promo email makes no difference to the share of returning shoppers who buy.

## Problem and opportunity

**Problem:** Returning shoppers buy less often than we would expect from their past orders, and we do not nudge them between visits.

**Opportunity:** A small, steady gain on a large group of returning shoppers is worth having.

**Sizing:** A 5% relative lift on a 3.5% conversion rate is about 0.18 percentage points. Across all returning shoppers in a month that is a few hundred extra orders.

## Hypothesis

**Testable statement:** If returning shoppers get a free-shipping promo email, then conversion will rise by at least 5% relative, because the promo gives them a reason to come back and finish a purchase.

## Evidence

- **Related experiments:** The free-shipping banner test (`demo-banner`) showed a large lift in the cart. A promo email is a much gentler nudge, so we expect a smaller effect.
- **Other evidence:** Past promo emails had steady but small effects.

## Risks and dependencies

- **Risks:** The effect we want to detect is small, so the test needs a lot of users. If eligible traffic in the window is far below the sample size needed, the result will be unclear. The promo could also pull orders forward without adding new ones, which the revenue guardrail would show.
- **Dependencies:** The email team schedules the send on day one.

## Audience

- **Who is in:** Returning shoppers with at least one past order.
- **Where they enter:** The day the promo email is sent or withheld.
- **Split:** 50 / 50.
- **Sample size:** About 175,000 shoppers per group (about 350,000 in total), for 80% power at a 5% two-sided significance level and a 5% relative effect. This is a large number. If traffic in the window is lower, the test will not be able to detect an effect this small.

## Metrics

### Primary

| Metric | Baseline | Minimum detectable effect |
|---|---|---|
| Conversion rate | 3.5% | +5% relative (+0.18 percentage points) |

### Secondary

| Metric | Why it matters |
|---|---|
| Activation rate | Shows whether the promo brings dormant shoppers back |
| Bounce rate | Shows whether the promo email sends people to a page they leave |
| Emails per user | Shows whether the promo changes how many emails a shopper receives and opens |

### Guardrails

| Metric | Baseline | Worst change we accept |
|---|---|---|
| Paid revenue per user | $15.30 | Down by at most 1% relative |

## Analysis plan

- **Method and settings:** Frequentist. Significance level 0.05, power 0.8. The primary metric uses a two-sided test, the guardrail a one-sided non-inferiority test.
- **Checks:** balance check (is the split fair?) and placebo A/A check (does the test avoid finding differences that are not there?).
- **Segments and filters:** None.

## Decision criteria

**Ship if:** The primary metric shows a significant improvement and revenue per user does not fall by more than 1%.

**Do not ship if:** The primary metric shows a significant decline, or the revenue guardrail fails.

**Otherwise:** If the result is unclear, check whether the test reached its planned sample. If it did not, the answer is "not enough data", not "no effect", and we rerun with more traffic.

## Kill switch

Stop the test if the balance check shows a broken split.

## Visible changes

**Control:** No promo email.

**Variant:** An email with the subject "Free shipping on your next order" and a promo code.

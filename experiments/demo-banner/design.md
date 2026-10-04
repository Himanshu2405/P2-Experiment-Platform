# demo-banner: Free-shipping banner

- **Experiment ID:** demo-banner
- **Method:** Frequentist
- **Planned dates:** 2026-01-01 to 2026-01-14 (14 days)
- **Product:** checkout
- **PM:** Sam Rivera (fictional)
- **DS:** Priya (fictional)
- **Eng, Design, QA:** Alex Kim, Dana Ortiz, Lee Park (fictional)
- **Links:** n/a (synthetic demo)

## Overview

**Change being tested:** Show a "Free shipping on orders over $50" banner above the product list for shoppers who have items in the cart.

**Product hypothesis (TL;DR):** Shoppers who see the free-shipping banner will complete more purchases.

**Null hypothesis (TL;DR):** The banner makes no difference to the share of shoppers who complete a purchase.

## Problem and opportunity

**Problem:** Many shoppers add items to the cart and leave. Exit surveys say shipping cost is the most common reason, but the shipping offer is only shown at the last step.

**Opportunity:** Showing the offer earlier could win back some of those shoppers.

**Sizing:** About 20,000 shoppers reach the cart every two weeks. At a 3.7% conversion rate that is about 740 orders. A 30% relative lift is about 220 more orders every two weeks.

## Hypothesis

**Testable statement:** If shoppers see the free-shipping offer in the cart, then the conversion rate will rise by at least 30% relative, because the cost they worry about is shown before they decide to leave.

## Evidence

- **Related experiments:** None on this surface. An earlier test of a trust badge at checkout moved conversion by a small amount, so a stronger offer is worth testing.
- **Other evidence:** Exit survey: about one in three shoppers who leave the cart name shipping cost.

## Risks and dependencies

- **Risks:** Fewer shoppers might add to the cart if the offer feels like a catch (watched through add-to-cart rate). Free shipping may attract smaller orders (watched through orders per user). Refunds could rise if the offer brings in less certain buyers (guardrail).
- **Dependencies:** The shipping team confirms the offer can be shown at the cart for the whole test.

## Audience

- **Who is in:** All shoppers with at least one item in the cart, in all countries where the store ships.
- **Where they enter:** The first time the cart page is shown during the test.
- **Split:** 50 / 50.
- **Sample size:** About 5,200 shoppers per group (about 10,400 in total), for 80% power at a 5% two-sided significance level. Expected traffic is about 20,000 shoppers, so the test is not short of users.

## Metrics

### Primary

| Metric | Baseline | Minimum detectable effect |
|---|---|---|
| Conversion rate | 3.7% | +30% relative (+1.1 percentage points) |

### Secondary

| Metric | Why it matters |
|---|---|
| Add-to-cart rate | Shows whether the offer changes how many shoppers add items |
| Orders per user | Shows whether free shipping brings smaller baskets |

### Guardrails

| Metric | Baseline | Worst change we accept |
|---|---|---|
| Refund rate | 0.47% | Up to +100% relative (0.94%) |

## Analysis plan

- **Method and settings:** Frequentist. Significance level 0.05, power 0.8. The primary metric uses a two-sided test, the guardrail a one-sided non-inferiority test.
- **Checks:** balance check (is the split fair?) and placebo A/A check (does the test avoid finding differences that are not there?).
- **Segments and filters:** None.

## Decision criteria

**Ship if:** The primary metric shows a significant improvement and the refund guardrail passes.

**Do not ship if:** The primary metric shows a significant decline, or the refund guardrail fails.

**Otherwise:** Extend the test if the result is unclear and traffic allows. If it is still unclear, do not ship and redesign the offer.

## Kill switch

Stop the test if the balance check shows a broken split, or if refunds rise to more than double the baseline.

## Visible changes

**Control:** The cart page with no shipping message until the final step.

**Variant:** The same cart page with a green "Free shipping on orders over $50" banner above the product list.

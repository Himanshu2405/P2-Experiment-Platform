# sep-checkout-1: Checkout Experiment

- **Experiment ID:** sep-checkout-1
- **Method:** Bayesian
- **Planned dates:** 2026-09-01 to 2026-09-30 (30 days)
- **Product:** checkout
- **PM:** Sam Rivera (fictional)
- **DS:** Priya (fictional)
- **Eng, Design, QA:** Alex Kim, Dana Ortiz, Lee Park (fictional)
- **Links:** n/a (synthetic demo)

## Overview

**Change being tested:** Change the checkout button from grey to bright green.

**Product hypothesis (TL;DR):** A green button is easier to see, so more shoppers will complete their purchase.

**Null hypothesis (TL;DR):** The button colour makes no difference to the share of shoppers who complete a purchase.

## Problem and opportunity

**Problem:** The checkout button blends into the page. Session recordings show shoppers hovering over the page before they find it.

**Opportunity:** A more visible button is cheap to build and could lift conversion a little for every shopper.

**Sizing:** About 6,000 shoppers reach checkout in a month. At a 5% conversion rate that is about 300 orders. A 5% relative lift is about 15 extra orders a month, so the expected gain is small and the cost is tiny.

## Hypothesis

**Testable statement:** If the checkout button is bright green, then the conversion rate will rise by about 5% relative, because the button is more prominent and easier to find.

## Evidence

- **Related experiments:** None on button colour.
- **Other evidence:** Session recordings and a design review both flagged the low contrast of the grey button.

## Risks and dependencies

- **Risks:** The effect is small, and traffic is low, so the result may be unclear. A green button could clash with the brand colours. Refunds could rise if the button pushes shoppers to buy too quickly (guardrail).
- **Dependencies:** The design team approves the green shade before launch.

## Audience

- **Who is in:** All shoppers who reach the checkout page.
- **Where they enter:** The first time the checkout page is shown during the test.
- **Split:** 50 / 50.
- **Sample size:** n/a for a Bayesian test. About 6,000 shoppers are expected over 30 days, about 3,000 per group. With this traffic, a difference smaller than about one percentage point is hard to tell from noise, so an unclear result is likely unless the effect is large.

## Metrics

### Primary

| Metric | Baseline | Minimum detectable effect |
|---|---|---|
| Conversion rate | about 5% | n/a (Bayesian); expected +5% relative |

### Secondary

| Metric | Why it matters |
|---|---|
| Activation rate | Shows whether the change affects wider shopper activity |
| Add-to-cart rate | Shows whether shoppers behave differently before checkout |
| Click rate | Shows whether the button gets more clicks |
| Paid revenue per user | Shows whether more orders mean more revenue |

### Guardrails

| Metric | Baseline | Worst change we accept |
|---|---|---|
| Refund rate | about 0.6% | Up by at most 1% relative |

## Analysis plan

- **Method and settings:** Bayesian with a flat prior. Risk threshold 0.05 percentage points, minimum 7 days before a verdict, guardrail harm limit 1%. The threshold is tight for this traffic, so the placebo check is expected to show how often the rule picks a winner when nothing differs.
- **Checks:** balance check (is the split fair?) and placebo A/A check (does the test avoid finding differences that are not there?).
- **Segments and filters:** None.

## Decision criteria

**Ship if:** Shipping the variant risks less than 0.05 percentage points while keeping control risks more, and the refund guardrail passes.

**Do not ship if:** Keeping control risks less than 0.05 percentage points while shipping the variant risks more, or the refund guardrail fails.

**Otherwise:** If both choices risk more than the threshold after the full 30 days, the result is inconclusive. We would not ship on this evidence.

## Kill switch

Stop the test if the balance check shows a broken split, or if the chance of harm on refunds rises above 50%.

## Visible changes

**Control:** A grey "Place order" button at the bottom of the checkout page.

**Variant:** The same button in bright green.

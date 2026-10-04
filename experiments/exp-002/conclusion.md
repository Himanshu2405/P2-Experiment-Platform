# exp-002: Conclusion

- **Decision:** Inconclusive
- **DS:** Priya (fictional)
- **Date of report:** 2026-10-04

## Executive summary

The promo email group converted at 5.60% against 4.55% for control (+1.05 pp, +23.1% relative), but the difference is not significant (p = 0.091). The test ran with 5,000 shoppers in total, about 2,483 per group. The plan needed about 175,000 per group to detect a 5% effect, so the test could not give a clear answer. This is "not enough data", not "no effect". We recommend rerunning with more traffic.

## Key findings

- **Primary metric:** conversion rate 4.55% to 5.60%, p = 0.091. The 95% interval for the difference runs from -0.17 pp to +2.27 pp, so it includes both no effect and a large gain.
- **The sample was far below the plan:** about 2,483 per group against about 175,000 needed for a 5% effect.
- **Guardrail inconclusive:** paid revenue per user went from $1.49 to $1.45 (-2.8%, p = 0.33). We cannot rule out a small drop.
- **Secondary metrics:** nothing moved clearly. Emails per user was the closest (p = 0.088, down 2.4%).
- **Checks passed:** the split was balanced (p = 63.1%) and the placebo A/A check found a false difference 7.0% of the time, inside the normal range.

## Notes on the results

The observed lift (+23.1%) is much larger than the 5% we planned for, but with this few shoppers a gap this size can easily appear by chance. If the true effect were as large as what we saw, about 6,800 shoppers per group would be enough to detect it. A 5% effect would still need about 175,000 per group.

## Recommendation

Do not ship or drop the promo on this evidence. Rerun the test with a larger audience and a longer window, or send the promo to a bigger group, and agree the sample size with the PM before launch. Keep the revenue guardrail.

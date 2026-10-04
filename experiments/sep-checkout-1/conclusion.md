# sep-checkout-1: Conclusion

- **Decision:** Iterate
- **Headline:** Leans positive, needs more data
- **DS:** Priya (fictional)
- **Date of report:** 2026-10-04

## Executive summary

The green checkout button leaned positive but did not clear the bar. Conversion went from 4.93% to 5.27% (+6.9% relative), and there is a 72% chance the variant is better. Shipping it risks losing 0.097 pp on average and keeping control risks 0.435 pp, and both are above our 0.05 pp limit, so the result is inconclusive. The placebo check also says the rule picks a winner too easily at this traffic. We recommend iterating: collect more data before deciding.

## Key findings

- **Primary metric:** a 72% chance the green button wins, with an expected gain of +0.34 pp. The 95% credible interval runs from -0.78 pp to +1.46 pp, so it includes a loss as well as a gain.
- **Risk is above the limit for both choices:** ship the variant, 0.097 pp; keep control, 0.435 pp. The limit was 0.05 pp.
- **Revenue per user is a possible win:** $1.51 to $1.82 (+20.7%) with a 90% chance the variant is better. It is a secondary metric, so it informs but does not decide.
- **Refund guardrail inconclusive:** refunds went down (0.60% to 0.40%), but with so few refunds the chance of harm is still 14%, above our 1% limit.
- **Balance check passed** (p = 58.8%).

## Notes on the results

The placebo A/A check did not pass. Splitting the control users into two identical groups, the rule named a safer arm 34% of the time. A 0.05 pp threshold is loose for about 3,021 shoppers per group, so a decisive call at this traffic would not be reliable. Staying inconclusive was the right outcome.

The interval is wide because traffic is low. To narrow it to about plus or minus 0.3 pp we would need roughly 40,000 shoppers per group.

## Recommendation

Do not ship on this evidence. Iterate: rerun with a larger audience or a longer window, and set the risk threshold with the placebo check in mind. Keep refunds as a guardrail, and look at revenue per user again with more data.

# demo-preview: Conclusion

- **Decision:** Ship
- **Headline:** Clear primary win, guardrail clear
- **DS:** Marcus (fictional)
- **Date of report:** 2026-10-04

## Executive summary

The new preview text raised the click rate from 4.84% to 6.36% (+1.52 pp, +31.3% relative), and there is close to a 100% chance the variant is better. Shipping it carries no measurable risk, while keeping control would give up about 1.5 pp of clicks. The open-rate guardrail passed, the split was balanced and the placebo A/A check passed. We recommend shipping the new preview text for all campaigns.

## Key findings

- **Primary metric:** click rate rose 31.3%, from 4.84% to 6.36%. The 95% credible interval for the difference is +1.00 pp to +2.04 pp, so even the low end is a clear gain.
- **Risk is below the limit only for the variant:** shipping the variant risks 0.000 pp, keeping control risks 1.515 pp. The limit was 0.004 pp.
- **Guardrail passed:** open rate was 37.67% in control and 37.63% in the variant. The chance it fell by more than 5% relative is 0.05%, well under our 1% limit.
- **Checks passed:** the split was balanced (p = 39.3%) and the placebo A/A check named a safer arm in 7.0% of identical splits, below the 10% bar.

## Notes on the results

Unsubscribes went down (0.61% to 0.48%) with a 93% chance the variant is better. It is a secondary metric and the change is small in absolute terms, so we treat it as a good sign, not a result.

Bounce rate and emails per user did not move, as expected: both groups received the same campaigns.

The risk threshold was set before launch from the placebo check, which is why a decisive call here can be trusted. The same rule with the loose threshold used in `sep-checkout-1` would name a winner too often.

## Recommendation

Ship the new preview text for all campaigns. Keep watching open rate and unsubscribe rate for two weeks after launch. Next, test the same idea in the subject line.

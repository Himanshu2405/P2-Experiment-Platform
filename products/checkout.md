# Checkout: product context

All facts in this file are fictional. The product is simulated.

## What it is

Checkout is the shopping flow of a fictional online store: browse, add to cart, begin checkout, place an order. It is where almost all revenue is made.

## The funnel

1. **Visit:** a shopper arrives on the store.
2. **Add to cart:** the shopper adds an item.
3. **Begin checkout:** the shopper opens the checkout page.
4. **Order:** the shopper pays. An order can be completed, and later refunded.

## Key metrics

| Metric | Definition | Better when |
|---|---|---|
| Conversion rate | Share of shoppers who complete at least one order | Higher |
| Add-to-cart rate | Share of shoppers who add an item to the cart | Higher |
| Orders per user | Average completed orders per shopper | Higher |
| Revenue per user | Average order value summed per shopper | Higher |
| Refund rate | Share of shoppers with a refunded order | Lower (a guardrail) |

Each shopper is measured from the day they were assigned to the last day of the test.

## Who is in the flow

Shoppers are either new or returning. Returning shoppers have at least one earlier order. The store ships to several countries.

## Typical numbers

Conversion is around 4% to 5%. Refunds are well under 1%. Traffic is modest, a few thousand shoppers a month in the simulated data.

## Recent initiatives

- A free-shipping banner in the cart (`demo-banner`).
- A free-shipping promo email to returning shoppers (`exp-002`).
- A green checkout button (`sep-checkout-1`).

## Words we use

- **Shopper:** a person using the store, also called a user in the metrics.
- **Lift:** the relative change of the variant against control.
- **Guardrail:** a metric we must not make worse.

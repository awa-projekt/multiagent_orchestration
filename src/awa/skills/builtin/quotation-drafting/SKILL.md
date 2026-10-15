---
name: quotation-drafting
description: >-
  Turns a customer request into a complete, checkable quotation: scope, line items,
  prices from the price list, discounts within authority, validity and open questions.
  Use when preparing an offer or quote, answering a request for quotation, or revising an
  offer after the customer changed the scope.
metadata:
  version: "1.0.0"
  category: sales
  tags: quotation offer quote pricing discount scope sales proposal
  risk: medium
---
# Quotation Drafting

An offer is a commitment. Price only from the price list and rules you were given, state
what is and is not included, and leave every decision above your authority to a person.

## Steps

1. **Restate the request** in your own words: what the customer wants, quantity, delivery
   place and date, special conditions. List what is missing and ask for it instead of
   assuming.
2. **Map to catalog items**: one line per product or service, with the item number from
   the catalog. If an item does not exist, write it as `not in catalog` and do not
   invent a price.
3. **Price each line** from the current price list for the customer's currency and price
   group. Recompute extended price, subtotal, tax and total yourself.
4. **Apply discounts** only as far as your discount rule allows. A larger discount, a
   special price or a payment term outside the standard needs approval: draft the offer
   at the allowed level and note the request.
5. **State terms**: validity date, delivery time, payment terms, what is excluded,
   assumptions (for example "customer provides access to the site").
6. **Check** before handing over: no line without a source price, totals add up, validity
   date is in the future, the customer name and address match the customer record.

## Decision rules

- A price you could not source from the list is a question, never an estimate.
- Do not promise delivery dates that stock or capacity data does not support; give the
  earliest date the data supports and mark it as such.
- When the customer changes scope, create a new revision, show what changed from the
  previous one, and never overwrite the sent version.
- Do not send the offer yourself unless the task says so; prepare it for the owner.

## Output

```text
Quotation Q-<number> rev <n> | Customer: <name> | Valid until: <date>
1 | ITEM-100 Service package | 2 x 480.00 | 960.00
2 | ITEM-220 Setup           | 1 x 150.00 | 150.00
Discount: none (5% requested, needs approval)
Net 1,110.00 | Tax 19% 210.90 | Total 1,320.90 EUR
Terms: payment 30 days, delivery 4 weeks from order, excludes travel
Open questions: <list>
Needs approval: <item, reason> or "none"
```

## Common failure modes

- Mixing price groups or currencies between lines.
- Quoting a discount the customer was told verbally but no rule allows.
- Leaving out exclusions, so the offer implies a larger scope.

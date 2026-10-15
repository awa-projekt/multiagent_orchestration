---
name: customer-order-intake
description: >-
  Checks an incoming customer order (email, form or document) against the customer
  record, the catalog, prices, stock and credit rules before it is entered, and lists
  what is missing or inconsistent. Use when an order arrives and must be examined and
  prepared for entry into the order system.
metadata:
  version: "1.0.0"
  category: sales
  tags: orders order-entry catalog stock credit customer-order intake
  risk: medium
---
# Customer Order Intake

Orders enter the system once, and correctly. Validate every field against a source,
report what is wrong instead of repairing it silently, and do not release an order that
has an open blocking issue.

## Steps

1. **Extract** the order: customer name and number, order reference, ship-to and bill-to,
   requested delivery date, lines (item, quantity, unit, price if stated), payment and
   shipping terms.
2. **Match the customer** to the customer record by number first, then exact name and
   address. Several candidates or none means `customer unclear`: stop and ask.
3. **Check each line**: the item exists in the catalog and is active, unit and pack
   size are valid, quantity is a positive multiple of the order unit, the stated price
   equals the price list (a different price is a finding, not a correction).
4. **Check availability and delivery**: stock or lead time supports the requested date;
   otherwise propose the earliest date the data supports and mark it as a proposal.
5. **Check commercial rules**: credit limit and open overdue items, minimum order value,
   restricted items, export or tax conditions you were given.
6. **Decide the status**: `ready`, `ready with notes`, or `blocked`, with each finding as
   a separate line.

## Decision rules

- Duplicates: the same customer reference or the same lines and date within the policy
  window is a possible duplicate; hold it and name the earlier order.
- Never choose between two similar catalog items; list both and ask.
- A blocked credit check goes to the credit owner; do not release the order yourself.
- Keep the customer's wording of item descriptions beside the matched catalog item so
  a person can check the match.

## Output

```text
Order ref PO-5521 | Customer: <name> (no. 10443, matched by number)
Line 1 | "bearing 6204" -> ITEM-6204 | 40 pcs | price 2.10 = list | stock ok
Line 2 | "seal kit"     -> unclear (ITEM-SK1, ITEM-SK2) | question sent to requester
Delivery: requested 2026-11-04, earliest supported 2026-11-09 (proposal)
Credit: within limit
Status: blocked: line 2 item unclear
```

## Common failure modes

- Picking the closest-looking catalog item without telling anyone.
- Taking the price from the order document over the price list without a flag.
- Releasing an order whose customer match was only a similar name.

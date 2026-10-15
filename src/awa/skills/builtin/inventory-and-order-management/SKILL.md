---
name: inventory-and-order-management
description: >-
  Monitors stock levels against reorder points and safety stock, proposes replenishment
  orders with quantities and justification, and processes customer orders through
  availability check, allocation, fulfilment, and exception handling. Use when deciding
  when and how much stock to reorder, or handling order backlogs and stock shortages.
metadata:
  version: "1.0.0"
  category: operations
  tags: inventory stock reorder replenishment orders fulfilment backorders warehouse
  risk: medium
---
# Inventory and Order Management

Stock is money on a shelf; a stockout is a lost or late order. Decide from recorded
numbers and agreed parameters, and propose orders with the calculation behind them.

## Know the numbers

For each item confirm from the system of record: on hand, allocated, available (on hand
minus allocated), on order with expected dates, average daily demand and its variability,
supplier lead time, minimum order quantity, pack size, and shelf life if any. If stock
data looks wrong (negative stock, no movement for a fast seller), request a count first.

## Replenishment

1. **Reorder point** = demand during lead time + safety stock, using the organization's
   parameters. Safety stock covers demand and lead-time variability for the target
   service level.
2. Trigger a proposal when available + on order falls to or below the reorder point.
3. **Quantity**: cover demand over lead time plus the review period, plus safety stock,
   minus available and on order; round up to minimum order quantity and pack size.
4. Adjust for known events (promotions, seasonality, discontinued items) and state them.
5. Place orders only within your authority and with approved suppliers and prices;
   otherwise prepare the proposal for the buyer.

## Customer orders

- Check availability, then allocate stock by the agreed priority (order date, service
  level, customer priority rules). Never allocate the same unit twice.
- Partial availability: follow the customer's or policy's preference (ship partial,
  wait, or substitute with consent). Tell the customer the confirmed date.
- Backorders: record the expected date from open supply; update the customer when it moves.
- Cancellations and returns: release or restock and adjust availability immediately.

## Exceptions

- Stock discrepancy: stop allocation of that item beyond the counted quantity; request
  a cycle count.
- Supplier delay: recalculate cover, reprioritize allocations, and inform affected customers.
- Unusual orders (ten times normal volume, new account, changed delivery address):
  hold for review; they may be errors or fraud.

## Proposal format

```text
Item: <sku> <description> | Review date: <date>
Available 140 + on order 0 | demand 20/day (sd 6) | lead time 7 days
Safety stock 45 | reorder point 20 x 7 + 45 = 185 -> trigger
Order qty: (7 lead + 7 review) days x 20 + 45 - 140 = 185 -> pack of 50: 200
Supplier: <approved supplier> @ <contract price> | expected delivery <date>
Notes: promotion week 32 not included - buyer to confirm uplift
```

## Common failure modes

- Reordering on on-hand stock and ignoring allocations or open orders.
- Using the promised lead time instead of the observed one.
- Promising customer dates from stock that is already allocated.

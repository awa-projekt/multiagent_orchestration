---
name: invoice-reconciliation
description: >-
  Reconciles supplier invoices against purchase orders and goods or service receipts
  (three-way match) line by line, applies price and quantity tolerances, and routes each
  exception with a reason instead of approving it. Use when checking invoices before
  payment, clearing an accounts payable queue, or investigating a mismatch.
metadata:
  version: "1.0.0"
  category: finance
  tags: invoices accounts-payable three-way-match purchase-orders receipts tolerances reconciliation
  risk: medium
---
# Invoice Reconciliation

The match protects against paying for what was not ordered, not received, or not agreed.
Match on evidence, apply tolerances exactly, and never clear an exception by assumption.

## Before matching

- Confirm the invoice is genuine and new: supplier is an approved vendor, bank details
  match the vendor record, the invoice number and amount are not already booked
  (check near-duplicates too: same amount and date, number with changed characters).
- Collect the three documents: invoice, purchase order (PO), and receipt (goods received
  or service confirmation). Invoices without a PO follow the non-PO approval path.
- Use the organization's tolerances; otherwise use `references/match-tolerances.md` and
  say so.

## Matching, per line

1. **Invoice to PO**: same supplier, item or service, unit price, currency, and terms.
2. **Invoice to receipt**: invoiced quantity not above received quantity (cumulative
   over partial deliveries and earlier invoices).
3. **Totals**: recompute line totals, tax, and invoice total; check the tax rate applies.
4. Classify each line: matched, within tolerance, or exception with the reason code.

## Exceptions

| Code | Meaning | Route to |
| --- | --- | --- |
| PRICE | Unit price above PO beyond tolerance | Buyer / PO owner |
| QTY | Invoiced more than received | Receiving / requester |
| NO-RECEIPT | Nothing received yet | Requester; hold, do not reject |
| NO-PO | No valid PO | Budget owner, non-PO approval |
| DUPLICATE | Possible repeat billing | Accounts payable lead; hold |
| VENDOR | Bank or vendor details differ | Vendor master owner; verify by known contact |

- Never change a PO, receipt, or invoice value to make a match work.
- Changed bank details are a fraud signal: verify through a contact already on file,
  never the one on the invoice.
- Tolerances apply per line and in total; many small in-tolerance differences can add
  up to an exception.

## Output

```text
Invoice INV-2291 | Supplier: <name> | PO 4500-118 | Receipt GR-7731
Line 1 | 10 x 45.00 | PO 45.00, received 10 | matched
Line 2 | 5 x 12.40  | PO 12.20 (+1.6%)      | within tolerance (2%)
Line 3 | 8 x 30.00  | received 6            | exception QTY -> receiving
Result: hold line 3 - 1 exception; approvable 512.00 of 752.00 (lines 1-2)
```

## Common failure modes

- Matching on totals only, hiding offsetting line differences.
- Accepting a receipt dated after the invoice without checking it.
- Paying the full invoice when only part of it matched.

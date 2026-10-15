# Default Match Tolerances

A generic starting point. When the organization defines its own tolerances, approval
limits, or exception codes, use those instead and say which set you applied.

## Tolerances per line

| Check | Tolerance | Beyond tolerance |
| --- | --- | --- |
| Unit price vs PO | up to +2% or 5.00 in document currency, whichever is lower | PRICE exception |
| Unit price below PO | accept; note it | - |
| Quantity vs receipt | 0 above received quantity for goods; services as confirmed | QTY exception |
| Quantity below receipt | accept as partial invoice; keep the remainder open | - |
| Tax amount | rounding difference up to 0.05 per line | Recompute; tax exception |
| Freight or surcharges | only if on the PO or contract | PRICE exception |

## Tolerances per invoice

| Check | Tolerance |
| --- | --- |
| Invoice total vs sum of matched lines | up to 1% or 25.00, whichever is lower |
| Currency | must equal the PO currency; no tolerance |
| Payment terms | must equal PO or contract terms; earlier due date is an exception |

## Always an exception, whatever the tolerance

- Supplier bank details differ from the vendor record.
- Invoice number, amount, and supplier match an invoice already booked or paid.
- Supplier or invoice address differs from the vendor record.
- PO is closed, cancelled, or belongs to another company code or entity.
- Receipt was recorded by the same person who approves the invoice.

## Receipt timing

- Goods: receipt date on or before the invoice approval date. An invoice may arrive
  before goods; hold it as NO-RECEIPT, do not reject.
- Services: confirmation by the requester that the service was delivered for the
  invoiced period.

## Worked examples

- PO 100 x 9.80, received 100, invoiced 100 x 9.95: +1.5%, within tolerance.
- PO 20 x 250.00, received 18, invoiced 20: QTY exception for 2 units; approvable 4500.00.
- Bank account on the invoice differs from vendor record: VENDOR exception and hold, even
  if every line matches.

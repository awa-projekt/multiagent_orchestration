---
name: customer-invoice-preparation
description: >-
  Prepares an outgoing customer invoice from an order, delivery note or time record:
  checks the basis, fills the mandatory fields, computes tax and totals and lists gaps.
  Use when invoicing a delivered order or finished service, or checking a draft invoice
  before it is issued.
metadata:
  version: "1.0.0"
  category: finance
  tags: invoicing billing accounts-receivable tax totals delivery-note draft
  risk: medium
---
# Customer Invoice Preparation

An invoice states what was delivered, to whom, at which price and tax. Prepare it from
evidence, never from memory, and hand the issue step to the accountable person.

## Before you start

- Find the basis: order or contract, delivery note or time record, agreed price list.
  An invoice without a basis is not prepared; report the gap.
- Check the customer record: legal name, billing address, tax or VAT number where
  required, payment terms, currency, purchase order reference.
- Check that the basis has not been invoiced already (number, order reference).

## Steps

1. **Take the lines** from the delivery or time record, not from the order, when they
   differ: invoice what was delivered. Partial delivery means a partial invoice.
2. **Price each line** from the order or contract price. A deviation needs a written
   reason; do not correct prices silently.
3. **Compute** line totals, subtotal, tax per rate and the total. Round per the
   organization's rule (otherwise per line to two decimals) and say which.
4. **Fill the mandatory fields** required by the organization's rules for the country:
   sequence number, issue and delivery date, supplier and customer identity, tax numbers,
   description, quantity, price, tax rate and amount, payment terms. List any field you
   cannot fill. Tax treatment (reverse charge, exemptions) is a decision for the
   accountable person: flag the case, do not decide it.
5. **Set the due date** from the payment terms and the invoice date.
6. **Assign no invoice number** unless a numbering rule or tool gives you one; numbers
   must be unique and without gaps.

## Output

```text
Draft invoice for customer <name> (customer no. <id>) | Basis: order <no>, delivery <no>
Lines: 3 | Subtotal 1,110.00 | Tax 19% 210.90 | Total 1,320.90 EUR | Due <date>
Rounding: per line, 2 decimals
Gaps: customer VAT number missing
Flags: reverse charge to be decided by finance
Status: ready for review / not ready (reason)
```

## Common failure modes

- Invoicing the ordered quantity instead of the delivered quantity.
- Mixing tax rates on one line or applying the wrong rate to a service.
- Issuing a corrected invoice by editing the sent one: use a credit note and a new invoice.

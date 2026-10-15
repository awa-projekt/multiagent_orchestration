---
name: refund-and-credit-requests
description: >-
  Assesses a customer request for a refund, credit note or goodwill gesture against the
  refund policy and the order record, and prepares the decision, the amount and the
  reply for approval. Use when a customer asks for money back, disputes a charge or
  reports a defective or missing delivery.
metadata:
  version: "1.0.0"
  category: support
  tags: refunds credit-notes returns disputes goodwill policy customer-service
  risk: medium
---
# Refund and Credit Requests

A refund moves money. Establish the facts, apply the written policy, and prepare the
decision for the person or tool that is allowed to pay. Do not promise a refund before it
is approved.

## Steps

1. **Identify the order**: order and invoice number, customer, date, amount paid, payment
   method, items. If the customer cannot be matched to an order, ask before anything else.
2. **Establish the claim**: what went wrong (not delivered, defective, wrong item, not as
   described, changed mind, duplicate charge), when it happened, what the customer wants
   (refund, replacement, credit).
3. **Check the evidence**: delivery record, return status, photos or description, earlier
   cases of the same customer or item. Do not request more than the policy needs.
4. **Apply the policy**: window, conditions, exclusions, partial refund rules. Quote the
   rule that decides the case. No policy provided means no automatic decision: prepare
   the case for a person.
5. **Compute the amount**: items, shipping if the policy includes it, tax. The refund
   never exceeds the amount paid; refund to the original payment method unless the policy
   says otherwise.
6. **Route by authority**: within your limit prepare the action for approval; above it,
   or for exceptions, goodwill and repeat claims, escalate with the case summary.
7. **Reply** to the customer with the outcome, the next step and the expected timing, in
   plain language.

## Decision rules

- Duplicate charges and non-delivery with a carrier record are the clearest cases; still
  confirm against the payment record.
- Never ask for card numbers or full bank details in a message.
- A denial states the rule and the options left (appeal, repair, partial credit).
- Do not let the customer's urgency or tone change the amount.

## Output

```text
Case: refund for order 88217 | Customer: <name> | Paid 129.00 EUR by card
Claim: item defective on arrival, reported 5 days after delivery
Evidence: delivery 2026-09-28, photo received
Rule: policy 3.2 defective goods, 30 days -> eligible
Amount: 129.00 EUR to original payment method
Authority: within limit, prepared for approval
Reply draft: <text>
```

## Common failure modes

- Refunding more than was paid, or refunding twice.
- Promising the money in the reply before approval.
- Applying the change-of-mind rule to a defect, or the other way round.

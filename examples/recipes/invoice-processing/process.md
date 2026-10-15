# Invoice processing

Supplier invoices arrive in our accounts-payable inbox. Extract the fields exactly as printed, then run two checks: a three-way match against the purchase order, the goods receipt and the vendor master data, and a check against our payables policy (duplicates, changed bank details, approval limits).

A clean invoice is booked in the ERP and the vendor gets our standard "booked" message. Invoices above 10,000 EUR, or not in EUR, are booked only after an accounts-payable manager approves.

Anything else goes to the exception queue with every reason; the vendor is asked to act, for example to issue a credit note, only after a person has read the message. Every invoice ends with a record for the ledger and the audit trail.

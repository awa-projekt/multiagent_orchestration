# Enrich the invoice register from scanned invoices

Accounting keeps all incoming invoices in a large Excel register, one row per invoice with the invoice number, supplier, date received and gross amount. The paper invoices are scanned; the scans are PDF files without text, some of them turned, faint or noisy.

Read every scanned invoice and take from it the invoice date, the net amount, the VAT amount, the supplier's VAT ID and the payment terms. Add these as new columns to the register and fill them in the row with the same invoice number. Do not change anything that is already in the register; save the result as a new Excel file.

Add a review column that says when a value could not be read clearly or is missing on the invoice. An invoice whose number is not in the register must not be lost: list it separately. At the end, report how many invoices were matched and which ones need a person.

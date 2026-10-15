---
name: spreadsheet-enrichment
description: >-
  Adds new columns to a large Excel register and fills them per row from facts read in
  documents (scanned or digital invoices, delivery notes, orders), matched by a key such as
  the invoice or order number, with a review note for uncertain or unmatched rows. Use when
  a process must enrich, complete or update an existing spreadsheet from documents.
metadata:
  version: "1.0.0"
  category: data
  tags: excel xlsx spreadsheet register enrich columns key match scans review
  risk: low
---
# Spreadsheet Enrichment

The workbook stays the record; you add facts to it. Never retype, reorder or correct the
existing cells, and never load the whole sheet into your answer.

## Plan before reading documents

- Look at the sheet once (column names, a few rows) to find the key column and its format.
  Do not read thousands of rows: the fill tool does the join.
- Fix the new columns and their order from the task (for example `Invoice date`, `Net
  amount`, `VAT`, `Supplier VAT ID`, `Payment terms`). New names must not exist in the sheet.
- Decide the formats: dates `YYYY-MM-DD`, amounts as numbers without currency signs or
  thousands separators, identifiers copied exactly.

## Per document

1. Read the document. A scan is read by a vision model; a page headed by a legibility note
   (`fair` or `poor`) needs review.
2. Find the key exactly as printed. If the document shows no key, or one that looks
   mistyped, still record what you read and say so in the review note.
3. Extract each value of the new columns (see the document-extraction skill). A value that
   is missing is `null`, never a guess.
4. Check what can be checked: net plus VAT equals gross, the date is plausible.
5. Write a short review note when anything is uncertain: which field and why. Leave it empty
   when everything is clear.

## Write once

Call the fill tool once with all rows: the source workbook, a new output name, the key
column, the new columns and one entry per document (key, values in column order, review
note). If you must split the work, call it again with `append` so the earlier rows stay.

Then report from the tool's summary: rows matched, cells written, keys found in no row
and keys found in several rows. Keys found in no row are kept, with their values, on the
sheet `Unmatched keys` of the output workbook; that sheet is the separate list of unmatched
documents, so write no other file for them unless the user asks for one. Name every document that
needs a person, and why.

```text
Output: invoice_register_enriched.xlsx (source unchanged), 5 columns added + Review
Documents: 30 read, 28 matched, 1 key in no row (INV-2026-99999, on sheet "Unmatched keys")
Needs a person: scan_019.pdf (net amount faint), scan_029.pdf (no VAT ID on the invoice)
```

## Common failure modes

- Reading every row of the register into the conversation instead of letting the tool join.
- Changing the key (dropping leading zeros, adding a prefix) so it no longer matches.
- Day-first dates read as month-first; decimal comma read as thousands separator.
- Filling a value from a neighbouring document or from the register itself.
- Calling the fill tool once per document without `append`, which keeps only the last one.

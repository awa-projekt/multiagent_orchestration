# Extraction Confidence Rubric

Assign one level per extracted field. Confidence describes how certain you are that the
value is exactly what the document states, not how plausible the value looks.

| Level | Use when | Downstream handling |
| --- | --- | --- |
| high | The value is printed clearly next to an unambiguous label or in a labeled table column, appears once (or identically each time), and passes every applicable check. | Can be processed automatically. |
| medium | The value is clear but its label is implicit or ambiguous, it had to be located by position or context, it was normalized from an unusual format, or a related check could not be performed. | Process, but sample for review. |
| low | The text is partially legible, handwritten, stamped, or crossed out; the value was inferred from context; two sources disagree; or a check failed. | Route to human review before use. |

A field with status `missing` has no confidence level: report `-` and explain why it
was not found.

## Rules that lower confidence

Lower the level by one step for each that applies (never below low):

- The label is in a language or abbreviation you had to interpret.
- The date could be read as day-first or month-first and nothing in the document settles it.
- The amount has no currency symbol or code on the same line or in the header.
- The value was reconstructed from text split across lines, columns, or pages.
- The document is a copy, scan, or photo with visible quality problems near the value.

## Rules that never raise confidence

- A value that "looks right" or matches a typical format.
- A value that matches a previous document from the same sender.
- A value that the requester said to expect.

## Examples

- "Invoice No.: INV-00417" printed in the header: high.
- A due date computed from "payment terms 30 days" and the invoice date: medium, and
  note that it was derived.
- A total whose header value differs from the sum of the line items: low, status
  `conflict`.
- A signature date that is handwritten and partly covered by a stamp: low, status
  `illegible` if no reading is defensible.

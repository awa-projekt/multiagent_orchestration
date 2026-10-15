# Excel and scan enrichment: input of a manual build run

The input of a from-scratch `awa build` run with a real model; no automated test runs it.

1. `python tests/build_inputs/excel-scan-enrichment/make_data.py` writes the invoice register
   and the scanned invoices to `data/` and the expected values to `expected.json`.
2. `awa build` gets `process.md` and the `data/` folder and designs the system itself.
3. `python tests/build_inputs/excel-scan-enrichment/check_output.py OUTPUT.xlsx` scores the
   workbook the built system wrote against `expected.json`.

# Data-quality reconciliation

Every night the ERP exports its orders as an Excel file into a folder. We want that export compared row by row with the orders table of our warehouse database.

Write a discrepancy report: amounts that differ, status conflicts, orders missing on either side, and orders that appear twice in the export. The ERP is right about amounts, so for every amount the warehouse has wrong, propose a correction for that order. Status conflicts, missing orders and duplicates are only reported, because they need a business decision.

No change is made to the warehouse database until a data steward has approved it.

# Inventory replenishment

Every Monday we check the warehouse stock. The current stock (on hand and on order) comes from our ERP; the items and the sales of the last weeks come from our database.

An item needs a reorder when on hand plus on order is at or below its reorder point: average weekly sales times the lead time plus one safety week. The order quantity restores four weeks of cover and is rounded up to the supplier's minimum order quantity. Create one purchase order in the ERP per item that needs one.

Orders worth more than 5,000 EUR wait for a purchasing manager's approval; smaller ones go out directly. Negative stock counts and unknown items are reported as issues and never ordered.

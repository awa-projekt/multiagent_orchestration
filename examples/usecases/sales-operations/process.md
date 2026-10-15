# Sales operations

The sales team of a food wholesaler asks questions about its business in plain German and gets
answers from the order database: which customers had the highest net revenue in a year, which
orders shipped late or are still open, which products need to be reordered, how each
salesperson stands against their yearly target, and how revenue splits by product category.

Net revenue is the sum of unit price times quantity minus the line discount, over orders that
are not cancelled. An order is late when it shipped after its required date. A product needs a
reorder when stock plus units on order is at or below its reorder level.

The team can also grant a customer a discount for their next orders. A discount above 10
percent needs the sales director's approval before it is recorded; smaller discounts are
recorded directly. If the director rejects a discount, nothing is recorded and the reply says
why.

The data is hand-typed master data: company names can carry stray spaces and one country can
be written in several ways. When the data has no rows for a question, the answer says so and
names the period the data covers.

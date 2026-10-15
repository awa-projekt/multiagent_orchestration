# The tour in pictures

This page shows in pictures how a process comes to life in the awa web app. The example is an **invoice desk**: it
checks supplier invoices against a ledger and asks the head of finance before it releases a payment of 5,000 EUR or
more.

```mermaid
flowchart LR
    F["A supplier invoice<br/>arrives as a PDF"] --> C["Invoice checker<br/>looks it up in the ledger"]
    C --> P["Payment clerk<br/>releases it for payment"]
    P -- "5,000 EUR or more" --> H(["Head of finance<br/>says yes or no"])
    P -- "less than 5,000 EUR" --> R["Released for payment"]
    H -- "yes" --> R
    H -- "no" --> X["Not paid<br/>the reason is reported"]
    C -- "not in the ledger<br/>or already paid" --> X
```

Two **agents** do the work: an agent is an AI model with one job, its tools and its rules. Between them and the
payment stands a person, the head of finance, who gives the **approval**.

You only look here. To do it yourself, step by step with the sample files, follow
**[the guided tour](docs/guide/tour.md)**. It takes about 30 minutes.

## 1. Save your key

Open the web app with `awa ui`. On **Set up** you enter the key of your AI provider once.

![The Set up page: the card of the AI provider opened with a hidden key field, and the optional field for the model](docs/assets/screenshots/guide/a02-setup.png)

## 2. Bring your files

On **Data** you drop sample invoices and the list of purchase orders. awa reads each file and shows what is in it.

![The Data page: the switch for personal data, the box for the files, the three copied files, a card for each file and the note under the purchase orders](docs/assets/screenshots/guide/a03-data.png)

## 3. Connect the ledger

awa tests the connection to the invoice ledger with one small read-only call and shows the tables it found.

![The card of the ledger: its name, Live, read-only, the table invoices with its columns, and the saved address](docs/assets/screenshots/guide/a03-systems.png)

## 4. Describe the process

On **Describe** you write in your own words what happens: invoices arrive, each one is checked, from 5,000 EUR the
head of finance decides.

![The Describe page: your files and your ledger from step 3, the description in the large box, the switch for automatic testing and the Create my system button](docs/assets/screenshots/guide/a04-describe-before.png)

You press **Create my system**. awa writes test cases from your words, drafts the agents and tests them.

![The Describe page after the build: the five steps ticked, the three eval cases that pass, the result in plain words and the data map with the live ledger](docs/assets/screenshots/guide/a04-describe-after.png)

## 5. Review the design

On **Review** you see who does what, which tool asks you first, and where the data comes from. You change what does
not fit your process.

![The Review page: the flow diagram, the two agents with the finance approval marked asks you first, the data map with the live ledger, and the eval cases](docs/assets/screenshots/guide/a05-review.png)

## 6. Run the tests

On **Test** awa runs every test case and shows how many pass.

![The Test page: the button Run all tests, the result 3 of 3 eval cases passed with the meter full, and the card of an eval case with what it sends, its files and its result](docs/assets/screenshots/guide/a07-test.png)

## 7. Try it, with an approval

On **Try it** you send an invoice of 7,450 EUR. The run stops and asks you before the payment.

![The Try it page: the example requests, the steps of the system in plain words, the card that asks for your OK with the values and the reason, and the buttons Approve and Say no](docs/assets/screenshots/guide/a08-try-approval.png)

You press **Approve**, and the run continues exactly where it stopped.

![The conversation after the approval: the step that ran, the line You approved and the answer that invoice INV-2042 is released for payment](docs/assets/screenshots/guide/a08-try-answer.png)

## 8. Let it run by itself

On **Use it** you add a trigger: every PDF that lands in the folder `inbox` starts a run.

![The Use it page, the form Add a trigger: the kind file-drop, the folder inbox, the pattern *.pdf, the task text and the Add this trigger button](docs/assets/screenshots/guide/a09-trigger-form.png)

## 9. Answer approvals

Runs that wait for a person collect on **Approvals**, with the values you decide on.

![The Approvals page: one waiting run from the trigger new-invoice with its card and the buttons Approve and Say no](docs/assets/screenshots/guide/a10-approvals.png)

## 10. See what happened

Every run can be followed step by step, here in Langfuse: both agents, every call, the approval, time and cost.

![Langfuse showing the run of invoice INV-2042 as a tree: the workflow, the invoice checker and the payment clerk with their model and tool calls, and the run's time, cost and tokens](docs/assets/screenshots/tracing-tree.png)

## What you have at the end

A folder with the whole process: the agents, the approval, the connection, the trigger and the test cases. It runs
on your computer. This is a pilot on sample files: before it handles real invoices, you need your own data, a
read-only login to your ledger, test cases from your own invoices that pass, and the agreement of the people
responsible.

**Next:** [the guided tour](docs/guide/tour.md) to do it yourself, or back to the [README](README.md).

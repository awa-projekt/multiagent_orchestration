# awa

**awa helps you automate business processes with AI agents that run inside hard limits enforced by code and wait
for a person's OK before the steps you choose. You describe the process in your own words, awa drafts and tests it,
and you decide.**

![The Describe page of the awa web app after a build: the five steps ticked, the three eval cases that pass, and the result: awa built invoice_desk, 3 of 3 eval cases pass](assets/screenshots/ui-describe.png)

## Start here

1. **Install awa.** One command, or a double-click installer if you have no Python: [Install](start/install.md).
2. **Open the web app.** Type `awa ui`. It opens in your browser and asks for the key of your AI provider.
3. **Follow the guided tour.** [The guided tour](guide/tour.md) builds one complete process with sample files,
   screen by screen, in about 30 minutes.

You prefer a terminal to the web app? Take [First automation](start/first-automation.md) instead of the tour.

## How it works

You describe, awa drafts and tests, you check and decide. When the process runs, a person approves the steps that
matter.

```mermaid
flowchart LR
    D["Step 1: Describe<br/>in your words"] --> F["Step 2: Data<br/>your sample files"]
    F --> B["Step 3: Build<br/>awa drafts and tests"]
    B --> C["Step 4: Check<br/>you read the design"]
    C --> N["Step 5: Connect<br/>your systems"]
    N --> T["Step 6: Test<br/>on your cases"]
    T --> R["Step 7: Run<br/>with approvals"]
    T -. "a case fails" .-> C
```

An example, the invoice desk of the tour: two agents check each invoice, and from 5,000 EUR the head of finance
decides.

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

An **agent** is an AI model with one job, its tools and its rules. An **approval** is a person's yes or no before a
step runs. A **test case** is one example with what the process must do with it.

## What it looks like

**Your files:** awa reads the invoices you bring and shows what is in them.

![The Data page: the cards of the copied invoices INV-2041 and INV-2042, each read, with the start of its text and the box What should happen with this data?](assets/screenshots/ui-data.png)

**The design:** who does what, which step asks you first, before anything runs.

![The Review page of the invoice desk: the work flows from invoice_checker to payment_clerk, the tool finance_approval is marked asks you first, and the invoice ledger is live](assets/screenshots/ui-review.png)

**The approval:** the run waits until a person says yes or no.

![Try it: the card Please check before this happens for invoice INV-2042 and 7,450.00 EUR, why you are asked, and the buttons Approve and Say no](assets/screenshots/ui-approval.png)

Every screen, step by step: [the guided tour](guide/tour.md).

## What awa is, and what it is not

| awa is | awa is not |
| --- | --- |
| A local web app and a command line on your computer | A hosted service or a one-click automation |
| A helper that drafts agents and test cases from your words and files | A system that knows your process: you check the design and decide what is right |
| Default limits per run ([run limits](reference/settings.md#run-limits-and-loop-protection)) and approvals for the steps you choose | A guarantee: AI models are not deterministic, so you test on your own cases first |

**Status:** beta (1.0.0b1), no production history yet. It suits a pilot of one to three real processes under
control. What is still missing: [Roadmap](about/roadmap.md).

## Where to go

<div class="grid cards" markdown>

-   **I want to automate a process**

    ---

    The web app screen by screen, from a description and sample files to a tested invoice desk that asks a
    person before a large payment. The **Guide** section of this site has a page for every step.

    [The guided tour](guide/tour.md)

-   **I write Python**

    ---

    Agents, teams and workflows in a few lines, the API, the architecture. The **Develop** and
    **Reference** sections of this site are for you.

    [Quickstart](develop/quickstart.md)

</div>

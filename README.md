# awa

**awa helps you automate business processes with AI agents that run inside hard limits enforced by code and wait
for a person's OK before the steps you choose. You describe the process in your own words, awa drafts and tests it,
and you decide.**

![version 1.0.0b1](docs/assets/badges/version.svg) ![Python 3.11 to 3.13](docs/assets/badges/python.svg) ![license MIT](docs/assets/badges/license.svg) ![status beta](docs/assets/badges/status.svg)

![The Describe page of the awa web app after a build: the five steps ticked, the three eval cases that pass, and the result: awa built invoice_desk, 3 of 3 eval cases pass](docs/assets/screenshots/ui-describe.png)

*The web app after a build: awa read the sample files, wrote three test cases from the description, drafted the
agents and reports that 3 of 3 cases pass. You read the draft, change what does not fit and test it on your data.*

## Start here

1. **Install awa.** One command, or a double-click installer if you have no Python: [Install](#install).
2. **Open the web app.** Type `awa ui`. It opens in your browser and asks for the key of your AI provider.
3. **Follow the guided tour.** [The guided tour](docs/guide/tour.md) builds one complete process with sample files,
   screen by screen, in about 30 minutes.

**First just look?** **[The tour in pictures](TOUR.md)** shows the whole way in ten screenshots.

You prefer a terminal to the web app? Take [First automation](docs/start/first-automation.md) instead of the tour.

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

## What you can automate

Processes where something comes in, gets checked against rules and goes out, with a person for the decisions that
matter. Examples that ship with awa as samples:

- **Invoices:** check supplier invoices against orders and a ledger, ask before large payments.
- **Inbox:** sort incoming emails, create a task for the right team and draft a reply.
- **Expenses:** check expense reports and their receipts against the expense policy.
- **Support:** answer order questions, and ask before a refund above 50 EUR.

There are 13 use cases and nine recipes in all, each on sample data.

## What it looks like

**Your files:** awa reads the invoices you bring and shows what is in them.

![The Data page: the cards of the copied invoices INV-2041 and INV-2042, each read, with the start of its text and the box What should happen with this data?](docs/assets/screenshots/ui-data.png)

**The design:** who does what, which step asks you first, before anything runs.

![The Review page of the invoice desk: the work flows from invoice_checker to payment_clerk, the tool finance_approval is marked asks you first, and the invoice ledger is live](docs/assets/screenshots/ui-review.png)

**The tests:** how many of the test cases pass.

![The Test page: 3 of 3 eval cases passed, and the card of eval case 1 with what it sends](docs/assets/screenshots/ui-test.png)

**The approval:** the run waits until a person says yes or no.

![Try it: the card Please check before this happens for invoice INV-2042 and 7,450.00 EUR, why you are asked, and the buttons Approve and Say no](docs/assets/screenshots/ui-approval.png)

**Connected systems:** each system is tested with one small read-only call.

![The Connect page: 1 of 1 live, the invoice ledger declared on the Data page with its saved address](docs/assets/screenshots/ui-connect.png)

**Running by itself:** a trigger starts runs, and the ones that wait for a person show up here.

![The Use it page: Is it running? says Not running, and the run of invoice INV-2042 from the trigger new-invoice waits for your OK](docs/assets/screenshots/ui-deploy.png)

More screens, step by step: [the tour in pictures](TOUR.md).

## What awa is, and what it is not

| awa is | awa is not |
| --- | --- |
| A local web app and a command line on your computer | A hosted service or a one-click automation |
| A helper that drafts agents and test cases from your words and files | A system that knows your process: you check the design and decide what is right |
| Default limits per run ([run limits](docs/reference/settings.md#run-limits-and-loop-protection)) and approvals for the steps you choose | A guarantee: AI models are not deterministic, so you test on your own cases first |

## Install

| You have | Do this |
| --- | --- |
| Python 3.11 or newer | `pip install "awa-agents[all] @ git+https://github.com/awa-projekt/multiagent_orchestration"` |
| Python 3.11 or newer and the awa folder | in the awa folder: `pip install ".[all]"` |
| Nothing installed (Windows) | double-click [`scripts/install-windows.cmd`](scripts/install-windows.cmd) |
| Nothing installed (macOS) | double-click [`scripts/install-macos.command`](scripts/install-macos.command) |
| Nothing installed (Linux) | `bash scripts/install-linux.sh` |

The installers set up their own Python, put an **awa** shortcut on your desktop and open the web app. Then run
`awa doctor`: it checks the installation and says what is missing. On Windows, use a short folder such as `C:\awa`.
Details and help: [Install](docs/start/install.md).

## See every run

Send the runs to a local [Langfuse](https://langfuse.com) and every step of one run is visible
in the browser. The pictures show one run of the invoice desk:

1. **The trace tree.** Both agents with every model and tool call, and the run's time, cost and tokens.
   ![Langfuse trace tree of one invoice desk run: the workflow, its two agents with their model and tool calls, the approval, and the run's time, cost and tokens](docs/assets/screenshots/tracing-tree.png)

2. **A tool call.** The arguments the agent chose and what came back.
   ![Langfuse detail of the invoice ledger tool call: the SQL query and invoice number going in, the ledger row coming out](docs/assets/screenshots/tracing-tool-call.png)

3. **The approval pause.** The exact values the person is asked about.
   ![Langfuse view of the approval request: the finance approval tool with amount 7450 and invoice INV-2042](docs/assets/screenshots/tracing-approval.png)

4. **A model call.** Each model call with its answer, time, tokens and cost.
   ![Langfuse detail of one model call of the invoice checker: its answer, time, cost and tokens](docs/assets/screenshots/tracing-model-call.png)

How to switch it on: [See your runs in Langfuse](docs/guide/langfuse.md).

## Status

- **Beta (1.0.0b1), no production history yet.** It suits a pilot of one to three real processes under control,
  for example invoice checks, inbox triage or spreadsheets from scanned documents.
- **Real runs need a model key** (or a local Ollama) and cost tokens. There is no hosted service.
- What is still missing: [Roadmap](docs/about/roadmap.md).

## For developers

Python, the blueprint file, the API, the architecture and how to work on awa itself:
**[DEVELOPERS.md](DEVELOPERS.md)**. It also shows how to read all docs as a site with `python -m mkdocs serve`.

## License

MIT, see [LICENSE](LICENSE). Security: [SECURITY.md](SECURITY.md).

## About

awa (Agentic Work Automation) is programmed by cd-bits. It is developed within the research project *Agentic Work Automation* (AWA), led by the Fraunhofer Institute for Applied Information Technology FIT, Branch Business & Information Systems Engineering, Augsburg, together with the partners Ancud IT-Beratung GmbH, DATEV eG, Brauerei Gebr. Maisel GmbH & Co. KG and soffico GmbH.

awa is an early, unfinished software library that is still under construction and not yet an interim result of the research project. It provides first core functions for multi-agent systems (agents, teams, workflows, communication between agents, approvals, limits and evaluation) and is meant to become the base on which reference architectures and decision models may later be built. Hybrid multi-agent systems, in which a lead agent delegates work and its members consult each other directly inside limits enforced by code, are tested on it first. The recipes and use cases shipped with awa are small first test cases on sample data: they show that the library applies to many kinds of processes, and the library was extended along them with standard functions such as connections to Microsoft 365, files including scanned PDFs and Excel workbooks, e-mail and databases. Proof in practice follows once the library has been hardened: larger and more complex use cases from companies are then applied to it.

The research project is funded by the Bavarian Transformation and Research Foundation (Bayerische Transformations- und Forschungsstiftung).

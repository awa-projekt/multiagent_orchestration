# Roadmap

This page shows where awa is heading: milestones in order, each with its goal and the functions it
adds. The milestones show a direction, not promises. They carry no dates, and an item may change
or be dropped when real use shows a better way.

Today awa is a public beta. It builds, tests and runs multi-agent systems on LangChain and
LangGraph from one blueprint file with agents, tools, limits, approvals and eval cases. It has no
production history yet, and its preview parts may still change ([Stability](stability.md)).

Four rules hold for every milestone:

- Plain Python code enforces the limits and decides when an approval is needed; a model chooses only
  within them.
- The core stays small. New features arrive as add-ons or plugins that you can switch off or
  replace with your own code.
- A number enters the docs only with the command, the model and the number of runs that produced it.
- What awa holds stays plain data and code that you can take along.

## At a glance

| Milestone or track | Goal |
| --- | --- |
| [1.0 — Stable core](#stable-core) | One small, stable core package with add-on packages around it, and safe defaults |
| [1.x — Ready for company processes](#company-processes) | Signed-in approvers, versioned blueprints, better documents and search, more connections, export of whole teams |
| [2.0 — Agents that check their work](#checking-agents) | Planning, checking and replanning inside the limits, on a simpler blueprint format |
| [3.x and beyond — Open to other systems](#open-systems) | Other agent systems, other engines, and agents that improve from their test results |
| [Track: Reference architectures and decision models](#reference-architectures) | Reference architectures, rules for choosing one, applied to the examples, and templates from real processes |
| [Track: Professional use](#professional-use) | The path from the beta to production use |

## 1.0 — Stable core { #stable-core }

**Goal:** a small core that keeps its promises, everything else in add-on packages, and safe
defaults before the stability promise starts.

- **Core and add-on packages.** The library splits into a small core package and add-on packages:
  server and automation; web app and project builder; Microsoft 365 and other SaaS connectors.
- **Little to migrate.** One install command still installs everything, and a command whose add-on is
  missing names the package to install. Blueprints, `.env` files and data files stay as they are,
  and a Python name that moved says where it went.
- **Commands from add-ons.** Add-ons and your own extensions add `awa` commands through one command
  table, and the core keeps a small set of commands.
- **Team features as plugins.** Consults, messages and shared notes become team features that you
  add, remove or replace. The agent network becomes an optional extension, and long-term memory sits
  behind a small provider interface.
- **A default money limit.** Every run gets a money limit by default, based on a maintained price
  table. Today you set `max_cost` and the prices of your model yourself.
- **Safer defaults.** Personal data in the file digests a model sees is replaced by placeholders by
  default, agents in a team get messaging tools only when the team asks for them, and a `map` step
  refuses more items than a ceiling that you can raise.

## 1.x — Ready for company processes { #company-processes }

**Goal:** run real company processes with signed-in approvers, versioned blueprints, better
documents and search, more connections, and export of whole teams.

### Approvals and decisions

- **Approval rights.** An approval names the roles that may decide it, and the person who started
  the run can be barred from deciding it.
- **Approval inbox with sign-in.** Approvers sign in with their company account (Microsoft Entra ID)
  and decide in one inbox, and every approval has a stable link.
- **A notice when a person is needed.** A run that starts waiting for an approval sends one signed
  event with the inbox link to a webhook.
- **Decision limits per role.** An approval states how far each role may decide, for example a
  department manager up to 25,000 EUR. A decision beyond the limit is refused and recorded, and eval
  cases can check this.
- **Exact amounts.** Computed amounts and threshold checks use exact decimal arithmetic with a stated
  rounding rule.
- **Decision tables.** A table of special cases over several fields is checked for gaps, overlaps
  and rows that can never apply, and each finding comes with an example input.
- **Ask a person.** An `ask_person` tool lets an agent ask a person a question and wait for the
  answer. It is built when a shipped process needs it.

### Blueprints and building

- **Versioned blueprints.** Every deployed version of a blueprint is recorded. You see what changed,
  roll back with one command, and every run keeps the version it started on.
- **A plan you can inspect.** Before any model call, a blueprint shows as plain JSON which agents,
  tools, approvals and limits it builds, and why each tool and skill was granted.
- **Role templates.** Twelve role templates (for example intake, classifier, checker, drafter,
  reviewer and coordinator) are selected by name and bring their instructions, output checks and
  approvals.
- **Honest build results.** `awa build` repeats its eval cases and reports for each case whether it
  passes in every run (pass^k), not the best of two runs.
- **Test templates per agent.** Test templates for each agent are generated from the blueprint, so a
  process only adds inputs and expected values.
- **Next steps.** `awa next` reads the build and test results of a project and says what is missing
  before it is ready.
- **Edit in the web app.** The web app edits the agents, skills and tools of a project, which stay
  plain files in the project folder.
- **Process drawings.** A BPMN 2.0 process drawing is imported and checked for structural problems
  before any model call, so a build starts from a sound process.

### Teams and workflows

- **Step deadlines and error handlers.** Every step of a workflow or process can have a deadline and
  a defined way to handle its failure.
- **Checked handoffs.** A team declares a typed contract on each handoff, and code checks what one
  agent passes on before the receiver runs.
- **One way to serve.** HTTP, the job queue and MCP start, resume and cancel runs and decide
  approvals the same way, and a client that reconnects to a stream gets the events it missed.
- **Team and workflow export.** `awa export` writes sequential workflows, parallel steps, loops, coordinator
  teams, handoffs and agents with MCP tools as plain LangGraph code that runs without awa. Today
  only a single agent is exported.
- **Connector tools in exported code.** An exported system uses the HTTP and Microsoft 365 connector
  tools from a small standalone package, without installing awa.

### Documents and search

- **Hybrid search without your own code.** A local embedding model behind an optional extra, an
  embedding cache and a fallback to keyword search make hybrid search work without an API key, and
  a recall benchmark measures it.
- **Filters, page citations and retrievers.** A search filters by document metadata, cites the page
  a passage comes from, and works as a LangChain retriever.
- **Local text recognition for scans.** A local open-source OCR engine reads scanned pages, as a
  choice next to the vision model, without sending page images to a model provider.
- **Tables out of documents.** Tables inside Word, PowerPoint and PDF files become CSV files or typed
  rows that the data tools can query.
- **Electronic invoices.** Invoices in the EN 16931 XML formats (CII and UBL) and PDF invoices with
  embedded XML are read into checked fields, and a total that does not add up is flagged.

### Connections

- **MCP servers in one line.** `mcp:<url>` in a blueprint, `awa mcp add` or one Python call attaches
  an MCP server.
- **MCP tool hints as approvals.** The hints an MCP server gives its tools can tighten risk, approval
  and retry decisions. They are pinned, so a server cannot loosen them unnoticed.
- **OAuth for MCP servers.** A remote MCP server that requires OAuth 2.0 is reached without a
  long-lived token in a header.
- **Microsoft 365 triggers.** A new mail in an Outlook folder or a changed document in a SharePoint
  library starts a process.
- **Microsoft 365 live acceptance.** The Microsoft 365 connectors pass a repeatable live acceptance
  on a test tenant, including writes, Excel and Teams, before their preview label goes.
- **People directory.** The people directory works on a real tenant, an approval by role can be
  limited to named people of that role, and other directory sources can plug in.
- **Large databases.** The SQL tools get a table allow-list, a schema ranked by the question, a check
  of the query plan and a size limit on results.
- **More connectors.** Vendor systems connect through their MCP server or the OpenAPI connector with
  tested recipes, and a built-in SaaS connector loses its preview label only after a live check.

### Testing and cost

- **Test gates next to the cases.** A `gate:` block in the test-case file sets the pass rate, the
  pass^k and the spending that a change must meet, and a test run stops at its spending budget.
- **Path checks.** A test checks how an agent reached its answer: which tools, in which order, and
  how many model calls.
- **Tool emulation.** Systems with tools that change things run in tests without touching external
  systems.
- **Conversations in tests.** An eval case carries the follow-up replies of a user, so conversations
  with several turns are tested.
- **Recorded system answers.** The answers of an external system are recorded once and replayed in
  offline tests.
- **Cost estimates.** The measured test runs give a cost estimate per run and per month.

## 2.0 — Agents that check their work { #checking-agents }

**Goal:** agents that plan, check and correct their work inside the limits, on one simpler
blueprint format.

- **A verify step.** A step checks a result before the next step uses it.
- **Plan review and replanning.** A plan is reviewed before the work starts and revised when a step
  fails, within the same limits.
- **Background work across approvals.** Delegations that run in the background continue when a run
  pauses for an approval and resumes.
- **Checked tool definitions.** Tool names, descriptions and input schemas are checked before a
  build, so an agent does not get an unclear or conflicting tool.
- **Word and Excel results.** Tools write results as new Word documents and Excel workbooks. Today
  agents fill existing workbooks.
- **A simpler blueprint format.** Blueprint format version 2 folds workflows into process steps and
  role trees into team members, and awa upgrades older files when it opens them.
- **Preview parts become stable.** Each preview part listed in [Stability](stability.md#preview-parts),
  for example the imports of LangChain tools, LangGraph graphs and skills and each SaaS connector
  that passed its live check, joins the stable API after its proof.

## 3.x and beyond — Open to other systems { #open-systems }

**Goal:** awa works with other agent systems and engines, and its agents improve from their own
test results.

- **Agent-to-agent protocol.** awa calls agents of other systems and serves its own over the open
  A2A protocol.
- **Other engines.** Further engines run behind the same engine port that LangGraph uses today, and
  blueprints stay the same.
- **A browser toolkit.** Agents read web pages and act in them through a controlled browser, with
  the same approvals and limits as other tools.
- **Instructions improved from eval failures.** A failed eval case leads to a proposed change of the
  one agent that failed, kept only when pass^k holds and the cost does not rise.
- **Online evaluation.** Live runs are scored continuously, not only eval cases.
- **Memory consolidation.** Long-term memory is condensed over time, so agents keep what matters.
- **Batch runs.** Large jobs use the batch interfaces of model providers at a lower price.
- **An optional visual editor.** An editor shows and changes the flow of a blueprint visually, and
  the blueprint stays the one source.
- **Large document collections.** A persistent search index, vector database backends and a reranker
  serve collections too large to search in memory.
- **More formats and importers.** Further file formats and process-drawing formats are read where
  real users need them.

## Track: Reference architectures and decision models { #reference-architectures }

This track runs next to the milestones. It turns what the examples show into knowledge you can
reuse: which architecture fits which process, with which limits and approvals, and templates built
from real company processes. Architectures and decision records for the shipped examples come
first, together with 1.x; the template library follows the first pilots on real processes.

### Reference architectures

- **Hybrid multi-agent systems.** An orchestrator delegates to members, members consult each other directly
  where the team allows it, and code enforces the delegation depth, the calls per member and the
  timeouts, as in [Teams and networks](../develop/teams.md).
- **The other shapes.** One agent, fixed workflows, review loops, parallel lists, handoff desks and
  networks on a task board, each as a reference with a blueprint, eval cases, limits and approvals.

### Decision models

Today `awa.blueprints.suggest_patterns` suggests patterns for a described process, and `try_patterns`
tests them side by side on the same cases. The decision model builds on both.

- **Which architecture fits.** A decision model maps properties of a process (fixed or open steps,
  the number of items, the kinds of know-how, who talks to the customer) to an architecture. It
  extends today's [choice of structure](../guide/agents-and-teams.md#choose-the-structure).
- **Which limits and approvals.** The same model says which limits fit the volume, and which actions
  need an approval, by whom and up to which amount.
- **Measured, not guessed.** `awa verdict` compares a team with its single-agent version on the same
  eval cases and says whether the team earns its extra cost, or that the data is not enough to decide.
- **In the builder.** The architecture suggestion in the web app and in `awa build` uses the decision
  model and shows the reason for each choice.

### Applied to the shipped examples

Each example gets a decision record that says why it has its architecture, its limits and its approvals:

| Example | Shape today | The decision record answers |
| --- | --- | --- |
| Invoice desk ([guided tour](../guide/tour.md)) | Fixed steps; a payment of 5,000 EUR or more waits for the head of finance | Why fixed steps fit, and which amount needs which approver |
| Invoice processing (`invoice-processing`) | An orchestrator delegates to extraction, validation and a policy check; booking above 10,000 EUR waits | Whether the team beats one agent on the same cases, and at what cost |
| Email inbox triage (`email-inbox-triage`) | An inbox orchestrator with a classifier and a drafter; every reply waits for a person | Where direct consults between members help, and which replies need an approval |
| Customer support desk (`customer-support-desk`) | A team with handoffs; refunds above 50.00 in the order's currency wait | When to hand the conversation over instead of delegating |
| Employee onboarding (`employee-onboarding`) | A network on a task board | When a shared board beats a lead |
| Contract review (`contract-review`) | Reviews side by side; advice only | When a process needs no approval |
| Spreadsheets from scans | The scan route and the `fill_excel` tool; a reference process is planned | How to split many pages into parts, which ceiling fits, and when writing waits |

The examples are listed in [Examples](../guide/examples.md).

### Templates from company workflows

- **Blueprints from real processes.** Real company workflows become reusable blueprint templates:
  anonymized, with their eval cases, limits, approvals and masked results.
- **A template library.** The templates are grouped by process type, for example accounts payable,
  inbox handling, purchasing, onboarding and reporting, each with its decision record.
- **Your process as a template.** A documented path turns your own process into an anonymized
  template. A check refuses unmasked IBANs, email addresses and phone numbers, and only masked
  numbers are published.
- **Roles that come along.** A template carries its roles and approval rules: who may decide what,
  and up to which amount.

## Track: Professional use { #professional-use }

This track is the path from the beta to production use. Each step names its milestone.

### Today: the beta

- Pre-releases carry no compatibility promise, preview parts are marked, and there is no production
  history yet ([Stability](stability.md)).
- Every release comes with checksums and a software bill of materials.

### Releases and support, from 1.0 on

- **Stability promise.** Releases follow semantic versioning: patch releases for fixes, minor
  releases for features, and major releases for removals. The public API of the core follows the
  [versioning policy](stability.md#what-a-version-number-promises): no incompatible change in a
  minor release, a deprecation warning for at least one minor release, and removal only in the
  next major release.
- **Preview parts graduate one by one.** A preview part joins the stable API only after its proof,
  for example a live check or an outside implementation that passes its contract suite.
- **Security fixes.** Security fixes come as patch releases of the latest minor release. After a
  major release, the last minor release of the previous major gets security fixes for at least six
  months.
- **Upstream releases.** New LangChain and LangGraph releases reach awa through one layer, which a
  compatibility test checks.
- **Support.** Questions and bugs go to the issue tracker of the project. There is no hosted service
  and no promised response time.

### Security, from 1.0 on

- **Reviews.** A security review of the whole code base before 1.0 and before each major release,
  with its findings fixed first.
- **Hardening.** One contract suite that every connector must pass, for example that no token
  appears in an error message. Approver sign-in, OAuth for MCP servers and pinned MCP tool hints
  are planned under [1.x](#company-processes).

### Operations, with 1.x

- **Deployment guide and image.** A deployment specification for `awa serve`, `awa worker` and
  `awa mcp serve`, and a reference container image that tests build, start, check for health and
  stop.
- **Monitoring.** An event when a run waits for a person, traces in Langfuse or OpenTelemetry, and a
  health signal when the prompt cache stops working.
- **Backups of run data.** Backups cover runs, waiting approvals and the release history of
  blueprints, and the deployment tests include a restore.

### Performance, with 1.x

- **Budgets checked by tests.** Import time, start-up, memory and per-run overhead of the library,
  and the model calls and tokens of each template, stay inside written budgets.
- **Cache and token savings.** Prompt-cache and token optimizations are adopted one at a time, only
  when a repeated measurement shows lower cost and an unchanged pass^k.

### Quality evidence, toward 2.0

- **Evidence per template.** Every shipped template gets an evaluation report that one command
  reproduces: pass^3, cost per case, tokens, cache share and latency.
- **Several models.** Evaluations and benchmarks run on several models, not on one hosted model as
  in the beta.
- **Public benchmark and comparison.** Results on a public benchmark of tool-using agents, and a
  comparison with the same tasks built by hand on plain LangGraph (code size, results, tokens and
  cost), both reproducible from the repository.
- **Pilots on real processes.** Real processes run on real data, and only masked numbers are
  published, with the model and the number of runs.
- **One evidence page.** One page collects the benchmarks, the comparison, the export proof and the
  pilots.

## Not planned

- A hosted service: awa stays a library that you run yourself.
- Process logic in an external workflow product: the logic stays in the blueprint and in Python.
- Payment protocols for agents.
- Graph memory.
- Computer use through screenshots.
- A separate classifier model that detects prompt injection: awa frames outside content as data.
- Open-ended agent swarms without limits that code enforces.

## Help shape the roadmap

- Open an issue with a real process, an anonymized sample file or a failing example. That moves an
  item further than a general idea.
- Run a live check with your own model, database or test tenant, and share the masked report with
  its scope and date.
- A proposal that would grow the core starts as an add-on or a plugin ([Extend awa](../develop/extend.md)).

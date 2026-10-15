# Microsoft 365

Use this page when your process works with Microsoft 365 mail, files or Teams. awa reaches one Outlook mailbox, one
SharePoint site and one Teams channel. It also ships six ready-made processes on top of them that stop where a person
has to decide.

The shortest path to a running process:

1. Register one app in Microsoft Entra ([Step 1](#step-1-register-one-app)).
2. Run `awa connect microsoft`. It asks for the missing values, saves them in `.env` and checks them with read-only
   calls.
3. Copy one of [the processes](#the-processes), test it and run it
   ([Use a process with your own data](#use-a-process-with-your-own-data)).

In a blueprint, the connectors are one line each:

```yaml
connectors:
  mail: {use: outlook}
  files: {use: sharepoint}
agents:
  - name: assistant
    tools: [connector:mail.list_messages, connector:mail.read_message, connector:mail.create_reply_draft]
```

What keeps it safe:

- The mailbox, the site and the channel are settings in `.env`, never tool arguments. A model cannot wander into
  another mailbox, site or channel.
- Nothing is deleted. Every change (a reply draft, a move, an upload, a send) waits for a person.
- Mail and file contents reach the model as data, never as instructions.

!!! info "Preview"
    The connectors `outlook`, `sharepoint` and `teams` and the six processes are tested offline against a simulated
    Microsoft 365 with scripted models. The connectors were also read live on one test tenant. Writes, Excel edits,
    Teams and people search are not yet accepted live. Run them on a test mailbox and a test site, with a person
    checking the results, before you rely on them.

## The processes

The processes are in the awa folder under `examples/microsoft/`, one folder per family. Invented test data is in
`testdata/`.

| Folder | Process | What it does | What it can change |
| --- | --- | --- | --- |
| `briefings/` | `outlook-briefing` | Reads the unread mail and writes a briefing: ids, summaries, stated deadlines, suspicious mail | nothing |
| `briefings/` | `sharepoint-briefing` | Reads the documents of one approved folder and writes a briefing that names its sources | nothing |
| `sales/` | `sales-inbox` | Sorts customer requests: what is wanted, what is missing, how urgent, the next step | nothing |
| `sales/` | `sales-offer-preparation` | Prices each request from the approved price list, checks every number, leaves a **reply draft** | drafts in the mailbox |
| `administration/` | `administration-inbox` | Finds the responsible department and the deadline in the approved list, leaves a **reply draft** | drafts in the mailbox |
| `digest/` | `approved-digest` | Combines mail and documents into a digest and sends it to one internal reviewer after a person approves | one mail, after approval |

- **A reply draft is not sent.** Saving it waits for an approval in awa. Then a person opens it in Outlook, checks it
  and presses **Send**.
- **Only the digest can send**, and only to the domains you allow (`allowed_domains`).
- **Every schedule is switched off** (`enabled: false`) until the [acceptance](#acceptance-before-a-schedule) is
  done.

**The offer is checked by code, not by the model.**

- Prices, discount tiers, approval roles and tax rates come from queries of the price list (`price-list.xlsx` in the
  SharePoint folder `Approved`).
- The calculator does the arithmetic in whole cents.
- A missing quantity, an unknown product, another currency or an expired price gets no amount. The line goes to a
  question for the customer or to a person.

The administration process takes the department and the deadline from `responsibilities.xlsx`. It never drafts a
reply to a sensitive request (payroll, personnel, banking).

Each process ships a `cases.yaml` whose checks need no judge model. `python examples/microsoft/make_testdata.py OUT`
builds the workbooks and sample documents for a test site.

The processes run offline against the simulated Microsoft 365 that the repository's tests use:

```console
python -m pytest tests/examples/test_microsoft_processes.py
```

This proves that they build, run end to end, keep their limits and produce the expected numbers and routes. It does
not prove how a real model words a draft, nor your tenant's permissions. That is what the acceptance is for.

## Connect Microsoft 365

Register the app (step 1), then run `awa connect microsoft`. It:

1. shows which values are set (never the values);
2. asks for the missing ones, with your typing hidden, and saves them in `.env`;
3. checks everything with read-only calls;
4. prints the message for your administrator.

Each connector needs its own Microsoft Graph permission:

| Connector | Works in | Needs (Microsoft Graph permission) |
| --- | --- | --- |
| `outlook` | one mailbox (`MS_MAILBOX`) | `Mail.ReadWrite` (drafts, mark, move); `Mail.Read` for read only; `Mail.Send` only to send |
| `sharepoint` | one site (`MS_SITE_URL`) | `Sites.Selected` and a role `read` or `write` on that one site |
| `teams` | one channel | nothing for a Workflows webhook; delegated `ChannelMessage.Send` and `ChannelMessage.Read.All` for a signed-in user |

### Step 1: register one app

1. In the Microsoft Entra admin center: **Entra ID**, **App registrations**, **New registration**. Name it `awa`, keep
   "Accounts in this organizational directory only", choose **Register**.
2. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the overview.
3. **Certificates & secrets**: a new client secret of 12 months or less, and copy its **Value** at once (not the
   Secret ID). Or upload a certificate, which Microsoft recommends and some tenants require; awa then reads
   `AZURE_CLIENT_CERTIFICATE_PATH` (a PEM file with key and certificate, or `.pfx`) and signs a short-lived proof per
   sign-in, so the key never leaves the computer.

```bash
AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000
AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000
AZURE_CLIENT_SECRET=paste-the-secret-value-here
```

`awa check microsoft` warns 30 days before a certificate expires; an expired secret answers "the client secret has
expired".

### Outlook mail

Add the application permission **`Mail.ReadWrite`** and **Grant admin consent**. Granted this way it reaches every
mailbox of the company, so an Exchange administrator narrows it to one mailbox with Exchange RBAC for Applications and
then revokes the tenant-wide consent (a change can take up to 2 hours):

```powershell
Connect-ExchangeOnline
New-ServicePrincipal -AppId <client id> -ObjectId <enterprise application object id> -DisplayName "awa"
New-ManagementScope -Name "awa-mailboxes" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'support@company.example'"
New-ManagementRoleAssignment -App <enterprise application object id> -Role "Application Mail.ReadWrite" -CustomResourceScope "awa-mailboxes"
```

Then set `MS_MAILBOX=support@company.example` in `.env` (a shared mailbox works the same) and run
`awa connectors test outlook`. The tools: `list_messages`, `search_messages`, `read_message` and `get_attachment`
read; `create_reply_draft`, `reply_to_message`, `send_mail`, `move_message` and `mark_processed` wait for approval.
A reply is a draft first and sent second; when Microsoft's answer to a send is lost, awa does not try again (you could
send two emails) and names the draft's id instead.

### SharePoint files and Excel

Add the application permission **`Sites.Selected`** and grant consent; the app can then reach no site at all. A
SharePoint administrator grants the one site in Graph Explorer: **GET**
`https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/team-reports` for its `id`, then **POST**
`https://graph.microsoft.com/v1.0/sites/<id>/permissions` with:

```json
{
  "roles": ["write"],
  "grantedToIdentities": [{ "application": { "id": "<Application (client) ID>", "displayName": "awa files" } }]
}
```

Set `MS_SITE_URL` to the site's address and run `awa connectors test sharepoint`. The agents can list, search,
download and read Excel sheets; uploads, new folders and Excel edits wait for approval. A workbook is edited as a file:
awa changes a copy and uploads it only if nobody saved it in between, and refuses workbooks with macros, pivot tables
or charts. Search under `Sites.Selected` covers file and folder names only.

### Teams

An app cannot post to a channel on its own, so awa offers two ways. A **Workflows webhook** (in Teams: the channel,
**Workflows**, the template **Send webhook alerts to a channel**; the link goes to `TEAMS_WEBHOOK_URL`) posts cards as
"Workflows". A **signed-in user** (`awa connect microsoft --teams`, a device code sign-in) posts, replies and reads in
the one channel named by `TEAMS_TEAM_ID` and `TEAMS_CHANNEL_ID`. Use a separate user for awa, protected with
multi-factor authentication.

### Check the connection

```console
awa check microsoft                      # live, read only: one line per check, red ones with the fix
awa check microsoft --allow-post teams   # also send one test card
```

`awa check microsoft` checks the shape of the values, the sign-in, the mailbox, the site, a workbook and Teams, and
two **negative tests**: the app must be refused on another mailbox (`AWA_CHECK_NEGATIVE_MAILBOX`) and on the root
site. A red line names the fix, for example `ms.secret_id_copied` (the Secret ID was pasted instead of the Value) or
`ms.rbac_not_scoped` (the app can still read other mailboxes).

Use the `outlook` connector, not `imap` or `smtp`, for Microsoft 365 mail: Microsoft is retiring password sign-in
for those protocols.

## Use a process with your own data

1. Copy the process folder, and `examples/microsoft/.env.example` next to it as `.env`. Fill in the values above, the
   mailbox, the site and a model (`AWA_DEFAULT_MODEL`).
2. In `blueprint.yaml` replace `company.example` (the digest's reviewer and `allowed_domains`). Nothing else holds
   company data.
3. Put the documents the process needs (the price list, the list of responsibilities) into the SharePoint folder
   `Approved`. Keep a **test** mailbox and site until the acceptance is done.
4. Check the connections, reading only: `awa validate blueprint.yaml`, then
   `awa connectors test mail --blueprint blueprint.yaml` and `awa connectors test files --blueprint blueprint.yaml`.
5. Run the cases three times: `awa eval blueprint.yaml cases.yaml --repetitions 3`. Then run your own requests with
   `awa run blueprint.yaml "..."` and read the drafts.
6. Only after the acceptance: set `enabled: true` on the trigger and keep `awa serve blueprint.yaml` running; waiting
   approvals are answered in the web app or with `awa runs decide`.

### Run budgets

Every run is bounded by `limits:` in its blueprint, and by `defaults.max_steps` and `automation.run_timeout` as a
second fence. Add `prices:` for your model and a `max_cost:` of your own.

| Process | `max_requests` | `max_total_tokens` | `max_tool_calls` | `max_duration` and `run_timeout` |
| --- | --- | --- | --- | --- |
| `sharepoint-briefing`, `outlook-briefing` | 25 | 40,000 | 14 | 5 minutes |
| `sales-inbox` | 30 | 50,000 | 16 | 10 minutes |
| `administration-inbox` | 50 | 80,000 | 30 | 10 minutes |
| `approved-digest` | 35 | 60,000 | 20 | 10 minutes |
| `sales-offer-preparation` | 80 | 120,000 | 60 | 15 minutes |

### Acceptance before a schedule

1. **Access is narrow.** `awa connectors test` is green for each connection, and both negative tests of
   `awa check microsoft` pass.
2. **The cases pass** three times each on test data; failures are reproduced, fixed and run again.
3. **Your own cases**: the typical requests of your company and the ones that scare you (a wrong price, another
   language, an order hidden in an email).
4. **A person reads the drafts** of one full run and compares every number with the price list.
5. **Approvals and recovery** (digest): approve, reject and edit a send on a test mailbox; stop the server while a
   send waits and start it again.
6. **Operation**: the owner, the stop procedure, how a rotated secret is replaced, the budgets, and the model, process
   version and test date written down.

Known limits: the briefings list *unread* mail, so a request can appear again on the next run; a re-run on a still
unread request makes a second draft; gross prices and other currencies are stopped, not converted.

## Send by role, not by address

`awa people` finds colleagues in the company directory and pins them to roles in `config/people.yaml` (kept out of
version control, readable by its owner only). A blueprint then names the role, and awa checks every recipient in the
directory when it sends:

```console
awa people search "carla mei"
awa people pin "carla mei" --role finance_recipients
awa people check
```

```yaml
connectors:
  mail: {use: outlook, recipients: [role:finance_recipients]}
```

`recipients` makes the roles the exact list of allowed recipients: any other address is refused, whatever the model
wrote, and a person who left or was disabled is left out. The app needs the application permission `User.Read.All`.
For each pinned person awa keeps only the directory ids, the name, the mail address, the state and the date of the last
check. `awa people check` ends with exit code 1 when a pinned person is gone.

## Hand-over to IT

Before a process runs on company data, the process owner and IT agree on one source, read only, and one output, such
as a reply draft. IT provides, per process: the owners and who may approve, the exact resources (mailbox, site,
folder), a dedicated integration identity (never a personal administrator account), only the read rights needed, the
sign-in method with its expiry, the model provider allowed for the data, and the trigger, time limit, cost limit and
alert target. Then the process is switched on in this order:

1. Define one process with its input, output, rules and stop cases.
2. Work in a copy with sample data and no production credentials.
3. Narrow the access: a test identity, read rights only, one resource.
4. Narrow the tools: `read_only: true`, `include`, single tool references per agent.
5. Check offline (`awa validate`, the project's tests), then make one live read; a forbidden resource must fail.
6. Test your normal, exception and attack cases, three times each. Run write tests only in a sandbox.
7. Allow one output, keep the approval by a person, then switch on the trigger and start a small pilot.

`read_only: true` limits the tools awa offers; it does not replace read-only rights in Microsoft 365, and it does not
stop data from going to the model provider. Dynamics 365, Business Central and other ERP systems are reached through
their OData or REST interfaces with the `odata`, `rest` and `openapi` connectors ([Connect systems](connect.md)), with
`create` and `update` left out at first.

## Common mistakes

| awa says | Meaning | Fix |
| --- | --- | --- |
| "the client secret is wrong" or "has expired" | The value (not the Secret ID) or its date | Create a new secret; update `AZURE_CLIENT_SECRET`. |
| `ms.mail_tenant_wide` (warning) | `Mail.*` is granted for the whole organization | Scope it with RBAC for Applications, then revoke the tenant-wide consent. |
| `ms.site_not_granted` (HTTP 403) | The app has no role on the site | Do the grant above; use `write` to upload. |
| HTTP 404 "requested user is invalid" | The mailbox does not exist or has no Exchange Online license | Check `MS_MAILBOX` (the full address). |
| "changed by someone else" | Someone saved the workbook after awa read it | Read it again and repeat the change. |
| "may or may not have been made" | A send got no clear answer | Do not resend; check the draft id the message names. |
| `ms.webhook_old_host` | The webhook link is on an old host | Copy the workflow's current trigger link. |

<!-- guide:do cd . -->
<!-- guide:do copy awa:examples/microsoft/briefings/outlook-briefing outlook-briefing -->

!!! example "In code"

    A Microsoft 365 process is a blueprint like any other: its connector, its budget and its switched-off schedule
    are plain fields ([Blueprints](../develop/blueprints.md)).

    <!-- guide:python -->
    ```python
    from awa.blueprints import Blueprint

    briefing = Blueprint.from_yaml_file("outlook-briefing/blueprint.yaml")
    print(briefing.name, {name: entry.get("use") for name, entry in briefing.connectors.items()})
    print(briefing.limits.max_requests, [(t.name, t.enabled) for t in briefing.triggers])
    ```

    <!-- guide:expect -->
    ```text
    outlook-briefing {'mail': 'outlook'}
    25 [('weekday-briefing', False)]
    ```

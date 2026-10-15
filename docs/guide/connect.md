# Connect systems

Use this page when your process must read from or write to one of your systems: a database, a web service, a mailbox
or a ticket tool. A [connector](../reference/glossary.md#connector) gives the agents tools for one system, such as
"find a customer" or "create a ticket". You give it an address and a key.

The shortest path: open the page **Connect** in the web app, or run `awa connect .` in the project folder. awa asks
for each missing setting, tests it and switches the process to the live system
([details](#enter-the-address-and-the-key)).

In the blueprint, a connector is a few lines. This one lets the agent `analyst` read a database:

```yaml
connectors:
  shop:
    use: sql
    env: {DATABASE_URL: SHOP_DATABASE_URL}
    read_only: true
agents:
  - name: analyst
    tools: [connector:shop.schema, connector:shop.query]
```

Two rules keep this safe:

- **Keys stay out of the blueprint.** The blueprint holds only the names of the settings. The values are in the file
  `.env` of the project folder, on your computer.
- **Changes wait for a person.** Reading runs directly. Anything that changes or sends something waits for approval,
  unless you switch that off for a connector with `requires_approval: false` (`awa validate` then warns).

## Two ways to connect a system

You can connect a system at two moments:

- **Before the build**, when you already know the system. The design then fits its real structure, as in
  [step 3 of the tour](tour.md#step-3-bring-your-files-and-your-ledger).
- **After the build**, when you want to try the process first, or when the process must change data in that system.
  Connecting changes only the entry under `connectors:` and `.env`; the agents and the tests stay. Then adjust the
  expected values of the eval cases to your real data and test again.

| | Before the build: page **Data**, or `awa data add-system` | After the build: page **Connect**, or `awa connect` |
| --- | --- | --- |
| What awa does | Tests the system, saves its settings, reads its structure (tables and columns, or the operations of an API) | Saves the settings, tests the system, switches it from its stand-in to the live system |
| The design | Uses the real tables or operations | Was made on the **stand-in**: sample records awa wrote from your description |
| Changes to the system | None: the system gets no tool that changes data | Possible, and each one waits for a person |

## See what awa can connect to

<!-- guide:do cd . -->
<!-- guide:run -->
```console
$ awa connectors list
NAME             CATEGORY       STABILITY  SETTINGS   TOOLS             TITLE
openapi          generic        core       1 missing  listed by system  OpenAPI (any documented REST API)
rest             generic        core       1 missing  1 read, 1 write   REST API
odata            generic        core       1 missing  4 read, 2 write   OData service
graphql          generic        core       1 missing  3 read, 1 write   GraphQL API
workspace        data           core       set        12 read, 5 write  Local workspace
sql              data           core       1 missing  2 read, 1 write   SQL database
web              web            core       set        2 read, 0 write   Web pages and search
smtp             communication  core       2 missing  0 read, 1 write   Email sending (SMTP)
...
```

- `STABILITY` is `core`, or `preview`: no stability promise, tested against fake connections.
- `SETTINGS` says `set` (the settings are there, the connection is not tested yet) or how many are missing.

The whole catalog:

| Connector | System | What the agents can do |
| --- | --- | --- |
| `workspace` | The project folder (`data/`): the stand-in of every system | Files, CSV and Excel tables, PDF and Word, a SQLite database; writing *(approval)* |
| `sql` | SQLite, PostgreSQL, MySQL or MariaDB, SQL Server | Read the structure, query; change data *(approval)* |
| `openapi` | Any web API with an OpenAPI description | The operations the description lists |
| `rest` | Any web API with JSON | The operations you describe; send *(approval)* |
| `odata` | OData services, such as ERP and CRM systems | List, describe, query, read; create and update *(approval)* |
| `graphql` | Any GraphQL API | Describe, query; change *(approval)* |
| `web` | Web pages and search | Read a page or PDF; search the web |
| `smtp`, `imap` | Sending and reading email | Send *(approval)*; search and read messages |
| `webhook` | A chat channel or automation endpoint | Post *(approval)* |
| `outlook`, `sharepoint`, `teams` | Microsoft 365 *(preview)* | Mail, files and channels; see [Microsoft 365](microsoft-365.md) |
| `microsoft_graph` | Microsoft 365, broad and older *(preview)* | Prefer `outlook`, `sharepoint` and `teams` |
| `slack` | Slack *(preview)* | Read channels and threads; post *(approval)* |
| `jira` | Jira *(preview)* | Search and read issues; create issues and comments *(approval)* |

To see what one system needs, name it:

<!-- guide:run -->
```console
$ awa connectors show jira
jira: Jira (ticketing)
  Search, read, create and comment on Jira issues.
  status: missing JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN
  variables:
    JIRA_URL                     not set    required  Address of your Jira site.
    JIRA_EMAIL                   not set    auth  Email address of the Jira account.
    JIRA_API_TOKEN               not set    auth  API token of that account.
    JIRA_TOKEN                   not set    auth  Personal access token (Jira Data Center).
  authentication (first complete one is used): JIRA_EMAIL + JIRA_API_TOKEN | JIRA_TOKEN
  tools:
    connector:jira.search_issues  Find issues with a JQL query (e.g. project = OPS AND status = 'In Progress').
    connector:jira.get_issue  Read one issue with its description and comments.
    connector:jira.create_issue [approval]  Create an issue.
    connector:jira.add_comment [approval]  Add a comment to an issue.
  setup: Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens.
  docs: https://developer.atlassian.com/cloud/jira/platform/rest/v3/
```

Read it like a form:

- `variables` are the settings to fill. awa shows whether each one is set, never its value.
- `authentication` says which combination of settings the system accepts.
- `tools` are what the agents get. `[approval]` marks the tools that wait for a person.

## Enter the address and the key

=== "Web app"

    Open the page **Connect**. Each system of your process has a card: **Live**, **Needs attention** (a setting is
    missing or not tested yet) or **Stand-in**.

    1. Fill in the fields. An address must start with `https://`; a key is hidden while you type and afterwards
       shows only *saved*.
    2. Press **Test connection**: one small, read-only call with what you typed, and the answer in plain words, for
       example *The system refused the key*. Nothing is saved.
    3. Press **Save and connect**: awa saves the values in `.env`, tests again and switches the system from its
       stand-in to the live one. **Go back to the stand-in** switches it back.

    To prepare a system before any process uses it, open **Set up**: under **External systems** every connector of the
    catalog has a card with its fields.

    ![The Set up page, the card of the system Jira opened: the three settings with hidden fields](../assets/screenshots/guide/b03-systems.png)

    --8<-- "docs/assets/screenshots/guide/b03-systems.legend.txt"

=== "Terminal"

    For a project from `awa build`, run `awa connect .` in the project folder. It asks for every missing setting
    (your typing is hidden), saves it, tests it and switches the project over. An empty answer skips a setting.
    Systems that are already connected are skipped the next time.

    To connect one database to any folder, give it a short name. Here the ledger of the tour plays the database,
    and `shop-desk` is a new folder:

    <!-- guide:run input="company-ledger.db" -->
    ```console
    $ awa connect shop-desk --database shop
    SHOP_DATABASE_URL (postgresql://, mysql://, mssql:// or SQLite file), hidden: 
    Test passed: SQLite database company-ledger.db is readable; 1 table(s) and view(s): invoices
    Saved SHOP_DATABASE_URL in shop-desk/.env

    Add this to blueprint.yaml (agents get the tools they list):

    connectors:
      shop:
        use: sql
        env: {DATABASE_URL: SHOP_DATABASE_URL}
        read_only: true   # only schema and query; no tool that changes data
    agents:
      - name: analyst
        tools: [connector:shop.schema, connector:shop.query]
    ```

    The test only lists table names; it reads no data.

### Database addresses

| Database | Address |
| --- | --- |
| PostgreSQL | `postgresql://reporting@db.example.com:5432/shop` |
| MySQL or MariaDB | `mysql://reporting:password@db.example.com/shop` |
| SQL Server | `mssql://reporting:password@db.example.com:1433/shop` |
| SQLite | The path of the file |

- PostgreSQL needs the `postgres` extra. MySQL, MariaDB and SQL Server need the `sql` extra. The double-click
  installer has both, and `awa doctor` says when one is missing.
- Ask for a login that may only **read**.
- Queries cannot change data. SQLite, PostgreSQL and MySQL refuse changes in the session. SQL Server relies on a check
  of the query text, so a SQL Server connector needs `accept_text_check: true`.

## A web service with a key

Many systems offer a web interface (a REST API) with a key. You do not need a ready-made connector: describe the
calls the agents need, and each one becomes a tool. This blueprint lets an agent find a customer in a CRM and add a
note:

<!-- guide:file crm-desk/blueprint.yaml -->
```yaml
name: crm-desk
description: Finds a customer in the CRM and adds a note.
connectors:
  crm:
    use: rest
    base_url_env: CRM_URL
    auth: {type: api_key, header: X-Api-Key, token_env: CRM_KEY}
    operations:
      - name: find_customer
        description: Find customers by name or email.
        path: /customers
        params: [{name: q, description: Name or email.}]
      - name: add_note
        description: Add a note to a customer.
        method: POST
        path: /customers/{id}/notes
        params:
          - {name: id, description: Customer id., in: path}
          - {name: text, description: The note., in: body}
agents:
  - name: account_manager
    description: Looks customers up and records notes.
    instructions: Find the customer with find_customer. When asked, add a note with add_note.
    tools: [connector:crm.find_customer, connector:crm.add_note]
```

awa tells you which settings are missing:

<!-- guide:run -->
```console
$ cd crm-desk
$ awa validate blueprint.yaml
warning: connectors.crm: connector 'crm' is not configured: set CRM_URL, CRM_KEY (see 'awa connectors show crm --blueprint <file>'); its tools fail until then
crm-desk: valid with 1 warning(s) (tool names not checked; no tools.py beside it)
$ awa connectors show crm --blueprint blueprint.yaml
crm: REST API (generic)
  Any HTTP/JSON API: operations declared in the configuration, or generic get/send tools below the base URL.
  status: missing CRM_URL, CRM_KEY
  variables:
    CRM_URL                      not set    required  Base address of the REST API.
    CRM_KEY                      not set    auth  API key.
  tools:
    connector:crm.find_customer  Find customers by name or email.
    connector:crm.add_note [approval]  Add a note to a customer.
  setup: Set <NAME>_URL to the API's base address and the credentials of the auth option (default: <NAME>_TOKEN as a bearer token, or <NAME>_USERNAME and <NAME>_PASSWORD).
```

Put the address and the key into `crm-desk/.env`:

<!-- guide:file .env -->
```text
CRM_URL=https://crm.example.com/api
CRM_KEY=your-key-here
```

<!-- guide:run -->
```console
$ awa connectors show crm --blueprint blueprint.yaml
crm: REST API (generic)
  Any HTTP/JSON API: operations declared in the configuration, or generic get/send tools below the base URL.
  status: variables set (not tested)
  variables:
    CRM_URL                      SET        required  Base address of the REST API.
    CRM_KEY                      SET        auth  API key.
  tools:
    connector:crm.find_customer  Find customers by name or email.
    connector:crm.add_note [approval]  Add a note to a customer.
  setup: Set <NAME>_URL to the API's base address and the credentials of the auth option (default: <NAME>_TOKEN as a bearer token, or <NAME>_USERNAME and <NAME>_PASSWORD).
$ awa validate blueprint.yaml
crm-desk: valid (tool names not checked; no tools.py beside it)
```

`variables set (not tested)` means both settings are there. To prove they work, make one small real call:
`awa connectors test crm --blueprint blueprint.yaml`. It ends with exit code 1 when it fails.

- The call is the operation named by `health:` on the connector, for example `health: find_customer`.
- Without `health:`, the test only checks that the tools are ready.
- `awa connectors test --all` tests every connector.

How to write `auth` for the common kinds of login:

| What the system wants | In `auth` |
| --- | --- |
| A key in a header | `{type: api_key, header: X-Api-Key, token_env: CRM_KEY}` |
| A key in the address (`?api_key=...`) | `{type: api_key, query: api_key, token_env: CRM_KEY}` |
| A bearer token | Nothing: set `<NAME>_TOKEN`. |
| A user name and a password | Nothing: set `<NAME>_USERNAME` and `<NAME>_PASSWORD`. |

A system with an OpenAPI (Swagger) description needs no `operations:` at all. Each operation of the description
becomes a tool:

```yaml
connectors:
  billing:
    use: openapi
    spec: specs/billing.yaml
    include: [list_invoices, get_invoice]
    health: list_invoices
```

## Connectors in a blueprint

Declare each system once under `connectors:`, then give agents its tools:

- `connector:<name>` gives every reading tool of the system.
- `connector:<name>.<operation>` gives one operation.

The name is yours. With `use:` you can have several instances of one connector, each with its own settings:

| Setting | Meaning |
| --- | --- |
| `use` | The catalog connector of a named instance. |
| `base_url` / `base_url_env` | The address, or the name of the setting that holds it. |
| `env` | Settings to read under other names, such as `{JIRA_URL: OPS_JIRA_URL}`. |
| `read_only: true` | Only the reading tools. |
| `include: [...]` | Only these operations. |
| `requires_approval: false` | Let writing tools run without approval (`awa validate` warns). |
| `timeout`, `retries` | Seconds per request; retries of temporary failures. |
| `allowed_hosts`, `allow_private_networks`, `allow_http` | Requests go only to the system's own address over `https://`; these allow more, for example a system inside your company network. |

Temporary failures are retried, and long lists are read page by page up to a limit. awa removes known credential
values and common credential formats from its answers, errors and logs. A tool you wrote that echoes a key back is
not covered.

Not covered by the catalog?

- The tools of an MCP server are added with `mcp:<server>` ([MCP](../develop/mcp.md)).
- For any other system, a developer writes a connector or a tool ([Connectors](../develop/connectors.md)).

## Common mistakes

| Mistake | What you see | Fix |
| --- | --- | --- |
| The key is in the blueprint | You can read it in the file | Move it to `.env` and name it with `token_env`. |
| `.env` is not in the folder you run awa in | `awa validate` warns that settings are not set | Go to the project folder. |
| The name in `.env` differs from what awa expects | `status: missing ...` although you entered it | Copy the name from `awa connectors show`. |
| `The connection test failed` | The system did not answer or refused the login | Check the address, the key and whether the system accepts connections from your computer (a VPN?). |
| `requests to internal networks are blocked` | The system is inside your company network | Set `allow_private_networks: true`, and `allow_http: true` if it has no TLS. |
| `.env` is shared or committed | The key is exposed | Never share it. The web app's download leaves it out; `.env.example` lists the names only. |

!!! example "In code"

    The catalog is a Python list too; a developer adds a connector of their own
    ([Write a connector](../develop/connectors.md#write-a-connector)).

    <!-- guide:python -->
    ```python
    from awa.connectors import list_connectors

    print(sorted(c.name for c in list_connectors() if c.category == "data"))
    ```

    <!-- guide:expect -->
    ```text
    ['sql', 'workspace']
    ```

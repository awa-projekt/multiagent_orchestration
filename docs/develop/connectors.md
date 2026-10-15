# Connectors

A connector gives agents the tools of one external system, such as Jira, a REST API or a database.
You configure it as data instead of writing code. This page shows how to use a connector in Python
and in a blueprint, how to configure the generic connectors, and how to write a connector for a
system awa does not know.

To set connectors up without code (`awa connect`, the web app), see
[Connect systems](../guide/connect.md).

## What every connector does for you

Each connector is a `ConnectorSpec`: where the system is, how to sign in and which operations exist.
One engine turns every operation into a tool and adds what a company needs:

- **Writes wait for a person.** Operations that write or send wait for approval by default and
  count as external actions in the risk check; reads run directly. `requires_approval: false` opts
  an instance out (`awa validate` warns).
- **Credentials come from environment variables** or `.env`. They are read at call time and
  replaced by `***` in results, errors and logs.
- **Requests stay where they belong.** Only the base URL's host (plus token endpoints) is allowed.
  Private addresses and plain `http://` are refused unless you allow them. Answers are size-limited
  and framed as external data.
- **Transient failures are retried**: reads after 429, 502, 503, 504 and failed connections, writes
  only after 429, honoring `Retry-After`. Expired OAuth tokens are renewed once.
- **Lists are paged** up to the tool's `limit`, whatever scheme the API uses. More items come with
  `has_more: true` and a signed `next_page` token that the model passes back as `page_token`.

## Use a connector

In Python, `connector_tools(name)` returns the tools of a connector:

```python
from awa import Agent
from awa.connectors import connector_tools

triage = Agent(
    "triage",
    instructions="Sort new support tickets and draft replies.",
    tools=[*connector_tools("slack"), *connector_tools("jira", config={"read_only": True})],
)
```

HTTP connectors read their variables on every call, so you can build the tools before the
credentials exist; a call then says which variable to set. Connectors that wrap a toolkit or a
protocol (`workspace`, `sql`, `smtp`, `imap`, `webhook`) need the variables when the tools are
built.

In a blueprint, list the connectors and reference their tools. `connector:<name>` gives all reads of
a connector; `connector:<name>.<operation>` gives one operation:

```yaml
name: support
connectors:
  slack: {}
  tickets:                       # a second instance of a connector
    use: jira
    env: {JIRA_URL: OPS_JIRA_URL, JIRA_API_TOKEN: OPS_JIRA_TOKEN}
agents:
  - name: triage
    instructions: Sort new tickets; open a Jira issue for bugs.
    tools: [connector:slack, connector:tickets.create_issue]
```

Tools are named `<connector>_<operation>`. An approval on `connector:tickets` covers every operation
of that instance; one on `connector:tickets.create_issue` covers one.

| Option | Meaning |
| --- | --- |
| `use` | The catalog connector of a named instance. |
| `base_url`, `base_url_env` | The API address, or the variable that holds it. |
| `env` | Variables to read under other names. |
| `include`, `read_only` | Only these operations; only reads. |
| `requires_approval` | `false` lets writes run without approval (warned). |
| `timeout`, `retries`, `max_response_bytes`, `max_output_chars` | Limits. |
| `allowed_hosts`, `allow_private_networks`, `allow_http` | More hosts, internal systems, plain HTTP. |
| `use_env_proxies` | Send requests through the proxies of the environment. |

## The catalog

awa ships 16 connectors. The 10 generic connectors are core: `openapi`, `rest`, `odata`,
`graphql`, `sql`, `web`, `smtp`, `imap`, `webhook` and `workspace`. The 6 SaaS connectors (Outlook,
SharePoint, Teams, Microsoft Graph, Slack and Jira) are preview: they are tested against fake
connections, and the Microsoft 365 connectors were also read live on one test tenant.

- `awa connectors list` shows every connector with the variables it needs. In code,
  `list_connectors()` and `get_connector(name)` return the specs.
- `discover()` reports what is configured in the environment and the `.env` of the working
  directory, by variable name only: `report.brief()` as text, `report.available()` as a list,
  `report.to_dict()` as JSON.
- `check_connector(name)` makes one small real call, which is what `awa connectors test` does.

## Generic connectors

The generic connectors take everything from their options. An instance's variables are named after
it: `billing` reads `BILLING_URL`, `BILLING_TOKEN`.

- **OpenAPI** (`use: openapi`, `spec:` a file or URL): operations become tools named after their
  `operationId`; methods other than `GET` are writes. `tags:` or `include:` select operations,
  `health:` names the test call. Operations that cannot be tools (file uploads, binary answers) are
  left out and listed in the spec's `notes`.
- **REST** (`use: rest`): describe the operations yourself, with parameters, `items` and paging.
  Without `operations`, an instance offers `get` (any path below the base URL) and the gated `send`.
- **OData** (`use: odata`): entity sets with `$filter`, `$select`, `$orderby` and `$expand`,
  server-driven paging and `$metadata`; `csrf: true` for SAP Gateway writes.
- **GraphQL** (`use: graphql`): `describe_schema`, `describe_type`, `query` (refuses mutations) and
  the gated `mutate`.
- **SQL** (`use: sql`, `DATABASE_URL`): `sqlite:///file.db`, `postgresql://` (extra `postgres`),
  `mysql://` or `mariadb://` and `mssql://` (extra `sql`). `query` accepts one `SELECT` or `WITH`
  and rolls back; PostgreSQL and MySQL also run it in a read-only session. SQL Server has no
  read-only session: give the login read rights only and set `accept_text_check: true`.

A REST instance with one operation:

```yaml
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
        items: data
        pages: {kind: cursor, next: meta.next_cursor, param: cursor}
```

`auth` takes `bearer`, `basic`, `api_key` (`header`, `prefix`, or `query` for a key in the URL),
`oauth2_client_credentials` (`token_url`, `scope`), `oauth2_refresh_token` or `none`, each with
`*_env` keys naming its variables. A list tries each in turn.

Two more safeguards:

- Writes that send an `Idempotency-Key` header send the same key for one tool call of one run, also
  when the run resumes after a crash.
- HTML that a model writes for Outlook and Teams passes an outbound filter first: remote images,
  scripts, forms and frames are removed, and links keep only targets on the hosts of
  `allowed_link_hosts`.

## Write a connector

Write a connector when awa has none for your system and the generic ones do not fit. The example
system is an inventory API with a list of items (a read) and a reservation (a write).

Create `inventory_connector.py`:

```python title="inventory_connector.py"
from awa.connectors import (
    BearerAuth, ConnectorSpec, EnvVar, Operation, Pagination, Param, register_connector,
)

SPEC = ConnectorSpec(
    name="inventory",
    title="Inventory",
    category="data",
    summary="Stock levels and reservations.",
    base_url="{INVENTORY_URL}",
    env=(EnvVar("INVENTORY_URL", "Address of the inventory API.", secret=False),),
    auth=(BearerAuth(EnvVar("INVENTORY_TOKEN", "API token.")),),
    operations=(
        Operation("list_items", "List stock items.", path="/items", items="items",
                  pages=Pagination("cursor", next="next", param="cursor", size_param="size")),
        Operation("reserve", "Reserve stock for an order.", method="POST",
                  path="/items/{sku}/reserve",
                  params=(Param("sku", "Item number.", location="path", required=True),
                          Param("quantity", "How many.", type="integer", location="body")),
                  write=True),
    ),
    health="list_items",  # the read that `awa connectors test inventory` makes
)


def register() -> None:
    register_connector(SPEC, replace=True)
```

- Write the descriptions for the model: it chooses a tool by its description.
- `write=True` marks an operation that changes something, which puts an approval in front of it.
- A system with an OpenAPI document needs no hand-written spec:
  `openapi_connector("specs/inventory.yaml", name="inventory")` builds it.

Try it against a fake system. `MockTransport` answers from routes and records every request, so
nothing is sent anywhere:

```python title="try_it.py"
import asyncio

from awa.connectors import MockTransport, connector_tools, mock_response
from inventory_connector import register

register()
transport = MockTransport(
    {"GET /items": mock_response(json={"items": [{"sku": "A-1", "stock": 4}], "next": None})}
)
env = {"INVENTORY_URL": "https://stock.example.com", "INVENTORY_TOKEN": "test-token"}
tools = {t.name: t for t in connector_tools("inventory", env=env, transport=transport)}
print(sorted(tools))
print("reserve needs approval:", tools["inventory_reserve"].metadata.get("awa_requires_approval"))
print("list_items needs approval:", tools["inventory_list_items"].metadata.get("awa_requires_approval"))


async def main() -> None:
    await tools["inventory_list_items"].ainvoke({"limit": 5})
    request = transport.requests[0]
    print(request.method, request.url)
    print(request.header("authorization") == "Bearer test-token")


asyncio.run(main())
```

```text title="try_it.out"
['inventory_list_items', 'inventory_reserve']
reserve needs approval: True
list_items needs approval: None
GET https://stock.example.com/items?size=5
True
```

Reference the tools in a blueprint. `check()` tests the references without calling anything:

```python title="check_blueprint.py"
from awa.blueprints import Blueprint
from inventory_connector import register

register()
blueprint = Blueprint.from_yaml_text("""
name: stock-desk
agents:
  - name: clerk
    instructions: Answer stock questions and reserve stock for orders.
    tools: [connector:inventory.list_items, connector:inventory.reserve]
""")
for issue in blueprint.check():
    print(issue)
```

```text title="check_blueprint.out"
warning: connectors.inventory: connector 'inventory' is not configured: set INVENTORY_URL, INVENTORY_TOKEN (see 'awa connectors show inventory'); its tools fail until then
```

A misspelled operation is an error that names the operations that exist. A test keeps the
connector honest:

```python title="test_inventory_connector.py"
import asyncio

from awa.connectors import MockTransport, connector_tools, mock_response
from inventory_connector import register

ENV = {"INVENTORY_URL": "https://stock.example.com", "INVENTORY_TOKEN": "secret-value"}


def tools_for(transport):
    register()
    return {t.name: t for t in connector_tools("inventory", env=ENV, transport=transport)}


def test_only_the_write_waits_for_approval() -> None:
    tools = tools_for(MockTransport())

    assert tools["inventory_reserve"].metadata.get("awa_requires_approval") is True
    assert not tools["inventory_list_items"].metadata.get("awa_requires_approval")


def test_a_read_sends_the_token_and_never_shows_it_to_the_model() -> None:
    transport = MockTransport({"GET /items": mock_response(json={"items": []})})
    answer = asyncio.run(tools_for(transport)["inventory_list_items"].ainvoke({"limit": 3}))

    assert transport.requests[0].header("authorization") == "Bearer secret-value"
    assert "secret-value" not in str(answer)
```

`register_connector` registers in the calling process only: call it at the start of the process that
uses the connector. The transport (`awa.connectors.Transport`, a function from an `HttpRequest` to a
response) is the only thing a connector depends on; awa's own transport is the hardened HTTP client.

## What can go wrong

| You see | Why | Fix |
| --- | --- | --- |
| `ConfigurationError: a connector named 'inventory' already exists` | Registered twice | `register_connector(spec, replace=True)`, or register once at start-up |
| A tool answers "set INVENTORY_URL" | The variable is not set when the tool runs | Set it in the environment or `.env`; `awa connectors show inventory` lists the names |
| The request is refused | Only the base URL's host is allowed; private addresses and plain `http://` are refused | `allowed_hosts`, or `allow_private_networks` and `allow_http` for an internal system |
| Your connector is missing in `awa connectors list` | `register_connector` runs in your process only | Call it at the start of the process that uses it |
| A write ran without asking | `write=True` was forgotten | Mark every operation that changes something |

Reference: [Connectors](../reference/api/data.md#awa.connectors).

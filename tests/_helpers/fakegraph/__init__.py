"""FakeGraph: a local Microsoft Graph and Entra ID look-alike for the offline tests.

A real threaded HTTP server on 127.0.0.1 (a random free port), standard library only. Connectors
reach it through the same code as in production: point `MS_GRAPH_URL` and `AZURE_AUTHORITY_HOST`
at it and pass the connector options `allow_http` and `allow_private_networks`
(`graph.env(app)` and `graph.connector_options` give both). Nothing is patched.

It is a simulation: every rule it enforces (`graph.rules`) names its documentation source and
date and carries `verified_live=False` until a run against a real tenant confirmed it. A green
result against FakeGraph says nothing about a real tenant.

Modules: `server` (`FakeGraph`), `core` (rules, tokens, the plug-in API), `identity` (the token
endpoint), `files`, `mail`, `teams`, `todo`, `directory` and `people` (service plug-ins),
`odata` (paging and delta).

Example:
    ```python
    from _helpers.fakegraph import FakeGraph

    with FakeGraph() as graph:
        app = graph.add_app("client-1", "s3cret", roles=("Mail.Read",))
        graph.add_user("ada@contoso.test", name="Ada")
        env = graph.env(app)  # MS_GRAPH_URL, AZURE_AUTHORITY_HOST, AZURE_TENANT_ID, ...
    ```
"""

from _helpers.fakegraph.core import Rule
from _helpers.fakegraph.identity import App, User
from _helpers.fakegraph.server import BUILTIN_PLUGINS, TENANT_DOMAIN, TENANT_ID, FakeGraph

__all__ = ["BUILTIN_PLUGINS", "TENANT_DOMAIN", "TENANT_ID", "App", "FakeGraph", "Rule", "User"]

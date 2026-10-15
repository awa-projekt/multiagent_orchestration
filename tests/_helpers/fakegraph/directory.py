"""Directory plug-in (minimal): `GET /organization`, the call a least-privilege app cannot make.
Using it also serves the `people` plug-in (`GET /users?$search=`, `GET /users/{id}`), which
`awa check microsoft` asks once, optionally.

Not part of `BUILTIN_PLUGINS`: a test that needs it adds it with `graph.use(fake_graph_directory)`.
"""

from __future__ import annotations

from typing import Any

from _helpers.fakegraph.core import Ctx, Perm, Plugin

plugin = Plugin("directory")

ORGANIZATION = Perm(
    app=("Organization.Read.All", "Directory.Read.All"),
    user=("User.Read", "Organization.Read.All", "Directory.Read.All"),
)

R_ORGANIZATION = plugin.rule(
    "directory.organization_needs_organization_read",
    "GET /organization needs Organization.Read.All (or Directory.Read.All) as an application "
    "permission; an app that holds only mail or site roles is 403 Authorization_RequestDenied.",
    source="learn.microsoft.com/graph/api/organization-list",
    source_date="2026-10-05",
)


@plugin.setup
def _setup(graph: Any) -> None:
    """The people search lives in the `people` plug-in (one user directory): serve it too."""

    from _helpers.fakegraph import people

    graph.use(people)


@plugin.route("GET", "/organization", perm=ORGANIZATION, family="directory")
def organization(ctx: Ctx) -> dict[str, object]:
    return {
        "@odata.context": f"{ctx.base}/v1.0/$metadata#organization",
        "value": [{"id": ctx.graph.tenant_id, "displayName": "Contoso"}],
    }

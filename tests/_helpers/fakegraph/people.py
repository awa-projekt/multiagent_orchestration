"""People plug-in: `GET /users` with `$search` or `$filter=startswith`, and `GET /users/{id}`.

The one user directory of FakeGraph: `awa.people.EntraPeople` and the people check of
`awa check microsoft` both read it (the `directory` plug-in serves it too). Not part of
`BUILTIN_PLUGINS`: add it with `graph.use(people)` and fill it with `graph.store("people").add(...)`.
The accounts of the identity plug-in (`graph.add_user`) are listed as members as well, with their
user principal name as mail; `delete` and `update` change the people added here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from _helpers.fakegraph.core import Ctx, GraphError, Perm, Plugin, new_guid
from _helpers.fakegraph.odata import apply_select, check_properties, collection, split_list

plugin = Plugin("people")

LIST_USERS = Perm(
    app=("User.Read.All", "User.ReadWrite.All", "Directory.Read.All", "Directory.ReadWrite.All"),
    user=(
        "User.ReadBasic.All",
        "User.Read.All",
        "User.ReadWrite.All",
        "Directory.Read.All",
        "Directory.ReadWrite.All",
    ),
)
FULL_PROFILE = frozenset(LIST_USERS.user) - {"User.ReadBasic.All"}
PROPERTIES = (
    "id",
    "displayName",
    "givenName",
    "surname",
    "mail",
    "userPrincipalName",
    "accountEnabled",
    "userType",
    "jobTitle",
)
_BASIC = {"id", "displayName", "givenName", "surname", "mail", "userPrincipalName"}
_SEARCHABLE = {"displayName", "description", "mail", "userPrincipalName", "givenName", "surname"}
_UNSUPPORTED = (
    "Request with $search query parameter only works through MSGraph with a special request "
    "header: 'ConsistencyLevel: eventual'"
)

R_CONSISTENCY = plugin.rule(
    "people.search_needs_consistency_level",
    "GET /users with $search needs the header ConsistencyLevel: eventual and $count=true; "
    "without them Graph answers 400 Request_UnsupportedQuery.",
    source="learn.microsoft.com/graph/aad-advanced-queries",
    source_date="2026-03-07",
)
R_SYNTAX = plugin.rule(
    "people.search_clause_syntax",
    'A $search clause is "<property>:<text>" in double quotes (inner " and \\ escaped with a '
    "backslash), joined by uppercase AND or OR; anything else is 400 BadRequest.",
    source="learn.microsoft.com/graph/search-query-parameter",
    source_date="2026-03-07",
)
R_TOKENS = plugin.rule(
    "people.search_matches_word_beginnings",
    "displayName is split into words (spaces, symbols, digits, lower-to-upper case changes) and "
    "every search word must begin a word, in any order, any case; other properties such as mail "
    "match from the start of the value.",
    source="learn.microsoft.com/graph/search-query-parameter",
    source_date="2026-03-07",
)
R_AMPERSAND = plugin.rule(
    "people.search_ampersand_refused",
    'On v1.0 a $search value holding an encoded & is 400 with "Unrecognized query argument '
    "specified: ''.\"",
    source="learn.microsoft.com/graph/known-issues",
    source_date="2026-04-11",
)
R_PERMISSION = plugin.rule(
    "people.list_users_permissions",
    "Listing or reading other users needs User.Read.All (application) or User.ReadBasic.All "
    "(delegated) or higher; without it 403 Authorization_RequestDenied.",
    source="learn.microsoft.com/graph/api/user-list",
    source_date="2026-05-14",
)
R_NOT_FOUND = plugin.rule(
    "people.get_missing_user_404",
    "GET /users/{id} of a user that does not exist (or was deleted) is 404 Request_ResourceNotFound.",
    source="learn.microsoft.com/graph/api/user-get",
    source_date="2025-07-23",
)
R_BASIC = plugin.rule(
    "people.readbasic_hides_other_properties",
    "With User.ReadBasic.All only the basic profile is read: accountEnabled and userType come back "
    "null even when selected.",
    source="learn.microsoft.com/graph/permissions-reference",
    source_date="2026-09-15",
)
R_GUEST = plugin.rule(
    "people.guest_upn_has_ext",
    "The userPrincipalName of a guest (B2B collaboration) user always contains #EXT#.",
    source="learn.microsoft.com/graph/api/user-list",
    source_date="2026-05-14",
)
R_PAGE = plugin.rule(
    "people.page_size",
    "GET /users returns at most $top users (100 by default, 999 at most).",
    source="learn.microsoft.com/graph/api/user-list",
    source_date="2026-05-14",
)
R_STARTSWITH = plugin.rule(
    "people.filter_startswith",
    "$filter=startswith(displayName,'<text>') matches names that begin with the text (any case), "
    "with '' for a quote inside; it needs no ConsistencyLevel header.",
    source="learn.microsoft.com/graph/api/user-list",
    source_date="2026-05-14",
)


@dataclass
class PeopleStore:
    """The directory of the fake tenant."""

    graph: Any
    people: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(
        self,
        name: str,
        mail: str | None,
        *,
        upn: str | None = None,
        guest: bool = False,
        enabled: bool = True,
        id: str | None = None,
    ) -> dict[str, Any]:
        """Add a person; a guest gets a `#EXT#` user principal name as Entra gives them."""

        local = (mail or name.replace(" ", ".")).split("@")[0]
        domain = self.graph.tenant_domain
        if upn is None:
            upn = (
                f"{(mail or local).replace('@', '_')}#EXT#@{domain}"
                if guest
                else f"{local}@{domain}"
            )
        person: dict[str, Any] = {
            "id": id or new_guid(f"person:{upn}"),
            "displayName": name,
            "givenName": name.split(" ")[0],
            "surname": name.split(" ")[-1],
            "mail": mail,
            "userPrincipalName": upn,
            "accountEnabled": enabled,
            "userType": "Guest" if guest else "Member",
            "jobTitle": None,
        }
        self.people[person["id"]] = person
        return person

    def delete(self, person_id: str) -> None:
        self.people.pop(person_id)

    def update(self, person_id: str, **changes: Any) -> None:
        self.people[person_id].update(changes)

    def everyone(self) -> list[dict[str, Any]]:
        """The people added here and, after them, the accounts of the identity plug-in."""

        found = list(self.people.values())
        for user in self.graph.users.values():
            if user.id not in self.people:
                found.append(
                    {
                        "id": user.id,
                        "displayName": user.name,
                        "givenName": user.name.split(" ")[0],
                        "surname": user.name.split(" ")[-1],
                        "mail": user.upn,
                        "userPrincipalName": user.upn,
                        "accountEnabled": True,
                        "userType": "Member",
                        "jobTitle": None,
                    }
                )
        return found

    def find(self, reference: str) -> dict[str, Any] | None:
        lowered = reference.lower()
        return next(
            (
                p
                for p in self.everyone()
                if p["id"].lower() == lowered or p["userPrincipalName"].lower() == lowered
            ),
            None,
        )


@plugin.setup
def _setup(graph: Any) -> PeopleStore:
    return PeopleStore(graph)


# --------------------------------------------------------------------------- matching


def tokens(text: str) -> list[str]:
    """Graph's words of a value: split at spaces, symbols, digits and lower-to-upper changes."""

    split = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return [t.lower() for t in re.findall(r"[^\W\d_]+|\d+", split)]


def _clause_matches(person: dict[str, Any], prop: str, text: str) -> bool:
    value = person.get(prop)
    if not isinstance(value, str):
        return False
    if prop in ("displayName", "description"):
        have = tokens(value)
        return all(any(h.startswith(w) for h in have) for w in tokens(text))
    return value.lower().startswith(text.lower())


_TOKEN = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|(AND|OR)\b|(\()|(\)))')


def _parse_search(text: str) -> list[Any]:
    """The clauses and operators of a `$search` value, as a flat list (`(`/`)` included)."""

    out: list[Any] = []
    position = 0
    while position < len(text):
        if not text[position:].strip():
            break
        found = _TOKEN.match(text, position)
        if found is None:
            raise GraphError(400, "BadRequest", "Syntax error: invalid $search expression.")
        quoted, operator, opened, closed = found.groups()
        if quoted is not None:
            clause = re.sub(r"\\(.)", r"\1", quoted)
            prop, colon, value = clause.partition(":")
            if not colon or prop not in _SEARCHABLE:
                raise GraphError(
                    400,
                    "BadRequest",
                    f"Invalid search property '{prop}'."
                    if colon
                    else "Syntax error: a $search clause needs '<property>:<text>'.",
                )
            out.append((prop, value))
        else:
            out.append(operator or opened or closed)
        position = found.end()
    return out


def _evaluate(items: list[Any], person: dict[str, Any]) -> bool:
    position = 0

    def expression() -> bool:
        nonlocal position
        result = term()
        while position < len(items) and items[position] == "OR":
            position += 1
            right = term()
            result = result or right
        return result

    def term() -> bool:
        nonlocal position
        result = factor()
        while position < len(items) and items[position] == "AND":
            position += 1
            right = factor()
            result = result and right
        return result

    def factor() -> bool:
        nonlocal position
        if position >= len(items):
            raise GraphError(400, "BadRequest", "Syntax error: the $search expression ends early.")
        item = items[position]
        position += 1
        if item == "(":
            inner = expression()
            if position >= len(items) or items[position] != ")":
                raise GraphError(400, "BadRequest", "Syntax error: a ( is not closed.")
            position += 1
            return inner
        if isinstance(item, tuple):
            return _clause_matches(person, *item)
        raise GraphError(400, "BadRequest", f"Syntax error: unexpected {item!r}.")

    result = expression()
    if position != len(items):
        raise GraphError(400, "BadRequest", "Syntax error: invalid $search expression.")
    return result


_STARTSWITH = re.compile(r"^startswith\(displayName,'((?:[^']|'')*)'\)$", re.IGNORECASE)


def _shown(ctx: Ctx, person: dict[str, Any], select: list[str]) -> dict[str, Any]:
    principal = ctx.principal
    visible = dict(person)
    if principal is not None and principal.kind == "user" and not principal.grants & FULL_PROFILE:
        visible = {k: (v if k in _BASIC else None) for k, v in visible.items()}
    if not select:
        select = ["id", "displayName", "mail", "userPrincipalName", "givenName", "surname"]
    return apply_select(visible, select, always=("id",))


# --------------------------------------------------------------------------- routes


@plugin.route("GET", "/users", perm=LIST_USERS, family="directory")
def list_users(ctx: Ctx) -> dict[str, Any]:
    store: PeopleStore = ctx.store
    request = ctx.request
    select = split_list(request.q("$select"))
    check_properties(select, PROPERTIES, "microsoft.graph.user")
    people = sorted(store.everyone(), key=lambda p: str(p["displayName"]).lower())
    search = request.q("$search")
    if search is not None:
        if (request.header("ConsistencyLevel") or "").lower() != "eventual" or (
            request.q("$count") or ""
        ).lower() != "true":
            raise GraphError(400, "Request_UnsupportedQuery", _UNSUPPORTED)
        if "&" in search:
            raise GraphError(400, "BadRequest", "Unrecognized query argument specified: ''.")
        parsed = _parse_search(search)
        if not parsed:
            raise GraphError(400, "BadRequest", "Syntax error: empty $search expression.")
        people = [p for p in people if _evaluate(parsed, p)]
    filter_text = request.q("$filter")
    if filter_text is not None:
        found = _STARTSWITH.match(filter_text.strip())
        if found is None:
            raise GraphError(
                400,
                "Request_UnsupportedQuery",
                "Unsupported or invalid query filter clause specified for property of resource "
                "'User'.",
            )
        prefix = found.group(1).replace("''", "'").lower()
        people = [p for p in people if str(p["displayName"]).lower().startswith(prefix)]
    body = collection(
        ctx,
        people,
        default_top=100,
        max_top=999,
        context="users",
        shape=lambda person: _shown(ctx, person, select),
        always=("id",),
    )
    if (request.q("$count") or "").lower() == "true":
        body["@odata.count"] = len(people)
    return body


@plugin.route("GET", "/users/{user-id}", perm=LIST_USERS, family="directory")
def get_user(ctx: Ctx) -> dict[str, Any]:
    store: PeopleStore = ctx.store
    select = split_list(ctx.request.q("$select"))
    check_properties(select, PROPERTIES, "microsoft.graph.user")
    reference = ctx.params["user_id"]
    person = store.find(reference)
    if person is None:
        raise GraphError(
            404,
            "Request_ResourceNotFound",
            f"Resource '{reference}' does not exist or one of its queried reference-property "
            "objects are not present.",
        )
    return {
        "@odata.context": f"{ctx.base}/v1.0/$metadata#users/$entity",
        **_shown(ctx, person, select),
    }

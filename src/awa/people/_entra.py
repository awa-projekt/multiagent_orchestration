"""`EntraPeople`: find colleagues in Microsoft Entra ID through Microsoft Graph; search text and
mail addresses never reach a log line, span attribute or error message."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from awa._internal.logging import get_logger
from awa._internal.retry import parse_retry_after
from awa._internal.upstream.tools import ToolException
from awa.connectors._engine import ConnectorContext, Transport, send
from awa.connectors._microsoft_auth import UserTokenProvider, decode_claims
from awa.connectors._microsoft_errors import MicrosoftAuthError
from awa.connectors._saas_microsoft import _AUTHORITY, _GRAPH_URL, _entra, microsoft_user_agent
from awa.connectors._spec import ConnectorSpec, TokenAuth
from awa.observability.spans import AttributeValue, timed_span
from awa.people._ref import (
    MAX_RESULTS,
    MIN_SEARCH,
    Kind,
    PeopleError,
    PersonRef,
    State,
    clean_search_text,
    is_guid,
    make_key,
    stamp,
)
from awa.people.ports import PeopleDirectory
from awa.runtime.clock import Clock, as_clock

logger = get_logger("people")

CACHE_SECONDS = 900.0
"""How long a person read from Entra counts as fresh (15 minutes)."""
SEARCH_SELECT = "id,displayName,mail,userPrincipalName"
"""The properties a search asks for: id, name, mail and the user principal name (for `#EXT#`)."""
GET_SELECT = SEARCH_SELECT + ",accountEnabled"
"""A single read also asks whether the account is enabled (answered with User.Read.All)."""
REDACTED = "[redacted]"
_GUEST_MARK = "#ext#"

ENTRA_PEOPLE = ConnectorSpec(
    name="entra_people",
    title="Microsoft Entra ID (people)",
    category="collaboration",
    summary="Find colleagues by name in Microsoft Entra ID through Microsoft Graph.",
    base_url="{MS_GRAPH_URL}/v1.0",
    env=(_GRAPH_URL, _AUTHORITY),
    auth=(
        _entra("https://graph.microsoft.com/.default"),
        TokenAuth(UserTokenProvider, kind="user", token_urls=("{AZURE_AUTHORITY_HOST}",)),
    ),
    headers={"User-Agent": microsoft_user_agent()},
    options=("token_cache", "account"),
    docs="https://learn.microsoft.com/graph/api/user-list",
)
"""The Graph connection people are read through (not in the connector catalog)."""

_SETUP = (
    "Connect Entra ID: set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET of an app "
    "registration with the application permission User.Read.All (an administrator grants admin "
    "consent), or sign a person in whose sign-in includes User.ReadBasic.All."
)


def graph_term(text: str) -> str:
    """`text` as one `$search` clause value: `&` dropped, `\\` and `"` escaped.

    Raises:
        PeopleError: If fewer than 2 characters remain.
    """

    term = " ".join(text.replace("&", " ").split())
    if len(term) < MIN_SEARCH:
        raise PeopleError(f"Type at least {MIN_SEARCH} letters of the name.")
    return term.replace("\\", "\\\\").replace('"', '\\"')


def redacted_query(params: Mapping[str, str]) -> str:
    """The query as it may appear in a span or log: `$search` and `$filter` values hidden."""

    return "&".join(
        f"{k}={REDACTED if k in ('$search', '$filter') else v}" for k, v in params.items()
    )


class EntraPeople(PeopleDirectory):
    """A `PeopleDirectory` over Microsoft Entra ID.

    Search is Graph `$search` on `displayName` and `mail` (word beginnings, any order), falling back
    to `startswith(displayName, ...)` where `$search` is refused; guests are left out unless asked
    for. A person Graph no longer has is `state="deleted"`. Signs in as the app (`AZURE_TENANT_ID`,
    `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, User.Read.All) or as a signed-in person from the token
    cache (User.ReadBasic.All).

    Args:
        env: Variables instead of the process environment (`AZURE_*`, `MS_GRAPH_URL`, ...).
        dotenv: The `.env` file read when `env` is not given (default `.env` in the working
            directory).
        options: `token_cache`, `account`, and the HTTP options (`allow_http`,
            `allow_private_networks`, `timeout`, `retries`) for a local test server.
        transport: Sends the requests instead of the network (a test double).
        clock: Stamps `checked_at`; also times the 15-minute cache.
    """

    source = "entra"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        dotenv: str | None = ".env",
        options: Mapping[str, Any] | None = None,
        transport: Transport | None = None,
        clock: Clock | None = None,
    ) -> None:
        from awa.connectors._catalog import connector_context

        ctx = connector_context(
            ENTRA_PEOPLE, env=env, config=options, transport=transport, dotenv=dotenv
        )
        self._setup(ctx, clock)

    @classmethod
    def from_context(cls, ctx: ConnectorContext, *, clock: Clock | None = None) -> EntraPeople:
        """The directory of the tenant a Microsoft connector instance signs in to (same
        variables, options and transport)."""

        found = cls.__new__(cls)
        found._setup(ctx.derive(ENTRA_PEOPLE), clock)
        return found

    def _setup(self, ctx: ConnectorContext, clock: Clock | None) -> None:
        self._ctx = ctx
        self._clock = as_clock(clock)
        self._cache: dict[str, tuple[float, PersonRef]] = {}

    def __repr__(self) -> str:
        return "EntraPeople()"

    def _stamp(self) -> str:
        return stamp(self._clock.now())

    def _call(
        self,
        path: str,
        params: Mapping[str, str],
        span: dict[str, AttributeValue | None],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One GET, its query and status written to `span`; returns the status and the JSON body
        (error statuses are returned for the caller to word, except those every caller words the
        same)."""

        span["url.query"] = redacted_query(params)
        url = f"{self._ctx.base_url()}{path}?{_query(params)}"
        sent = {"Accept": "application/json", **ENTRA_PEOPLE.headers, **(headers or {})}
        try:
            response = send(self._ctx, "GET", url, headers=sent)
        except MicrosoftAuthError as exc:
            raise PeopleError(f"Entra ID sign-in failed: {exc} {_SETUP}") from None
        except ToolException as exc:
            text = self._ctx.redact(str(exc)).replace(url, "Microsoft Graph")
            raise PeopleError(f"Entra ID could not be reached: {text}") from None
        try:
            data = response.json() if response.body else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        data = data if isinstance(data, dict) else {}
        span["http.response.status_code"] = response.status
        logger.debug("people request: HTTP %s", response.status)
        if response.status == 429:
            wait = parse_retry_after(response.headers.get("Retry-After"))
            raise PeopleError(
                "Microsoft Graph is limiting the requests (HTTP 429)"
                + (f"; try again in {wait:.0f} seconds." if wait is not None else "; try later."),
                details={"retry_after": wait},
            )
        if response.status == 401:
            raise PeopleError(f"Microsoft Graph rejected the sign-in (HTTP 401). {_SETUP}")
        if response.status == 403:
            raise PeopleError(
                "Microsoft Graph refused to list people (HTTP 403): the app lacks User.Read.All "
                "(application, admin consent) or the signed-in person lacks User.ReadBasic.All. "
                + _SETUP
            )
        if response.status >= 500:
            raise PeopleError(f"Microsoft Graph had a server error (HTTP {response.status}).")
        return response.status, data

    @staticmethod
    def _span(
        operation: str, path: str
    ) -> AbstractContextManager[dict[str, AttributeValue | None]]:
        return timed_span(
            f"awa.people.{operation}",
            {
                "awa.people.adapter": "entra",
                "awa.people.operation": operation,
                "http.request.method": "GET",
                "url.path": "/v1.0" + path,
            },
        )

    def tenant_id(self) -> str:
        """The tenant: `AZURE_TENANT_ID` when it is a GUID, else the `tid` of the token used.

        Raises:
            PeopleError: If neither is known.
        """

        configured = self._ctx.value("AZURE_TENANT_ID")
        if is_guid(configured):
            return str(configured).lower()
        claimed = decode_claims(self._ctx._tokens.value or "").tenant
        if is_guid(claimed):
            return str(claimed).lower()
        raise PeopleError(
            "Set AZURE_TENANT_ID to the Directory (tenant) ID, the GUID on the overview page of the "
            "app registration."
        )

    def _person(self, item: Mapping[str, Any], key: str) -> PersonRef:
        upn = str(item.get("userPrincipalName") or "")
        mail = item.get("mail")
        kind: Kind = "member" if upn else "unknown"
        if _GUEST_MARK in upn.lower():
            kind = "guest"
        return PersonRef(
            key=key,
            source="entra",
            tenant_id=self.tenant_id(),
            object_id=str(item["id"]).lower(),
            name=str(item.get("displayName") or upn or item["id"]),
            mail=str(mail) if isinstance(mail, str) and mail else None,
            kind=kind,
            state=_state(item.get("accountEnabled")),
            checked_at=self._stamp(),
        )

    def search(self, text: str, *, limit: int = 10, guests: bool = False) -> list[PersonRef]:
        """Up to `limit` (10 at most) people whose name words or mail start with `text`."""

        query = clean_search_text(text)
        term = graph_term(query)
        limit = max(1, min(int(limit), MAX_RESULTS))
        params = {
            "$search": f'"displayName:{term}" OR "mail:{term}"',
            "$select": SEARCH_SELECT,
            "$top": str(MAX_RESULTS),
            "$count": "true",
        }
        with self._span("search", "/users") as span:
            status, data = self._call(
                "/users", params, span, headers={"ConsistencyLevel": "eventual"}
            )
            fallback = status == 400 and _error_code(data) == "Request_UnsupportedQuery"
            span["awa.people.fallback"] = fallback
            if fallback:
                # A tenant without advanced queries (Azure AD B2C) still has startswith on names.
                literal = " ".join(query.replace("&", " ").split()).replace("'", "''")
                params = {
                    "$filter": f"startswith(displayName,'{literal}')",
                    "$select": SEARCH_SELECT,
                    "$top": str(MAX_RESULTS),
                }
                status, data = self._call("/users", params, span)
            found = self._people(data.get("value") or [], guests)[:limit] if status == 200 else []
            span["awa.people.results"] = len(found)
            if status != 200:
                code = _error_code(data) or "error"
                raise PeopleError(f"Microsoft Graph refused the search (HTTP {status}, {code}).")
        logger.debug("people search: %d results, startswith fallback %s", len(found), fallback)
        return found

    def _people(self, items: Sequence[Any], guests: bool) -> list[PersonRef]:
        found: list[PersonRef] = []
        taken: set[str] = set()
        for item in items:
            if not (isinstance(item, Mapping) and item.get("id")):
                continue
            person = self._person(item, make_key(str(item.get("displayName") or "person"), taken))
            if person.kind == "guest" and not guests:
                continue
            taken.add(person.key)
            found.append(person)
        return found

    def lookup(self, reference: str) -> PersonRef:
        """The person with object id or user principal name `reference` (to pin by id).

        Raises:
            PeopleError: If Entra has no such person.
        """

        text = str(reference).strip()
        if not (is_guid(text) or ("@" in text and len(text) <= 320)) or any(
            c in text for c in "/?#\\'\""
        ):
            raise PeopleError(
                "Pin by an object id (a GUID) or a user principal name (name@company)."
            )
        params = {"$select": GET_SELECT}
        with self._span("get", "/users/{id}") as span:
            status, data = self._call(f"/users/{urllib.parse.quote(text, safe='@')}", params, span)
            if status == 404:
                raise PeopleError("Entra ID has no person with that id or user principal name.")
            if status != 200 or not data.get("id"):
                raise PeopleError(f"Microsoft Graph refused to read the person (HTTP {status}).")
        return self._person(data, make_key(str(data.get("displayName") or "person")))

    def get(self, ref: PersonRef) -> PersonRef:
        """A fresh copy of `ref` from Entra (cached 15 minutes); `state="deleted"` after a 404."""

        if ref.source != "entra" or not ref.object_id:
            raise PeopleError(f"{ref.name} is not a person from Entra ID.")
        if ref.tenant_id and ref.tenant_id.lower() != self.tenant_id():
            raise PeopleError(
                f"{ref.name} belongs to another tenant than this Entra connection; pin them again."
            )
        oid = ref.object_id.lower()
        cached = self._cache.get(oid)
        now = self._clock.monotonic()
        if cached is not None and now - cached[0] < CACHE_SECONDS:
            return cached[1].model_copy(update={"key": ref.key})
        params = {"$select": GET_SELECT}
        with self._span("get", "/users/{id}") as span:
            status, data = self._call(f"/users/{urllib.parse.quote(oid, safe='')}", params, span)
            if status == 404:
                fresh = ref.model_copy(update={"state": "deleted", "checked_at": self._stamp()})
            elif status == 200 and str(data.get("id") or "").lower() == oid:
                fresh = self._person(data, ref.key)
            else:
                raise PeopleError(f"Microsoft Graph refused to read {ref.name} (HTTP {status}).")
        self._cache[oid] = (now, fresh)
        return fresh

    def check(self, refs: Sequence[PersonRef]) -> list[PersonRef]:
        """`get` for each of `refs`, in order."""

        return [self.get(ref) for ref in refs]


def _query(params: Mapping[str, str]) -> str:
    return "&".join(
        f"{urllib.parse.quote(k, safe='$')}={urllib.parse.quote(v, safe='')}"
        for k, v in params.items()
    )


def _state(enabled: object) -> State:
    if enabled is None:  # not selected, or null without User.Read.All: the state is unknown
        return "unknown"
    return "disabled" if enabled is False else "active"


def _error_code(data: Mapping[str, Any]) -> str | None:
    error = data.get("error")
    return error.get("code") if isinstance(error, Mapping) else None

"""The generic connectors: any REST API, OData service or GraphQL endpoint, from configuration.

Variable names follow the instance name (`billing` reads `BILLING_URL`, `BILLING_TOKEN`).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, replace
from typing import Any, Literal, cast
from xml.etree import ElementTree

from awa._internal.upstream.tools import ToolException
from awa.connectors._engine import xml_to_data
from awa.connectors._spec import (
    ApiKeyAuth,
    Auth,
    BasicAuth,
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    Location,
    OAuth2ClientCredentials,
    OAuth2RefreshToken,
    Operation,
    Pagination,
    Param,
)
from awa.errors import ConfigurationError

GENERIC_OPTIONS = ("auth", "headers", "health")
"""Options every generic connector accepts besides the common ones."""


def env_prefix(instance: str) -> str:
    """The variable prefix of an instance: `billing` -> `BILLING`."""

    return re.sub(r"[^A-Z0-9]+", "_", instance.upper()).strip("_")


def default_auth(prefix: str) -> tuple[Auth, ...]:
    """A bearer token `<PREFIX>_TOKEN`, else basic `<PREFIX>_USERNAME`/`<PREFIX>_PASSWORD`."""

    return (
        BearerAuth(EnvVar(f"{prefix}_TOKEN", "Access token or API key, sent as a bearer token.")),
        BasicAuth(
            EnvVar(f"{prefix}_USERNAME", "User name for basic authentication.", secret=False),
            EnvVar(f"{prefix}_PASSWORD", "Password for basic authentication."),
        ),
    )


_AUTH_KEYS: dict[str, set[str]] = {
    "none": set(),
    "bearer": {"token_env"},
    "api_key": {"token_env", "header", "prefix", "query"},
    "basic": {"username_env", "password_env", "username_format"},
    "oauth2_client_credentials": {"token_url", "client_id_env", "client_secret_env", "scope"},
    "oauth2_refresh_token": {
        "token_url",
        "client_id_env",
        "client_secret_env",
        "refresh_token_env",
    },
}


def auth_from_config(value: Any, prefix: str) -> tuple[Auth, ...]:
    """Authentication from a connector's `auth` option.

    `value` is a type name (`bearer`, `basic`, `api_key`, `oauth2_client_credentials`,
    `oauth2_refresh_token`, `none`), a mapping with `type` and the variable names (`token_env`,
    ...; unnamed ones default to `<PREFIX>_TOKEN`, ...), or a list of those (tried in order).

    Raises:
        ConfigurationError: If a type or key is unknown or a token URL is missing.
    """

    if value is None:
        return default_auth(prefix)
    entries = value if isinstance(value, list | tuple) else [value]
    result: list[Auth] = []
    for entry in entries:
        options = {"type": entry} if isinstance(entry, str) else dict(entry)
        kind = str(options.pop("type", ""))
        if kind not in _AUTH_KEYS:
            raise ConfigurationError(
                f"unknown auth type {kind!r} (known: {', '.join(sorted(_AUTH_KEYS))})"
            )
        unknown = sorted(set(options) - _AUTH_KEYS[kind])
        if unknown:
            raise ConfigurationError(
                f"auth type {kind!r} has no option(s) {', '.join(unknown)} "
                f"(known: {', '.join(sorted(_AUTH_KEYS[kind])) or 'none'})"
            )
        auth = _auth(kind, options, prefix)
        if auth is not None:
            result.append(auth)
    return tuple(result)


def _auth(kind: str, options: Mapping[str, Any], prefix: str) -> Auth | None:
    def var(key: str, default: str, description: str, *, secret: bool = True) -> EnvVar:
        return EnvVar(str(options.get(key) or f"{prefix}_{default}"), description, secret=secret)

    if kind == "none":
        return None
    if kind == "bearer":
        return BearerAuth(var("token_env", "TOKEN", "Access token, sent as a bearer token."))
    if kind == "api_key":
        if options.get("header") and options.get("query"):
            raise ConfigurationError("auth type 'api_key' takes header or query, not both")
        return ApiKeyAuth(
            var("token_env", "API_KEY", "API key."),
            header=str(options.get("header") or "X-API-Key"),
            prefix=str(options.get("prefix") or ""),
            query=str(options["query"]) if options.get("query") else None,
        )
    if kind == "basic":
        return BasicAuth(
            var("username_env", "USERNAME", "User name.", secret=False),
            var("password_env", "PASSWORD", "Password."),
            username_format=str(options.get("username_format") or "{}"),
        )
    token_url = options.get("token_url")
    if not isinstance(token_url, str) or not token_url.startswith(("https://", "http://", "{")):
        raise ConfigurationError(f"auth type {kind!r} needs a token_url")
    client_id = var("client_id_env", "CLIENT_ID", "OAuth client id.", secret=False)
    client_secret = var("client_secret_env", "CLIENT_SECRET", "OAuth client secret.")
    if kind == "oauth2_refresh_token":
        return OAuth2RefreshToken(
            token_url,
            client_id,
            client_secret,
            var("refresh_token_env", "REFRESH_TOKEN", "OAuth refresh token."),
        )
    return OAuth2ClientCredentials(
        token_url, client_id, client_secret, scope=options.get("scope") or None
    )


_OPERATION_KEYS = {
    "name",
    "description",
    "method",
    "path",
    "params",
    "write",
    "query",
    "body",
    "body_format",
    "headers",
    "items",
    "pages",
    "result",
    "response",
    "default_limit",
    "max_limit",
}
_PARAM_KEYS = {f.name for f in fields(Param)} - {"convert"} | {"in"}
_PAGES_KEYS = {f.name for f in fields(Pagination)} | {"in"}


def _checked(data: Any, known: set[str], what: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ConfigurationError(f"{what} must be a mapping")
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigurationError(
            f"{what} has unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(known))})"
        )
    return dict(data)


def operation_from_config(data: Any) -> Operation:
    """An `Operation` from its description in a blueprint (`connectors.<name>.operations`).

    Keys follow `Operation`; a parameter's location is `in` (`path`, `query`,
    `body`, `header`); `write` defaults to true for any method but `GET`.

    Raises:
        ConfigurationError: If a key is unknown or a value is invalid.
    """

    options = _checked(data, _OPERATION_KEYS, "an operation")
    name = options.get("name")
    where = f"operation {name!r}"
    if not isinstance(name, str) or not options.get("description"):
        raise ConfigurationError("an operation needs a name and a description")
    method = str(options.get("method", "GET")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise ConfigurationError(f"{where}: unknown method {method!r}")
    params = []
    for entry in options.get("params") or ():
        param = _checked(entry, _PARAM_KEYS, f"a parameter of {where}")
        location = param.pop("in", None) or param.pop("location", None) or "query"
        if location not in ("path", "query", "body", "header"):
            raise ConfigurationError(f"{where}: unknown parameter location {location!r}")
        if "enum" in param:
            param["enum"] = tuple(str(value) for value in param["enum"])
        try:
            params.append(Param(location=cast(Location, location), **param))
        except TypeError as exc:
            raise ConfigurationError(f"{where}: {exc}") from None
    pages = None
    if options.get("pages") is not None:
        paging = _checked(options["pages"], _PAGES_KEYS, f"the pages of {where}")
        location = paging.pop("in", None) or paging.pop("location", None) or "query"
        if paging.get("kind") not in ("cursor", "next_url", "link_header", "offset"):
            raise ConfigurationError(
                f"{where}: pages.kind must be cursor, next_url, link_header or offset"
            )
        pages = Pagination(location=cast(Literal["query", "body"], location), **paging)
    placeholders = set(re.findall(r"\{(\w+)\}", str(options.get("path", ""))))
    missing = placeholders - {p.name for p in params if p.location == "path"}
    if missing:
        raise ConfigurationError(f"{where}: no path parameter for {', '.join(sorted(missing))}")
    options.update(
        method=method,
        params=tuple(params),
        pages=pages,
        write=bool(options.get("write", method != "GET")),
    )
    try:
        return Operation(**options)
    except TypeError as exc:
        raise ConfigurationError(f"{where}: {exc}") from None


def configure_generic(
    spec: ConnectorSpec,
    instance: str,
    config: Mapping[str, Any],
    *,
    operations: Sequence[Operation] | None = None,
) -> ConnectorSpec:
    """The spec of a generic instance: `<PREFIX>_URL`, auth and headers from `config`."""

    prefix = env_prefix(instance)
    base = EnvVar(
        f"{prefix}_URL",
        f"Base address of the {spec.title}.",
        secret=False,
        required=True,
        example="https://api.example.com",
    )
    headers = config.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise ConfigurationError("headers must map header names to values")
    auth = auth_from_config(config.get("auth"), prefix)
    return replace(
        spec,
        base_url=f"{{{base.name}}}",
        env=(base, *(v for v in spec.env if v.name != base.name)),
        auth=auth,
        auth_optional=config.get("auth") is None or not auth,
        headers={**spec.headers, **{str(k): str(v) for k, v in headers.items()}},
        operations=tuple(operations) if operations is not None else spec.operations,
        health=config.get("health") or spec.health,
        configure=None,
    )


def _configure_rest(spec: ConnectorSpec, instance: str, config: Mapping[str, Any]) -> ConnectorSpec:
    entries = config.get("operations")
    if entries is None:
        return configure_generic(spec, instance, config)
    if not isinstance(entries, list | tuple) or not entries:
        raise ConfigurationError("operations must be a non-empty list")
    return configure_generic(
        spec, instance, config, operations=[operation_from_config(entry) for entry in entries]
    )


_FREE_PATH = Param(
    "path",
    "Path below the base URL, e.g. customers/42.",
    location="path",
    required=True,
    safe="/-._~",
    pattern=r"^[^?#\\]*$",
)
_FREE_QUERY = Param("query", "Query parameters.", type="object", key="")

REST = ConnectorSpec(
    name="rest",
    title="REST API",
    category="generic",
    summary=(
        "Any HTTP/JSON API: operations declared in the configuration, or generic get/send "
        "tools below the base URL."
    ),
    operations=(
        Operation(
            "get",
            "Read a resource below the base URL (GET).",
            path="/{path}",
            params=(_FREE_PATH, _FREE_QUERY),
            continuation=True,
        ),
        Operation(
            "send",
            "Create, change or delete a resource below the base URL.",
            method="POST",
            path="/{path}",
            params=(
                _FREE_PATH,
                Param(
                    "method",
                    "HTTP method.",
                    enum=("POST", "PUT", "PATCH", "DELETE"),
                    default="POST",
                ),
                _FREE_QUERY,
                Param("body", "JSON body.", type="object", location="body", key=""),
            ),
            method_param="method",
            write=True,
        ),
    ),
    options=(*GENERIC_OPTIONS, "operations"),
    configure=_configure_rest,
    setup=(
        "Set <NAME>_URL to the API's base address and the credentials of the auth option "
        "(default: <NAME>_TOKEN as a bearer token, or <NAME>_USERNAME and <NAME>_PASSWORD)."
    ),
)


_ENTITY_SET = Param(
    "entity_set",
    "Name of the entity set (from list_entity_sets).",
    location="path",
    required=True,
    pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
)
_KEY = Param(
    "key",
    "Key predicate as OData writes it: 42, 'ABC' or OrderID=1,Item=2.",
    location="path",
    required=True,
    safe="'=,:",
)


def _entity_sets(result: Any, _: Mapping[str, Any]) -> Any:
    if isinstance(result, dict) and isinstance(result.get("value"), list):
        return [item.get("name") for item in result["value"] if isinstance(item, dict)]
    sets = result.get("d", {}).get("EntitySets") if isinstance(result, dict) else None
    return sets if isinstance(sets, list) else result


def _unwrap(result: Any, _: Mapping[str, Any]) -> Any:
    if isinstance(result, dict) and set(result) == {"d"}:
        inner = result["d"]
        return inner.get("results", inner) if isinstance(inner, dict) else inner
    return result


def describe_odata(metadata: str, entity_set: str | None = None) -> Any:
    """Entity sets of an OData `$metadata` document with their keys and properties."""

    xml_to_data(metadata)  # refuses a document type declaration and malformed XML
    root = ElementTree.fromstring(metadata)

    def local(element: ElementTree.Element) -> str:
        return element.tag.rpartition("}")[2]

    types: dict[str, ElementTree.Element] = {}
    sets: dict[str, str] = {}
    for element in root.iter():
        if local(element) == "EntityType" and element.get("Name"):
            types[str(element.get("Name"))] = element
        elif local(element) == "EntitySet" and element.get("Name"):
            sets[str(element.get("Name"))] = str(element.get("EntityType", "")).rpartition(".")[2]
    wanted = [entity_set] if entity_set else sorted(sets)
    result = []
    for name in wanted:
        if name not in sets:
            known = ", ".join(sorted(sets)) or "none"
            raise ToolException(f"no entity set {name!r} (known: {known})")
        entity = types.get(sets[name])
        children = list(entity) if entity is not None else []
        descendants = list(entity.iter()) if entity is not None else []
        properties = [c for c in children if local(c) == "Property"]
        keys = [str(ref.get("Name")) for ref in descendants if local(ref) == "PropertyRef"]
        navigation = [str(c.get("Name")) for c in children if local(c) == "NavigationProperty"]
        entry: dict[str, Any] = {"entity_set": name, "entity_type": sets[name], "key": keys}
        if entity_set or len(wanted) <= 5:
            entry["properties"] = [
                {"name": p.get("Name"), "type": p.get("Type")} for p in properties
            ]
            entry["navigation"] = navigation
        else:
            entry["properties"] = [p.get("Name") for p in properties]
        result.append(entry)
    return result


def _describe(result: Any, args: Mapping[str, Any]) -> Any:
    return describe_odata(str(result), args.get("entity_set"))


ODATA = ConnectorSpec(
    name="odata",
    title="OData service",
    category="generic",
    summary="Query, read, create and update entities of any OData v2/v4 service (SAP, Dynamics).",
    operations=(
        Operation(
            "list_entity_sets",
            "List the entity sets (tables) the service offers.",
            path="/",
            transform=_entity_sets,
        ),
        Operation(
            "describe",
            "Describe entity sets: key, properties and navigation properties.",
            path="/$metadata",
            params=(Param("entity_set", "Only this entity set."),),
            headers={"Accept": "application/xml"},
            response="text",
            transform=_describe,
        ),
        Operation(
            "query",
            "Query an entity set with OData options ($filter, $select, $orderby, $expand).",
            path="/{entity_set}",
            params=(
                _ENTITY_SET,
                Param(
                    "filter", "OData filter, e.g. Country eq 'DE' and Amount gt 100.", key="$filter"
                ),
                Param("select", "Comma-separated properties.", key="$select"),
                Param("orderby", "Sort order, e.g. CreatedAt desc.", key="$orderby"),
                Param("expand", "Navigation properties to include.", key="$expand"),
            ),
            items="value|d.results",
            pages=Pagination(
                "next_url", next="@odata.nextLink|d.__next", size_param="$top", size_max=1000
            ),
        ),
        Operation(
            "get",
            "Read one entity by its key.",
            path="/{entity_set}({key})",
            params=(
                _ENTITY_SET,
                _KEY,
                Param("expand", "Navigation properties to include.", key="$expand"),
            ),
            transform=_unwrap,
        ),
        Operation(
            "create",
            "Create an entity.",
            method="POST",
            path="/{entity_set}",
            params=(
                _ENTITY_SET,
                Param(
                    "values",
                    "Property values.",
                    type="object",
                    location="body",
                    key="",
                    required=True,
                ),
            ),
            transform=_unwrap,
            write=True,
        ),
        Operation(
            "update",
            "Change properties of an entity.",
            method="PATCH",
            path="/{entity_set}({key})",
            params=(
                _ENTITY_SET,
                _KEY,
                Param(
                    "values",
                    "Property values to change.",
                    type="object",
                    location="body",
                    key="",
                    required=True,
                ),
            ),
            write=True,
        ),
    ),
    health="list_entity_sets",
    csrf_header="X-CSRF-Token",
    errors=("error.message.value", "error.message", "error.code", "message"),
    options=(*GENERIC_OPTIONS, "csrf"),
    configure=configure_generic,
    docs="https://www.odata.org/documentation/",
    setup=(
        "Set <NAME>_URL to the service root (for SAP: https://host/sap/opu/odata/sap/<SERVICE>) "
        "and the credentials; set csrf: true for SAP Gateway writes."
    ),
)


_BLOCK = re.compile(r"\{[^{}]*\}")
_ARGUMENTS = re.compile(r"\([^()]*\)")
_DEFINITION = re.compile(r"(?:\b(query|mutation|subscription|fragment)\b[^\x00]*?)?\x00")
_STRINGS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|#[^\n\r]*')


def _fields(depth: int) -> str:
    """Introspection of fields with type references `depth` wrappers deep."""

    ref = "kind name"
    for _ in range(depth):
        ref = f"kind name ofType {{ {ref} }}"
    return f"name description args {{ name defaultValue type {{ {ref} }} }} type {{ {ref} }}"


# Public servers limit query depth (often to 7 levels): the overview has room for type
# references three levels deep ("…" marks the rest, as in `[…!]!`), describe_type for four.
_INTROSPECTION = (
    f"query {{ __schema {{ queryType {{ name fields {{ {_fields(2)} }} }} "
    f"mutationType {{ name fields {{ {_fields(2)} }} }} types {{ name kind }} }} }}"
)
_TYPE_QUERY = (
    f"query($name: String!) {{ __type(name: $name) {{ name kind description "
    f"fields {{ {_fields(3)} }} inputFields {{ name description defaultValue type {{ "
    f"kind name ofType {{ kind name ofType {{ kind name ofType {{ kind name }} }} }} }} }} "
    f"enumValues {{ name description }} possibleTypes {{ name }} }} }}"
)


def _kinds(document: str) -> set[str]:
    """The operation types a GraphQL document defines (`query`, `mutation`, ...)."""

    text = _STRINGS.sub(" ", document)
    while True:
        reduced = _BLOCK.sub("\x00", _ARGUMENTS.sub("", text))
        if reduced == text:
            break
        text = reduced
    return {kind or "query" for kind in _DEFINITION.findall(text)} - {"fragment"}


def _only_queries(args: Mapping[str, Any]) -> None:
    kinds = _kinds(str(args.get("query", "")))
    if kinds - {"query"}:
        raise ToolException("this tool only runs queries; use mutate to change data")


def _only_mutations(args: Mapping[str, Any]) -> None:
    kinds = _kinds(str(args.get("query", "")))
    if kinds != {"mutation"}:
        raise ToolException("mutate runs exactly the mutations of the document; use query to read")


def _graphql_result(result: Any, _: Mapping[str, Any]) -> Any:
    if not isinstance(result, dict):
        return result
    errors = [e.get("message") if isinstance(e, dict) else e for e in result.get("errors") or ()]
    if errors and result.get("data") is None:
        raise ToolException(
            "the GraphQL server answered with errors: " + "; ".join(str(e) for e in errors[:5])
        )
    return result if errors else result.get("data")


def _text(value: Any, limit: int | None = None) -> str:
    """Server text on one line, without control characters (it goes into the model's context)."""

    kept = "".join(c if c.isprintable() else " " for c in str(value))
    return " ".join(kept.split())[:limit]


def _well_formed(
    function: Callable[[Any, Mapping[str, Any]], Any],
) -> Callable[[Any, Mapping[str, Any]], Any]:
    """Turn a malformed introspection answer into a plain error instead of a crash."""

    @functools.wraps(function)
    def guarded(result: Any, args: Mapping[str, Any]) -> Any:
        try:
            return function(result, args)
        except (KeyError, TypeError, AttributeError):
            raise ToolException(
                "the GraphQL server's schema answer has an unexpected shape"
            ) from None

    return guarded


def _type_name(ref: Any) -> str:
    """`[String!]!` from an introspection type reference."""

    if not isinstance(ref, dict):
        return "…"
    if ref.get("kind") == "NON_NULL":
        return _type_name(ref.get("ofType")) + "!"
    if ref.get("kind") == "LIST":
        return f"[{_type_name(ref.get('ofType'))}]"
    return _text(ref.get("name") or "…")


def _signature(field: dict[str, Any]) -> str:
    """`orders(first: Int = 10, status: [Status!]): OrderConnection!`"""

    args = [
        f"{_text(a['name'])}: {_type_name(a.get('type'))}"
        + (f" = {_text(a['defaultValue'])}" if a.get("defaultValue") is not None else "")
        for a in field.get("args") or ()
    ]
    shown = f"({', '.join(args)})" if args else ""
    return f"{_text(field['name'])}{shown}" + (
        f": {_type_name(field['type'])}" if "type" in field else ""
    )


def _described(field: dict[str, Any]) -> dict[str, str]:
    entry = {"field": _signature(field)}
    if field.get("description"):
        entry["description"] = _text(field["description"], 200)
    return entry


def _introspected(result: Any, key: str) -> Any:
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or key not in data:
        _graphql_result(result, {})
        raise ToolException(f"the GraphQL server gave no {key} (introspection is off?)")
    return data[key]


@_well_formed
def _schema(result: Any, _: Mapping[str, Any]) -> Any:
    schema = _introspected(result, "__schema") or {}

    def signatures(root: Any) -> list[str]:
        fields = root.get("fields") if isinstance(root, dict) else None
        return [_signature(f) for f in fields or () if isinstance(f, dict)]

    return {
        "queries": signatures(schema.get("queryType")),
        "mutations": signatures(schema.get("mutationType")),
        "types": sorted(
            _text(t["name"])
            for t in schema.get("types") or ()
            if isinstance(t, dict)
            and not str(t.get("name", "")).startswith("__")
            and t.get("kind") in ("OBJECT", "INPUT_OBJECT", "ENUM", "INTERFACE", "UNION")
        ),
        "hint": "describe_type shows the fields of a type with their argument and result types.",
    }


@_well_formed
def _type(result: Any, args: Mapping[str, Any]) -> Any:
    found = _introspected(result, "__type")
    if not isinstance(found, dict):
        raise ToolException(f"the GraphQL API has no type {args.get('name')!r}")
    described: dict[str, Any] = {"name": found.get("name"), "kind": found.get("kind")}
    if found.get("description"):
        described["description"] = _text(found["description"], 500)
    if found.get("fields"):
        described["fields"] = [_described(f) for f in found["fields"]]
    if found.get("inputFields"):
        described["input_fields"] = [_described(f) for f in found["inputFields"]]
    if found.get("enumValues"):
        described["values"] = [_text(v["name"]) for v in found["enumValues"]]
    if found.get("possibleTypes"):
        described["possible_types"] = [_text(t["name"]) for t in found["possibleTypes"]]
    return described


def _introspection_off(error: Exception) -> Any:
    """Introspection switched off or limited (as on many production servers): a plain answer."""

    text = str(error)
    if not re.search(r"introspection|__schema|__type|depth|complexity", text, re.I) or re.search(
        r"HTTP (401|403|5\d\d)", text
    ):
        raise error
    return {
        "introspection": "not allowed (or limited) by this server",
        "server_said": text[:300],
        "hint": "Write queries from the API's documentation (or its schema file); "
        "'{ __typename }' checks that the endpoint answers.",
    }


_DOCUMENT = Param("query", "The GraphQL document.", location="body", required=True)
_VARIABLES = Param("variables", "Variables of the document.", type="object", location="body")

GRAPHQL = ConnectorSpec(
    name="graphql",
    title="GraphQL API",
    category="generic",
    summary="Run queries (and approved mutations) against any GraphQL endpoint.",
    operations=(
        Operation(
            "describe_schema",
            "List the queries and mutations (with argument and result types) and the type "
            "names the API offers.",
            method="POST",
            body={"query": _INTROSPECTION},
            transform=_schema,
            fallback=_introspection_off,
        ),
        Operation(
            "describe_type",
            "Describe one type: its fields with argument and result types, input fields or "
            "enum values.",
            method="POST",
            body={"query": _TYPE_QUERY},
            params=(
                Param(
                    "name",
                    "Type name, e.g. Order or OrderFilter.",
                    location="body",
                    key="variables.name",
                    required=True,
                    pattern=r"^[_A-Za-z][_0-9A-Za-z]*$",
                ),
            ),
            transform=_type,
            fallback=_introspection_off,
        ),
        Operation(
            "query",
            "Run a GraphQL query (no mutations).",
            method="POST",
            params=(_DOCUMENT, _VARIABLES),
            check=_only_queries,
            transform=_graphql_result,
        ),
        Operation(
            "mutate",
            "Run a GraphQL mutation.",
            method="POST",
            params=(_DOCUMENT, _VARIABLES),
            check=_only_mutations,
            transform=_graphql_result,
            write=True,
        ),
    ),
    health="describe_schema",
    errors=("errors.0.message", "message", "error"),
    options=GENERIC_OPTIONS,
    configure=configure_generic,
    setup="Set <NAME>_URL to the GraphQL endpoint and the credentials of the auth option.",
)

"""The OpenAPI connector: any documented REST API becomes tools, from its description.

Operations that cannot become tools are listed in the spec's `notes` and a log warning.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel

from awa._internal.http import HttpPolicy, HttpRequest, fetch
from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool, ToolException
from awa._internal.util import preview
from awa.connectors._engine import ConnectorContext, http_tools
from awa.connectors._generic import GENERIC_OPTIONS, auth_from_config, default_auth, env_prefix
from awa.connectors._spec import (
    ApiKeyAuth,
    Auth,
    BasicAuth,
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    OAuth2ClientCredentials,
    Operation,
    Param,
    ParamType,
)
from awa.errors import ConfigurationError

MAX_OPERATIONS = 64
"""Most operations one document may turn into tools without `include` or `tags`."""

_MAX_DOCUMENT_BYTES = 10_000_000
_METHODS = ("get", "post", "put", "patch", "delete")
_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
_SKIPPED_HEADERS = {"authorization", "content-type", "accept", "cookie"}
_RESERVED = {"limit", "page_token"}
_TEXT_TYPE = re.compile(r"^(text/[\w.+-]+|application/([\w.-]+\+)?xml)$")
_READABLE = re.compile(r"json|xml|^text/|^\*/\*$|^application/(x-www-form-urlencoded|javascript)")

_logger = get_logger("connectors")


def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text:
        return "value"
    return f"p_{text}" if text[0].isdigit() else text


class _Resolver:
    def __init__(self, document: Mapping[str, Any]) -> None:
        self.document = document

    def __call__(self, node: Any) -> Any:
        seen: set[str] = set()
        while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
            reference = node["$ref"]
            if reference in seen or not reference.startswith("#/"):
                return {}
            seen.add(reference)
            target: Any = self.document
            for part in reference[2:].split("/"):
                part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, Mapping) else None
            node = target if target is not None else {}
        return node


def _schema_type(schema: Mapping[str, Any]) -> ParamType:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind in _TYPES:
        return cast(ParamType, kind)
    if "properties" in schema:
        return "object"
    return "string"


def _describe(*texts: Any) -> str:
    text = next((str(t).strip() for t in texts if t and str(t).strip()), "")
    return preview(text, 300, flatten=True)


def _param(
    resolve: _Resolver,
    name: str,
    schema: Mapping[str, Any],
    *,
    location: str,
    required: bool,
    description: str = "",
    explode: bool | None = None,
    taken: set[str],
) -> Param:
    schema = resolve(schema) or {}
    kind = _schema_type(schema)
    items_schema = resolve(schema.get("items")) if kind == "array" else None
    items = _schema_type(items_schema) if isinstance(items_schema, Mapping) else None
    enum = schema.get("enum")
    argument = _snake(name)
    while argument in taken or argument in _RESERVED or hasattr(BaseModel, argument):
        argument += "_" + location
    taken.add(argument)
    return Param(
        argument,
        _describe(description, schema.get("description"), name) or name,
        type=kind,
        location=location,  # type: ignore[arg-type]  # callers pass a location name
        required=required,
        default=schema.get("default") if not required else None,
        key=name,
        enum=tuple(str(v) for v in enum) if isinstance(enum, list) and kind == "string" else (),
        items=items,
        explode=kind == "array" and location == "query" and explode is not False,
    )


def _body_params(
    resolve: _Resolver, schema: Any, required_body: bool, taken: set[str]
) -> list[Param]:
    schema = resolve(schema) or {}
    properties = schema.get("properties")
    if isinstance(properties, Mapping) and properties:
        required = set(schema.get("required") or ())
        return [
            _param(
                resolve,
                str(name),
                prop if isinstance(prop, Mapping) else {},
                location="body",
                required=name in required,
                taken=taken,
            )
            for name, prop in properties.items()
            if not (isinstance(prop, Mapping) and resolve(prop).get("readOnly"))
        ]
    body = _param(resolve, "body", schema, location="body", required=required_body, taken=taken)
    return [replace(body, key="", description=_describe(schema.get("description"), "Body."))]


def openapi_operations(
    document: Mapping[str, Any],
    *,
    include: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    max_operations: int = MAX_OPERATIONS,
) -> list[Operation]:
    """The operations of an OpenAPI 3 or Swagger 2 document.

    `include` keeps operations by snake_case name or operationId, `tags` by tag; `max_operations`
    applies after filtering. Operations that cannot become tools are left out with a log warning.

    Raises:
        ConfigurationError: If the document has no paths, `include` names an unknown or unusable
            operation, or there are too many operations.
    """

    operations, skipped = _operations(document, include, tags, max_operations)
    for note in skipped:
        _logger.warning("OpenAPI operation left out: %s", note)
    return operations


def _operations(
    document: Mapping[str, Any],
    include: Sequence[str] | None,
    tags: Sequence[str] | None,
    max_operations: int,
) -> tuple[list[Operation], list[str]]:
    """The usable operations, and a note per operation left out (`name (METHOD /path): why`)."""

    resolve = _Resolver(document)
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise ConfigurationError("the OpenAPI document has no paths")
    swagger = str(document.get("swagger", "")).startswith("2")
    operations: list[Operation] = []
    skipped: list[str] = []
    unusable: list[str] = []
    names: set[str] = set()
    wanted = set(include or ())
    matched: set[str] = set()
    for path, item in paths.items():
        item = resolve(item)
        if not isinstance(item, Mapping):
            continue
        shared = list(item.get("parameters") or ())
        for method in _METHODS:
            spec = item.get(method)
            if not isinstance(spec, Mapping):
                continue
            operation_id = str(spec.get("operationId") or "")
            name = _snake(operation_id) if operation_id else _snake(f"{method} {path}")
            if include is not None:
                hit = {name, operation_id} & wanted
                if not hit:
                    continue
                matched |= hit
            if tags is not None and not set(spec.get("tags") or ()) & set(tags):
                continue
            built = (
                "deprecated"
                if spec.get("deprecated")
                else _operation(resolve, name, method, str(path), spec, shared, swagger)
            )
            if isinstance(built, str):
                skipped.append(f"{name} ({method.upper()} {path}): {built}")
                if include is not None:
                    unusable.append(skipped[-1])
                continue
            while built.name in names:
                built = replace(built, name=built.name + "_2")
            names.add(built.name)
            operations.append(built)
    unknown = wanted - matched
    if unknown:
        raise ConfigurationError(
            f"the OpenAPI document has no operation(s) {', '.join(sorted(unknown))}"
        )
    if unusable:
        raise ConfigurationError("cannot use " + "; ".join(unusable))
    if len(operations) > max_operations:
        raise ConfigurationError(
            f"the OpenAPI document has {len(operations)} operations (more than "
            f"{max_operations}); choose some with include or tags"
        )
    if not operations:
        reasons = f" ({'; '.join(skipped[:5])})" if skipped else ""
        raise ConfigurationError(f"no usable operations in the OpenAPI document{reasons}")
    return operations, skipped


def _operation(
    resolve: _Resolver,
    name: str,
    method: str,
    path: str,
    spec: Mapping[str, Any],
    shared: list[Any],
    swagger: bool,
) -> Operation | str:
    """The operation, or why it cannot become a tool."""

    merged: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in [*shared, *(spec.get("parameters") or ())]:
        parameter = resolve(raw)
        if isinstance(parameter, Mapping) and parameter.get("name"):
            merged[(str(parameter["name"]), str(parameter.get("in")))] = parameter
    taken: set[str] = set()
    params: list[Param] = []
    body_format = "json"
    renamed: dict[str, str] = {}
    form_fields: list[Mapping[str, Any]] = []
    for (wire, location), parameter in merged.items():
        if location == "body" and swagger:
            params.extend(
                _body_params(
                    resolve, parameter.get("schema"), bool(parameter.get("required")), taken
                )
            )
            continue
        if location == "formData":
            form_fields.append(parameter)
            continue
        if location not in ("path", "query", "header") or (
            location == "header" and wire.lower() in _SKIPPED_HEADERS
        ):
            continue
        schema = parameter if swagger else parameter.get("schema") or {}
        param = _param(
            resolve,
            wire,
            schema,
            location=location,
            required=bool(parameter.get("required")),
            description=str(parameter.get("description") or ""),
            explode=parameter.get("explode"),
            taken=taken,
        )
        if location == "path":
            renamed[wire] = param.name
        params.append(param)
    headers: dict[str, str] = {}
    if form_fields:
        if any(field.get("type") == "file" for field in form_fields):
            return "file uploads (multipart/form-data) are not supported"
        body_format = "form"
        for field in form_fields:
            params.append(
                _param(
                    resolve,
                    str(field["name"]),
                    field,
                    location="body",
                    required=bool(field.get("required")),
                    description=str(field.get("description") or ""),
                    taken=taken,
                )
            )
    body = resolve(spec.get("requestBody"))
    if isinstance(body, Mapping) and body.get("content"):
        request = _request_body(resolve, body, taken)
        if isinstance(request, str):
            return request
        body_params, kind, content_type = request
        params.extend(body_params)
        body_format = body_format if kind == "json" else kind
        headers.update(content_type)
    binary = _binary_answer(resolve, spec)
    if binary is not None:
        return f"binary answers ({binary}) are not supported"
    template = re.sub(
        r"\{([^}]+)\}", lambda m: "{" + renamed.get(m.group(1), _snake(m.group(1))) + "}", path
    )
    upper = method.upper()
    description = _describe(spec.get("summary"), spec.get("description"), f"{upper} {path}")
    return Operation(
        name,
        f"{description} ({upper} {path})",
        method=upper,  # type: ignore[arg-type]  # an upper-case HTTP method name
        path=template,
        params=tuple(params),
        body_format=body_format,  # type: ignore[arg-type]  # one of the formats chosen above
        headers=headers,
        write=upper != "GET",
    )


def _request_body(
    resolve: _Resolver, body: Mapping[str, Any], taken: set[str]
) -> tuple[list[Param], str, dict[str, str]] | str:
    """The parameters, body format and headers of an OpenAPI 3 request body, or why it is unusable."""

    content = body["content"]
    required = bool(body.get("required"))
    json_type = next((t for t in content if "json" in t), None)
    if json_type is not None:
        return _body_params(resolve, content[json_type].get("schema"), required, taken), "json", {}
    form = content.get("application/x-www-form-urlencoded")
    if form is not None:
        return _body_params(resolve, form.get("schema"), required, taken), "form", {}
    text_type = next((t for t in content if _TEXT_TYPE.match(t)), None)
    if text_type is None:
        return f"{', '.join(content)} request bodies are not supported"
    description = _describe(body.get("description"), f"The {text_type} body.")
    param = Param("body", description, location="body", required=required)
    return [param], "raw", {"Content-Type": text_type}


def _binary_answer(resolve: _Resolver, spec: Mapping[str, Any]) -> str | None:
    """The content type when every successful answer is binary (a PDF or image download)."""

    types: list[str] = []
    for status, answer in (spec.get("responses") or {}).items():
        answer = resolve(answer)
        if str(status).startswith("2") and isinstance(answer, Mapping):
            types.extend(str(t) for t in answer.get("content") or ())
    if types and not any(_READABLE.search(t) for t in types):
        return ", ".join(dict.fromkeys(types))
    return None


def _web_address(source: str | None) -> str | None:
    return source if source and source.startswith(("https://", "http://")) else None


def openapi_base_url(document: Mapping[str, Any], source: str | None = None) -> str | None:
    """The API's base URL from `servers` (OpenAPI 3) or `host`/`basePath` (Swagger 2)."""

    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
        url = str(servers[0].get("url") or "")
        for name, variable in (servers[0].get("variables") or {}).items():
            url = url.replace(f"{{{name}}}", str((variable or {}).get("default", "")))
    elif document.get("host"):
        schemes = document.get("schemes") or ["https"]
        scheme = "https" if "https" in schemes else str(schemes[0])
        url = f"{scheme}://{document['host']}{document.get('basePath', '')}"
    else:
        return None
    if source and source.startswith(("https://", "http://")):
        url = urllib.parse.urljoin(source, url)
    return url.rstrip("/") if url.startswith(("https://", "http://")) else None


def openapi_auth(
    document: Mapping[str, Any], prefix: str, *, base: str | None = None
) -> tuple[Auth, ...]:
    """Authentication from the document's security schemes, variables named `<PREFIX>_...`.

    A relative OAuth `tokenUrl` resolves against `base` (where the document was read).

    Raises:
        ConfigurationError: If a `tokenUrl` is relative and there is no `base`.
    """

    schemes = (
        (document.get("components") or {}).get("securitySchemes")
        or document.get("securityDefinitions")
        or {}
    )
    result: list[Auth] = []
    for scheme in schemes.values():
        if not isinstance(scheme, Mapping):
            continue
        kind, variant = scheme.get("type"), str(scheme.get("scheme", "")).lower()
        if (kind == "http" and variant == "bearer") or kind in ("oauth2", "openIdConnect"):
            flow = (scheme.get("flows") or {}).get("clientCredentials") or (
                scheme if scheme.get("flow") == "application" else None
            )
            if isinstance(flow, Mapping) and flow.get("tokenUrl"):
                token_url = str(flow["tokenUrl"])
                if not token_url.startswith(("https://", "http://")):
                    if base is None:
                        raise ConfigurationError(
                            f"The OAuth tokenUrl {token_url!r} of the document is relative; "
                            "read the document from its https:// address, or set auth: in the "
                            "connector options"
                        )
                    token_url = urllib.parse.urljoin(base, token_url)
                result.append(
                    OAuth2ClientCredentials(
                        token_url,
                        EnvVar(f"{prefix}_CLIENT_ID", "OAuth client id.", secret=False),
                        EnvVar(f"{prefix}_CLIENT_SECRET", "OAuth client secret."),
                    )
                )
            result.append(BearerAuth(EnvVar(f"{prefix}_TOKEN", "Access token.")))
        elif (kind == "http" and variant == "basic") or kind == "basic":
            result.append(
                BasicAuth(
                    EnvVar(f"{prefix}_USERNAME", "User name.", secret=False),
                    EnvVar(f"{prefix}_PASSWORD", "Password."),
                )
            )
        elif kind == "apiKey" and scheme.get("in") in ("header", "query") and scheme.get("name"):
            key = EnvVar(f"{prefix}_API_KEY", "API key.")
            name = str(scheme["name"])
            result.append(
                ApiKeyAuth(key, header=name)
                if scheme.get("in") == "header"
                else ApiKeyAuth(key, query=name)
            )
    unique: dict[tuple[str, ...], Auth] = {}
    for auth in result:
        unique.setdefault(tuple(v.name for v in auth.variables()), auth)
    return tuple(unique.values())


def _parse(text: str, where: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{where} is not JSON or YAML: {exc}") from None
    if not isinstance(document, dict) or not ("openapi" in document or "swagger" in document):
        raise ConfigurationError(f"{where} is not an OpenAPI or Swagger document")
    return document


def load_document(source: str | Path, ctx: ConnectorContext | None = None) -> dict[str, Any]:
    """Read an OpenAPI document from a file or an http(s) URL (JSON or YAML).

    A URL is fetched under the instance's network options; the document itself
    needs no credentials.

    Raises:
        ConfigurationError: If it cannot be read or is not an OpenAPI document.
    """

    text = str(source)
    if not text.startswith(("https://", "http://")):
        path = Path(text)
        if not path.is_file():
            raise ConfigurationError(f"OpenAPI document {text!r} does not exist")
        if path.stat().st_size > _MAX_DOCUMENT_BYTES:
            raise ConfigurationError(f"OpenAPI document {text!r} is too large")
        return _parse(path.read_text(encoding="utf-8"), text)
    headers = {"Accept": "application/json, application/yaml;q=0.9, */*;q=0.5"}
    try:
        if ctx is not None and ctx.transport is not None:
            response = ctx.transport(HttpRequest("GET", text, headers))
        else:
            options = ctx.config if ctx is not None else {}
            policy = HttpPolicy(
                allowed_hosts=((urllib.parse.urlsplit(text).hostname or "").lower(),),
                allow_private_networks=bool(options.get("allow_private_networks", False)),
                allow_http=bool(options.get("allow_http", False)),
                timeout=float(options.get("timeout", 30.0)),
                max_response_bytes=_MAX_DOCUMENT_BYTES,
            )
            response = fetch(policy, text, headers=headers)
    except ToolException as exc:
        raise ConfigurationError(f"cannot read the OpenAPI document: {exc}") from None
    if response.status >= 400:
        raise ConfigurationError(f"the OpenAPI document answered HTTP {response.status}")
    return _parse(response.text(), text)


def openapi_connector(
    source: str | Path | Mapping[str, Any],
    *,
    name: str,
    title: str | None = None,
    base_url: str | None = None,
    auth: Any = None,
    include: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    max_operations: int = MAX_OPERATIONS,
) -> ConnectorSpec:
    """A `ConnectorSpec` for the API an OpenAPI document describes.

    Args:
        source: The document: a file path, an http(s) URL or the parsed mapping.
        name: Connector name (snake_case); variables are named `<NAME>_...`.
        title: Display name; defaults to the document's `info.title`.
        base_url: Base URL; defaults to `<NAME>_URL` if set, else the document's server.
        auth: Authentication as in a connector's `auth` option; defaults to the document's
            security schemes.
        include: Only these operations (snake_case names or operationIds).

    Raises:
        ConfigurationError: If the document cannot be read or converted.

    Example:
        ```python
        billing = openapi_connector("billing.yaml", name="billing", tags=["invoices"])
        tools = connector_tools(billing)
        ```
    """

    document = dict(source) if isinstance(source, Mapping) else load_document(source)
    location = None if isinstance(source, Mapping) else str(source)
    spec = convert(
        replace(OPENAPI, name=name, options=GENERIC_OPTIONS),
        name,
        document,
        location,
        auth=auth,
        include=include,
        tags=tags,
        max_operations=max_operations,
    )
    if base_url:  # the address is given: the URL variable is no longer needed
        spec = replace(spec, env=tuple(replace(v, required=False) for v in spec.env))
    return replace(spec, title=title or spec.title, base_url=base_url or spec.base_url)


def convert(
    spec: ConnectorSpec,
    instance: str,
    document: Mapping[str, Any],
    location: str | None,
    *,
    auth: Any = None,
    include: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    max_operations: int = MAX_OPERATIONS,
    headers: Mapping[str, Any] | None = None,
    health: str | None = None,
) -> ConnectorSpec:
    """`spec` with the document's operations, base URL and authentication."""

    prefix = env_prefix(instance)
    server = openapi_base_url(document, location)
    url = EnvVar(
        f"{prefix}_URL",
        "Base address of the API (defaults to the document's server).",
        secret=False,
        required=server is None,
        default=server,
    )
    schemes = (
        auth_from_config(auth, prefix)
        if auth is not None
        else openapi_auth(document, prefix, base=_web_address(location))
    )
    info = document.get("info") or {}
    operations, skipped = _operations(document, include, tags, max_operations)
    for note in skipped:
        _logger.warning("OpenAPI operation left out: %s", note)
    return replace(
        spec,
        title=str(info.get("title") or spec.title),
        base_url=f"{{{url.name}}}",
        env=(url,),
        auth=schemes or default_auth(prefix),
        auth_optional=auth is None,
        headers={**spec.headers, **{str(k): str(v) for k, v in (headers or {}).items()}},
        operations=tuple(operations),
        notes=tuple(f"left out {note}" for note in skipped),
        health=health,
        dynamic=False,
        factory=None,
        configure=None,
    )


def _source(config: Mapping[str, Any]) -> str | None:
    value = config.get("spec")
    return str(value) if value else None


def _converted(
    spec: ConnectorSpec,
    instance: str,
    document: Mapping[str, Any],
    source: str,
    config: Mapping[str, Any],
) -> ConnectorSpec:
    """`convert` with the options of a configured instance."""

    tags = config.get("tags")
    return convert(
        spec,
        instance,
        document,
        source,
        auth=config.get("auth"),
        tags=[tags] if isinstance(tags, str) else tags,
        max_operations=int(config.get("max_operations", MAX_OPERATIONS)),
        headers=config.get("headers"),
        health=config.get("health"),
    )


def instance_spec(ctx: ConnectorContext) -> ConnectorSpec:
    """The spec of a configured OpenAPI instance, with its document read now.

    The document comes from the `spec` option or `<NAME>_SPEC`, a URL read under the instance's
    network options.
    """

    source = _source(ctx.config) or ctx.require(f"{env_prefix(ctx.name)}_SPEC")
    return _converted(ctx.spec, ctx.name, load_document(source, ctx), source, ctx.config)


def _configure(spec: ConnectorSpec, instance: str, config: Mapping[str, Any]) -> ConnectorSpec:
    prefix = env_prefix(instance)
    source = _source(config)
    if source is not None and not source.startswith(("https://", "http://")):
        return _converted(spec, instance, load_document(source), source, config)
    variable = EnvVar(
        f"{prefix}_SPEC",
        "Location of the OpenAPI document: a URL or a file path.",
        secret=False,
        required=source is None,
        example="https://api.example.com/openapi.json",
    )
    return replace(spec, env=(variable,), auth=(), configure=None)


def _factory(ctx: ConnectorContext) -> list[BaseTool]:
    return http_tools(ctx.derive(instance_spec(ctx)))


OPENAPI = ConnectorSpec(
    name="openapi",
    title="OpenAPI (any documented REST API)",
    category="generic",
    summary="Turns the operations an OpenAPI or Swagger document describes into tools.",
    dynamic=True,
    factory=_factory,
    configure=_configure,
    options=(*GENERIC_OPTIONS, "spec", "tags", "max_operations"),
    docs="https://spec.openapis.org/oas/latest.html",
    setup=(
        "Point the spec option (or <NAME>_SPEC) at the API's OpenAPI document and set the "
        "credentials its security scheme names (<NAME>_TOKEN, <NAME>_API_KEY, ...)."
    ),
)

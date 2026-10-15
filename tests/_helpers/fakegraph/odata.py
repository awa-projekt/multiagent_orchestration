"""Paging, `$select`, `$top` and delta rounds for `FakeGraph` plug-ins.

Collections answer like Graph: `value`, and an absolute `@odata.nextLink` that carries the
original query plus an opaque, signed continuation token (`$skiptoken`). Delta rounds page the
same way and end with an absolute `@odata.deltaLink`. Filter and `$orderby` grammar belongs to the
service plug-in that needs it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from _helpers.fakegraph.core import Ctx, GraphError, b64url, unb64url


def clean(item: Mapping[str, Any]) -> dict[str, Any]:
    """`item` without its private (`_`-prefixed) fields."""

    return {key: value for key, value in item.items() if not key.startswith("_")}


def split_list(text: str | None) -> list[str]:
    """`"a, b"` as `["a", "b"]`."""

    return [part.strip() for part in (text or "").split(",") if part.strip()]


def check_properties(names: Iterable[str], known: Iterable[str] | None, type_name: str) -> None:
    """Raise 400 for a property the type does not have (first path segment, any case)."""

    if known is None:
        return
    lowered = {name.lower() for name in known}
    for name in names:
        if name.split("/")[0].lower() not in lowered:
            raise GraphError(
                400,
                "BadRequest",
                f"Could not find a property named '{name}' on type '{type_name}'.",
            )


def apply_select(
    item: Mapping[str, Any], select: list[str], *, always: tuple[str, ...] = ("id", "@odata.etag")
) -> dict[str, Any]:
    """The public fields of `item` named in `select` (all of them if `select` is empty)."""

    visible = clean(item)
    if not select:
        return visible
    wanted = {name.lower() for name in (*select, *always)}
    return {key: value for key, value in visible.items() if key.lower() in wanted}


def top_value(ctx: Ctx, *, default: int, maximum: int, reject: bool = False) -> int:
    """`$top` of the request: capped at `maximum`, or rejected when `reject` (as Teams does)."""

    raw = ctx.request.q("$top")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise GraphError(400, "BadRequest", f"Invalid value '{raw}' for $top.")
    if value > maximum:
        if reject:
            raise GraphError(
                400,
                "BadRequest",
                f"Invalid page size specified: {value}. Must be between 1 and {maximum} inclusive.",
            )
        return maximum
    return value


# --------------------------------------------------------------------------- signed tokens


def _sign(ctx: Ctx, payload: bytes) -> str:
    return hmac.new(ctx.graph.page_key, payload, hashlib.sha256).hexdigest()[:12]


def encode_token(ctx: Ctx, data: Mapping[str, Any]) -> str:
    """An opaque continuation token; only this server can make or read one."""

    payload = json.dumps(dict(data), separators=(",", ":"))
    return b64url(f"{payload}|{_sign(ctx, payload.encode())}".encode())


def decode_token(ctx: Ctx, token: str) -> dict[str, Any]:
    """The data of `encode_token`.

    Raises:
        GraphError: 400 `BadRequest` if `token` is not one of ours.
    """

    try:
        payload, _, signature = unb64url(token).decode().rpartition("|")
        if not hmac.compare_digest(signature, _sign(ctx, payload.encode())):
            raise ValueError("signature")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("shape")
        return data
    except (ValueError, UnicodeDecodeError):
        raise GraphError(400, "BadRequest", "The continuation token is not valid.") from None


def link_with(ctx: Ctx, name: str, token: str, *, drop: tuple[str, ...] = ()) -> str:
    """The request's absolute URL with `name=token` (and the original query otherwise kept)."""

    request = ctx.request
    skipped = {"$skiptoken", "$skip", name, *drop}
    pairs = [
        (key, value)
        for key, values in request.query.items()
        if key not in skipped
        for value in values
    ]
    pairs.append((name, token))
    path = request.target.split("?", 1)[0]
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~:/,')}" for k, v in pairs)
    return f"{ctx.base}{path}?{query}"


# --------------------------------------------------------------------------- collections


def collection(
    ctx: Ctx,
    items: list[dict[str, Any]],
    *,
    default_top: int = 10,
    max_top: int = 1000,
    reject_top: bool = False,
    known: Iterable[str] | None = None,
    type_name: str = "entity",
    context: str = "collection",
    shape: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    always: tuple[str, ...] = ("id", "@odata.etag"),
) -> dict[str, Any]:
    """One page of `items` as a Graph collection, with `@odata.nextLink` if more remain.

    `$select` is applied (an unknown property is a 400 when `known` is given), `$top` is capped
    at `max_top` or, with `reject_top`, rejected above it.
    """

    select = split_list(ctx.request.q("$select"))
    check_properties(select, known, type_name)
    size = top_value(ctx, default=default_top, maximum=max_top, reject=reject_top)
    token = ctx.request.q("$skiptoken")
    start = int(decode_token(ctx, token).get("o", 0)) if token else 0
    body: dict[str, Any] = {
        "@odata.context": f"{ctx.base}/v1.0/$metadata#{context}",
        "value": [
            apply_select(shape(item) if shape else item, select, always=always)
            for item in items[start : start + size]
        ],
    }
    if start + size < len(items):
        body["@odata.nextLink"] = link_with(
            ctx, "$skiptoken", encode_token(ctx, {"o": start + size})
        )
    return body


# --------------------------------------------------------------------------- delta


class DeltaFeed:
    """The change log of a collection (a drive, a mail folder) behind its delta rounds."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self.log: list[tuple[int, str]] = []
        self.seq = 0

    def upsert(self, key: str, item: dict[str, Any]) -> None:
        """Record that `key` now looks like `item`."""

        self.seq += 1
        self.state[key] = item
        self.log.append((self.seq, key))

    def delete(self, key: str) -> None:
        """Record that `key` is gone."""

        self.seq += 1
        self.state.pop(key, None)
        self.log.append((self.seq, key))

    def since(self, seq: int) -> list[dict[str, Any]]:
        """The changed items after `seq`, once each, deleted ones as `{"id", "deleted"}`."""

        keys = list(dict.fromkeys(key for number, key in self.log if number > seq))
        return [self.state.get(key, {"id": key, "deleted": {"state": "deleted"}}) for key in keys]

    def current(self) -> list[dict[str, Any]]:
        return list(self.state.values())


def delta_round(
    ctx: Ctx,
    feed: DeltaFeed,
    *,
    default_top: int = 200,
    shape: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One response of a delta query, in Graph's protocol.

    No token: everything, in pages (`@odata.nextLink` with `$skiptoken`), then the last page has
    `@odata.deltaLink` (`token=<opaque>`). `token=latest`: no items and a delta
    link for "from now on". A delta link's token: only what changed since, in the same way.
    """

    request = ctx.request
    size = top_value(ctx, default=default_top, maximum=default_top)
    skip = request.q("$skiptoken")
    given = request.q("token")
    if skip:
        position = decode_token(ctx, skip)
    elif given == "latest":
        position = {"since": feed.seq, "end": feed.seq, "o": 0, "latest": True}
    elif given:
        since = int(decode_token(ctx, given).get("s", 0))
        position = {"since": since, "end": feed.seq, "o": 0}
    else:
        position = {"since": None, "end": feed.seq, "o": 0}
    if position.get("latest"):
        items: list[dict[str, Any]] = []
    elif position["since"] is None:
        items = feed.current()
    else:
        items = feed.since(int(position["since"]))
    start = int(position["o"])
    page = items[start : start + size]
    body: dict[str, Any] = {
        "@odata.context": f"{ctx.base}/v1.0/$metadata#Collection",
        "value": [clean(shape(item) if shape else item) for item in page],
    }
    if start + size < len(items):
        body["@odata.nextLink"] = link_with(
            ctx, "$skiptoken", encode_token(ctx, {**position, "o": start + size}), drop=("token",)
        )
    else:
        body["@odata.deltaLink"] = link_with(
            ctx, "token", encode_token(ctx, {"s": position["end"]}), drop=("$skiptoken",)
        )
    return body

"""File transfers of connector operations: downloads to and uploads from the workspace folder.

Downloads send credentials on the first hop only; pre-authenticated URLs are redacted like them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import secrets
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awa._internal.sandbox import Sandbox
from awa._internal.upstream.tools import ToolException
from awa.connectors._engine import (
    ConnectorContext,
    _Call,
    _parse,
    _query_string,
    _raise_for,
    _with_query,
    fill_path,
    pick,
    send,
)
from awa.connectors._spec import Operation
from awa.runtime.case import active_case_dir

FILE_OPTIONS = ("root", "max_file_bytes")
"""The options of a connector with file operations; list them in `ConnectorSpec.options`."""

DEFAULT_MAX_FILE_BYTES = 50_000_000
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 5
_MAX_RESYNCS = 5


def workspace_root(ctx: ConnectorContext) -> Path:
    """The workspace folder: the `root` option, else `AWA_WORKSPACE_DIR`, else `data`.

    Inside an eval case a relative folder resolves against the case's folder (see
    `awa.runtime.case_dir`), so every case works on its own copy of the files."""

    root = Path(str(ctx.option("root") or ctx.value("AWA_WORKSPACE_DIR") or "data"))
    active = active_case_dir()
    return active / root if active is not None and not root.is_absolute() else root


def sandbox(ctx: ConnectorContext) -> Sandbox:
    """The workspace folder transfers read from and write to; it must exist."""

    root = workspace_root(ctx)
    if not root.is_dir():
        raise ToolException(
            f"the workspace folder {str(root)!r} does not exist; create it or set the root option"
        )
    return Sandbox(root)


def max_file_bytes(ctx: ConnectorContext) -> int:
    """The largest file a transfer moves (the `max_file_bytes` option)."""

    return int(ctx.option("max_file_bytes", DEFAULT_MAX_FILE_BYTES))


def transfer(
    ctx: ConnectorContext, operation: Operation, call: _Call, args: Mapping[str, Any]
) -> Any:
    """Run a file operation (`response="file"` or `body_format="file"`) and return its result."""

    box = sandbox(ctx)
    given = args.get(operation.file_param or "")
    if not given:
        raise ToolException(f"{operation.file_param} is required")
    if operation.response == "file":
        destination = box.resolve(str(given))
        if destination.exists():
            raise ToolException(
                f"{str(given)!r} already exists in the workspace; choose another path"
            )
        headers = dict(call.headers)
        if headers.get("Accept") == "application/json" and "Accept" not in operation.headers:
            headers["Accept"] = "*/*"
        url = _with_query(call.url, _query_string(call.query, call.explode))
        destination.parent.mkdir(parents=True, exist_ok=True)
        saved = download(ctx, call.method, url, destination, headers=headers)
        return {"saved_to": box.relative(destination), **saved}
    source = box.resolve(str(given), must_exist=True)
    if not source.is_file():
        raise ToolException(f"{str(given)!r} is not a file")
    return upload(ctx, operation, call, args, source)


def download(
    ctx: ConnectorContext,
    method: str,
    url: str,
    destination: Path,
    *,
    headers: Mapping[str, str] | None = None,
    authenticate: bool = True,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Fetch `url` into `destination` (replacing it), atomically; returns size, SHA-256 and type.

    Redirects are followed by hand: credentials go to the first URL only, and every hop passes the
    host allowlist. Pass `authenticate=False` for a pre-authenticated URL (`downloadUrl`).
    """

    limit = max_bytes or max_file_bytes(ctx)
    current, hop_headers = url, dict(headers or {})
    for hop in range(_MAX_HOPS + 1):
        response = send(
            ctx,
            method,
            current,
            headers=hop_headers,
            authenticate=authenticate and hop == 0,
            follow_redirects=False,
            max_bytes=limit,
        )
        location = response.headers.get("Location")
        if response.status not in _REDIRECTS or not location:
            break
        current = urllib.parse.urljoin(current, location)
        ctx._ephemeral.append(current)  # a pre-authenticated URL is a credential
        hop_headers = {"Accept": hop_headers.get("Accept", "*/*")}
    else:
        raise ToolException(f"{ctx.spec.title} redirected more than {_MAX_HOPS} times")
    if response.status >= 400:
        _raise_for(ctx, response, _parse(response, None))
    if not 200 <= response.status < 300:
        raise ToolException(f"{ctx.spec.title} answered HTTP {response.status} instead of a file")
    if response.truncated:
        raise ToolException(
            f"the file is larger than {limit} bytes (the max_file_bytes option); nothing was saved"
        )
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.part")
    try:
        temporary.write_bytes(response.body)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "size": len(response.body),
        "sha256": hashlib.sha256(response.body).hexdigest(),
        "content_type": response.content_type,
    }


def _shaped(operation: Operation, data: Any, status: int) -> Any:
    result = pick(data, operation.result) if operation.result is not None else data
    if result in (None, "") and operation.write and status < 300:
        return {"ok": True, "status": status}
    return result


def upload(
    ctx: ConnectorContext, operation: Operation, call: _Call, args: Mapping[str, Any], source: Path
) -> Any:
    """Send `source`: one request, or (above the session's `simple_limit`) in fragments."""

    size = source.stat().st_size
    limit = max_file_bytes(ctx)
    if size > limit:
        raise ToolException(
            f"{source.name!r} is {size} bytes, more than the max_file_bytes option ({limit})"
        )
    session = operation.session
    if session is not None and size > max(session.simple_limit, 0) and size > 0:
        return _session_upload(ctx, operation, call, args, source, size)
    headers = dict(call.headers)
    headers["Content-Type"] = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    url = _with_query(call.url, _query_string(call.query, call.explode))
    response = send(
        ctx, call.method, url, headers=headers, body=source.read_bytes(), write=operation.write
    )
    data = _parse(response, operation.response)
    _raise_for(ctx, response, data, write=operation.write)
    return _shaped(operation, data, response.status)


def _session_upload(
    ctx: ConnectorContext,
    operation: Operation,
    call: _Call,
    args: Mapping[str, Any],
    source: Path,
    size: int,
) -> Any:
    session = operation.session
    assert session is not None
    opening = dict(session.body_builder(args, size, source.name)) if session.body_builder else {}
    opened = send(
        ctx,
        "POST",
        fill_path(ctx, operation, session.path, args),
        headers={**call.headers, "Content-Type": "application/json"},
        body=json.dumps(opening).encode("utf-8"),
        write=True,
    )
    data = _parse(opened, "json")
    _raise_for(ctx, opened, data, write=True)
    upload_url = pick(data, session.url)
    if not isinstance(upload_url, str) or not upload_url:
        raise ToolException(f"{ctx.spec.title} gave no upload URL for {source.name!r}")
    ctx._ephemeral.append(upload_url)
    try:
        return _send_fragments(ctx, operation, session.fragment, upload_url, source, size)
    except ToolException:
        with contextlib.suppress(ToolException):  # free the half-uploaded session
            send(ctx, "DELETE", upload_url, authenticate=False, write=True)
        raise


def _expected_start(data: Any) -> int | None:
    """The first byte the service still wants, from `nextExpectedRanges` (`["327680-"]`)."""

    ranges = pick(data, "nextExpectedRanges")
    if isinstance(ranges, list) and ranges and isinstance(ranges[0], str):
        start = ranges[0].split("-", 1)[0]
        return int(start) if start.isdigit() else None
    return None


def _send_fragments(
    ctx: ConnectorContext,
    operation: Operation,
    fragment: int,
    upload_url: str,
    source: Path,
    size: int,
) -> Any:
    offset, resyncs = 0, 0
    with source.open("rb") as handle:
        while True:
            handle.seek(offset)
            chunk = handle.read(fragment)
            end = offset + len(chunk)
            headers = {
                "Content-Range": f"bytes {offset}-{end - 1}/{size}",
                "Content-Type": "application/octet-stream",
                "Accept": "application/json",
            }
            try:
                # A fragment is idempotent (same bytes, same range), so it may be retried.
                response = send(
                    ctx, "PUT", upload_url, headers=headers, body=chunk, authenticate=False
                )
            except ToolException:
                resyncs += 1
                if resyncs > _MAX_RESYNCS:
                    raise
                status = send(ctx, "GET", upload_url, authenticate=False)
                resumed = _expected_start(_parse(status, "json")) if status.status < 400 else None
                if resumed is None:
                    raise
                offset = resumed
                continue
            data = _parse(response, "json")
            if response.status in (200, 201):
                return _shaped(operation, data, response.status)
            if response.status == 202:
                resumed = _expected_start(data)
                following = end if resumed is None else resumed
                if following <= offset:
                    # The server asks for the same range again: count it like a resync, so a
                    # server that never moves on cannot keep the upload going forever.
                    resyncs += 1
                    if resyncs > _MAX_RESYNCS:
                        raise ToolException(
                            f"{ctx.spec.title} kept asking for the upload range starting at "
                            f"byte {offset}; the upload was stopped after {_MAX_RESYNCS} tries"
                        )
                offset = following
                continue
            if response.status == 416 and resyncs < _MAX_RESYNCS:
                resyncs += 1
                resumed = _expected_start(pick(data, "error") or data)
                if resumed is not None:
                    offset = resumed
                    continue
            _raise_for(ctx, response, data)
            raise ToolException(
                f"{ctx.spec.title} answered HTTP {response.status} to an upload fragment"
            )

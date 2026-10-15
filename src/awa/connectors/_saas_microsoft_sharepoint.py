"""Catalog entry for SharePoint and OneDrive files over Microsoft Graph.

Built for an app that sees ONE site (`Sites.Selected`, `MS_SITE_URL`); no tool deletes anything.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from awa._internal.install import require_module
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.connectors._engine import (
    ConnectorContext,
    RetryAfter,
    UncertainOutcome,
    build_connector_tool,
    call_operation,
)
from awa.connectors._microsoft_errors import explain
from awa.connectors._saas_microsoft import _GRAPH_URL, _app_sign_in, microsoft_user_agent
from awa.connectors._sharepoint_excel import (
    MAX_CELLS,
    MAX_ROWS,
    append_rows,
    inspect_for_edit,
    update_cells,
)
from awa.connectors._spec import (
    ConnectorSpec,
    Operation,
    Pagination,
    Param,
    UploadSession,
    plain_var,
)
from awa.connectors._transfer import FILE_OPTIONS, max_file_bytes
from awa.errors import OptionalDependencyError

_ID = r"^[A-Za-z0-9!_.,=$~-]+$"
_SIMPLE_UPLOAD_LIMIT = 4_000_000  # bytes; larger files go in an upload session
_WALK_REQUESTS = 40  # most folder listings a name search makes
_WALK_ITEMS = 5000  # most items a name search looks at
_BAD_NAME = re.compile(r'[\x00-\x1f"*:<>?|]')
_MAX_NAME = 255
_MAX_PATH = 400  # SharePoint refuses longer paths
_HTTP = re.compile(r"\(HTTP (\d{3})\)(?:: ([A-Za-z][\w.-]*))?")
_WORKBOOKS = {".xlsx": "xlsx", ".xlsm": "xlsm"}
_ITEM_FIELDS = "id,name,size,webUrl,eTag,lastModifiedDateTime,file,folder,parentReference"
_PAGES = Pagination("next_url", next="@odata.nextLink", size_param="$top", size_max=200)


def _arg(name: str, **options: Any) -> Param:
    return Param(name, "", location="path", required=True, **options)


_DRIVE = _arg("drive_id", pattern=_ID)
_REF = _arg("item_ref", safe="/:")
_SITE_ID = _arg("site_id", pattern=_ID, safe=",")


def _item_list(result: Any, args: Mapping[str, Any]) -> Any:
    parent = args.get("parent")
    return {**result, "items": [_item_view(item, parent) for item in result["items"]]}


def _drive_list(result: Any, args: Mapping[str, Any]) -> Any:
    keep = {"id": "id", "name": "name", "webUrl": "web_url", "driveType": "drive_type"}
    items = [{new: i.get(old) for old, new in keep.items() if i.get(old)} for i in result["items"]]
    return {**result, "items": items}


def _upload_session(args: Mapping[str, Any], size: int, name: str) -> Mapping[str, Any]:
    item: dict[str, Any] = {"@microsoft.graph.conflictBehavior": args.get("conflict") or "fail"}
    if args.get("name"):
        item["name"] = args["name"]
    return {"item": item}


_SITE_BY_ADDRESS = Operation(
    "site",
    "",
    path="/sites/{site_ref}",
    params=(_arg("site_ref", safe="/:"),),
    query={"$select": "id,name,displayName,webUrl"},
)
_MAIN_LIBRARY = Operation(
    "main_library", "", path="/sites/{site_id}/drive", params=(_SITE_ID,), query={"$select": "id"}
)
_LIBRARIES = Operation(
    "libraries",
    "",
    path="/sites/{site_id}/drives",
    params=(_SITE_ID,),
    items="value",
    pages=_PAGES,
    transform=_drive_list,
)
_CHILDREN = Operation(
    "children",
    "",
    path="/drives/{drive_id}/{item_ref}/children",
    params=(_DRIVE, _REF),
    query={"$select": _ITEM_FIELDS},
    items="value",
    pages=_PAGES,
    max_limit=200,
    transform=_item_list,
)
_ITEM = Operation(
    "item",
    "",
    path="/drives/{drive_id}/{item_ref}",
    params=(_DRIVE, _REF),
    query={"$select": _ITEM_FIELDS},
)
_FIND = Operation(
    "find",
    "",
    path="/drives/{drive_id}/root/search(q='{query}')",
    params=(_DRIVE, _arg("query")),
    query={"$select": _ITEM_FIELDS},
    items="value",
    pages=_PAGES,
    max_limit=200,
    transform=_item_list,
)
_DOWNLOAD = Operation(
    "download",
    "",
    path="/drives/{drive_id}/{item_ref}/content",
    params=(_DRIVE, _REF, Param("save_to", "", location="local", required=True)),
    response="file",
    file_param="save_to",
)
_UPLOAD = Operation(
    "upload",
    "",
    method="PUT",
    path="/drives/{drive_id}/{item_ref}/content",
    params=(
        _DRIVE,
        _REF,
        Param("local_path", "", location="local", required=True),
        Param("conflict", "", key="@microsoft.graph.conflictBehavior"),
        Param("if_match", "", location="header", key="If-Match"),
    ),
    write=True,
    body_format="file",
    file_param="local_path",
    session=UploadSession(
        "/drives/{drive_id}/{item_ref}/createUploadSession",
        simple_limit=_SIMPLE_UPLOAD_LIMIT,
        body_builder=_upload_session,
    ),
)
_NEW_FOLDER = Operation(
    "new_folder",
    "",
    method="POST",
    path="/drives/{drive_id}/{item_ref}/children",
    params=(_DRIVE, _REF, Param("name", "", location="body", required=True)),
    write=True,
    body_builder=lambda args: {"folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
)


def _segments(path: str | None, what: str = "path") -> list[str]:
    """The folder and file names of a `/`-separated library path, checked."""

    parts = [part for part in re.split(r"[\\/]+", path or "") if part]
    for part in parts:
        if part in (".", ".."):
            raise ToolException(f"the {what} must not contain '.' or '..'")
        if part != part.strip() or _BAD_NAME.search(part):
            raise ToolException(
                f"{part!r} is not a valid SharePoint name (no leading or trailing spaces and none "
                'of " * : < > ? |)'
            )
        if len(part) > _MAX_NAME:
            raise ToolException(f"a name in the {what} is longer than {_MAX_NAME} characters")
    if sum(len(part) + 1 for part in parts) > _MAX_PATH:
        raise ToolException(f"the {what} is longer than {_MAX_PATH} characters")
    return parts


def _path_ref(parts: list[str]) -> str:
    """How the Graph path names the item at `parts` (`root`, or `root:/a/b:`)."""

    return "root:/" + "/".join(parts) + ":" if parts else "root"


def _ref(path: str | None, item_id: str | None) -> str:
    if path and item_id:
        raise ToolException("give either path or item_id, not both")
    if item_id:
        if not re.fullmatch(_ID, item_id):
            raise ToolException("item_id is not a valid item id")
        return f"items/{item_id}"
    parts = _segments(path)
    if not parts:
        raise ToolException("give the path of the file (or its item_id)")
    return _path_ref(parts)


def _parent_path(item: Mapping[str, Any]) -> str | None:
    reference = item.get("parentReference")
    path = reference.get("path") if isinstance(reference, Mapping) else None
    if not isinstance(path, str) or "root:" not in path:
        return None
    return urllib.parse.unquote(path.partition("root:")[2]).strip("/")


def _item_view(item: Mapping[str, Any], parent: str | None = None) -> dict[str, Any]:
    """What the model sees of a drive item: no pre-authenticated URL, only stable facts."""

    name = item.get("name")
    parent = _parent_path(item) if parent is None else parent
    kind = "folder" if "folder" in item else "file" if "file" in item else None
    view = {
        "name": name,
        "kind": kind,
        "path": f"{parent}/{name}".strip("/") if name and parent is not None else None,
        "id": item.get("id"),
        "size": item.get("size"),
        "modified": item.get("lastModifiedDateTime"),
        "etag": item.get("eTag"),
        "web_url": item.get("webUrl"),
    }
    return {key: value for key, value in view.items() if value is not None}


def _status_of(exc: ToolException) -> int | None:
    found = _HTTP.search(str(exc))
    return int(found.group(1)) if found else None


def _advise(ctx: ConnectorContext, exc: ToolException, operation: str, *, write: bool) -> Exception:
    """`exc` in the plain words of `explain` (the engine's message says status and error code)."""

    text = str(exc)
    found = _HTTP.search(text)
    sign_in = "AADSTS" in text
    if found is None and not sign_in:
        return exc  # not an answer of Microsoft (a refusal, a size limit, a missing variable)
    status = int(found.group(1)) if found else 400
    body = (
        {"error_description": text}
        if sign_in
        else {"error": {"code": (found.group(2) if found else "") or ""}}
    )
    advice = explain(status, body, service="sharepoint", operation=operation)
    extra = (
        " Writing needs the role 'write' on the site, 'read' is not enough."
        if (write and status == 403)
        else ""
    )
    return ToolException(
        ctx.redact(
            f"{ctx.spec.title}: {advice.text} {advice.fix}{extra} [{advice.code}, HTTP {status}]"
        )
    )


@dataclass
class _Library:
    """What one connector instance has looked up: sites by address, libraries by site id."""

    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    libraries: dict[str, str] = field(default_factory=dict)
    search_closed: bool = False


def _site_ref(address: str) -> str:
    parts = urllib.parse.urlsplit(address.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ToolException(
            "a site address looks like https://contoso.sharepoint.com/sites/team-name"
        )
    names = [urllib.parse.unquote(part) for part in parts.path.split("/") if part]
    if len(names) >= 2 and names[0].lower() in ("sites", "teams"):
        for name in names[:2]:
            _segments(name, "site address")
        return f"{parts.hostname}:/{names[0]}/{names[1]}"
    return "root"  # an address without /sites/<name> is the tenant's root site


def _site(ctx: ConnectorContext, state: _Library, address: str | None) -> dict[str, Any]:
    address = address or ctx.require("MS_SITE_URL")
    if address not in state.sites:
        found = call_operation(ctx, _SITE_BY_ADDRESS, {"site_ref": _site_ref(address)})
        state.sites[address] = found if isinstance(found, dict) else {}
    return state.sites[address]


def _site_id(site: Mapping[str, Any]) -> str:
    found = site.get("id")
    if not isinstance(found, str) or not found:
        raise ToolException("SharePoint gave no site id; check MS_SITE_URL")
    return found


def _drive(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> str:
    """The library to use: the `drive_id` argument, else the main library of the site."""

    if args.get("drive_id"):
        return str(args["drive_id"])
    site = _site_id(_site(ctx, state, None))
    if site not in state.libraries:
        found = call_operation(ctx, _MAIN_LIBRARY, {"site_id": site})
        state.libraries[site] = str(found.get("id") or "") if isinstance(found, dict) else ""
        if not state.libraries[site]:
            raise ToolException("the site has no document library the app can use")
    return state.libraries[site]


def _meta(ctx: ConnectorContext, drive: str, ref: str) -> dict[str, Any]:
    found = call_operation(ctx, _ITEM, {"drive_id": drive, "item_ref": ref})
    return found if isinstance(found, dict) else {}


def _get_site(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    site = _site(ctx, state, args.get("site_url"))
    keep = {"id": "id", "name": "name", "displayName": "display_name", "webUrl": "web_url"}
    return {new: site[old] for old, new in keep.items() if site.get(old)}


def _list_drives(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    site = _site_id(_site(ctx, state, args.get("site_url")))
    return call_operation(
        ctx,
        _LIBRARIES,
        {"site_id": site, "limit": args.get("limit"), "page_token": args.get("page_token")},
    )


def _list_children(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    parts = _segments(args.get("folder_path"), "folder path")
    return call_operation(
        ctx,
        _CHILDREN,
        {
            "drive_id": _drive(ctx, state, args),
            "item_ref": _path_ref(parts),
            "parent": "/".join(parts),
            "limit": args.get("limit"),
            "page_token": args.get("page_token"),
        },
    )


def _search(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    words = " ".join(str(args["query"]).split())
    if not words or re.search(r"[\x00-\x1f]", words):
        raise ToolException("give some words to search for")
    drive, limit = _drive(ctx, state, args), args.get("limit")
    if not state.search_closed:
        try:
            found = call_operation(
                ctx,
                _FIND,
                {
                    "drive_id": drive,
                    "query": words.replace("'", "''"),
                    "limit": limit,
                    "page_token": args.get("page_token"),
                },
            )
            return {"mode": "microsoft_search", **found}
        except (RetryAfter, UncertainOutcome):
            raise
        except ToolException as exc:
            if _status_of(exc) != 403 or args.get("page_token"):
                raise
            state.search_closed = True  # do not knock again: the app has only Sites.Selected
    return _name_search(ctx, drive, words, int(limit or 25))


def _name_search(ctx: ConnectorContext, drive: str, words: str, limit: int) -> Any:
    """Microsoft's search is closed to an app with only `Sites.Selected`: look through the folders."""

    wanted = words.lower().split()
    queue: deque[tuple[str, str]] = deque([("", "root")])
    found: list[dict[str, Any]] = []
    scanned = requests = 0
    token: str | None = None
    while queue and len(found) < limit and requests < _WALK_REQUESTS and scanned < _WALK_ITEMS:
        parent, ref = queue.popleft()
        token = None
        while requests < _WALK_REQUESTS:
            page = call_operation(
                ctx,
                _CHILDREN,
                {
                    "drive_id": drive,
                    "item_ref": ref,
                    "parent": parent,
                    "limit": _CHILDREN.max_limit,
                    "page_token": token,
                },
            )
            requests += 1
            for item in page["items"]:
                scanned += 1
                if all(word in str(item.get("name", "")).lower() for word in wanted):
                    found.append(item)
                if item.get("kind") == "folder" and item.get("id"):
                    queue.append((str(item.get("path", "")), f"items/{item['id']}"))
            token = page.get("next_page")
            if not token or len(found) >= limit or scanned >= _WALK_ITEMS:
                break
    return {
        "mode": "name_search",
        "items": found[:limit],
        "count": len(found[:limit]),
        "complete": not queue and not token and len(found) <= limit,
        "scanned": scanned,
        "note": (
            "Microsoft's search (which also reads file contents) is not open to an app that only "
            "has Sites.Selected, so only file and folder NAMES were searched, folder by folder."
        ),
    }


def _download(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    drive = _drive(ctx, state, args)
    ref = _ref(args.get("path"), args.get("item_id"))
    item = _meta(ctx, drive, ref)
    _need_file(item, ctx)
    saved = call_operation(
        ctx, _DOWNLOAD, {"drive_id": drive, "item_ref": ref, "save_to": args["save_to"]}
    )
    return {**_item_view(item), **saved}


def _need_file(item: Mapping[str, Any], ctx: ConnectorContext) -> None:
    if "folder" in item:
        raise ToolException(f"{item.get('name', 'that item')!r} is a folder, not a file")
    size, limit = item.get("size"), max_file_bytes(ctx)
    if isinstance(size, int) and size > limit:
        raise ToolException(
            f"the file is {size} bytes, more than the max_file_bytes option ({limit}); nothing "
            "was downloaded"
        )


def _changed_meanwhile(status: int | None, what: str) -> ToolException | None:
    if status == 412:
        return ToolException(
            f"{what} was changed by someone else after it was read, so nothing was written. Read "
            "it again and repeat the change."
        )
    if status == 423:
        return ToolException(
            f"{what} is locked (checked out or being edited), so nothing was written. Try again "
            "later."
        )
    return None


def _upload(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    drive = _drive(ctx, state, args)
    folder = _segments(args.get("folder_path"), "folder path")
    name = args.get("name") or Path(str(args["local_path"])).name
    parts = [*folder, *_segments(name, "file name")]
    if len(parts) != len(folder) + 1:
        raise ToolException("name is one file name, not a path; use folder_path for the folder")
    conflict = args.get("conflict") or ("replace" if args.get("if_match") else "fail")
    what = f"{'/'.join(parts)!r}"
    try:
        saved = call_operation(
            ctx,
            _UPLOAD,
            {
                "drive_id": drive,
                "item_ref": _path_ref(parts),
                "local_path": args["local_path"],
                "name": parts[-1],
                "conflict": conflict,
                "if_match": args.get("if_match"),
            },
        )
    except (RetryAfter, UncertainOutcome):
        raise
    except ToolException as exc:
        status = _status_of(exc)
        if status == 409:
            raise ToolException(
                f"{what} already exists. Use conflict='rename' to keep both or 'replace' to "
                "overwrite it (SharePoint keeps the old version in its version history)."
            ) from None
        raise _changed_meanwhile(status, what) or exc from None
    return _item_view(saved if isinstance(saved, dict) else {}, "/".join(folder))


def _create_folder(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    drive = _drive(ctx, state, args)
    parts = _segments(args.get("folder_path"), "folder path")
    if not parts:
        raise ToolException("give the path of the folder to create")
    created: list[str] = []
    for depth, name in enumerate(parts):
        try:
            call_operation(
                ctx,
                _NEW_FOLDER,
                {"drive_id": drive, "item_ref": _path_ref(parts[:depth]), "name": name},
            )
            created.append("/".join(parts[: depth + 1]))
        except (RetryAfter, UncertainOutcome):
            raise
        except ToolException as exc:
            if _status_of(exc) != 409:  # 409: the name is taken; fine if it is a folder
                raise
    folder = _meta(ctx, drive, _path_ref(parts))
    if "folder" not in folder:
        raise ToolException(f"{'/'.join(parts)!r} exists and is a file, not a folder")
    return {**_item_view(folder, "/".join(parts[:-1])), "created": created}


@contextlib.contextmanager
def _scratch(ctx: ConnectorContext) -> Iterator[tuple[ConnectorContext, Path]]:
    """A private temporary folder (outside the workspace) and the connector working in it."""

    with tempfile.TemporaryDirectory(prefix="awa-sharepoint-") as folder:
        yield replace(ctx, config={**ctx.config, "root": folder}), Path(folder)


def _workbook(
    ctx: ConnectorContext, state: _Library, args: Mapping[str, Any], *, edit: bool
) -> tuple[str, str, dict[str, Any], str]:
    """The library, the item reference, the item and the file extension of a workbook."""

    require_module("openpyxl", "openpyxl", feature="Excel files")  # before anything is fetched
    drive = _drive(ctx, state, args)
    item = _meta(ctx, drive, _ref(args.get("path"), args.get("item_id")))
    _need_file(item, ctx)
    name = str(item.get("name") or "")
    suffix = Path(name).suffix.lower()
    if suffix == ".xlsm" and edit:
        raise ToolException(
            f"{name!r} is a macro-enabled workbook (.xlsm); awa never edits those because saving "
            "could remove the macros. (Reading it is fine.)"
        )
    if suffix not in _WORKBOOKS:
        raise ToolException(
            f"{name!r} is not an .xlsx workbook; an old .xls or .xlsb file must be saved as .xlsx "
            "in Excel first"
        )
    return drive, f"items/{item.get('id')}", item, suffix


def _read_excel(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    from awa.toolkits.data import data_toolkit

    drive, ref, item, suffix = _workbook(ctx, state, args, edit=False)
    with _scratch(ctx) as (inner, folder):
        call_operation(
            inner, _DOWNLOAD, {"drive_id": drive, "item_ref": ref, "save_to": f"book{suffix}"}
        )
        [reader] = data_toolkit(
            folder, include=["read_excel"], frame_output=False, max_file_bytes=max_file_bytes(ctx)
        )
        text = reader.invoke(
            {
                "path": f"book{suffix}",
                "sheet": args.get("sheet"),
                "header_row": 1 if args.get("header_row") is None else int(args["header_row"]),
                "offset": int(args.get("offset") or 0),
                "limit": int(args.get("limit") or 50),
            }
        )
    if text.startswith("Error: "):
        raise ToolException(text[len("Error: ") :])
    return {"file": _item_view(item), **json.loads(text)}


def _edit_excel(
    ctx: ConnectorContext,
    state: _Library,
    args: Mapping[str, Any],
    edit: Callable[[Path, Path], dict[str, Any]],
) -> Any:
    """Download the workbook, edit the copy, upload it only if nobody changed the file meanwhile."""

    drive, ref, item, suffix = _workbook(ctx, state, args, edit=True)
    name = str(item.get("name"))
    with _scratch(ctx) as (inner, folder):
        original, edited = folder / "original.xlsx", folder / "edited.xlsx"
        call_operation(
            inner, _DOWNLOAD, {"drive_id": drive, "item_ref": ref, "save_to": original.name}
        )
        inspection = inspect_for_edit(original)
        done = edit(original, edited)
        try:
            saved = call_operation(
                inner,
                _UPLOAD,
                {
                    "drive_id": drive,
                    "item_ref": ref,
                    "local_path": edited.name,
                    "name": name,
                    "conflict": "replace",
                    "if_match": item.get("eTag"),
                },
            )
        except (RetryAfter, UncertainOutcome):
            raise
        except ToolException as exc:
            raise _changed_meanwhile(_status_of(exc), f"the workbook {name!r}") or exc from None
    note = (
        "The workbook has formulas: their saved results are recalculated when the file is next "
        "opened in Excel (awa does not calculate them), so a reader that does not calculate "
        "sees empty values until then. "
        if inspection.formulas
        else ""
    )
    return {
        **done,
        "saved": True,
        "file": _item_view(saved if isinstance(saved, dict) else item),
        "note": note + "SharePoint keeps the previous version in the file's version history.",
    }


def _append_rows(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    return _edit_excel(
        ctx,
        state,
        args,
        lambda source, target: append_rows(source, target, str(args["sheet"]), args["rows"]),
    )


def _update_cells(ctx: ConnectorContext, state: _Library, args: Mapping[str, Any]) -> Any:
    return _edit_excel(
        ctx,
        state,
        args,
        lambda source, target: update_cells(source, target, str(args["sheet"]), args["cells"]),
    )


_SITE_URL = plain_var(
    "MS_SITE_URL",
    "Web address of the SharePoint site the app may use, e.g. https://contoso.sharepoint.com/sites/team.",
    "https://contoso.sharepoint.com/sites/awa-files",
)
_SITE_ARG = Param(
    "site_url",
    "Web address of a SharePoint site (https://contoso.sharepoint.com/sites/team); default: "
    "MS_SITE_URL.",
)
_DRIVE_ARG = Param(
    "drive_id",
    "Id of another document library of the site (from list_drives); default: the site's main "
    "library ('Documents').",
    pattern=_ID,
)
_PATH_ARG = Param("path", "Path of the file in the library, e.g. Reports/2026/sales.xlsx.")
_ITEM_ARG = Param(
    "item_id", "Id of the file (from list_children or search) instead of path.", pattern=_ID
)
_SHEET = Param("sheet", "Name of the sheet.", required=True)
_FOLDER = Param("folder_path", "Folder in the library, e.g. Reports/2026; empty is the top folder.")

_EXCEL_NOTE = (
    " Works on the file: awa downloads it, changes a copy and uploads it only if nobody else "
    "changed the file meanwhile (otherwise nothing is written and you are told to read it again)."
    " Workbooks with macros, pivot tables, charts or pictures are refused so they are not damaged."
    " Values are stored as plain values, never as formulas."
)

_OPERATIONS: tuple[tuple[Operation, Callable[..., Any]], ...] = (
    (
        Operation(
            "get_site",
            "Look up a SharePoint site by its web address: its id, name and link. Use it to check "
            "that the app can reach the site.",
            params=(_SITE_ARG,),
        ),
        _get_site,
    ),
    (
        Operation(
            "list_drives",
            "List the document libraries of a site.",
            params=(_SITE_ARG,),
            items="value",
            pages=_PAGES,
            max_limit=200,
        ),
        _list_drives,
    ),
    (
        Operation(
            "list_children",
            "List the files and folders in a folder of a library (name, kind, path, size, "
            "modified, etag, id).",
            params=(_FOLDER, _DRIVE_ARG),
            items="value",
            pages=_PAGES,
            max_limit=200,
        ),
        _list_children,
    ),
    (
        Operation(
            "search",
            "Search a library for files and folders. Microsoft's search (it also reads the file "
            "contents) needs a tenant-wide permission; an app that may only see one site "
            "(Sites.Selected) gets a search of the file and folder names instead, which says so "
            "in the result (mode).",
            params=(Param("query", "Words to look for.", required=True), _DRIVE_ARG),
            items="value",
            pages=_PAGES,
            max_limit=200,
        ),
        _search,
    ),
    (
        Operation(
            "download_file",
            "Download a file of the library into the workspace folder. Returns the saved path, "
            "size, SHA-256 and the etag. The content is not returned: read it from the workspace.",
            params=(
                _PATH_ARG,
                _ITEM_ARG,
                Param(
                    "save_to",
                    "Where to save it in the workspace folder (must not exist yet).",
                    location="local",
                    required=True,
                ),
                _DRIVE_ARG,
            ),
        ),
        _download,
    ),
    (
        Operation(
            "upload_file",
            "Upload a file from the workspace folder into a folder of the library (small files in "
            "one request, large ones in parts). Never overwrites unless conflict is 'replace'.",
            params=(
                Param(
                    "local_path",
                    "The file in the workspace folder.",
                    location="local",
                    required=True,
                ),
                _FOLDER,
                Param("name", "File name in the library; default: the local file's name."),
                Param(
                    "conflict",
                    "If the name exists: 'fail' (default), 'rename' (keep both) or 'replace' "
                    "(the old version stays in the version history).",
                    enum=("fail", "rename", "replace"),
                ),
                Param(
                    "if_match",
                    "The etag you read: replace the file only if it is still that version "
                    "(implies conflict 'replace').",
                ),
                _DRIVE_ARG,
            ),
            write=True,
        ),
        _upload,
    ),
    (
        Operation(
            "create_folder",
            "Create a folder (and the folders above it that do not exist yet) in the library.",
            params=(
                Param("folder_path", "Folder to create, e.g. Reports/2026.", required=True),
                _DRIVE_ARG,
            ),
            write=True,
        ),
        _create_folder,
    ),
    (
        Operation(
            "read_excel",
            "Read a sheet of an Excel workbook (.xlsx) in the library: sheet names, columns and a "
            "window of rows. The workbook is downloaded to a temporary folder and not kept.",
            params=(
                _PATH_ARG,
                _ITEM_ARG,
                Param("sheet", "Sheet name; default the first sheet."),
                Param(
                    "header_row",
                    "Row number with the column names; 0: columns A, B, C.",
                    type="integer",
                ),
                Param("offset", "Data rows to skip first.", type="integer"),
                Param("limit", "Most rows returned (default 50).", type="integer"),
                _DRIVE_ARG,
            ),
        ),
        _read_excel,
    ),
    (
        Operation(
            "append_excel_rows",
            f"Add rows (at most {MAX_ROWS}) below the last used row of a sheet of an Excel "
            "workbook (.xlsx) in the library. Each row is a list of values for the columns A, B, "
            "C, ...; read the sheet first to see the column order." + _EXCEL_NOTE,
            params=(
                _PATH_ARG,
                _ITEM_ARG,
                _SHEET,
                Param(
                    "rows",
                    "The rows: a list of rows, each a list of text, numbers, true/false or null.",
                    type="array",
                    items="array",
                    required=True,
                ),
                _DRIVE_ARG,
            ),
            write=True,
        ),
        _append_rows,
    ),
    (
        Operation(
            "update_excel_cells",
            f"Set single cells (at most {MAX_CELLS}) of a sheet of an Excel workbook (.xlsx) in "
            'the library, e.g. {"B2": 5}. Cells with formulas and merged cells are not '
            "overwritten." + _EXCEL_NOTE,
            params=(
                _PATH_ARG,
                _ITEM_ARG,
                _SHEET,
                Param(
                    "cells",
                    'The new values by cell name, e.g. {"B2": 5, "C3": "done"}.',
                    type="object",
                    required=True,
                ),
                _DRIVE_ARG,
            ),
            write=True,
        ),
        _update_cells,
    ),
)


def _tools(ctx: ConnectorContext) -> list[BaseTool]:
    state = _Library()
    handlers = {operation.name: handler for operation, handler in _OPERATIONS}

    def tool(operation: Operation) -> BaseTool:
        handler = handlers[operation.name]

        def run(ctx: ConnectorContext, operation: Operation, args: Mapping[str, Any]) -> Any:
            try:
                return handler(ctx, state, args)
            except OptionalDependencyError as exc:
                raise ToolException(str(exc)) from None

        def advised(exc: ToolException) -> Exception:
            if isinstance(exc, RetryAfter | UncertainOutcome):
                return exc
            return _advise(ctx, exc, operation.name, write=operation.write)

        return build_connector_tool(ctx, operation, run, advised)

    return [tool(operation) for operation in ctx.operations()]


def _check(ctx: ConnectorContext) -> str:
    """The health probe: the site and its main library, which a Sites.Selected grant allows."""

    state = _Library()
    try:
        site = _site(ctx, state, None)
        _drive(ctx, state, {})
    except (RetryAfter, UncertainOutcome):
        raise
    except ToolException as exc:
        raise _advise(ctx, exc, "get_site", write=False) from None
    return f"site {site.get('displayName') or site.get('name') or 'found'} and its library answered"


SHAREPOINT = ConnectorSpec(
    name="sharepoint",
    title="SharePoint and OneDrive files",
    category="files",
    summary=(
        "Files and Excel workbooks in a SharePoint site (or OneDrive) over Microsoft Graph: list, "
        "search, download, upload, create folders, read and edit Excel; never deletes anything."
    ),
    base_url="{MS_GRAPH_URL}/v1.0",
    env=(_GRAPH_URL, _SITE_URL),
    auth=_app_sign_in("https://graph.microsoft.com/.default"),
    headers={"User-Agent": microsoft_user_agent()},
    hosts=("*.sharepoint.com",),
    errors=("error.code", "error_description", "error.message", "message"),
    operations=tuple(operation for operation, _ in _OPERATIONS),
    health="get_site",
    factory=_tools,
    check=_check,
    options=FILE_OPTIONS,
    notes=(
        "no tool deletes files or folders",
        "with Sites.Selected the search covers file and folder names only",
        "Excel workbooks with macros, pivot tables, charts or pictures are not edited",
    ),
    docs="https://learn.microsoft.com/graph/permissions-selected-overview",
    setup=(
        "Register an app in Microsoft Entra ID, add the application permission Sites.Selected, grant "
        "admin consent and create a client secret; then an administrator grants the app access to "
        "ONE site (role 'write' to upload, 'read' to only read). Set MS_SITE_URL to that site's "
        "address."
    ),
)

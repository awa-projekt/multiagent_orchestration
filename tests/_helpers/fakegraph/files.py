"""SharePoint / OneDrive plug-in: sites, drives, items, downloads, uploads, delta.

Routes (all below `/v1.0`, each also under `/sites/{site-id}/drive` instead of `/drives/{drive-id}`):

- `GET /sites?search=`, `GET /sites/{host}:/{path}`, `GET /sites/{id}`, `.../drive`, `.../drives`,
  `POST /sites/{id}/permissions` (the admin's `Sites.Selected` grant)
- `GET .../root`, `.../items/{id}`, `.../root:/{path}` and their `/children`; `POST .../children`
  (create a folder)
- `GET .../content` answers `302` to `/_download/<token>`, which accepts no `Authorization`
- `PUT .../content` (simple upload, `If-Match`, `@microsoft.graph.conflictBehavior`)
- `POST .../createUploadSession`, then `PUT /_upload/<id>` fragments with `Content-Range`
- `GET .../root/delta` (`?token=latest`)
- `GET .../root/search(q='...')` (not for an app that only has `Sites.Selected`)

State is `graph.store("files")`: build it with `add_site`, `put_file`, `add_folder`, `grant`, `lock`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from _helpers.fakegraph.core import Ctx, GraphError, Perm, Plugin, Reply, new_guid
from _helpers.fakegraph.odata import DeltaFeed, clean, collection, delta_round

plugin = Plugin("files")

CHUNK = 327_680  # 320 KiB: every upload fragment but the last is a multiple of it
MAX_FRAGMENT = 60 * 1024 * 1024
SIMPLE_PUT_LIMIT = 4 * 1024 * 1024
_FILE_READ_ALL = ("Files.Read.All", "Files.ReadWrite.All", "Sites.Read.All", "Sites.ReadWrite.All")
_FILE_WRITE_ALL = ("Files.ReadWrite.All", "Sites.ReadWrite.All", "Sites.FullControl.All")
_SITE_ROLES_READ = {"read", "write", "owner", "fullcontrol"}
_SITE_ROLES_WRITE = {"write", "owner", "fullcontrol"}
_BAD_NAME = re.compile(r'[\\*:<>?/|"]')
_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")

READ = Perm(
    app=(*_FILE_READ_ALL, "Sites.FullControl.All", "Sites.Selected"),
    user=("Files.Read", "Files.ReadWrite", *_FILE_READ_ALL),
)
WRITE = Perm(app=(*_FILE_WRITE_ALL, "Sites.Selected"), user=("Files.ReadWrite", *_FILE_WRITE_ALL))
SITES_SEARCH = Perm(
    app=("Sites.Read.All", "Sites.ReadWrite.All", "Sites.FullControl.All"),
    user=("Sites.Read.All", "Sites.ReadWrite.All"),
)
SITE_READ = Perm(
    app=("Sites.Read.All", "Sites.ReadWrite.All", "Sites.FullControl.All", "Sites.Selected"),
    user=("Sites.Read.All", "Sites.ReadWrite.All"),
)
SITE_FULL = Perm(app=("Sites.FullControl.All",), user=("Sites.FullControl.All",))

R_SELECTED_NO_SEARCH = plugin.rule(
    "sharepoint.sites_selected_cannot_search",
    "GET /sites?search= needs Sites.Read.All or more; an app that only has Sites.Selected gets 403.",
    source="learn.microsoft.com/graph/permissions-selected-overview",
    source_date="2026-10-05",
)
R_SELECTED_GRANT = plugin.rule(
    "sharepoint.sites_selected_needs_grant",
    "With Sites.Selected an app reaches a site only after an admin granted it: an ungranted site "
    "is 403, a read grant cannot write. The roles are read, write, owner and fullcontrol "
    "('manage' is not a role).",
    source="learn.microsoft.com/graph/permissions-selected-overview",
    source_date="2026-02-26",
)
R_GRANT_SHAPE = plugin.rule(
    "sharepoint.grant_input_is_grantedToIdentities",
    "POST /sites/{id}/permissions takes `grantedToIdentities` as input; `grantedToIdentitiesV2` "
    "is rejected with 400; the call needs Sites.FullControl.All.",
    source="learn.microsoft.com/graph/api/site-post-permissions (+ Q&A thread)",
    source_date="2026-10-05",
)
R_REDIRECT = plugin.rule(
    "files.content_redirects_without_credentials",
    "GET .../content answers 302 to a pre-authenticated URL; that URL answers 401 when the "
    "request carries an Authorization header.",
    source="learn.microsoft.com/graph/api/driveitem-get-content",
    source_date="2026-10-05",
)
R_SESSION_AUTH = plugin.rule(
    "files.upload_url_rejects_authorization",
    "A PUT to an upload session URL with an Authorization header is rejected with 401.",
    source="learn.microsoft.com/graph/api/driveitem-createuploadsession",
    source_date="2026-10-05",
)
R_FRAGMENTS = plugin.rule(
    "files.upload_fragment_rules",
    "Fragments need Content-Range, start at the next expected byte (416 otherwise), be a multiple "
    "of 320 KiB except the last, and stay under 60 MiB.",
    source="learn.microsoft.com/graph/api/driveitem-createuploadsession",
    source_date="2026-10-05",
)
R_SIMPLE = plugin.rule(
    "files.simple_put_conservative_limit",
    "A simple PUT above 4 MiB is refused with 413 in this fake (the documentation says up to "
    "250 MB; awa treats 4 MB as the bound, so the fake enforces the bound it relies on).",
    source="learn.microsoft.com/graph/api/driveitem-put-content (older material: 4 MB)",
    source_date="2026-10-05",
)
R_CONFLICT = plugin.rule(
    "files.conflict_behavior",
    "@microsoft.graph.conflictBehavior fail answers 409 nameAlreadyExists, rename appends ' 1', "
    "replace overwrites (200 instead of 201).",
    source="learn.microsoft.com/graph/api/driveitem-put-content",
    source_date="2026-10-05",
)
R_IF_MATCH = plugin.rule(
    "files.if_match_etag",
    "A PUT with If-Match that no longer equals the eTag answers 412; a locked file answers 423.",
    source="learn.microsoft.com/graph/api/driveitem-put-content (If-Match request header)",
    source_date="2026-10-05",
)
R_CHILDREN_TOP = plugin.rule(
    "files.children_top_capped_at_200",
    "$top of a children listing is capped at 200 (not rejected); further pages come as an "
    "absolute @odata.nextLink.",
    source="learn.microsoft.com/graph/api/driveitem-list-children",
    source_date="2026-10-05",
)
R_DELTA = plugin.rule(
    "files.delta_rounds",
    "root/delta pages through everything, ends with @odata.deltaLink; later rounds return only "
    "changes, deleted items with a `deleted` facet; ?token=latest returns no items.",
    source="learn.microsoft.com/graph/api/driveitem-delta",
    source_date="unverified",
)
R_DELTA_ROOT = plugin.rule(
    "files.delta_root_only",
    "On SharePoint and OneDrive for Business delta works on the drive root only.",
    source="learn.microsoft.com/graph/api/driveitem-delta",
    source_date="unverified",
)
R_SEARCH_SELECTED = plugin.rule(
    "files.drive_search_excludes_sites_selected",
    "GET .../root/search(q='...') does not support the Sites.Selected application permission: an "
    "app that only has Sites.Selected gets 403 even on a site it was granted (the exact status "
    "is not documented; the fake answers 403 accessDenied).",
    source="learn.microsoft.com/graph/api/driveitem-search (note under Permissions)",
    source_date="2026-06-19",
)
R_SESSION_IF_MATCH = plugin.rule(
    "files.session_honours_if_match",
    "createUploadSession with If-Match (or If-None-Match) answers 412 when the eTag does not (or "
    "does) equal the item's current eTag, before any byte is sent.",
    source="learn.microsoft.com/graph/api/driveitem-createuploadsession (Request headers)",
    source_date="2026-08-11",
)
R_ITEM_NAME = plugin.rule(
    "files.invalid_names",
    'Names with any of \\ * : < > ? / | " are refused with 400 invalidRequest.',
    source="support.microsoft.com (restrictions for OneDrive and SharePoint file names)",
    source_date="unverified",
)


# --------------------------------------------------------------------------- state


def _tags(item_id: str, version: int) -> tuple[str, str]:
    """`(eTag, cTag)` of an item version, shaped like SharePoint's."""

    etag = new_guid(f"etag:{item_id}:{version}")
    ctag = new_guid(f"ctag:{item_id}:{version}")
    return f'"{{{etag}}},{version}"', f'"c:{{{ctag}}},{version}"'


@dataclass
class Site:
    id: str
    hostname: str
    path: str
    name: str
    drive_id: str


@dataclass
class _Session:
    drive_id: str
    parent_id: str
    name: str
    conflict: str
    total: int | None
    expires: float
    received: bytearray = field(default_factory=bytearray)
    done: bool = False


@dataclass
class Drive:
    id: str
    site_id: str | None
    root_id: str
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    feed: DeltaFeed = field(default_factory=DeltaFeed)


@dataclass
class FilesStore:
    """Sites, drives, grants and open transfers of the files plug-in."""

    graph: Any
    sites: dict[str, Site] = field(default_factory=dict)
    drives: dict[str, Drive] = field(default_factory=dict)
    grants: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    sessions: dict[str, _Session] = field(default_factory=dict)
    downloads: dict[str, tuple[str, str, float]] = field(default_factory=dict)
    locked: set[str] = field(default_factory=set)
    simple_put_limit: int | None = SIMPLE_PUT_LIMIT
    download_host: str | None = None
    counter: int = 0

    # ---- building the world

    def _id(self, prefix: str = "01") -> str:
        self.counter += 1
        return prefix + hashlib.sha1(f"item-{self.counter}".encode()).hexdigest()[:32].upper()

    def add_site(self, hostname: str, path: str, name: str | None = None) -> Site:
        """A site `https://<hostname>/<path>` with its default document library."""

        site_id = (
            f"{hostname},{new_guid(f'sc:{hostname}/{path}')},{new_guid(f'web:{hostname}/{path}')}"
        )
        drive = self.add_drive(site_id)
        site = Site(
            site_id, hostname, path.strip("/"), name or path.strip("/").split("/")[-1], drive.id
        )
        self.sites[site_id] = site
        return site

    def add_drive(self, site_id: str | None = None) -> Drive:
        drive_id = "b!" + hashlib.sha1(f"drive-{len(self.drives)}".encode()).hexdigest()[:30]
        root_id = self._id()
        drive = Drive(drive_id, site_id, root_id)
        root = self._item(drive, root_id, "root", None, folder=True)
        drive.items[root_id] = root
        drive.feed.upsert(root_id, root)
        self.drives[drive_id] = drive
        return drive

    def _item(
        self,
        drive: Drive,
        item_id: str,
        name: str,
        parent: str | None,
        *,
        folder: bool,
        content: bytes = b"",
    ) -> dict[str, Any]:
        now = self.graph.clock.iso()
        etag, ctag = _tags(item_id, 1)
        item: dict[str, Any] = {
            "id": item_id,
            "name": name,
            "createdDateTime": now,
            "lastModifiedDateTime": now,
            "webUrl": f"https://{self._host(drive)}/{name}",
            "size": len(content),
            "eTag": etag,
            "cTag": ctag,
            "_version": 1,
            "_parent": parent,
            "_content": content,
        }
        if parent is not None:
            item["parentReference"] = {"driveId": drive.id, "id": parent}
        if folder:
            item["folder"] = {"childCount": 0}
        else:
            item["file"] = {"mimeType": "application/octet-stream"}
        return item

    def _host(self, drive: Drive) -> str:
        site = self.sites.get(drive.site_id or "")
        return f"{site.hostname}/{site.path}" if site else "onedrive.test"

    def _touch(self, drive: Drive, item: dict[str, Any]) -> None:
        item["_version"] += 1
        item["lastModifiedDateTime"] = self.graph.clock.iso()
        item["size"] = len(item["_content"])
        item["eTag"], item["cTag"] = _tags(item["id"], item["_version"])
        for entry in drive.items.values():
            if "folder" in entry:
                entry["folder"]["childCount"] = len(self.children(drive, entry["id"]))
        drive.feed.upsert(item["id"], item)

    def children(self, drive: Drive, folder_id: str) -> list[dict[str, Any]]:
        found = [i for i in drive.items.values() if i["_parent"] == folder_id]
        return sorted(found, key=lambda i: i["name"].lower())

    def find_child(self, drive: Drive, folder_id: str, name: str) -> dict[str, Any] | None:
        return next(
            (c for c in self.children(drive, folder_id) if c["name"].lower() == name.lower()), None
        )

    def folder_path(
        self, drive: Drive, path: str, *, create: bool = False
    ) -> dict[str, Any] | None:
        """The folder at `path` (`a/b`), created on the way when `create`."""

        current = drive.items[drive.root_id]
        for part in [p for p in path.split("/") if p]:
            child = self.find_child(drive, current["id"], part)
            if child is None:
                if not create:
                    return None
                child = self._make(drive, current["id"], part, folder=True)
            current = child
        return current

    def _make(
        self, drive: Drive, parent: str, name: str, *, folder: bool, content: bytes = b""
    ) -> dict[str, Any]:
        item = self._item(drive, self._id(), name, parent, folder=folder, content=content)
        drive.items[item["id"]] = item
        parent_item = drive.items[parent]
        drive.feed.upsert(item["id"], item)
        self._touch_parent(drive, parent_item)
        return item

    def _touch_parent(self, drive: Drive, parent: dict[str, Any]) -> None:
        parent["folder"]["childCount"] = len(self.children(drive, parent["id"]))
        drive.feed.upsert(parent["id"], parent)

    def put_file(
        self, drive: Drive | Site, path: str, content: bytes, *, mime: str | None = None
    ) -> dict[str, Any]:
        """Create or replace the file at `path` (`Folder/name.txt`) and return its item."""

        target = self.drives[drive.drive_id] if isinstance(drive, Site) else drive
        folder_path, _, name = path.strip("/").rpartition("/")
        folder = self.folder_path(target, folder_path, create=True)
        assert folder is not None
        existing = self.find_child(target, folder["id"], name)
        if existing is not None:
            existing["_content"] = content
            self._touch(target, existing)
            return existing
        item = self._make(target, folder["id"], name, folder=False, content=content)
        if mime:
            item["file"]["mimeType"] = mime
        return item

    def add_folder(self, drive: Drive | Site, path: str) -> dict[str, Any]:
        target = self.drives[drive.drive_id] if isinstance(drive, Site) else drive
        folder = self.folder_path(target, path, create=True)
        assert folder is not None
        return folder

    def delete(self, drive: Drive | Site, item_id: str) -> None:
        """Remove an item (it shows in the next delta round with a `deleted` facet)."""

        target = self.drives[drive.drive_id] if isinstance(drive, Site) else drive
        item = target.items.pop(item_id)
        target.feed.delete(item_id)
        if item["_parent"] in target.items:
            self._touch_parent(target, target.items[item["_parent"]])

    def grant(self, site: Site, client_id: str, *roles: str) -> None:
        """What the admin's `POST /sites/{id}/permissions` does."""

        self.grants.setdefault(site.id, {}).setdefault(client_id, set()).update(roles)

    def lock(self, item_id: str) -> None:
        """Check the file out: writes answer 423 until `unlock`."""

        self.locked.add(item_id)

    def unlock(self, item_id: str) -> None:
        self.locked.discard(item_id)

    def content(self, drive: Drive | Site, path: str) -> bytes:
        """The bytes of the file at `path`."""

        target = self.drives[drive.drive_id] if isinstance(drive, Site) else drive
        folder_path, _, name = path.strip("/").rpartition("/")
        folder = self.folder_path(target, folder_path)
        assert folder is not None
        item = self.find_child(target, folder["id"], name)
        assert item is not None, f"no file {path}"
        return bytes(item["_content"])


@plugin.setup
def _setup(graph: Any) -> FilesStore:
    return FilesStore(graph)


# --------------------------------------------------------------------------- helpers


def _public(ctx: Ctx, drive: Drive, item: dict[str, Any]) -> dict[str, Any]:
    body = clean(item)
    if "file" in item:
        token = _download_token(ctx, drive, item)
        body["@microsoft.graph.downloadUrl"] = _download_url(ctx, token)
    body["parentReference"] = {**item.get("parentReference", {}), "driveId": drive.id}
    return body


def _download_token(ctx: Ctx, drive: Drive, item: dict[str, Any]) -> str:
    store: FilesStore = ctx.store
    token = secrets.token_urlsafe(24)
    store.downloads[token] = (drive.id, item["id"], ctx.clock.time() + 3600)
    return token


def _download_url(ctx: Ctx, token: str) -> str:
    """The pre-authenticated URL; on `download_host` (another name for this server, such as
    `localhost`) it models the redirect to a different host."""

    store: FilesStore = ctx.store
    base = ctx.base
    if store.download_host:
        parts = urlsplit(base)
        base = f"{parts.scheme}://{store.download_host}:{parts.port}"
    return f"{base}/_download/{token}"


def _item_error(code: str, message: str, status: int = 404) -> GraphError:
    return GraphError(status, code, message)


def _drive(ctx: Ctx) -> Drive:
    store: FilesStore = ctx.store
    if "site_id" in ctx.params:
        site = store.sites.get(ctx.params["site_id"])
        if site is None:
            raise _item_error("itemNotFound", "The site was not found.")
        return store.drives[site.drive_id]
    drive = store.drives.get(ctx.params["drive_id"])
    if drive is None:
        raise _item_error("itemNotFound", "The drive was not found.")
    return drive


def _authorise(ctx: Ctx, drive: Drive, *, write: bool) -> None:
    """The per-site rule behind `Sites.Selected` (tenant-wide roles and user tokens pass)."""

    principal = ctx.principal
    assert principal is not None
    if principal.kind != "app":
        return
    wide = _FILE_WRITE_ALL if write else (*_FILE_READ_ALL, "Sites.FullControl.All")
    if principal.grants & set(wide):
        return
    store: FilesStore = ctx.store
    roles = store.grants.get(drive.site_id or "", {}).get(principal.client_id, set())
    if roles & (_SITE_ROLES_WRITE if write else _SITE_ROLES_READ):
        return
    raise GraphError(403, "accessDenied", "Access denied")


def _locate(ctx: Ctx, drive: Drive) -> dict[str, Any]:
    """The item a route addresses: `items/{id}`, `root` or `root:/{path}`."""

    store: FilesStore = ctx.store
    if "item_id" in ctx.params:
        key = ctx.params["item_id"]
        item = drive.items.get(drive.root_id if key == "root" else key)
    elif "path" in ctx.params:
        folder_path, _, name = ctx.params["path"].strip(":/").rpartition("/")
        folder = store.folder_path(drive, folder_path)
        item = store.find_child(drive, folder["id"], name) if folder else None
    else:
        item = drive.items[drive.root_id]
    if item is None:
        raise _item_error("itemNotFound", "The resource could not be found.")
    return item


def _check_name(name: str) -> None:
    if not name or _BAD_NAME.search(name) or name.strip() != name:
        raise GraphError(400, "invalidRequest", f"The name '{name}' is not valid.")


def _unique(store: FilesStore, drive: Drive, parent: str, name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    number = 1
    while store.find_child(drive, parent, name) is not None:
        name = f"{stem} {number}{dot}{extension}"
        number += 1
    return name


def _conflict(value: str | None, default: str) -> str:
    behavior = value or default
    if behavior not in ("fail", "rename", "replace"):
        raise GraphError(400, "invalidRequest", f"Unknown conflictBehavior '{behavior}'.")
    return behavior


def _store_file(
    ctx: Ctx,
    drive: Drive,
    parent: dict[str, Any],
    name: str,
    content: bytes,
    conflict: str,
    *,
    if_match: str | None = None,
) -> Reply:
    """Create or replace `name` below `parent`, following the conflict behavior."""

    store: FilesStore = ctx.store
    _check_name(name)
    existing = store.find_child(drive, parent["id"], name)
    if existing is not None:
        if if_match is not None and if_match != existing["eTag"]:
            raise GraphError(
                412, "preconditionFailed", "The precondition (If-Match) failed: the item changed."
            )
        if existing["id"] in store.locked:
            raise GraphError(423, "resourceLocked", "The file is checked out or locked.")
        if conflict == "fail":
            raise GraphError(409, "nameAlreadyExists", "The specified item name already exists.")
        if conflict == "replace":
            existing["_content"] = content
            store._touch(drive, existing)
            return Reply(200, _public(ctx, drive, existing))
        name = _unique(store, drive, parent["id"], name)
    elif if_match is not None:
        raise GraphError(412, "preconditionFailed", "The precondition (If-Match) failed.")
    item = store._make(drive, parent["id"], name, folder=False, content=content)
    return Reply(201, _public(ctx, drive, item))


# --------------------------------------------------------------------------- sites


@plugin.route("GET", "/sites", perm=SITES_SEARCH, family="sharepoint")
def search_sites(ctx: Ctx) -> Any:
    store: FilesStore = ctx.store
    text = (ctx.request.q("search") or "*").lower()
    found = [
        _site_json(s)
        for s in store.sites.values()
        if text == "*" or text in s.name.lower() or text in s.path.lower()
    ]
    return collection(ctx, found, default_top=200, max_top=200, context="sites")


def _site_json(site: Site) -> dict[str, Any]:
    return {
        "id": site.id,
        "name": site.name,
        "displayName": site.name,
        "webUrl": f"https://{site.hostname}/{site.path}",
    }


def _site_access(ctx: Ctx, site: Site) -> None:
    principal = ctx.principal
    assert principal is not None
    if principal.kind == "app" and not principal.grants & {
        "Sites.Read.All",
        "Sites.ReadWrite.All",
        "Sites.FullControl.All",
    }:
        store: FilesStore = ctx.store
        if not store.grants.get(site.id, {}).get(principal.client_id):
            raise GraphError(403, "accessDenied", "Access denied")


@plugin.route("GET", "/sites/{host}:/{spath*}:/drive", perm=SITE_READ, family="sharepoint")
def site_drive_by_path(ctx: Ctx) -> Any:
    site = _site_by_path(ctx)
    drive = ctx.graph.store("files").drives[site.drive_id]
    return _drive_json(drive, site)


@plugin.route("GET", "/sites/{host}:/{spath*}", perm=SITE_READ, family="sharepoint")
def site_by_path(ctx: Ctx) -> Any:
    return _site_json(_site_by_path(ctx))


def _site_by_path(ctx: Ctx) -> Site:
    store: FilesStore = ctx.store
    host, path = ctx.params["host"].lower(), ctx.params["spath"].strip("/:").lower()
    site = next(
        (s for s in store.sites.values() if s.hostname.lower() == host and s.path.lower() == path),
        None,
    )
    if site is None:
        raise GraphError(404, "itemNotFound", "The requested resource was not found.")
    _site_access(ctx, site)
    return site


def _drive_json(drive: Drive, site: Site | None = None) -> dict[str, Any]:
    return {
        "id": drive.id,
        "driveType": "documentLibrary",
        "name": "Documents",
        "webUrl": f"https://{site.hostname}/{site.path}/Shared Documents" if site else "",
    }


R_ROOT_SITE = plugin.rule(
    "sharepoint.sites_selected_cannot_read_the_root_site",
    "GET /sites/root (the tenant's root site) works with Sites.Read.All or more; an app that has "
    "only Sites.Selected and no grant on the root site gets 403 accessDenied.",
    source="learn.microsoft.com/graph/permissions-selected-overview",
    source_date="2026-02-26",
)


@plugin.route("GET", "/sites/root", perm=SITE_READ, family="sharepoint")
def root_site(ctx: Ctx) -> Any:
    store: FilesStore = ctx.store
    host = next((s.hostname for s in store.sites.values()), "contoso.sharepoint.com")
    root = next((s for s in store.sites.values() if not s.path), None)
    if root is None:
        root = store.add_site(host, "", name="Communication site")
    _site_access(ctx, root)
    return _site_json(root)


@plugin.route("GET", "/sites/{site-id}/drives", perm=SITE_READ, family="sharepoint")
def site_drives(ctx: Ctx) -> Any:
    site = _find_site(ctx)
    store: FilesStore = ctx.store
    return collection(
        ctx, [_drive_json(store.drives[site.drive_id], site)], context="drives", default_top=200
    )


@plugin.route("GET", "/sites/{site-id}/drive", perm=READ, family="sharepoint")
def site_drive(ctx: Ctx) -> Any:
    site = _find_site(ctx)
    store: FilesStore = ctx.store
    return _drive_json(store.drives[site.drive_id], site)


@plugin.route("GET", "/sites/{site-id}", perm=SITE_READ, family="sharepoint")
def site_by_id(ctx: Ctx) -> Any:
    return _site_json(_find_site(ctx))


def _find_site(ctx: Ctx) -> Site:
    store: FilesStore = ctx.store
    site = store.sites.get(ctx.params["site_id"])
    if site is None:
        raise GraphError(404, "itemNotFound", "The requested resource was not found.")
    _site_access(ctx, site)
    return site


@plugin.route("POST", "/sites/{site-id}/permissions", perm=SITE_FULL, family="sharepoint")
def grant_site(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    site = store.sites.get(ctx.params["site_id"])
    if site is None:
        raise GraphError(404, "itemNotFound", "The requested resource was not found.")
    body = ctx.json()
    if not isinstance(body, dict):
        raise GraphError(400, "invalidRequest", "Invalid request body.")
    if "grantedToIdentitiesV2" in body:
        raise GraphError(
            400,
            "invalidRequest",
            "The property 'grantedToIdentitiesV2' is read-only on input; use grantedToIdentities.",
        )
    roles, identities = body.get("roles"), body.get("grantedToIdentities")
    if (
        not isinstance(roles, list)
        or not roles
        or not set(roles) <= {"read", "write", "owner", "fullcontrol"}
        or not isinstance(identities, list)
        or not identities
    ):
        raise GraphError(
            400,
            "invalidRequest",
            "roles and grantedToIdentities are required (roles: read, write, owner, fullcontrol).",
        )
    found = [entry.get("application") if isinstance(entry, dict) else None for entry in identities]
    if not all(isinstance(application, dict) and application.get("id") for application in found):
        raise GraphError(400, "invalidRequest", "Each identity needs application.id.")
    applications = []
    for application in found:
        store.grant(site, str(application["id"]), *roles)
        applications.append(
            {
                "application": {
                    "id": application["id"],
                    "displayName": application.get("displayName", ""),
                }
            }
        )
    return Reply(
        201,
        {
            "id": new_guid(),
            "roles": roles,
            "grantedToIdentitiesV2": applications,
            "grantedToIdentities": applications,
        },
    )


# --------------------------------------------------------------------------- items


def _register(prefix: str) -> None:
    """Routes below a drive prefix (`/drives/{drive-id}` or `/sites/{site-id}/drive`)."""

    for base in (f"{prefix}/items/{{item-id}}", f"{prefix}/root", f"{prefix}/root:/{{path*}}"):
        _bind(base)
    plugin.route("GET", f"{prefix}/root/search(q='{{query*}}')", perm=READ, family="sharepoint")(
        _search
    )
    plugin.route("GET", f"{prefix}/root/delta", perm=READ, family="sharepoint")(_delta)
    plugin.route("GET", f"{prefix}/items/{{item-id}}/delta", perm=READ, family="sharepoint")(
        _delta_not_root
    )


def _bind(base: str) -> None:
    """The item routes of one addressing form (`items/{id}`, `root`, `root:/{path}`); the
    suffix routes come first so that the plain item route does not swallow them."""

    sep = ":/" if "{path*}" in base else "/"
    family = "sharepoint"
    plugin.route("GET", f"{base}{sep}children", perm=READ, family=family)(_children)
    plugin.route("POST", f"{base}{sep}children", perm=WRITE, family=family)(_create_child)
    plugin.route("GET", f"{base}{sep}content", perm=READ, family=family)(_content_redirect)
    plugin.route("PUT", f"{base}{sep}content", perm=WRITE, family=family)(_put_content)
    plugin.route("POST", f"{base}{sep}createUploadSession", perm=WRITE, family=family)(
        _create_session
    )
    plugin.route("GET", base, perm=READ, family=family)(_get_item)


def _get_item(ctx: Ctx) -> Any:
    drive = _drive(ctx)
    _authorise(ctx, drive, write=False)
    return _public(ctx, drive, _locate(ctx, drive))


_ITEM_PROPERTIES = (
    "id",
    "name",
    "size",
    "webUrl",
    "eTag",
    "cTag",
    "createdDateTime",
    "lastModifiedDateTime",
    "file",
    "folder",
    "parentReference",
    "@microsoft.graph.downloadUrl",
    "deleted",
)


def _children(ctx: Ctx) -> Any:
    store: FilesStore = ctx.store
    drive = _drive(ctx)
    _authorise(ctx, drive, write=False)
    folder = _locate(ctx, drive)
    if "folder" not in folder:
        raise _item_error("itemNotFound", "The item is not a folder.")
    return collection(
        ctx,
        store.children(drive, folder["id"]),
        default_top=200,
        max_top=200,
        known=_ITEM_PROPERTIES,
        type_name="microsoft.graph.driveItem",
        context="drives('x')/items/children",
        shape=lambda item: _public(ctx, drive, item),
    )


def _create_child(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    drive = _drive(ctx)
    _authorise(ctx, drive, write=True)
    parent = _locate(ctx, drive)
    body = ctx.json()
    if not isinstance(body, dict) or "folder" not in body or not body.get("name"):
        raise GraphError(400, "invalidRequest", "A new folder needs a name and a `folder` facet.")
    name = str(body["name"])
    _check_name(name)
    conflict = _conflict(body.get("@microsoft.graph.conflictBehavior"), "fail")
    existing = store.find_child(drive, parent["id"], name)
    if existing is not None:
        if conflict == "fail":
            raise GraphError(409, "nameAlreadyExists", "The specified item name already exists.")
        if conflict == "replace" and "folder" in existing:
            return Reply(200, _public(ctx, drive, existing))
        name = _unique(store, drive, parent["id"], name)
    return Reply(201, _public(ctx, drive, store._make(drive, parent["id"], name, folder=True)))


def _content_redirect(ctx: Ctx) -> Reply:
    drive = _drive(ctx)
    _authorise(ctx, drive, write=False)
    item = _locate(ctx, drive)
    if "file" not in item:
        raise _item_error("itemNotFound", "The item is not a file.")
    token = _download_token(ctx, drive, item)
    return Reply(302, None, {"Location": _download_url(ctx, token)})


def _put_content(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    drive = _drive(ctx)
    _authorise(ctx, drive, write=True)
    body = ctx.request.body
    if store.simple_put_limit is not None and len(body) > store.simple_put_limit:
        raise GraphError(
            413,
            "requestEntityTooLarge",
            f"The request body is larger than {store.simple_put_limit} bytes; use an upload session.",
        )
    conflict = _conflict(ctx.request.q("@microsoft.graph.conflictBehavior"), "replace")
    if "path" not in ctx.params:  # an item by id, or the root
        existing = _locate(ctx, drive)
        if "file" not in existing:
            raise _item_error("invalidRequest", "Content can only be uploaded to a file.", 400)
        parent = drive.items[existing["_parent"]]
        name = existing["name"]
    else:
        folder_path, _, name = ctx.params["path"].strip(":/").rpartition("/")
        _check_name(name)
        parent = store.folder_path(drive, folder_path, create=True) or drive.items[drive.root_id]
        if "folder" not in parent:
            raise _item_error("nameAlreadyExists", f"'{parent['name']}' is a file.", 409)
    return _store_file(
        ctx, drive, parent, name, body, conflict, if_match=ctx.request.header("if-match")
    )


@plugin.route("GET", "/_download/{token}", auth="none")
def download(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    if ctx.request.header("authorization") is not None:
        raise GraphError(
            401,
            "InvalidAuthenticationToken",
            "The pre-authenticated download URL must be called without an Authorization header.",
        )
    entry = store.downloads.get(ctx.params["token"])
    if entry is None or ctx.clock.time() >= entry[2]:
        raise GraphError(
            401, "InvalidAuthenticationToken", "The download URL is not valid or has expired."
        )
    drive, item_id = store.drives[entry[0]], entry[1]
    item = drive.items.get(item_id)
    if item is None:
        raise _item_error("itemNotFound", "The file no longer exists.")
    return Reply(200, bytes(item["_content"]), content_type="application/octet-stream")


# --------------------------------------------------------------------------- upload sessions


def _create_session(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    drive = _drive(ctx)
    _authorise(ctx, drive, write=True)
    body = ctx.json() or {}
    properties = body.get("item", {}) if isinstance(body, dict) else {}
    if not isinstance(properties, dict):
        raise GraphError(400, "invalidRequest", "`item` must be an object.")
    conflict = _conflict(properties.get("@microsoft.graph.conflictBehavior"), "replace")
    if "item_id" in ctx.params:
        existing: dict[str, Any] | None = _locate(ctx, drive)
        assert existing is not None
        parent_id, name = existing["_parent"], existing["name"]
    else:
        folder_path, _, name = ctx.params["path"].strip(":/").rpartition("/")
        _check_name(name)
        parent = store.folder_path(drive, folder_path, create=True)
        assert parent is not None
        parent_id = parent["id"]
        existing = store.find_child(drive, parent_id, name)
    _check_session_tags(ctx, existing)
    session_id = secrets.token_urlsafe(24)
    expires = ctx.clock.time() + 86_400
    total = properties.get("fileSize")
    store.sessions[session_id] = _Session(
        drive.id, parent_id, name, conflict, int(total) if total is not None else None, expires
    )
    return Reply(
        200,
        {
            "uploadUrl": f"{ctx.base}/_upload/{session_id}",
            "expirationDateTime": ctx.clock.iso(ctx.clock.now() + timedelta(seconds=86_400)),
            "nextExpectedRanges": ["0-"],
        },
    )


def _check_session_tags(ctx: Ctx, existing: dict[str, Any] | None) -> None:
    if_match, if_none_match = ctx.request.header("if-match"), ctx.request.header("if-none-match")
    current = existing["eTag"] if existing is not None else None
    if (if_match is not None and if_match != current) or (
        if_none_match is not None and if_none_match == current
    ):
        raise GraphError(
            412, "preconditionFailed", "The precondition (If-Match) failed: the item changed."
        )


def _session(ctx: Ctx) -> _Session:
    store: FilesStore = ctx.store
    if ctx.request.header("authorization") is not None:
        raise GraphError(
            401,
            "InvalidAuthenticationToken",
            "An upload session URL is pre-authenticated; send no Authorization header.",
        )
    session = store.sessions.get(ctx.params["session"])
    if session is None or session.done or ctx.clock.time() >= session.expires:
        raise _item_error("itemNotFound", "The upload session was not found or has expired.")
    return session


def _expected(ctx: Ctx, session: _Session) -> dict[str, Any]:
    return {
        "expirationDateTime": ctx.clock.iso(
            ctx.clock.now() + timedelta(seconds=session.expires - ctx.clock.time())
        ),
        "nextExpectedRanges": [f"{len(session.received)}-"],
    }


@plugin.route("GET", "/_upload/{session}", auth="none")
def upload_status(ctx: Ctx) -> Any:
    return _expected(ctx, _session(ctx))


@plugin.route("DELETE", "/_upload/{session}", auth="none")
def upload_cancel(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    _session(ctx)
    del store.sessions[ctx.params["session"]]
    return Reply(204)


@plugin.route("PUT", "/_upload/{session}", auth="none")
def upload_fragment(ctx: Ctx) -> Reply:
    store: FilesStore = ctx.store
    session = _session(ctx)
    header = ctx.request.header("content-range")
    match = _RANGE.match(header or "")
    if match is None:
        raise GraphError(
            400,
            "invalidRequest",
            "A Content-Range header of the form 'bytes a-b/total' is required.",
        )
    first, last, total = (int(g) for g in match.groups())
    length = last - first + 1
    final = last + 1 == total
    if first > last or last >= total:
        raise GraphError(416, "invalidRange", "The Content-Range header is not valid.")
    if len(ctx.request.body) != length:
        raise GraphError(
            400, "invalidRequest", "The Content-Length does not match the Content-Range."
        )
    if session.total is not None and session.total != total:
        raise GraphError(416, "invalidRange", "The total size differs from the first fragment.")
    session.total = total
    if first != len(session.received):
        raise GraphError(
            416,
            "invalidRange",
            "The fragment does not start at the next expected byte.",
            extra={"nextExpectedRanges": [f"{len(session.received)}-"]},
        )
    if length > MAX_FRAGMENT:
        raise GraphError(413, "requestEntityTooLarge", "A fragment may be at most 60 MiB.")
    if not final and length % CHUNK:
        raise GraphError(
            400,
            "invalidRequest",
            "Fragment size must be a multiple of 320 KiB (327,680 bytes) except for the last.",
        )
    session.received.extend(ctx.request.body)
    if not final:
        return Reply(202, _expected(ctx, session))
    drive = store.drives[session.drive_id]
    parent = drive.items[session.parent_id]
    try:
        reply = _store_file(
            ctx, drive, parent, session.name, bytes(session.received), session.conflict
        )
    finally:
        session.done = True
    return reply


# --------------------------------------------------------------------------- search


def _search(ctx: Ctx) -> Any:
    """Name search (the real service also matches content and metadata)."""

    drive = _drive(ctx)
    principal = ctx.principal
    assert principal is not None
    if principal.kind == "app" and not principal.grants & set(_FILE_READ_ALL):
        raise GraphError(403, "accessDenied", "Access denied")
    _authorise(ctx, drive, write=False)
    words = ctx.params["query"].replace("''", "'").lower().split()
    found = [
        item
        for item in sorted(drive.items.values(), key=lambda i: i["name"].lower())
        if item["id"] != drive.root_id and all(word in item["name"].lower() for word in words)
    ]
    return collection(
        ctx,
        found,
        default_top=200,
        max_top=200,
        known=_ITEM_PROPERTIES,
        type_name="microsoft.graph.driveItem",
        context="drives('x')/root/search",
        shape=lambda item: {**_public(ctx, drive, item), "searchResult": {}},
    )


# --------------------------------------------------------------------------- delta


def _delta(ctx: Ctx) -> Any:
    drive = _drive(ctx)
    _authorise(ctx, drive, write=False)
    return delta_round(
        ctx,
        drive.feed,
        shape=lambda item: _public(ctx, drive, item) if "deleted" not in item else item,
    )


def _delta_not_root(ctx: Ctx) -> Any:
    raise GraphError(
        400,
        "invalidRequest",
        "Delta is only supported on the root of a SharePoint or OneDrive for Business drive.",
    )


for _prefix in ("/drives/{drive-id}", "/sites/{site-id}/drive"):
    _register(_prefix)

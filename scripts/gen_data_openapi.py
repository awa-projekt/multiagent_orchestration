"""Write the OpenAPI fragment of the data endpoints to docs/develop/data-api.openapi.json.

The fragment holds only the paths of the upload, profile and build endpoints (and the schemas they
use): the contract a web frontend codes against. Run it from the repository root after changing
a request or response shape of src/awa/server/_data_api.py or _builds_api.py:

    python scripts/gen_data_openapi.py            # rewrite the file
    python scripts/gen_data_openapi.py --check    # exit 1 when the file is out of date

tests/unit/test_server_data_openapi.py fails while the checked-in file differs from the code.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

TARGET = Path(__file__).resolve().parents[1] / "docs" / "develop" / "data-api.openapi.json"
_PREFIXES = ("/v1/uploads", "/v1/data/profile", "/v1/builds")


def _references(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                found.add(item.rsplit("/", 1)[-1])
            else:
                _references(item, found)
    elif isinstance(value, list):
        for item in value:
            _references(item, found)


def build() -> dict[str, Any]:
    """The OpenAPI fragment of an app with the data endpoints (nothing is started or stored)."""

    from awa import Agent
    from awa.server import DataOptions, create_app
    from awa.testing import FunctionChatModel

    with tempfile.TemporaryDirectory() as folder:
        app = create_app(
            Agent("fragment", model=FunctionChatModel(lambda call: "")),
            title="awa data endpoints",
            data=DataOptions(root=folder),
        )
        full = app.openapi()
    paths = {path: item for path, item in full["paths"].items() if path.startswith(_PREFIXES)}
    schemas = full.get("components", {}).get("schemas", {})
    direct: set[str] = set()
    _references(paths, direct)
    needed: set[str] = set()
    pending = list(direct)
    while pending:
        name = pending.pop()
        if name not in needed:
            needed.add(name)
            more: set[str] = set()
            _references(schemas[name], more)
            pending += list(more)
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "awa data endpoints",
            "version": "1",
            "description": "Uploads, profiles and builds of awa.server (create_app(data=DataOptions(...))).",
        },
        "paths": dict(sorted(paths.items())),
        "components": {"schemas": {name: schemas[name] for name in sorted(needed)}},
    }


def main(argv: list[str]) -> int:
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != text:
            print(f"{TARGET} is out of date; run python scripts/gen_data_openapi.py")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""`awa` with a scripted model and a fake network, for the offline run of benchmark scenario 8.

`python mcp_launcher.py 02_supplier_orders mcp serve blueprint.yaml --tools tools.py` is the real
`awa mcp serve blueprint.yaml --tools tools.py`, except that the model named `default` plays the
script of that scenario (`offline.py`) and the keyless endpoints answer from canned data. Run it in
the folder that holds `blueprint.yaml`. Live runs start `python -m awa` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import offline

import awa.connectors  # noqa: F401 - imported first, so the fake network can patch what they use
import awa.toolkits  # noqa: F401
from awa.blueprints import Blueprint
from awa.cli import main
from awa.models import ModelRegistry

if __name__ == "__main__":
    slug, argv = sys.argv[1], sys.argv[2:]
    script = offline.script_for(slug, Blueprint.from_yaml(Path("blueprint.yaml")))
    ModelRegistry.default().register("default", script.model())
    with offline.network():
        sys.exit(main(argv))

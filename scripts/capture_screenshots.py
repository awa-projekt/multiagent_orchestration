"""Re-create the pictures of the README and the docs: one invoice desk, from first screen to trace.

Everything runs offline and reproducibly: the models are scripted fakes
(`awa.testing.FunctionChatModel`), provider settings are removed from the environment, and the
work folder is short and neutral (`C:/awa-guide-work` on Windows, `/tmp/awa-guide-work`
elsewhere, or `--workdir`). The web app builds its project through the real build loop; the
guide's commands run for real and their output is rendered as a terminal window. A headless
Chrome or Edge (Chrome DevTools protocol, no extra package) takes every picture, and every page
is checked for secret values and personal paths before its image is written.

Groups (`--only`):

- `guide-web` (also `ui`): the web app, driven through the guided tour. The guide pictures carry
  numbered callouts (`Mark`: a CSS selector and a legend line, so a missing element stops the
  capture with its name) and a legend file each; the plain `ui-*.png` pictures are the same
  moments without callouts in a fixed 1280x800 frame (`PLAIN_TWINS`), the phone ones at 390x844.
- `guide-terminal`: a terminal picture for every guide block marked `shot=` (the guide is
  replayed through its test harness to get the output).
- `tracing`: the same invoice desk in a local Langfuse (`tracing-*.png`), after
  `scripts/langfuse_showcase.py` sent the run.
- `files`: the guide's sample files (docs/guide/files).

    python scripts/capture_screenshots.py --redact                    # everything but tracing
    python scripts/capture_screenshots.py --redact --only ui          # the web app
    python scripts/capture_screenshots.py --only tracing --redact --langfuse-login <compose .env>

The pictures show the awa that Python imports, and `awa doctor` checks how it is installed. So run
the script with the Python of a clean install of this tree (a fresh environment, the tree installed
into it, not editable and without PYTHONPATH); the script stops before any capture when the awa it
imports is not that (`install_problem`), and `CLEAN_INSTALL_HINT` gives the commands.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "screenshots"
GUIDE_FILES = ROOT / "docs" / "guide" / "files"


@dataclass(frozen=True)
class Frame:
    """The fixed size of one picture type: every picture of the type has exactly this size."""

    width: int
    height: int
    mobile: bool = False


APP_FRAME = Frame(1280, 800)
"""The web app on a desktop screen (`ui-<page>.png`)."""
PHONE_FRAME = Frame(390, 844, mobile=True)
"""The web app on a phone (`ui-<page>-phone.png`)."""
WIDTH = APP_FRAME.width
MAX_PICTURE_BYTES = 250 * 1024
"""No picture of the docs is larger; `optimise_png` keeps every one at or under it."""
GROUPS = ("ui", "guide", "files", "tracing")
GUIDE_PARTS = ("guide-web", "guide-terminal")  # `guide` is both; each can run alone

# ---------------------------------------------------------------------- the demo process

INVOICES = {
    "INV-2041": ("Nordwind Office GmbH", "PO-7710", 1180.00, "open"),
    "INV-2042": ("Alpen Maschinenbau AG", "PO-7725", 7450.00, "open"),
    "INV-2043": ("Nordwind Papier KG", "PO-7731", 342.50, "paid"),
}

ACCEPTANCE: dict[str, Any] = {
    "criteria": [
        "Open invoices under 5,000 EUR are released for payment without asking anyone.",
        "Invoices from 5,000 EUR are released only after the head of finance approves.",
        "Paid invoices are never released again.",
    ],
    "systems": [
        {
            "name": "Invoice ledger",
            "key": "ledger",
            "description": "Supplier invoices with their purchase orders and payment status.",
            "kind": "database",
            "tables": [
                {
                    "name": "invoices",
                    "description": "One supplier invoice.",
                    "columns": [
                        {"name": "invoice_id"},
                        {"name": "supplier"},
                        {"name": "po_number"},
                        {"name": "amount_eur", "type": "real"},
                        {"name": "status"},
                    ],
                    "rows": [
                        f"{key}, {s}, {po}, {amount:.2f}, {status}"
                        for key, (s, po, amount, status) in INVOICES.items()
                    ],
                }
            ],
        }
    ],
    "approvals": [
        {
            "name": "finance_approval",
            "description": "The head of finance approves payments from 5,000 EUR.",
            "arguments": [
                {"name": "invoice_id", "type": "string", "description": "The invoice number."},
                {"name": "amount_eur", "type": "number", "description": "The amount in EUR."},
            ],
            "when": "amount_eur >= 5000",
            "examples": [
                {"arguments": '{"invoice_id": "INV-2042", "amount_eur": 7450}', "asks": True},
                {"arguments": '{"invoice_id": "INV-2041", "amount_eur": 1180}', "asks": False},
            ],
        }
    ],
    "cases": [
        {
            "id": "small-invoice",
            "input": "Please release invoice INV-2041 for payment.",
            "expectations": [
                {"kind": "tool_called", "value": "ledger_query"},
                {"kind": "no_approval", "value": "finance_approval"},
                {"kind": "contains", "value": "INV-2041"},
            ],
        },
        {
            "id": "large-invoice",
            "input": "Please release invoice INV-2042 for payment.",
            "expectations": [
                {"kind": "approval", "value": "finance_approval"},
                {"kind": "contains", "value": "INV-2042"},
            ],
            "decisions": [{"action": "approve", "tool": "finance_approval"}],
        },
        {
            "id": "paid-invoice",
            "input": "Please pay invoice INV-2043.",
            "expectations": [
                {"kind": "no_approval", "value": "finance_approval"},
                {"kind": "contains", "value": "paid"},
            ],
        },
    ],
}

DESIGN: dict[str, Any] = {
    "name": "invoice_desk",
    "description": "Checks supplier invoices against the ledger and releases them for payment.",
    "entrypoint": "workflow",
    "agents": [
        {
            "name": "invoice_checker",
            "description": "Looks the invoice up in the ledger and checks its purchase order.",
            "instructions": (
                "Look the invoice up in the invoices table with ledger_query. Report the "
                "supplier, the purchase order, the amount in EUR and the status."
            ),
            "tools": ["connector:ledger.query"],
        },
        {
            "name": "payment_clerk",
            "description": "Releases checked, open invoices for payment.",
            "instructions": (
                "For an open invoice call finance_approval with its number and amount, then "
                "confirm that it is released for payment. Never release a paid invoice again."
            ),
            "tools": ["finance_approval"],
        },
    ],
    "workflow": [{"agents": ["invoice_checker"]}, {"agents": ["payment_clerk"]}],
}


LIVE_QUERY = "invoice_ledger_query"
"""The ledger's query tool when the reader declared it under Your data as "Invoice ledger"."""

ACCEPTANCE_LIVE: dict[str, Any] = {
    **{k: v for k, v in ACCEPTANCE.items() if k != "systems"},
    "cases": [
        {
            **case,
            "expectations": [
                {**e, "value": LIVE_QUERY} if e["value"] == "ledger_query" else e
                for e in case["expectations"]
            ],
        }
        for case in ACCEPTANCE["cases"]
    ],
}
DESIGN_LIVE: dict[str, Any] = {
    **DESIGN,
    "agents": [
        {
            **DESIGN["agents"][0],
            "instructions": DESIGN["agents"][0]["instructions"].replace("ledger_query", LIVE_QUERY),
            "tools": [LIVE_QUERY],
        },
        DESIGN["agents"][1],
    ],
}


def _system_prompt(call: Any) -> str:
    from langchain_core.messages import SystemMessage

    first = call.messages[0] if call.messages else None
    return str(first.content) if isinstance(first, SystemMessage) else ""


def _tool_results(call: Any) -> list[Any]:
    from langchain_core.messages import ToolMessage

    return [m for m in call.messages if isinstance(m, ToolMessage)]


def desk_model() -> Any:
    """Test designer, architect and the two invoice-desk agents in one scripted model."""

    from langchain_core.messages import HumanMessage

    from awa.blueprints import AcceptanceDesign, BlueprintDesign
    from awa.testing import FunctionChatModel, structured_output, tool_call

    def respond(call: Any) -> Any:
        system = _system_prompt(call)
        asked = " ".join(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        live = "## Your systems and interfaces" in asked  # the reader declared the ledger
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, ACCEPTANCE_LIVE if live else ACCEPTANCE)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, DESIGN_LIVE if live else DESIGN)
        found = re.search(r"INV-\d{4}", asked)
        invoice = found.group(0) if found else ""
        results = [str(m.content) for m in _tool_results(call)]
        known = INVOICES.get(invoice)
        query_tool = next((t for t in (LIVE_QUERY, "ledger_query") if t in call.tool_names), "")
        if query_tool:
            if not results:
                query = "SELECT supplier, po_number, amount_eur, status FROM invoices WHERE invoice_id = ?"
                return tool_call(query_tool, {"query": query, "parameters": [invoice]})
            if known is None:
                return f"There is no invoice {invoice or 'with this number'} in the ledger."
            supplier, po, amount, status = known
            return (
                f"Invoice {invoice} from {supplier}: {amount:,.2f} EUR, purchase order {po}, "
                f"status {status}."
            )
        if "finance_approval" in call.tool_names:
            if known is None:
                return "There is no invoice to release."
            supplier, _, amount, status = known
            if status == "paid":
                return f"Invoice {invoice} is already paid; nothing to release."
            if not results:
                return tool_call("finance_approval", {"invoice_id": invoice, "amount_eur": amount})
            if '"approved": true' in results[-1]:
                return (
                    f"Invoice {invoice} from {supplier} ({amount:,.2f} EUR) is released for "
                    "payment."
                )
            return f"Invoice {invoice} stays on hold: {results[-1]}"
        return "I handle supplier invoices only."

    return FunctionChatModel(respond)


FILES_PROCESS = (
    "Supplier invoices arrive as PDF files. Check each invoice against the open purchase orders "
    "in purchase_orders.csv: the purchase order must be open and the amount must match. A "
    "matching invoice is released for payment. From 5,000 EUR the head of finance must approve "
    "the payment first."
)

FILES_ACCEPTANCE: dict[str, Any] = {
    "criteria": [
        "An invoice whose purchase order is open and whose amount matches is released for payment.",
        "An invoice of 5,000 EUR or more is released only after the head of finance approved it.",
        "The decision and its reason are written to the output folder.",
    ],
    "data_notes": [
        {
            "file": "INV-2041.pdf",
            "role": "input",
            "description": "A supplier invoice as PDF with a text layer.",
            "handling": "Read the text, take the purchase order number and the amount due.",
        },
        {
            "file": "INV-2042.pdf",
            "role": "input",
            "description": "A supplier invoice as PDF with a text layer.",
            "handling": "Read the text, take the purchase order number and the amount due.",
        },
        {
            "file": "purchase_orders.csv",
            "role": "reference",
            "description": "The open purchase orders: number, vendor, amount and status.",
            "handling": "Look the purchase order up by its number; the amount must match.",
        },
    ],
    "approvals": [
        {
            "name": "finance_approval",
            "description": "The head of finance approves payments from 5,000 EUR.",
            "arguments": [
                {"name": "po", "type": "string", "description": "The purchase order number."},
                {"name": "amount_eur", "type": "number", "description": "The amount in EUR."},
            ],
            "when": "amount_eur >= 5000",
            "examples": [
                {"arguments": '{"po": "PO-7725", "amount_eur": 7450}', "asks": True},
                {"arguments": '{"po": "PO-7710", "amount_eur": 1180}', "asks": False},
            ],
        }
    ],
    "cases": [
        {
            "id": "small-invoice",
            "input": "Check the invoice in INV-2041.pdf and release it for payment.",
            "files": ["INV-2041.pdf", "purchase_orders.csv"],
            "expectations": [
                {"kind": "tool_called", "value": "files_read_pdf"},
                {"kind": "no_approval", "value": "finance_approval"},
                {"kind": "approval", "value": "output_write_file"},
                {"kind": "contains", "value": "PO-7710"},
            ],
            "decisions": [{"action": "approve", "tool": "output_write_file"}],
        },
        {
            "id": "large-invoice",
            "input": "Check the invoice in INV-2042.pdf and release it for payment.",
            "files": ["INV-2042.pdf", "purchase_orders.csv"],
            "expectations": [
                {"kind": "approval", "value": "finance_approval"},
                {"kind": "approval", "value": "output_write_file"},
                {"kind": "contains", "value": "PO-7725"},
            ],
            "decisions": [
                {"action": "approve", "tool": "finance_approval"},
                {"action": "approve", "tool": "output_write_file"},
            ],
        },
    ],
}

FILES_DESIGN: dict[str, Any] = {
    "name": "invoice_check",
    "description": "Checks supplier invoices against the open purchase orders.",
    "entrypoint": "agent",
    "agents": [
        {
            "name": "checker",
            "description": "Checks an invoice against its purchase order and releases it.",
            "instructions": (
                "Read the invoice PDF named in the request with files_read_pdf. Look its "
                "purchase order up in purchase_orders.csv with files_query_csv (columns po, vendor, "
                "amount, status; the delimiter is a comma). The purchase order must be open and "
                "the amount must match. For a matching invoice call finance_approval with the "
                "purchase order and the amount, then write the decision and its reason to a text "
                "file with output_write_file."
            ),
            "tools": [
                "connector:files.read_pdf",
                "connector:files.query_csv",
                "connector:output.write_file",
                "finance_approval",
            ],
        }
    ],
}


def files_model() -> Any:
    """The test designer, the architect and the invoice checker of the data-first chapters."""

    from langchain_core.messages import HumanMessage

    from awa.blueprints import AcceptanceDesign, BlueprintDesign
    from awa.testing import FunctionChatModel, structured_output, tool_call

    def respond(call: Any) -> Any:
        system = _system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, FILES_ACCEPTANCE)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, FILES_DESIGN)
        asked = " ".join(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        found = re.search(r"INV-\d{4}", asked)
        invoice = found.group(0) if found else ""
        known = {"INV-2041": ("PO-7710", 1180.0), "INV-2042": ("PO-7725", 7450.0)}.get(invoice)
        results = {m.name: str(m.content) for m in _tool_results(call)}
        if known is None:
            return "I check the invoices of the folder invoice-files."
        po, amount = known
        if "files_read_pdf" not in results:
            return tool_call("files_read_pdf", {"path": f"{invoice}.pdf"})
        if "files_query_csv" not in results:
            return tool_call(
                "files_query_csv", {"path": "purchase_orders.csv", "where": f"po == '{po}'"}
            )
        if "finance_approval" not in results and "finance_approval" in call.tool_names:
            return tool_call("finance_approval", {"po": po, "amount_eur": amount})
        if "output_write_file" not in results:
            line = (
                f"{invoice}: purchase order {po} is open and the amount {amount:,.2f} EUR matches."
            )
            return tool_call("output_write_file", {"path": f"{invoice}.txt", "content": line})
        return f"{invoice}: purchase order {po} matches {amount:,.2f} EUR; released for payment."

    return FunctionChatModel(respond)


def bench_model(slug: str, project: Path) -> tuple[Any, Any]:
    """The scripted model of scenario `slug` of the core benchmark (examples/benchmarks/core) for
    the blueprint in `project`, and the context manager of its fake network. The scripts read the
    real files with the builtin tools and answer like a careful model."""

    import importlib.util

    from awa.blueprints import Blueprint

    path = ROOT / "examples" / "benchmarks" / "core" / "offline.py"
    spec = importlib.util.spec_from_file_location("core_benchmark_offline", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    script = module.script_for(slug, Blueprint.from_yaml(project / "blueprint.yaml"))
    return script.model(), module.network


MODELS: dict[str, Callable[[], Any]] = {"desk": desk_model, "files": files_model}


def scripted_registry(name: str, project: Path | None = None) -> Any:
    """A model registry whose profiles all resolve to the scripted model `name`: `desk`, `files`,
    or `bench:<scenario>` (for the blueprint in `project`; `intake` is `bench:04_document_intake`).
    `registry.network` is the context manager of the fake network, or None."""

    from awa.models import ModelRegistry

    name = "bench:04_document_intake" if name == "intake" else name
    network = None
    if name.startswith("bench:"):
        model, network = bench_model(name.removeprefix("bench:"), project or Path.cwd())
    else:
        model = MODELS[name]()
    registry = ModelRegistry()
    for profile in ("default", "fast", "strong"):
        registry.register(profile, model)
    registry.scripted = model  # type: ignore[attr-defined]
    registry.network = network  # type: ignore[attr-defined]
    return registry


def _install_model(name: str, project: Path | None = None) -> Any:
    from awa.models import ModelRegistry

    registry = scripted_registry(name, project)
    ModelRegistry._default = registry  # every blueprint profile resolves to the fake
    return registry.scripted, registry


# ---------------------------------------------------------------------- environment and privacy


def demo_folder(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path("C:/demo") if os.name == "nt" else Path("/tmp/demo")


_KEEP = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PYTHONPATH",
}  # fmt: skip


def clean_environment() -> dict[str, str]:
    """Only operating-system variables: no provider keys, endpoints or AWA_* settings."""

    env = {k: v for k, v in os.environ.items() if k.upper() in _KEEP}
    scripts = str(Path(sys.executable).parent)
    env["PATH"] = scripts + os.pathsep + env.get("PATH", "")  # `awa` resolves to this install
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", COLUMNS="92", NO_COLOR="1", TERM="dumb")
    return env


def secret_values() -> dict[str, str]:
    """Values that must never appear in an image (to their variable names): the repository
    .env and secret-like environment variables."""

    pairs = list(os.environ.items())
    dotenv = ROOT / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                pairs.append((name.strip(), value.strip().strip("'\"")))
    pattern = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|ENDPOINT|DSN|CONNECTION|DEPLOYMENT|URL|HOST")
    return {v: k for k, v in pairs if pattern.search(k.upper()) and len(v) >= 6}


class Privacy:
    """Shortens local paths to neutral ones and refuses text with a secret or personal path."""

    def __init__(self, work: Path) -> None:
        self.secrets = secret_values()
        home = Path.home()
        venv = Path(sys.prefix)
        pairs = [
            (venv, work / ".venv"),
            (ROOT, work / "awa"),
            (Path(tempfile.gettempdir()), work / "tmp"),
            (home, Path("~")),
        ]
        self.replacements: list[tuple[str, str]] = []
        for old, new in pairs:
            for text in {str(old), old.as_posix()}:
                self.replacements.append((text, str(new)))
        self.forbidden = [home.name] if len(home.name) >= 4 else []

    def clean(self, text: str) -> str:
        for old, new in self.replacements:
            text = re.compile(re.escape(old), re.IGNORECASE).sub(new.replace("\\", "\\\\"), text)
        return text

    def check(self, text: str, what: str) -> None:
        for value, name in self.secrets.items():
            if value in text:
                raise SystemExit(f"{what}: the value of {name} would be visible; nothing written")
        for name in self.forbidden:
            if re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE):
                raise SystemExit(
                    f"{what}: the user name {name!r} would be visible; nothing written"
                )


# ---------------------------------------------------------------------- annotated images (guide)

ACCENT = "#e8590c"  # the one color of every badge and highlight in the guide
GUIDE_OUT = OUT / "guide"


@dataclass(frozen=True)
class Mark:
    """One numbered callout of an annotated image, and its line in the legend under it.

    In a browser page `target` is a CSS selector; `text` picks the match that contains it when
    several elements match; `corner` (`tl`, `tr`, `bl`, `br`) is where the numbered badge sits and
    `box` draws the highlight rectangle. In a terminal image `target` is a piece of the line to
    highlight (the first line that contains it and is not marked yet).
    """

    target: str
    label: str
    text: str = ""
    corner: str = "tl"
    box: bool = True


_MARK_JS = """
((marks, accent) => {
  document.querySelectorAll('.awa-mark').forEach((e) => e.remove());
  const missing = [];
  const bottoms = [];
  const layer = document.createElement('div');
  layer.className = 'awa-mark';
  layer.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;overflow:visible;z-index:2147483000;pointer-events:none';
  marks.forEach((mark, index) => {
    const found = [...document.querySelectorAll(mark.target)].filter(
      (e) => (!mark.text || e.textContent.includes(mark.text)) && e.getBoundingClientRect().width > 0);
    if (!found.length) { missing.push(index); return; }
    const r = found[0].getBoundingClientRect();
    const x = r.left + scrollX, y = r.top + scrollY;
    bottoms.push(y + r.height + 3);
    if (mark.box) {
      const box = document.createElement('div');
      box.style.cssText = `position:absolute;box-sizing:border-box;left:${x - 3}px;top:${y - 3}px;width:${r.width + 6}px;height:${r.height + 6}px;border:3px solid ${accent};border-radius:8px;background:${accent}1f`;
      layer.append(box);
    }
    const badge = document.createElement('div');
    const left = mark.corner.endsWith('r') ? x + r.width - 14 : x - 16;
    const top = mark.corner.startsWith('b') ? y + r.height - 14 : y - 16;
    badge.textContent = String(index + 1);
    badge.style.cssText = `position:absolute;box-sizing:border-box;left:${Math.max(left, 2)}px;top:${Math.max(top, 2)}px;width:28px;height:28px;border-radius:50%;background:${accent};color:#fff;font:700 15px/24px system-ui,sans-serif;text-align:center;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.45)`;
    layer.append(badge);
  });
  layer.dataset.bottom = String(Math.max(0, ...bottoms));
  document.body.append(layer);
  return missing;
})
"""


def _png_size(data: bytes) -> list[int]:
    return [int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")]


def record_image(name: str, marks: Sequence[Mark], png: bytes) -> None:
    """Write the legend file of an annotated guide image and list the image in `index.json`
    (its marks and size), so the docs and their test can find what every image shows."""

    index = GUIDE_OUT / "index.json"
    entries = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {}
    entries[name.removeprefix("guide/")] = {
        "marks": [m.label for m in marks],
        "size": _png_size(png),
    }
    index.write_text(
        json.dumps(dict(sorted(entries.items())), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    write_legend(name, marks)


def write_legend(name: str, marks: Sequence[Mark]) -> None:
    """Write (or remove) the legend file the docs include under an annotated image."""

    legend = OUT / f"{name}.legend.txt"
    if marks:
        lines = [f"{number}. {mark.label}" for number, mark in enumerate(marks, start=1)]
        legend.write_text(
            "*In the picture:*\n\n" + "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
    else:
        legend.unlink(missing_ok=True)


# ---------------------------------------------------------------------- headless browser (DevTools protocol)


def find_browser(explicit: str | None) -> str:
    if explicit:
        return explicit
    candidates = [
        shutil.which(name)
        for name in (
            "chrome",
            "google-chrome",
            "chromium",
            "chromium-browser",
            "msedge",
            "microsoft-edge",
        )
    ]
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("No Chrome or Edge found; pass --browser PATH")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


LIGHT_THEME_JS = (
    "document.documentElement.dataset.theme === 'dark'"
    " && (document.documentElement.dataset.theme = 'light');"
    " (t => t && (t.style.setProperty('backdrop-filter', 'none', 'important'),"
    " t.style.setProperty('background', 'var(--surface)', 'important')))"
    "(document.querySelector('.top'))"
)
"""Every picture shows the light theme (the web app's own switch is set back to light), and the
web app's sticky header is opaque, so the page scrolled under it does not shine through."""


class Page:
    """One browser tab driven over its DevTools websocket."""

    def __init__(self, url: str, privacy: Privacy) -> None:
        from websockets.sync.client import connect

        self._ws = connect(url, max_size=None, open_timeout=30)
        self._id = 0
        self.privacy = privacy
        self.send("Page.enable")
        self.send(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": "light"}]},
        )
        self.viewport(WIDTH, 800)

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._ws.recv(timeout=120))
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return dict(message.get("result", {}))

    def viewport(self, width: int, height: int, *, mobile: bool = False) -> None:
        self.width = width
        self.height = height
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
        )

    def frame_shot(self, name: str, selector: str, frame: Frame) -> Path:
        """Picture exactly `frame` of the page: the viewport at the frame's size, scrolled so that
        `selector` starts just below the sticky header (or the page top when it fits there)."""

        width, height = self.width, self.height
        self.viewport(frame.width, frame.height, mobile=frame.mobile)
        self.js(LIGHT_THEME_JS)
        self.js(
            "document.activeElement && document.activeElement.blur && document.activeElement.blur()"
        )
        try:
            place = self.js(
                f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
                " const top = document.querySelector('.top');"
                " return {top: el.getBoundingClientRect().top + scrollY,"
                " header: top ? top.getBoundingClientRect().height : 0,"
                " page: document.documentElement.scrollHeight}; })()"
            )
            y = frame_scroll(place["top"], place["header"], place["page"], frame)
            self.js(f"window.scrollTo(0, {y})")
            time.sleep(0.4)
            literals = REDACT_LITERALS
            if literals is not None:
                redact_page(self, literals)
            try:
                self.privacy.check(self.js("document.body.innerText") or "", name)
                data = self.send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "clip": {
                            "x": 0,
                            "y": float(self.js("scrollY")),
                            "width": frame.width,
                            "height": frame.height,
                            "scale": 1,
                        },
                    },
                )
            finally:
                if literals is not None:
                    unredact_page(self)
            path = write_picture(name, base64.b64decode(data["data"]))
        finally:
            self.js("window.scrollTo(0, 0)")
            self.viewport(width, height)
        return path

    def js(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JavaScript failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait(self, condition: str, what: str, timeout: float = 120, show: str = "") -> None:
        """Wait until `condition` holds; the failure names `what`, and, when given, the text of
        the element `show` (a selector), so a stuck page says what it shows instead."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.js(f"Boolean({condition})"):
                return
            time.sleep(0.25)
        shown = ""
        if show:
            text = self.js(f"(document.querySelector({json.dumps(show)}) || {{}}).innerText")
            shown = f"\nThe page shows ({show}): {text!r}"
        raise SystemExit(f"Timed out waiting for {what}{shown}")

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        time.sleep(0.3)
        self.wait("document.readyState === 'complete'", url)
        self.js("document.fonts ? document.fonts.ready.then(() => true) : true")
        time.sleep(0.4)

    def mark(self, name: str, marks: Sequence[Mark]) -> None:
        """Draw the numbered badges and highlight boxes of `marks` into the page."""

        data = [
            {"target": m.target, "text": m.text, "corner": m.corner, "box": m.box} for m in marks
        ]
        missing = self.js(f"({_MARK_JS.strip()})({json.dumps(data)}, {json.dumps(ACCENT)})")
        if missing:
            lost = ", ".join(
                f"{i + 1} ({marks[i].target!r}"
                + (f" with the text {marks[i].text!r}" if marks[i].text else "")
                + ")"
                for i in missing
            )
            raise SystemExit(
                f"{name}: mark {lost} matched no visible element; the page has changed, "
                "update its marks in scripts/capture_screenshots.py"
            )

    def shot(
        self,
        name: str,
        selector: str | None = None,
        *,
        pad: int = 0,
        max_height: int = 1700,
        top: bool = False,
        to: str | None = None,
        marks: Sequence[Mark] = (),
        legend: Sequence[Mark] = (),
    ) -> Path:
        """Screenshot `selector` (or the whole page); with `top`, the page from its top down to it,
        with `to`, the region from `selector` down to the bottom of the element `to`.

        `marks` are drawn first (numbered badges and highlight boxes) and written as the legend;
        `legend` is for marks the page already contains (a terminal image).
        """

        self.js(
            "document.activeElement && document.activeElement.blur && document.activeElement.blur()"
        )
        self.js(LIGHT_THEME_JS)
        literals = REDACT_LITERALS
        if literals is not None:
            redact_page(self, literals)
        try:
            return self._capture(name, selector, pad, max_height, top, to, marks, legend)
        finally:
            if literals is not None:
                unredact_page(self)

    def _capture(
        self,
        name: str,
        selector: str | None,
        pad: int,
        max_height: int,
        top: bool,
        to: str | None,
        marks: Sequence[Mark],
        legend: Sequence[Mark],
    ) -> Path:
        self.privacy.check(self.js("document.body.innerText") or "", name)
        if marks:
            self.mark(name, marks)
        box = self.js(
            "(() => { const el = %s; const r = el.getBoundingClientRect();"
            " return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}; })()"
            % (
                f"document.querySelector({json.dumps(selector)})"
                if selector
                else "document.documentElement"
            )
        )
        height = (
            min(int(box["y"] + box["h"]) + pad, max_height)
            if selector
            else min(int(self.js("document.documentElement.scrollHeight")), max_height)
        )
        clip = {"x": 0, "y": 0, "width": self.width, "height": height, "scale": 1}
        if selector and not top:
            clip = {
                "x": max(box["x"] - pad, 0),
                "y": max(box["y"] - pad, 0),
                "width": box["w"] + 2 * pad,
                "height": box["h"] + 2 * pad,
                "scale": 1,
            }
            if to:
                end = self.js(
                    f"document.querySelector({json.dumps(to)}).getBoundingClientRect().bottom + scrollY"
                )
                clip["height"] = end - clip["y"] + pad
        if marks:
            lowest = float(self.js("document.querySelector('.awa-mark').dataset.bottom"))
            if lowest > clip["y"] + clip["height"] + 1:
                raise SystemExit(
                    f"{name}: a marked element ends at {int(lowest)} px but the image ends at "
                    f"{int(clip['y'] + clip['height'])} px; raise max_height of this image"
                )
        data = self.send(
            "Page.captureScreenshot", {"format": "png", "clip": clip, "captureBeyondViewport": True}
        )
        if marks:
            self.js("document.querySelectorAll('.awa-mark').forEach((e) => e.remove())")
        png = base64.b64decode(data["data"])
        path = write_picture(name, png)
        if name.startswith("guide/"):
            record_image(name, marks or legend, png)
        return path

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


def frame_scroll(top: float, header: float, page: float, frame: Frame) -> int:
    """Where to scroll so an element at `top` starts 16 px below the sticky `header` of a `page`
    that high, without scrolling past the page's end (0: the page top)."""

    return int(max(0, min(top - header - 16, page - frame.height)))


def optimise_png(png: bytes, name: str, limit: int = MAX_PICTURE_BYTES) -> bytes:
    """The smallest PNG of the same picture that stays at or under `limit` bytes.

    First lossless: the image re-encoded at the highest compression, then, when it has at most 256
    colors, as an exact palette image. Only a picture still over the limit is reduced to a
    256-color palette without dithering (no visible loss on interface pictures). A picture that
    stays over the limit stops the capture, naming it.
    """

    import io

    from PIL import Image

    def encode(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    image = Image.open(io.BytesIO(png)).convert("RGB")
    candidates = [png, encode(image)]
    colors = image.getcolors(256)
    if colors is not None:
        palette = Image.new("P", (1, 1))
        flat = [
            int(c)
            for _count, color in colors
            for c in (color if isinstance(color, tuple) else (color,))
        ]
        palette.putpalette(flat + [0] * (768 - len(flat)))
        exact = image.quantize(palette=palette, dither=Image.Dither.NONE)
        candidates.append(encode(exact))
    best = min(candidates, key=len)
    if len(best) > limit and colors is None:
        best = encode(
            image.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        )
    if len(best) > limit:
        raise SystemExit(
            f"{name}: {len(best) // 1024} KB after optimizing, over {limit // 1024} KB; "
            "picture a smaller region"
        )
    return best


def write_picture(name: str, png: bytes) -> Path:
    """Optimise `png` and write it as `docs/assets/screenshots/<name>.png`."""

    path = OUT / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(optimise_png(png, name))
    print(f"  {path.relative_to(ROOT).as_posix()} ({path.stat().st_size // 1024} KB)")
    return path


PHONE_PAGES = {"a10-approvals": "inbox"}
"""Guide moments that are also pictured on a phone (`ui-<page>-phone.png`), by guide image."""


class Browser:
    def __init__(self, executable: str, privacy: Privacy) -> None:
        import httpx

        self.privacy = privacy
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="awa-shots-")
        self.process = subprocess.Popen(
            [
                executable, "--headless=new", f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}", "--remote-allow-origins=*", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
                "--force-color-profile=srgb", "--lang=en-US", f"--window-size={WIDTH},800",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self._http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=5)
        deadline = time.monotonic() + 30
        while True:
            try:
                self._http.get("/json/version").raise_for_status()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise SystemExit("The browser did not start") from None
                time.sleep(0.3)

    def page(self) -> Page:
        target = self._http.put("/json/new?about:blank").json()
        return Page(target["webSocketDebuggerUrl"], self.privacy)

    def close(self) -> None:
        self.process.terminate()
        with contextlib.suppress(Exception):
            self.process.wait(10)
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------- rendered pages (terminal, HTTP)

_STYLE = """
:root { color-scheme: light; }
body { margin: 0; padding: 24px; background: #f5f5f7; font-family: "Segoe UI", system-ui, sans-serif; }
.window { width: 940px; border-radius: 12px; overflow: hidden; border: 1px solid #e2e4e9; box-shadow: 0 1px 2px rgba(0,0,0,.06), 0 6px 20px rgba(0,0,0,.05); background: #fff; }
.bar { background: #f0f1f4; border-bottom: 1px solid #e2e4e9; padding: 9px 14px; display: flex; gap: 7px; align-items: center; }
.bar i { width: 11px; height: 11px; border-radius: 50%; display: inline-block; background: #ff5f57; }
.bar i:nth-child(2) { background: #febc2e; } .bar i:nth-child(3) { background: #28c840; }
.bar span { color: #5b5f68; font-size: 12px; margin-left: 10px; }
pre { margin: 0; padding: 14px 18px 18px; color: #1d1d1f; font: 13px/1.5 "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; white-space: pre-wrap; word-break: break-word; }
.cmd { color: #0a6cff; font-weight: 600; } .dim { color: #5b5f68; }
.panel { width: 940px; background: #fff; border-radius: 10px; box-shadow: 0 6px 24px rgba(20,24,60,.14); overflow: hidden; }
.panel h2 { margin: 0; padding: 12px 18px; font-size: 15px; background: #3f51b5; color: #fff; font-weight: 600; }
.panel h3 { margin: 0; padding: 12px 18px 4px; font-size: 13px; color: #3f51b5; font-family: "Cascadia Mono", Consolas, monospace; }
.panel pre { color: #1f2340; background: #f6f7fb; margin: 4px 18px 12px; border-radius: 6px; padding: 10px 12px; font-size: 12.5px; }
.method { font-weight: 700; color: #1b7f3b; } .ev { color: #3f51b5; font-weight: 600; }
.annot pre { padding-left: 46px; }
.l { display: block; position: relative; min-height: 1.5em; }
.hl { margin: 0 -18px 0 -46px; padding: 0 18px 0 46px; background: rgba(232,89,12,.12); box-shadow: inset 4px 0 0 #e8590c; }
.n { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); width: 22px; height: 22px; border-radius: 50%; background: #e8590c; color: #fff; font: 700 13px/22px "Segoe UI", system-ui, sans-serif; text-align: center; }
"""


def render_terminal(
    page: Page,
    name: str,
    blocks: list[tuple[str, str]],
    title: str = "Terminal",
    marks: Sequence[Mark] = (),
) -> Path:
    """Render commands and their output as a terminal window; `marks` highlight the lines that
    matter with a numbered badge and write the legend (see `Mark`)."""

    if marks:
        body, windowed = _marked_lines(name, blocks, marks), "window annot"
    else:
        body = "".join(
            f'<span class="cmd">$ {html.escape(cmd)}</span>\n{html.escape(out.rstrip())}\n'
            for cmd, out in blocks
        ).rstrip()
        windowed = "window"
    document = (
        f"<!doctype html><meta charset='utf-8'><style>{_STYLE}</style><div class='{windowed}' id='shot'>"
        f"<div class='bar'><i></i><i></i><i></i><span>{html.escape(title)}</span></div><pre>{body}</pre></div>"
    )
    return _show(page, name, document, marks)


def _marked_lines(name: str, blocks: list[tuple[str, str]], marks: Sequence[Mark]) -> str:
    rows: list[tuple[bool, str]] = []
    for command, output in blocks:
        rows.append((True, f"$ {command}"))
        rows += [(False, line) for line in output.rstrip().split("\n")]
    numbered: dict[int, int] = {}
    for number, mark in enumerate(marks, start=1):
        found = next(
            (i for i, (_, text) in enumerate(rows) if mark.target in text and i not in numbered),
            None,
        )
        if found is None:
            raise SystemExit(
                f"{name}: no line of the terminal output contains {mark.target!r} (mark {number}); "
                "the output has changed, update its marks in scripts/capture_screenshots.py"
            )
        numbered[found] = number
    lines = []
    for index, (is_command, text) in enumerate(rows):
        classes = "l" + (" cmd" if is_command else "") + (" hl" if index in numbered else "")
        badge = f'<b class="n">{numbered[index]}</b>' if index in numbered else ""
        lines.append(f'<span class="{classes}">{badge}{html.escape(text) or "&nbsp;"}</span>')
    return "".join(lines)


def _show(page: Page, name: str, document: str, marks: Sequence[Mark] = ()) -> Path:
    file = Path(tempfile.gettempdir()) / f"awa-shot-{name.replace('/', '-')}.html"
    file.write_text(document, encoding="utf-8")
    try:
        page.goto(file.as_uri())
        return page.shot(name, "#shot", pad=0, legend=marks)
    finally:
        file.unlink(missing_ok=True)


# ---------------------------------------------------------------------- the web app


@contextlib.contextmanager
def serve_app(app: Any, port: int) -> Iterator[None]:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise SystemExit(f"The web app did not start on 127.0.0.1:{port} (port in use?).")
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(10)


# ---------------------------------------------------------------------- the guide (docs/guide)

GUIDE_WIDTH = APP_FRAME.width
GUIDE_PROCESS = (
    "Supplier invoices arrive as PDF files. For each invoice, look it up in our invoice ledger by "
    "its invoice number and check its purchase order. An invoice that is already paid is never "
    "paid twice. An open invoice under 5,000 EUR is released for payment right away. From 5,000 "
    "EUR the head of finance must approve the payment first. If the invoice is not in the ledger, "
    "say so and do not release it."
)
GUIDE_DATA_NOTE = "Match each invoice to its purchase order by the PO number and check the amount."
GUIDE_SAMPLE_TEXT = " ".join(
    ["INVOICE INV-2042", "Alpen Maschinenbau AG", "Your purchase order: PO-7725"]
)
GUIDE_TRIGGER_INPUT = (
    "Check the invoice in {payload.name} and release it for payment. "
    "The invoice says: {payload.text}"
)
INVOICE_PDFS = {
    "INV-2041.pdf": [
        "INVOICE INV-2041",
        "Nordwind Office GmbH",
        "Your purchase order: PO-7710",
        "Office chairs, 4 pieces",
        "Amount due: 1,180.00 EUR (including VAT)",
        "Please pay within 30 days.",
    ],
    "INV-2042.pdf": [
        "INVOICE INV-2042",
        "Alpen Maschinenbau AG",
        "Your purchase order: PO-7725",
        "Hydraulic press service, 1 piece",
        "Amount due: 7,450.00 EUR (including VAT)",
        "Please pay within 30 days.",
    ],
}


def guide_folder(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path("C:/awa-guide-work") if os.name == "nt" else Path("/tmp/awa-guide-work")


PURCHASE_ORDERS = (
    "po,vendor,amount,status\n"
    "PO-7710,Nordwind Office GmbH,1180.00,open\n"
    "PO-7725,Alpen Maschinenbau AG,7450.00,open\n"
)


def guide_sample_files() -> dict[str, bytes]:
    """The sample files of Part A (docs/guide/files), byte for byte reproducible: two invoices
    as PDF and the open purchase orders as CSV."""

    import importlib.util

    path = ROOT / "examples" / "benchmarks" / "core" / "data_gen.py"
    spec = importlib.util.spec_from_file_location("core_benchmark_data_gen", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    files = {name: module.text_pdf(lines) for name, lines in INVOICE_PDFS.items()}
    files["purchase_orders.csv"] = PURCHASE_ORDERS.encode("utf-8")
    files[LEDGER_FILE] = ledger_database()
    return files


LEDGER_FILE = "company-ledger.db"
"""The reader's ledger in the guide: a small SQLite database with the invoices."""


def ledger_database() -> bytes:
    """`company-ledger.db`, byte for byte reproducible: the invoices table, written by SQLite with
    the header fields that name the writing library's version set to fixed values."""

    import sqlite3

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / LEDGER_FILE
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE invoices (invoice_id TEXT PRIMARY KEY, supplier TEXT, po_number TEXT, "
            "amount_eur REAL, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO invoices VALUES (?, ?, ?, ?, ?)",
            [(key, s, po, amount, status) for key, (s, po, amount, status) in INVOICES.items()],
        )
        connection.commit()
        connection.close()
        data = bytearray(path.read_bytes())
    data[24:28] = (1).to_bytes(4, "big")  # file change counter
    data[92:96] = (1).to_bytes(4, "big")  # version-valid-for: the same counter
    data[96:100] = (3045000).to_bytes(4, "big")  # the library that wrote it: a fixed number
    return bytes(data)


def write_guide_files() -> None:
    GUIDE_FILES.mkdir(parents=True, exist_ok=True)
    for name, data in guide_sample_files().items():
        (GUIDE_FILES / name).write_bytes(data)
        print(f"  {(GUIDE_FILES / name).relative_to(ROOT).as_posix()}")


_MASK_MODELS_JS = """
(() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const pattern = /\\b(?:azure_openai|anthropic|openai|google_genai|google):[\\w.\\-]+/g;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.parentElement && node.parentElement.closest('label[for=model]')) continue;  // the documented example
    const text = node.nodeValue.replace(pattern, '<provider>:<model>');
    if (text !== node.nodeValue) node.nodeValue = text;
  }
})()
"""
_MASK_VOLATILE_JS = """
(() => {
  const rules = [
    [/[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}/g, '2026-10-05 10:30'],
    [/[0-9]{1,2}:[0-9]{2} (AM|PM)/g, '10:30 AM'],
    [/proc_[0-9a-f]{8,}/g, 'proc_8f3a1c9d2b47'],
  ];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    let text = node.nodeValue;
    for (const [pattern, fixed] of rules) text = text.replace(pattern, fixed);
    if (text !== node.nodeValue) node.nodeValue = text;
  }
})()
"""
_STALE_MODEL = re.compile(r"\b(?:claude|gpt|gemini)-\d", re.IGNORECASE)


# The pictures outside docs/assets/screenshots/guide (the README, the tour, the web-app page) are the
# plain twins of guide pictures: the same moment of the same run, without callouts. Key: the guide
# picture, value: the plain picture.
PLAIN_TWINS = {
    "a02-setup": "ui-setup",
    "a03-data": "ui-data",
    "a04-describe-after": "ui-describe",
    "a05-review": "ui-review",
    "a03-systems": "ui-systems",
    "a06-connect": "ui-connect",
    "a07-test": "ui-test",
    "a08-try-approval": "ui-approval",
    "a08-try-answer": "ui-run",
    "a10-use-it-waiting": "ui-deploy",
    "a10-approvals": "ui-inbox",
}
TWIN_FRAMES = {"ui-describe": "#build-view", "ui-approval": "#approvals"}
"""Where a plain picture starts when it is not where its guide picture starts."""


def guide_shot(page: Page, name: str, selector: str, **options: Any) -> Path:
    """`Page.shot` for the guide: model ids that the page of the day names are replaced by the
    placeholder first (only the Current models table of the docs may name a model)."""

    page.js(_MASK_MODELS_JS)
    page.js(_MASK_VOLATILE_JS)  # the clock and the run ids of the day: the same picture every time
    text = page.js("document.body.innerText") or ""
    if _STALE_MODEL.search(text):
        raise SystemExit(f"{name}: the page names a model id; the guide shows placeholders only")
    if name in PLAIN_TWINS:
        twin = PLAIN_TWINS[name]
        page.frame_shot(twin, TWIN_FRAMES.get(twin, selector), APP_FRAME)
    if name in PHONE_PAGES:
        section = PHONE_PAGES[name]
        page.frame_shot(f"ui-{section}-phone", f"#{section}", PHONE_FRAME)
    return page.shot(f"guide/{name}", selector, **options)


def load_harness() -> Any:
    """The guide test as a module: its `Work` runs `awa` commands in this process, exactly as the
    test does, so a terminal image shows what the page's expected output is checked against."""

    import importlib.util

    if str(ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(ROOT / "tests"))  # the test's `_helpers` package
    spec = importlib.util.spec_from_file_location(
        "guide_harness", ROOT / "tests" / "docs" / "test_guide.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WORKER_WAITING = 2
"""The exit code of `awa worker --once` when runs wait for a person (expected in the guide)."""


def check_exit(argv: Sequence[str], code: int, output: str, ok: tuple[int, ...]) -> None:
    """Stop the capture when `awa argv` ended with a code outside `ok`."""

    if code not in ok:
        raise SystemExit(f"awa {' '.join(argv)} ended with {code}:\n{output}")


class GuideCommands:
    """Runs `awa` commands in the reader's work folder, in this process, the way the guide test does
    (a fake terminal, the scripted model, typed answers)."""

    def __init__(self, work: Path) -> None:
        import pytest

        self.work = work
        self.monkeypatch = pytest.MonkeyPatch()
        self.harness = load_harness().Work(work, self.monkeypatch, sys.modules["__main__"])

    def close(self) -> None:
        self.monkeypatch.undo()

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        typed: list[str] | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> str:
        """Run `awa argv` in `cwd`; an exit code outside `ok` stops the capture."""

        self.harness.cwd = cwd
        code, output, _ = self.harness.awa(argv, "desk", typed or [])
        check_exit(argv, code, output, ok)
        return str(output)


def guide_terminal_marks() -> dict[str, list[Mark]]:
    """The callouts of every terminal image of the guide. The key is the `shot=` name of the guide
    block the image shows; a mark is a piece of one of its lines and the legend line for it."""

    return {
        "a01-doctor": [
            Mark("OK    python", "Python is found and new enough."),
            Mark("WARN  credentials", "No key yet. You enter it in step 2."),
            Mark("Result:", "The last line says whether awa is usable."),
        ],
        "a03-inspect": [
            Mark("3 files, 1 table", "The count: files, tables, rows, warnings and errors."),
            Mark("INV-2041.pdf         pdf", "One line for each file: what it is and what awa read."),
            Mark("amount                 decimal", "A column of the purchase orders. awa found that it holds decimal numbers."),
        ],
        "a04-build": [
            Mark("[design] Writing", "awa writes the tests first and then designs the system."),
            Mark("Round 1: 3 of 3", "It runs every eval case on sample data."),
            Mark("Where the data comes from:", "Where each source of data comes from, in plain words."),
            Mark("Invoice ledger (SQLite database): live.", "Your ledger: live, read through the SQL connector, read-only."),
            Mark("details:", "The exact technical names, for your IT team."),
        ],
        "a05-show": [
            Mark("invoice_checker  default", "The invoice checker. Its only tool reads the ledger."),
            Mark("payment_clerk    default", "The payment clerk. Its tool finance_approval needs a person."),
            Mark("workflow:", "The order of the work: the checker first, then the clerk."),
            Mark("approvals:", "The steps that wait for a person."),
        ],
        "a07-eval": [
            Mark("Pass rate", "The share of eval cases that passed."),
            Mark("large-invoice", "The case with 7,450 EUR: the approval was asked for and answered."),
        ],
        "a03-add-system": [
            Mark("INVOICE_LEDGER_DATABASE_URL, hidden", "The question. What you type stays hidden."),
            Mark("Test passed", "awa made one small, read-only call to the ledger."),
            Mark("table invoices", "The structure awa read: the table and its columns, no rows."),
            Mark("tools:", "The tools the agents get: they can read the ledger, not change it."),
        ],
        "a06-nothing": [
            Mark("There is nothing to connect.", "Every system is live already: nothing works on a stand-in."),
        ],
        "a08-chat": [
            Mark("approval needed", "The system stops. It shows the step and its exact values."),
            Mark("approve? [y/N] y", "You answer. y approves; anything else says no."),
            Mark("already paid", "An invoice that was paid is not paid twice."),
        ],
        "a09-trigger": [
            Mark("added trigger", "The trigger that awa wrote into blueprint.yaml."),
            Mark("note: the blueprint has no", "A run that starts by itself needs a budget (limits)."),
            Mark("awa worker blueprint.yaml", "The command that starts the automation."),
        ],
        "a09-worker": [
            Mark("runs are kept in", "Where awa keeps the runs and the waiting approvals."),
            Mark("waiting", "The run stopped at the approval. Its id starts with proc_."),
        ],
        "a10-trace": [
            Mark("Totals:", "What the whole run used."),
            Mark("tool invoice_ledger_query", "The checker looked the invoice up in the ledger."),
            Mark("approval for finance_approval", "Who approved, and how long the run waited."),
        ],
    }  # fmt: skip


def capture_guide(
    browser: Browser, work: Path, privacy: Privacy, parts: Sequence[str] = GUIDE_PARTS
) -> None:
    """The images of the guide: the web app, driven through Part A with a scripted model
    (`guide-web`), and a terminal image for every guide block marked `shot=` (`guide-terminal`:
    the guide is replayed to get its output)."""

    GUIDE_OUT.mkdir(parents=True, exist_ok=True)
    if "guide-web" in parts:
        clear_folder(work)
        guide_web_app(browser, work)
    if "guide-terminal" not in parts:
        return
    clear_folder(work)
    commands = GuideCommands(work)
    try:
        harness = commands.harness
        problems = load_harness().run_guide(
            harness, [page for page in load_harness().guide_pages() if page.is_file()]
        )
        if problems:
            raise SystemExit("the guide does not run as written:\n\n" + "\n\n".join(problems))
        marks = guide_terminal_marks()
        orphans = sorted(set(marks) - set(harness.shots))
        if orphans:
            raise SystemExit(f"marks for terminal images that no guide block makes: {orphans}")
        page = browser.page()
        page.viewport(WIDTH, 800)
        for name, steps in sorted(harness.shots.items()):
            blocks = [(_plain(command), _plain(privacy.clean(output))) for command, output in steps]
            render_terminal(page, f"guide/{name}", blocks, marks=marks.get(name, ()))
        page.close()
    finally:
        commands.close()


def clear_folder(folder: Path) -> None:
    """Empty the guide's work folder (a reader starts with nothing in it)."""

    folder.mkdir(parents=True, exist_ok=True)
    for child in folder.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


_STEADY_MS = (94, 39, 7, 45, 110, 176, 58, 14, 83, 121, 67, 102)


def _plain(text: str) -> str:
    """A developer's checkout is not the reader's: its folder names are replaced. Times, durations
    and ids of the day are replaced by fixed values, so the same picture comes out every time."""

    text = re.sub(r"~[\\/]\S*", "<awa folder>", text)
    text = re.sub(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00", "2026-10-05T10:30:00.000000+00:00", text
    )
    text = re.sub(r"\b\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\b", "2026-10-05 16:32:55", text)
    text = re.sub(r"\bDuration: [\d.]+ s\b", "Duration: 0.2 s", text)
    text = re.sub(r"\bafter \d+(?:\.\d+)? ?(?:ms|s)\b", "after 176 ms", text)
    text = re.sub(
        r"Windows 11 \(\)", "Windows 11 (AMD64)", text
    )  # the machine name comes late on Windows
    ordinal = iter(range(10_000))

    def steady() -> int:
        return _STEADY_MS[next(ordinal) % len(_STEADY_MS)]

    text = re.sub(r"\b\d+ ms\b", lambda _: f"{steady()} ms", text)
    text = re.sub(  # a percentile pair stays in order
        r"(latency p50 / p95(?: \| |\s+))\d+ ms / \d+ ms", r"\g<1>98 ms / 104 ms", text
    )
    # the time column of an evaluation table: | case | passed | scores | ms | tokens |
    text = re.sub(
        r"\| \d+ \| (\d+) \|$",
        lambda found: f"| {steady()} | {found.group(1)} |",
        text,
        flags=re.MULTILINE,
    )
    seen: dict[str, str] = {}

    def fixed(found: re.Match[str]) -> str:
        whole = found.group(0)
        if whole not in seen:
            digest = hashlib.sha256(f"guide-{len(seen)}".encode()).hexdigest()
            seen[whole] = found.group(1) + digest[: len(found.group(2))]
        return seen[whole]

    return re.sub(r"\b(run_|thread_|proc_|approval:)([0-9a-f]{12,32})\b", fixed, text)


def guide_web_app(browser: Browser, work: Path) -> None:
    """Part A in the real web app: every page and click of steps 2 to 10, with a scripted model."""

    from awa.ui import create_ui_app

    project = work / "invoice-desk"
    project.mkdir(parents=True)
    files = guide_invoice_files(work)
    commands = GuideCommands(work)
    model, registry = _install_model("desk")
    port = free_port()
    app = create_ui_app(project, port=port, model=model, model_registry=registry)
    page = browser.page()
    page.viewport(GUIDE_WIDTH, 800)
    try:
        with serve_app(app, port):
            page.goto(f"http://127.0.0.1:{port}/?token={app.state.token}#setup")
            guide_setup(page)
            guide_data(page, files, project)
            guide_systems_part(page, work / LEDGER_FILE, project)
            guide_describe(page)
            guide_review(page)
            guide_connect(page, project)
            guide_test(page)
            guide_try_it(page)
            guide_systems(page)
            guide_use_it(page, project)
            with (project / "blueprint.yaml").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("limits:\n  max_requests: 30\n  max_duration: 5m\n")
            inbox = project / "inbox"
            inbox.mkdir(exist_ok=True)
            shutil.copy2(guide_sample_files_path("INV-2042.pdf"), inbox / "INV-2042.pdf")
            commands.run(
                ["worker", "blueprint.yaml", "--once"], cwd=project, ok=(WORKER_WAITING,)
            )  # the large invoice waits for the head of finance
            guide_approvals(page)
            if "waiting" in commands.run(["runs", "list"], cwd=project):
                raise SystemExit("a run is still waiting after the approvals were answered")
    finally:
        page.close()
        commands.close()
        for name in ("ANTHROPIC_API_KEY", "INVOICE_LEDGER_DATABASE_URL"):
            os.environ.pop(name, None)  # the app exported what the pages saved


def guide_sample_files_path(name: str) -> Path:
    path = GUIDE_FILES / name
    if not path.is_file():
        raise SystemExit(f"{path.relative_to(ROOT).as_posix()} is missing: run --only files first")
    return path


def guide_invoice_files(work: Path) -> list[Path]:
    """The folder `invoice-files` of step 3: what the reader downloaded from docs/guide/files (the
    ledger goes next to it: it is a system, not a file to copy into the project)."""

    folder = work / "invoice-files"
    folder.mkdir(parents=True, exist_ok=True)
    for name in guide_sample_files():
        target = work / name if name == LEDGER_FILE else folder / name
        shutil.copy2(guide_sample_files_path(name), target)
    return sorted(folder.iterdir())


def upload(page: Page, selector: str, paths: Sequence[Path]) -> None:
    """Put files into a (hidden) file input, as choosing them in the dialog would."""

    root = page.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
    node = page.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
    if not node:
        raise SystemExit(f"upload: no element {selector!r} to put the files into")
    page.send("DOM.setFileInputFiles", {"files": [str(p) for p in paths], "nodeId": node})


def _wait_for_cards(page: Page, count: int) -> None:
    page.wait(
        f"document.querySelectorAll('#data-cards .card.file').length >= {count}"
        " && !document.querySelector('#data-status').children.length",
        "the files to be read",
        180,
    )
    time.sleep(0.5)


def guide_setup(page: Page) -> None:
    """Step 2: the key goes into the card of the provider; Save and continue stores it."""

    page.wait("document.querySelector('#providers .card')", "the Set up page")
    page.js(
        "(() => { const card = [...document.querySelectorAll('#providers .card')].find("
        "(c) => c.textContent.includes('Anthropic')); card.id = 'guide-provider';"
        " const details = card.matches('details') ? card : card.querySelector('details');"
        " if (details) details.open = true;"
        " card.querySelector('input[data-env=\"ANTHROPIC_API_KEY\"]').value = 'sk-' + 'example-not-a-real-key'; })()"
    )
    guide_shot(
        page, "a02-setup", "#providers", pad=16, to="#model-state",
        marks=[
            Mark("#guide-provider summary", "The card of your AI provider. A click opens it."),
            Mark("input[data-env='ANTHROPIC_API_KEY']", "Paste your key here. What you type is hidden."),
            Mark("#model", "Optional: the model to use. Leave it empty for awa's default."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#save-setup').click()")
    page.wait("document.querySelector('#setup').hidden", "the next page after saving")


def guide_data(page: Page, files: Sequence[Path], project: Path) -> None:
    """Step 3: drop the files, read the cards, write a note, switch the masking on."""

    page.js("location.hash = '#data'")
    page.wait("!document.querySelector('#data').hidden", "the Data step")
    upload(page, "#pick-files", files)
    _wait_for_cards(page, len(files))
    page.js(
        "(() => { const card = [...document.querySelectorAll('#data-cards .card.file')].find("
        "(c) => c.textContent.includes('purchase_orders.csv')); card.id = 'guide-csv-card';"
        f" const box = card.querySelector('textarea'); box.value = {json.dumps(GUIDE_DATA_NOTE)};"
        " box.dispatchEvent(new Event('change')); })()"
    )
    page.js("document.querySelector('#mask').click()")
    page.wait(
        "document.querySelector('#guide-csv-card .hint[role=status]').textContent.includes('Saved')"
        " && document.querySelector('#mask-state').textContent.includes('hidden')",
        "the note and the masking to be saved",
    )
    saved = json.loads((project / ".awa" / "data.json").read_text(encoding="utf-8"))
    if GUIDE_DATA_NOTE not in json.dumps(saved):
        raise SystemExit("the note of the Data step was not saved in .awa/data.json")
    guide_shot(
        page, "a03-data", "#data-cards", pad=24, top=True, max_height=2400,
        marks=[
            Mark("label:has(#mask)", "Hides names, IBANs and the like from the AI model. You still see everything."),
            Mark("#dropzone", "Drop your files here, or press Choose files. Your originals are never changed."),
            Mark("#upload-list", "Every file is copied into your project."),
            Mark("#data-cards .card.file", "One card per file: what it is and what awa found in it, in plain words."),
            Mark("#guide-csv-card textarea", "A note in your own words: what should happen with this file."),
        ],
    )  # fmt: skip


def guide_systems_part(page: Page, ledger: Path, project: Path) -> None:
    """Step 3, second part: declare the ledger under External systems, test it, save it."""

    page.js(
        "(() => { document.querySelector('#add-system').open = true;"
        " const kind = document.querySelector('#system-type'); kind.value = 'sql';"
        " kind.dispatchEvent(new Event('change'));"
        " const name = document.querySelector('#system-name'); name.value = 'Invoice ledger';"
        " name.dispatchEvent(new Event('change')); })()"
    )
    page.wait(
        "document.querySelector(\"#system-fields input[data-env='INVOICE_LEDGER_DATABASE_URL']\")",
        "the field of the ledger's address",
    )
    page.js(
        "(() => { const db = document.querySelector('#system-database'); db.value = 'sqlite';"
        " db.dispatchEvent(new Event('change'));"
        " document.querySelector(\"#system-fields input[data-env='INVOICE_LEDGER_DATABASE_URL']\").value = "
        + json.dumps(str(ledger))
        + "; document.querySelector('#test-system').click(); })()"
    )
    page.wait("document.querySelector('#system-result .alert')", "the test of the ledger", 60)
    if page.js("document.querySelector('#system-result .alert.error') !== null"):
        raise SystemExit(
            "The test of the ledger failed: "
            + str(page.js("document.querySelector('#system-result').innerText"))
        )
    guide_shot(
        page, "a03-systems-test", "#add-system", pad=16,
        marks=[
            Mark("#system-type", "The kind of system: here a SQL database. The list comes from awa's catalog."),
            Mark("#system-name", "Your name for it. The design and the data map use this name."),
            Mark("#system-database", "Which database. For a file: SQLite."),
            Mark("#system-fields input[type=password]", "Where it is: for a file its path, for a server its address. Hidden."),
            Mark("#test-system", "One small, read-only call. Nothing is saved yet."),
            Mark("#system-result .alert", "The result of the test, in plain words."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#save-system').click()")
    page.wait("document.querySelector('#system-cards .card.system')", "the card of the ledger", 60)
    page.js("document.querySelector('#system-cards .table-info').open = true")
    time.sleep(0.3)
    guide_shot(
        page, "a03-systems", "#system-cards", pad=16,
        marks=[
            Mark("#system-cards .card.system h3", "The ledger, under the name you gave it."),
            Mark("#system-cards .state.live", "Live: the design reads the real ledger, read-only."),
            Mark("#system-cards .table-info", "What awa read: the table and its columns. No rows."),
            Mark("#system-cards .chip", "Where the address is saved: in .env, never shown again."),
        ],
    )  # fmt: skip
    text = (project / "data" / "SYSTEMS.json").read_text(encoding="utf-8")
    if "invoice_ledger" not in text or str(ledger) in text or ledger.as_posix() in text:
        raise SystemExit(
            "the declared ledger is missing from data/SYSTEMS.json, or its address is in it"
        )


def guide_describe(page: Page) -> None:
    """Step 4: describe the process and create the system."""

    page.js("location.hash = '#describe'")
    page.wait("!document.querySelector('#describe').hidden", "the Describe step")
    page.wait("document.querySelector('#describe-files .chip')", "the list of your data")
    page.js(f"document.querySelector('#process').value = {json.dumps(GUIDE_PROCESS)}")
    guide_shot(
        page, "a04-describe-before", "#describe", pad=24, top=True, max_height=1500,
        marks=[
            Mark("#describe-files .chips", "Your files and your ledger from step 3. × takes one out."),
            Mark("#process", "Your description, in your own words."),
            Mark("label.check", "Leave this on: awa tests and repairs the design."),
            Mark("#create", "Starts the build."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#suggest-patterns').click()")
    page.wait(
        "document.querySelectorAll('#patterns-result .card.pattern').length > 1",
        "the suggested patterns",
        60,
    )
    page.frame_shot("ui-patterns", "#patterns-result", APP_FRAME)
    page.js("document.querySelector('#patterns-result').replaceChildren()")
    page.js("document.querySelector('#create').click()")
    page.wait("document.querySelector('#describe-result').textContent.trim()", "the build", 300)
    if page.js("document.querySelector('#describe-result .alert.error') !== null"):
        raise SystemExit(
            "The build failed: "
            + str(page.js("document.querySelector('#describe-result').innerText"))
        )
    guide_shot(
        page, "a04-describe-after", "#describe-result", pad=24, top=True, max_height=3600,
        marks=[
            Mark("#stages", "The five steps of the build. Each one is ticked when it is done."),
            Mark("#build-cases", "Every eval case and whether it passes."),
            Mark("#describe-result .alert", "The result in plain words: what was built and what passes."),
            Mark("#describe-result .data-map .map-source", "Where the data comes from: your ledger is live.", text="Invoice ledger"),
            Mark("#describe-result a.button", "Opens the next step, Review."),
            Mark("#build-details summary", "The technical details, for your IT team."),
        ],
    )  # fmt: skip


def _clip_shot(page: Page, name: str, selector: str, top: float) -> None:
    """Screenshot one element at full width (for an element far down a long page)."""

    page.privacy.check(
        page.js(f"document.querySelector({json.dumps(selector)}).innerText") or "", name
    )
    height = page.js(
        f"document.querySelector({json.dumps(selector)}).getBoundingClientRect().height"
    )
    clip = {
        "x": 0,
        "y": max(top - 16, 0),
        "width": page.width,
        "height": min(height + 32, 1400),
        "scale": 1,
    }
    data = page.send(
        "Page.captureScreenshot", {"format": "png", "clip": clip, "captureBeyondViewport": True}
    )
    write_picture(name, base64.b64decode(data["data"]))


def guide_review(page: Page) -> None:
    """Step 5: what awa designed."""

    page.js("location.hash = '#review'")
    page.wait("document.querySelector('#review-body .diagram svg')", "the Review page")
    time.sleep(0.5)
    guide_shot(
        page, "a05-review", "#review-body", pad=24, top=True, max_height=2100,
        marks=[
            Mark("#review-body .diagram", "How the work flows: the checker hands over to the clerk."),
            Mark(".chip.gate", "A tool that asks you first: it waits for a person."),
            Mark("#review-body .map-source .state.live", "The ledger is live: the design reads your real ledger."),
            Mark("#review-body .map-agents", "Per agent: what it reads and writes, and with which tool."),
            Mark("#review-body > .scroll", "The eval cases: each with its input and what it expects."),
        ],
    )  # fmt: skip
    editor = "#review-body > details"  # the plain picture of the editor for the web-app page
    page.js(f"document.querySelector({json.dumps(editor)}).open = true")
    top = page.js(
        f"document.querySelector({json.dumps(editor)}).getBoundingClientRect().top + scrollY"
    )
    _clip_shot(page, "ui-edit-blueprint", editor, top)
    page.js(f"document.querySelector({json.dumps(editor)}).open = false")


def guide_connect(page: Page, project: Path) -> None:
    """Step 6: the Connect page shows the ledger as live; nothing works on a stand-in."""

    page.js("location.hash = '#connect'")
    page.wait(
        "document.querySelector('#connect-body .card.system')",
        "the system cards",
        60,
        show="#connect",
    )
    guide_shot(
        page, "a06-connect", "#connect", pad=24, top=True, max_height=1500,
        marks=[
            Mark("#connect-summary", "How many systems are live, and how many work on a stand-in."),
            Mark("#connect-body .card.system .badge.ready", "Live: the ledger you added in step 3."),
            Mark("#connect-body .card.system .status-text", "Why: it was declared under Your data."),
        ],
    )  # fmt: skip
    if "use: sql" not in (project / "blueprint.yaml").read_text(encoding="utf-8"):
        raise SystemExit("the design is not bound to the declared ledger (use: sql)")
    page.js("location.hash = '#deploy'")
    page.wait("document.querySelector('#deploy-body #health')", "the Use it page", 30)
    if "Not connected yet" in str(page.js("document.querySelector('#deploy-body').innerText")):
        raise SystemExit("Use it warns that something is not connected, but the ledger is live")


def guide_test(page: Page) -> None:
    """Step 7: run the eval cases."""

    page.js("location.hash = '#test'")
    page.wait("!document.querySelector('#test').hidden", "the Test page")
    page.wait("document.querySelector('#test-results .case-edit')", "the eval cases", 60)
    page.js("document.querySelector('#run-tests').click()")
    page.wait("document.querySelector('#test-summary .meter')", "the tests", 300)
    time.sleep(0.5)
    guide_shot(
        page, "a07-test", "#test", pad=24, top=True, max_height=2200,
        to="#test-results .case-edit",
        marks=[
            Mark("#run-tests", "Runs every eval case."),
            Mark("#test-summary .alert", "The result: all eval cases passed."),
            Mark("#test-results .case-edit textarea", "What the case sends. Change it to use other files."),
            Mark("#test-results .case-edit .chips", "The files this case uses, found in its text."),
            Mark("#test-results .case-edit button", "Saves the change in evals/cases.yaml and runs only this case.", text="Save and run"),
            Mark("#test-results .case-edit .case-result .badge", "The result of this case."),
        ],
    )  # fmt: skip


def guide_systems(page: Page) -> None:
    """Chapter 3: the card of a system on Set up, opened and filled in (nothing is saved)."""

    page.js("document.querySelector('#connectors').replaceChildren()")  # no stale cards
    page.js("location.hash = '#setup'")
    page.wait("document.querySelector('#connectors .card details')", "the systems of Set up")
    page.js(
        "(() => { const card = [...document.querySelectorAll('#connectors .card')].find("
        "(c) => c.querySelector('h3') && c.querySelector('h3').textContent.includes('Jira'));"
        " card.id = 'guide-jira'; card.querySelector('details').open = true;"
        " const fill = (name, value) => { card.querySelector('input[data-env=' + JSON.stringify(name) + ']').value = value; };"
        " fill('JIRA_URL', 'https://example.atlassian.net'); fill('JIRA_EMAIL', 'you@example.com');"
        " fill('JIRA_API_TOKEN', 'example-not-a-real-token'); })()"
    )
    guide_shot(
        page, "b03-systems", "#guide-jira", pad=16,
        marks=[
            Mark("#guide-jira h3", "The system, and whether it is set up."),
            Mark("#guide-jira summary", "Opens the fields. Fill in only the systems your process uses."),
            Mark("#guide-jira input[data-env='JIRA_URL']", "The address of the system."),
            Mark("#guide-jira input[data-env='JIRA_API_TOKEN']", "The key. What you type is hidden."),
        ],
    )  # fmt: skip


def guide_try_it(page: Page) -> None:
    """Step 8: ask for a payment from 5,000 EUR and answer the approval."""

    page.js("location.hash = '#run'")
    page.wait("!document.querySelector('#run').hidden", "the Try it page")
    page.wait("document.querySelector('#examples .example')", "the examples", 60)
    page.js(
        "document.querySelector('#message').value = 'Please release invoice INV-2042 for payment.'"
    )
    page.js("document.querySelector('#send').click()")
    page.wait("document.querySelector('#approvals .card.approval')", "the approval request", 120)
    time.sleep(0.5)
    guide_shot(
        page, "a08-try-approval", "#run", pad=24, top=True, max_height=1700,
        marks=[
            Mark("#examples .example", "Example requests from your eval cases. A click fills the box."),
            Mark("#conversation .timeline", "Every step the system takes, in plain words."),
            Mark("#approvals .card.approval", "What will happen, the exact values, and why you are asked."),
            Mark("#approvals button.primary", "Approve: lets it go ahead."),
            Mark("#approvals button", "Say no: stops the step. You can leave a note.", text="Say no"),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#approvals button.primary').click()")
    page.wait("document.querySelector('#conversation .bubble.system')", "the answer")
    time.sleep(0.5)
    guide_shot(
        page, "a08-try-answer", "#conversation", pad=16,
        marks=[
            Mark("#conversation .step.ok", "The step ran after your OK.", text="head of finance approves"),
            Mark("#conversation .decision", "Your decision stays in the conversation."),
            Mark("#conversation .bubble.system", "The answer.", text="released for payment"),
        ],
    )  # fmt: skip


def guide_use_it(page: Page, project: Path) -> None:
    """Steps 9 and 10: add the file-drop trigger, run it once with a sample, answer the approval."""

    page.js("location.hash = '#test'")  # leave the page, so that it is read afresh
    page.js("document.querySelector('#deploy-body').replaceChildren()")  # no stale content
    page.js("location.hash = '#deploy'")
    page.wait("document.querySelector('#add-trigger')", "the Use it page")
    page.js(
        "(() => { document.querySelector('#add-trigger').open = true;"
        " const kind = document.querySelector('#trigger-kind'); kind.value = 'file-drop';"
        " kind.dispatchEvent(new Event('change')); })()"
    )
    page.js(
        "document.querySelector('#trigger-pattern').value = '*.pdf';"
        "document.querySelector('#trigger-name').value = 'new-invoice';"
        f"document.querySelector('#trigger-input').value = {json.dumps(GUIDE_TRIGGER_INPUT)};"
    )
    guide_shot(
        page, "a09-trigger-form", "#add-trigger", pad=12,
        marks=[
            Mark("#trigger-kind", "What starts the run: a file that appears in a folder."),
            Mark("#trigger-directory", "The folder to watch."),
            Mark("#trigger-pattern", "Which files count."),
            Mark("#trigger-input", "What each run is asked. {payload.name} and {payload.text} come from the file."),
            Mark("#add-trigger button.primary", "Writes the trigger into blueprint.yaml."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#add-trigger button.primary').click()")
    page.wait("document.querySelector('.card.trigger')", "the trigger to be added")
    if "new-invoice" not in (project / "blueprint.yaml").read_text(encoding="utf-8"):
        raise SystemExit("the trigger form did not write the trigger into blueprint.yaml")
    page.js(
        "(() => { const card = document.querySelector('.card.trigger'); card.id = 'guide-trigger';"
        " card.querySelector('details').open = true;"
        " const fill = (name, value) => { const box = card.querySelector('input[data-field=' + JSON.stringify(name) + ']');"
        " if (box) box.value = value; };"
        f" fill('name', 'INV-2042.pdf'); fill('text', {json.dumps(GUIDE_SAMPLE_TEXT)}); }})()"
    )
    guide_shot(
        page, "a09-trigger-sample", "#guide-trigger", pad=12,
        marks=[
            Mark("#guide-trigger h3", "The trigger you added, in plain words below it."),
            Mark("#guide-trigger summary", "Run it now with a sample: nothing has to arrive."),
            Mark("#guide-trigger input[data-field='name']", "What such a file would carry: its name ..."),
            Mark("#guide-trigger input[data-field='text']", "... and its text."),
            Mark("#guide-trigger button.run-sample", "Runs the system once, for real, on this computer."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#guide-trigger button.run-sample').click()")
    page.wait(
        "document.querySelector('#use-inbox .card.approval')", "the run to wait for your OK", 120
    )
    time.sleep(0.5)
    guide_shot(
        page, "a10-use-it-waiting", "#health", pad=24, to="#add-trigger",
        marks=[
            Mark("#health", "Is it running? Nothing runs by itself until you start a worker."),
            Mark("#use-inbox .card.approval", "The run waits for your OK. It is the same card as on Try it."),
            Mark("#use-inbox button.primary", "Approve."),
            Mark(".card.trigger", "The trigger and what starts it."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#use-inbox button.primary').click()")
    page.wait(
        "document.querySelector('#use-inbox .result .actions button')",
        "the button that continues the run",
        show="#use-inbox",
    )
    page.js("document.querySelector('#use-inbox .result .actions button').click()")
    page.wait(
        "document.querySelector('table.runs .badge.ready')",
        "the run to finish",
        120,
        show="#deploy-body",
    )
    time.sleep(1.5)
    page.js(
        "[...document.querySelectorAll('#deploy-body h2')].find((h) => h.textContent === 'Recent runs').id = 'guide-runs'"
    )
    guide_shot(
        page, "a10-use-it-done", "#guide-runs", pad=16, to="#keep-running",
        marks=[
            Mark("table.runs .badge.ready", "The run succeeded."),
            Mark("#keep-running .card.command", "The one command that keeps it running after you close this window."),
            Mark("#keep-running .card.command button", "Copies the command."),
        ],
    )  # fmt: skip


def guide_approvals(page: Page) -> None:
    """Step 10: a run that a trigger started waits on the Approvals page; the button answers it."""

    page.js("location.hash = '#inbox'")
    page.js("document.querySelector('#inbox-body').replaceChildren()")  # no stale card
    page.js("document.querySelector('#refresh-inbox').click()")
    page.wait("document.querySelector('#inbox-body .card.approval')", "the waiting approval")
    time.sleep(0.5)
    guide_shot(
        page, "a10-approvals", "#inbox-body", pad=24,
        marks=[
            Mark("#inbox-body h3", "The waiting run and the trigger that started it."),
            Mark("#inbox-body .card.approval", "What it wants to do, with the exact values."),
            Mark("#inbox-body .actions button.primary", "Approve. The decision is saved in the project folder."),
        ],
    )  # fmt: skip
    page.js("document.querySelector('#inbox-body button.primary').click()")
    page.wait(
        "document.querySelector('#inbox-body .result .actions button')",
        "the button that continues the run",
        show="#inbox-body",
    )
    page.js("document.querySelector('#inbox-body .result .actions button').click()")
    page.wait(
        "document.querySelector('#inbox-body .alert.ok')",
        "the run to continue",
        120,
        show="#inbox-body",
    )
    time.sleep(1.5)


# ---------------------------------------------------------------------- tracing (Langfuse pages)

REDACTED_MARK = "#5b5b5b"
REDACT_LITERALS: list[str] | None = None
"""With `--redact`: the extra words to mask in every captured page (None: no redaction)."""
_REDACT_JS = r"""
((patterns, literals, color) => {
  const rules = patterns.map((p) => new RegExp(p, 'gi'));
  const words = literals.filter((w) => w && w.length >= 3);
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  if (words.length) rules.unshift(new RegExp(words.map(escape).join('|'), 'gi'));
  let count = 0;
  const redactNode = (node) => {
    const text = node.nodeValue;
    if (!text || !text.trim() || !node.parentNode) return;
    const spans = [];
    for (const rule of rules) {
      rule.lastIndex = 0;
      let m;
      while ((m = rule.exec(text)) !== null) {
        if (!m[0]) { rule.lastIndex++; continue; }
        spans.push([m.index, m.index + m[0].length]);
      }
    }
    if (!spans.length) return;
    spans.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const s of spans) {
      const last = merged[merged.length - 1];
      if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1]); else merged.push([...s]);
    }
    const fragment = document.createDocumentFragment();
    let at = 0;
    for (const [start, end] of merged) {
      if (start > at) fragment.append(text.slice(at, start));
      const box = document.createElement('span');
      box.className = 'awa-redacted';
      box.dataset.original = text.slice(start, end);
      box.textContent = '█'.repeat(Math.min(Math.max(end - start, 4), 24));
      box.style.cssText = `color:${color};background:${color};border-radius:3px;letter-spacing:-1px`;
      fragment.append(box);
      at = end;
      count++;
    }
    if (at < text.length) fragment.append(text.slice(at));
    node.replaceWith(fragment);
  };
  const redactTree = (root) => {
    if (root.nodeType === Node.TEXT_NODE) { redactNode(root); return; }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(redactNode);
  };
  redactTree(document.body);
  window.__awaRedactedInputs = window.__awaRedactedInputs || [];
  document.querySelectorAll('input, textarea').forEach((el) => {
    if (rules.some((r) => { r.lastIndex = 0; return r.test(el.value || ''); })) {
      window.__awaRedactedInputs.push([el, el.value]);
      el.value = '•'.repeat(Math.min(el.value.length, 24));
      count++;
    }
  });
  // The page may render again before the capture: redact whatever it writes from now on too.
  if (window.__awaRedactObserver) window.__awaRedactObserver.disconnect();
  window.__awaRedactObserver = new MutationObserver((changes) => {
    for (const change of changes) {
      if (change.type === 'characterData') redactNode(change.target);
      change.addedNodes.forEach((n) => { if (n.nodeType === Node.TEXT_NODE || n.nodeType === Node.ELEMENT_NODE) redactTree(n); });
    }
  });
  window.__awaRedactObserver.observe(document.body, {childList: true, subtree: true, characterData: true});
  return count;
})
"""

REDACT_PATTERNS = (
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # email addresses
    r"\b(?:sk|pk)-(?:lf-|ant-|proj-)?[A-Za-z0-9_-]{6,}",  # API keys
    r"\blsv2_[A-Za-z0-9_]{8,}",  # LangSmith keys
    r"[A-Za-z]:\\[^\s\"'<>]+",  # Windows paths
    r"/(?:Users|home)/[^\s\"'<>]+",  # home folders
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|de|ai|cloud|internal|local)\b",  # host names
)
"""What `--redact` masks in every captured page, before the capture (not by editing images)."""
_UNREDACT_JS = r"""
(() => {
  if (window.__awaRedactObserver) { window.__awaRedactObserver.disconnect(); window.__awaRedactObserver = null; }
  document.querySelectorAll('.awa-redacted').forEach((box) => box.replaceWith(box.dataset.original || ''));
  (window.__awaRedactedInputs || []).forEach(([el, value]) => { el.value = value; });
  window.__awaRedactedInputs = [];
  return true;
})()
"""
NEUTRAL_FOLDERS: list[Path] = []
"""The demo work folders: their paths name no person, so they stay readable (set by `main`)."""


def redact_patterns(neutral: Sequence[Path]) -> tuple[str, ...]:
    """`REDACT_PATTERNS`, with Windows paths inside the `neutral` folders left readable."""

    keep = [re.escape(str(PureWindowsPath(f))) for f in neutral if PureWindowsPath(f).drive]
    if not keep:
        return REDACT_PATTERNS
    spare = "(?!(?:" + "|".join(keep) + r")(?![\w-]))"
    return tuple(spare + p if p.startswith("[A-Za-z]:") else p for p in REDACT_PATTERNS)


_SECRET_LIKE = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|ENDPOINT|DSN|CONNECTION|DEPLOYMENT|URL|HOST|USER|MAIL"
)


def redact_page(page: Page, literals: Sequence[str]) -> int:
    """Mask email addresses, keys, paths, host names and the `literals` in the page's text."""

    return int(
        page.js(
            f"({_REDACT_JS.strip()})({json.dumps(list(redact_patterns(NEUTRAL_FOLDERS)))}, "
            f"{json.dumps(list(literals))}, {json.dumps(REDACTED_MARK)})"
        )
        or 0
    )


def unredact_page(page: Page) -> None:
    """Undo `redact_page` after the capture: the page's text and input values come back, so the
    next steps of a flow send what the reader typed."""

    page.js(_UNREDACT_JS.strip())


@dataclass
class TracingSetup:
    """Where the Langfuse UI is and how to log in; what must not be readable in its pictures."""

    url: str
    project: str
    email: str
    password: str
    literals: list[str]
    project_name: str


def tracing_setup(options: argparse.Namespace) -> TracingSetup:
    """Read the Langfuse address, project and login (never printed) for the `tracing` group."""

    url = (options.langfuse_url or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    login: dict[str, str] = {}
    if options.langfuse_login:
        for line in Path(options.langfuse_login).read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            login[name.strip()] = value.strip()
    email = os.environ.get("LANGFUSE_UI_EMAIL") or login.get("LANGFUSE_INIT_USER_EMAIL", "")
    password = os.environ.get("LANGFUSE_UI_PASSWORD") or login.get(
        "LANGFUSE_INIT_USER_PASSWORD", ""
    )
    if not (url and public and secret and email and password):
        raise SystemExit(
            "The tracing pictures need LANGFUSE_BASE_URL (or --langfuse-url), LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY and a login: --langfuse-login <the .env of the Langfuse compose "
            "folder> or LANGFUSE_UI_EMAIL and LANGFUSE_UI_PASSWORD"
        )
    projects = _langfuse_get(url, public, secret, "/api/public/projects")["data"]
    literals = [email, public, secret, password, *options.redact_name]
    if options.redact_env:
        for line in (
            Path(options.redact_env).read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            name, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            # Settings like `false` or `INFO` would mask ordinary words: only secret-like names.
            if (
                len(value) >= 6
                and _SECRET_LIKE.search(name.upper())
                and not line.lstrip().startswith("#")
            ):
                literals.append(value)
    project = projects[0]
    return TracingSetup(url, str(project["id"]), email, password, literals, str(project["name"]))


def _langfuse_get(url: str, public: str, secret: str, path: str) -> dict[str, Any]:
    import urllib.request

    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    request = urllib.request.Request(url + path, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local Langfuse
        return dict(json.load(response))


SHOWCASE_MAX_AGE_S = 24 * 3600
"""The tracing pictures show runs of the last day, so they match the awa that took them."""


def showcase_traces(setup: TracingSetup) -> dict[str, str]:
    """Trace ids of the newest invoice-desk run of `langfuse_showcase.py`:
    `paused` and `resumed` (INV-2042, before and after the approval)."""

    from datetime import UTC, datetime

    since = datetime.fromtimestamp(time.time() - SHOWCASE_MAX_AGE_S, UTC)
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = (
            "limit=100&fields=core,basic,metadata&name=invoke_workflow%20invoice_desk"
            f"&fromStartTime={since:%Y-%m-%dT%H:%M:%SZ}" + (f"&cursor={cursor}" if cursor else "")
        )
        page = _langfuse_get(
            setup.url,
            os.environ["LANGFUSE_PUBLIC_KEY"],
            os.environ["LANGFUSE_SECRET_KEY"],
            f"/api/public/v2/observations?{query}",
        )
        rows += page["data"]
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor or not page["data"]:
            break
    roots = [
        r
        for r in rows
        if (r.get("metadata") or {}).get("showcase") in ("scripted", "live")
        and not r.get("parentObservationId")
    ]
    roots.sort(key=lambda r: r["startTime"])
    runs = [r for r in roots if (r.get("metadata") or {}).get("request_id") == "req-inv-2042"]
    if runs:
        thread = runs[-1].get("sessionId")
        runs = [r for r in runs if r.get("sessionId") == thread]
    if len(runs) < 2:
        raise SystemExit(
            "No invoice-desk run with its approval in Langfuse from the last day: run "
            "python scripts/langfuse_showcase.py first"
        )
    return {"paused": runs[-2]["traceId"], "resumed": runs[-1]["traceId"]}


_TAG_JS = """
((id, selector, pattern, nth) => {
  const rule = new RegExp(pattern);
  const found = [...document.querySelectorAll(selector)].filter(
    (e) => rule.test((e.getAttribute('title') || e.textContent || '').trim()) && e.getBoundingClientRect().width > 0);
  const inner = found.filter((e) => !found.some((o) => o !== e && e.contains(o)));
  const el = inner[nth < 0 ? inner.length + nth : nth];
  if (!el) return false;
  document.querySelectorAll('#' + id).forEach((e) => e.removeAttribute('id'));
  el.id = id;
  return true;
})
"""


class LangfuseUi:
    """The logged-in Langfuse pages of the tracing pictures."""

    def __init__(self, page: Page, setup: TracingSetup) -> None:
        self.page = page
        self.setup = setup
        page.viewport(1400, 1150)
        page.goto(setup.url + "/auth/sign-in")
        page.wait("document.querySelector('input[type=password]')", "the Langfuse sign-in form")
        for selector, value in (
            ("input[name=email]", setup.email),
            ("input[type=password]", setup.password),
        ):
            page.js(f"document.querySelector({json.dumps(selector)}).focus()")
            page.send("Input.insertText", {"text": value})
        page.js(
            "[...document.querySelectorAll('button')].find((b) => /sign in/i.test(b.textContent)).click()"
        )
        page.wait("!location.pathname.startsWith('/auth')", "the Langfuse sign-in", 60)
        page.viewport(APP_FRAME.width, APP_FRAME.height)

    def open(self, path: str, ready: str, what: str) -> None:
        self.page.goto(f"{self.setup.url}/project/{self.setup.project}{path}")
        self.page.wait(ready, what, 90)
        time.sleep(2.5)

    def tag(self, id_: str, selector: str, pattern: str, nth: int = 0) -> None:
        ok = self.page.js(
            f"({_TAG_JS.strip()})({json.dumps(id_)}, {json.dumps(selector)}, {json.dumps(pattern)}, {nth})"
        )
        if not ok:
            raise SystemExit(f"Langfuse page: nothing matches {selector!r} with {pattern!r}")

    def click(self, title: str, nth: int = 0, wait_for: str = "") -> None:
        """Select the node of the run tree whose name matches the pattern `title`."""

        self.tag("awa-click", "span[title]", title, nth)
        self.page.js("document.getElementById('awa-click').closest('button').click()")
        if wait_for:
            self.page.wait(wait_for, f"the details of {title}", 30)
        self.loaded(f"the input and output of {title}")

    def loaded(self, what: str) -> None:
        """Wait until the page shows no loading placeholder."""

        time.sleep(1.5)
        self.page.wait("document.querySelectorAll('[class*=animate-pulse]').length === 0", what, 30)
        time.sleep(1.0)

    def picture(self, name: str) -> None:
        """Take `name` in the frame of the web-app pictures, with the sidebars folded and the
        organization of the local install (the breadcrumb before the project) shown as `demo`."""

        self.page.js(
            "document.querySelector('[data-state=expanded][data-collapsible]')"
            " && document.querySelector('[data-sidebar=trigger]').click();"
            " document.querySelector('button[aria-label=\"Hide filters\"]')?.click()"
        )
        self.loaded(f"the page of {name}")
        self.page.js(
            "((project) => { const crumbs = [...document.querySelectorAll('button')].filter("
            "(b) => b.getBoundingClientRect().top < 40 && b.innerText.trim());"
            " const at = crumbs.findIndex((b) => b.innerText.trim() === project);"
            " if (at < 1) return; const walker = document.createTreeWalker(crumbs[at - 1], NodeFilter.SHOW_TEXT);"
            " for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.nodeValue.trim()) n.nodeValue = 'demo'; })"
            f"({json.dumps(self.setup.project_name)})"
        )
        self.page.frame_shot(name, "body", APP_FRAME)


def capture_tracing(browser: Browser, options: argparse.Namespace) -> None:
    """The Langfuse pictures (`tracing-*.png`): the invoice desk of the web-app pictures, sent to
    Langfuse by `langfuse_showcase.py`, in the same 1280x800 frame as the web app."""

    global REDACT_LITERALS  # noqa: PLW0603 - the tracing pictures are always redacted
    setup = tracing_setup(options)
    REDACT_LITERALS = [*(REDACT_LITERALS or []), *setup.literals]
    traces = showcase_traces(setup)
    ui = LangfuseUi(browser.page(), setup)
    tree = "document.querySelector('span[title^=\"invoke_\"]')"
    detail = "document.body.innerText.includes('Preview')"

    # The run of INV-2042 up to the approval: the workflow, both agents and their calls.
    ui.open(f"/traces/{traces['paused']}", tree, "the run tree")
    ui.click(r"^invoke_agent invoice_checker$", wait_for=detail)
    ui.picture("tracing-tree")

    # The ledger lookup: the query the checker sent and the rows it got back.
    ui.click(r"^invoice_ledger_query$", wait_for=detail)
    ui.picture("tracing-tool-call")

    # The approval pause: the payment waits for the head of finance.
    ui.click(r"^awa\.approval_requested$", wait_for=detail)
    ui.picture("tracing-approval")

    # One model call: its tokens and cost.
    ui.click(r"^chat ", 1, wait_for=detail)
    ui.picture("tracing-model-call")

    # The resumed run: the decision and the released payment.
    ui.open(f"/traces/{traces['resumed']}", tree, "the resumed run")
    ui.click(r"^awa\.approval_decided$", wait_for=detail)
    ui.picture("tracing-approved")


# ---------------------------------------------------------------------- main


CLEAN_INSTALL_HINT = (
    "The pictures show `awa doctor` and the version of the awa they run, so they are made with a "
    "clean install of this tree: create a fresh environment and install the tree into it (not "
    "editable, no PYTHONPATH), for example `uv venv <dir> --python 3.13` and "
    '`uv pip install --python <dir> ".[all]" pytest` (on Windows at a short path such as '
    "C:\\awa-shot), then run this script with that Python."
)


def install_problem() -> str | None:
    """Why the awa Python imports is not a clean install of this tree, or `None`.

    A picture of `awa doctor` from a tree that shadows an older installed awa shows a warning
    no reader gets; the version in the pictures must be the version of this tree.
    """

    from awa.cli._doctor import run_doctor

    wanted = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text("utf-8"))
    check = run_doctor().check("awa")
    if check is None or check.status != "ok":
        return f"awa doctor does not pass its install check: {check.summary if check else 'none'}"
    import awa

    if wanted and awa.__version__ != wanted.group(1):
        return f"Python imports awa {awa.__version__}, but this tree is awa {wanted.group(1)}"
    return None


def chosen_groups(only: list[str] | None) -> list[str]:
    """The groups `--only` names; without it every group but tracing, which needs Langfuse."""

    return list(only) if only is not None else [g for g in GROUPS if g != "tracing"]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", choices=(*GROUPS, *GUIDE_PARTS))
    parser.add_argument("--browser", help="Chrome or Edge executable (found automatically)")
    parser.add_argument(
        "--workdir",
        help="neutral work folder (default C:/demo or /tmp/demo; the guide: C:/awa-guide-work or /tmp/awa-guide-work)",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="mask email addresses, keys, paths, host names and --redact-name values in every "
        "page before it is captured (always on for the tracing group)",
    )
    parser.add_argument(
        "--redact-name", action="append", default=[], help="a word to mask (repeatable)"
    )
    parser.add_argument(
        "--redact-env", help="a .env file whose values must not be readable (never printed)"
    )
    parser.add_argument("--langfuse-url", help="tracing: the Langfuse UI (else LANGFUSE_BASE_URL)")
    parser.add_argument(
        "--langfuse-login", help="tracing: the .env of the Langfuse compose folder (its login)"
    )
    options = parser.parse_args(argv)
    options.only = chosen_groups(options.only)
    if options.redact:
        global REDACT_LITERALS  # noqa: PLW0603 - one switch for every capture of this process
        REDACT_LITERALS = list(options.redact_name)
    NEUTRAL_FOLDERS[:] = [demo_folder(options.workdir), guide_folder(options.workdir)]

    if "tracing" in options.only:
        print("tracing (Langfuse)")
        tracing_browser = Browser(
            find_browser(options.browser), Privacy(demo_folder(options.workdir))
        )
        try:
            capture_tracing(tracing_browser, options)
        finally:
            tracing_browser.close()
        options.only = [g for g in options.only if g != "tracing"]

    if "files" in options.only:
        print("guide sample files")
        write_guide_files()
    browsed = [g for g in options.only if g != "files"]
    if not browsed:
        return 0
    if problem := install_problem():
        raise SystemExit(f"{problem}\n{CLEAN_INSTALL_HINT}")
    work = demo_folder(options.workdir)
    work.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    privacy = Privacy(work)
    for name in list(os.environ):
        if name.upper() not in _KEEP:
            del os.environ[name]  # no provider key or AWA_* setting reaches the in-process web app
    os.environ.update(clean_environment())
    os.chdir(work)

    browser = Browser(find_browser(options.browser), privacy)
    try:
        parts = [
            p
            for p in GUIDE_PARTS
            if p in options.only
            or "guide" in options.only
            or (p == "guide-web" and "ui" in options.only)
        ]
        if parts:
            print("guide: " + ", ".join(parts))
            guide = guide_folder(options.workdir)
            guide.mkdir(parents=True, exist_ok=True)
            os.chdir(guide)
            capture_guide(browser, guide, Privacy(guide), parts)
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

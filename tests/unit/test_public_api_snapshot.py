"""The public API is a promise: a stable name never disappears silently.

`tests/fixtures/public_api.json` records every package's `__all__`, the CLI command names and the
persisted format versions. Names of preview parts (`__stability__ = "preview"`) sit in their own
section: they may change without a deprecation. Update the snapshot deliberately:

    UPDATE_PUBLIC_API_SNAPSHOT=1 python -m pytest tests/unit/test_public_api_snapshot.py

and add the matching CHANGELOG line (Added / Changed / Deprecated / Removed).
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import warnings
from pathlib import Path
from typing import Any

import awa
from architecture.scan import port_snapshot
from awa.cli._doctor import PREVIEW_MODULES

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "public_api.json"
EXTRA_MODULES = ("awa.observability.otel", "awa.team.network", "awa.testing.contracts")
"""Public modules inside a package that is not preview as a whole."""

UPDATE_HINT = (
    "If this change is deliberate, regenerate the snapshot with\n"
    "    UPDATE_PUBLIC_API_SNAPSHOT=1 python -m pytest tests/unit/test_public_api_snapshot.py\n"
    "and add a CHANGELOG line (Added / Changed / Deprecated / Removed). A stable name is removed "
    "only after one minor release of DeprecationWarning (docs/about/stability.md)."
)


def _module_names() -> list[str]:
    top = [
        f"awa.{info.name}"
        for info in pkgutil.iter_modules(awa.__path__)
        if not info.name.startswith("_")
    ]
    return ["awa", *sorted([*top, *EXTRA_MODULES])]


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _origin(module: Any, name: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # upstream names may warn on access
            return str(getattr(getattr(module, name), "__module__", "") or "")
    except Exception:  # an optional dependency that is missing: count the name as stable
        return ""


def current() -> dict[str, Any]:
    modules = {name: _import(name) for name in _module_names()}
    previews = sorted(
        n for n, m in modules.items() if getattr(m, "__stability__", None) == "preview"
    )
    stable: dict[str, list[str]] = {}
    preview: dict[str, list[str]] = {}
    for name, module in modules.items():
        if not hasattr(module, "__all__"):
            continue
        names = sorted(module.__all__)
        if name in previews:
            preview[name] = names
            continue
        in_preview = [
            n
            for n in names
            if any(
                (origin := _origin(module, n)) == p or origin.startswith(f"{p}.") for p in previews
            )
        ]
        stable[name] = [n for n in names if n not in in_preview]
        if in_preview:
            preview[name] = in_preview
    from awa.blueprints.schema import BLUEPRINT_FORMAT_VERSION
    from awa.cli import COMMANDS, PREVIEW_COMMANDS, PREVIEW_SUBCOMMANDS
    from awa.data import DataProfile
    from awa.persistence.sqlite import FORMAT_VERSION

    return {
        "stable": stable,
        "preview": preview,
        "cli": {
            "stable": sorted(set(COMMANDS) - PREVIEW_COMMANDS),
            "preview": sorted([*PREVIEW_COMMANDS, *PREVIEW_SUBCOMMANDS]),
        },
        "formats": {
            "blueprint_format_version": BLUEPRINT_FORMAT_VERSION,
            "checkpoint_database": FORMAT_VERSION,
            "data_profile": DataProfile.model_fields["schema_id"].default,
        },
        "ports": port_snapshot(),
    }


def _write(data: dict[str, Any]) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _diff(old: dict[str, list[str]], new: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    removed = [f"{m}.{n}" for m, names in old.items() for n in names if n not in new.get(m, [])]
    added = [f"{m}.{n}" for m, names in new.items() for n in names if n not in old.get(m, [])]
    return removed, added


def _port_problems(old: dict[str, list[str]], new: dict[str, list[str]]) -> list[str]:
    """A stable backend protocol (a Protocol or an ABC that backends implement) never gains or loses a member:
    every implementer would break. Preview backend protocols are not compared."""

    found = [
        f"A stable backend protocol disappeared: {name}."
        for name in sorted(old.keys() - new.keys())
    ]
    found += [
        f"New stable backend protocol is not in the snapshot: {name}."
        for name in sorted(new.keys() - old.keys())
    ]
    for name in sorted(old.keys() & new.keys()):
        added = [m for m in new[name] if m not in old[name]]
        removed = [m for m in old[name] if m not in new[name]]
        if added or removed:
            found.append(
                f"Stable backend protocol {name} changed (added {added}, removed {removed}). A backend protocol gains "
                "behavior through a new capability Protocol, a keyword-only argument with a "
                "default, or a deprecation, never a new required member (rule R8)."
            )
    return found


def problems(recorded: dict[str, Any], now: dict[str, Any]) -> list[str]:
    found: list[str] = []
    removed, added = _diff(recorded["stable"], now["stable"])
    if removed:
        found.append(f"Stable public names disappeared: {', '.join(removed)}.")
    if added:
        found.append(f"New stable public names are not in the snapshot: {', '.join(added)}.")
    cli_removed, cli_added = _diff(
        {"awa": recorded["cli"]["stable"]}, {"awa": now["cli"]["stable"]}
    )
    if cli_removed:
        found.append(f"Stable CLI commands disappeared: {', '.join(cli_removed)}.")
    if cli_added:
        found.append(f"New stable CLI commands are not in the snapshot: {', '.join(cli_added)}.")
    found += _port_problems(
        recorded.get("ports", {}).get("stable", {}), now.get("ports", {}).get("stable", {})
    )
    if recorded["formats"] != now["formats"]:
        found.append(
            f"A persisted format or the blueprint format_version changed: "
            f"{recorded['formats']} -> {now['formats']}. A new format version needs a migration "
            "step for the old one."
        )
    return found


def test_public_api_matches_the_snapshot() -> None:
    now = current()
    if os.environ.get("UPDATE_PUBLIC_API_SNAPSHOT"):
        _write(now)
        return
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    found = problems(recorded, now)
    assert not found, "\n".join([*found, UPDATE_HINT])


def test_a_vanished_stable_name_is_reported_but_preview_changes_are_not() -> None:
    recorded = {
        "stable": {"awa.x": ["a", "b"]},
        "preview": {"awa.y": ["p"]},
        "cli": {"stable": ["run"], "preview": ["add"]},
        "formats": {"run_store": 1},
    }
    now = {**recorded, "stable": {"awa.x": ["a"]}, "preview": {"awa.y": []}}

    assert problems(recorded, now) == ["Stable public names disappeared: awa.x.b."]
    assert problems(recorded, {**recorded, "formats": {"run_store": 2}})[0].startswith(
        "A persisted"
    )


def test_a_stable_port_that_gains_a_member_is_reported_but_a_preview_port_is_not() -> None:
    recorded = {
        "stable": {},
        "cli": {"stable": [], "preview": []},
        "formats": {},
        "ports": {"stable": {"awa.x.Store": ["get(self, key)"]}, "preview": {}},
    }
    grown = {
        **recorded,
        "ports": {
            "stable": {"awa.x.Store": ["get(self, key)", "put(self)"]},
            "preview": {"awa.y.Bus": ["a()"]},
        },
    }

    [message] = problems(recorded, grown)
    assert message.startswith("Stable backend protocol awa.x.Store changed (added ['put(self)']")
    assert (
        problems(
            recorded, {**grown, "ports": {"stable": recorded["ports"]["stable"], "preview": {}}}
        )
        == []
    )


def test_preview_modules_are_exactly_those_that_declare_it() -> None:
    declared = sorted(
        name
        for name in _module_names()
        if getattr(_import(name), "__stability__", None) == "preview"
    )

    assert declared == sorted(PREVIEW_MODULES)


def test_stability_is_preview_or_absent() -> None:
    for name in _module_names():
        assert getattr(_import(name), "__stability__", "preview") == "preview", name


def test_the_run_api_base_is_no_backend_port_but_exported_abstract_bases_are() -> None:
    ports = port_snapshot()

    assert "awa.runtime.RunApi" not in ports["stable"] | ports["preview"]
    assert {"awa.automation.BaseTrigger", "awa.server.BaseRateLimiter"} <= ports["preview"].keys()

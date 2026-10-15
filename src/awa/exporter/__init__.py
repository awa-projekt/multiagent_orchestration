"""Eject an awa project with one agent into a standalone LangChain and LangGraph project.

A project stays an awa project by default: `blueprint.yaml`, `tools.py` and the cases remain
the source of truth. `export` is the optional, one-way exit: it writes a folder that runs on
LangChain and LangGraph alone, without awa installed, which you then edit with those libraries
only. Nothing is synced back.

The export reads the validated blueprint, not the awa runtime: it freezes what awa decides at
build time (system prompt, model, tools, limits) and re-expresses it with LangChain built-ins.
A blueprint that uses anything the export does not carry over is refused as a whole, with one
finding per feature and nothing written.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path, PurePosixPath

from awa.exporter._plan import build_plan
from awa.exporter._probe import import_graph
from awa.exporter._render import render
from awa.exporter._report import ExportError, ExportReport, ExportUnsupportedError, Finding
from awa.kernel.redaction import RegexRedactor


def export(
    project: str | Path, out: str | Path, *, check: bool = False, force: bool = False
) -> ExportReport:
    """Export an awa project as a standalone LangChain and LangGraph project.

    Args:
        project: The project folder, or its blueprint file.
        out: The folder to write; it must not exist or be empty, unless `force` replaces an
            earlier export.
        check: Render and import the result in a temporary folder and write nothing.
        force: Replace an earlier export in `out` (a folder with its `NOTICE.md`).

    Returns:
        The files of the export and the settings it leaves out.

    Raises:
        ExportUnsupportedError: If the blueprint uses anything the export does not carry over;
            nothing is written.
        ExportError: If the destination may not be written, the output would hold a secret, or
            the generated code does not import.
        BlueprintError: If the blueprint is invalid or its tools cannot be loaded.
    """

    from awa import __version__

    target = Path(out)
    check_destination(target, force=force)
    plan = build_plan(Path(project))
    files = render(plan, __version__, date.today())
    secrets = find_secrets(files)
    if secrets:
        raise ExportError(
            f"the export would contain secrets ({', '.join(secrets)}); nothing written"
        )
    if check:
        with tempfile.TemporaryDirectory() as scratch:
            write_tree(files, Path(scratch) / "export")
            import_graph(Path(scratch) / "export", plan.package)
    else:
        write_tree(files, target, force=force)
    return ExportReport(target, plan.package, tuple(sorted(files)), plan.warnings, not check)


MARKER = "NOTICE.md"
"""The file that marks a folder as an earlier export (the only kind `--force` replaces)."""


def check_destination(out: Path, *, force: bool) -> None:
    """Refuse a destination the export must not touch.

    A missing or empty folder is fine. A folder with files is replaced only with `force`, and
    only when it is an earlier export (it holds `NOTICE.md`), never another folder.

    Raises:
        ExportError: If `out` is a file, or a folder that may not be replaced.
    """

    if out.is_file() or out.is_symlink():
        raise ExportError(f"{out} is a file; give a folder that does not exist yet")
    if not out.exists() or not any(out.iterdir()):
        return
    if not force:
        raise ExportError(f"{out} is not empty; give a new folder, or --force to replace an export")
    if not (out / MARKER).is_file():
        raise ExportError(
            f"{out} is not an earlier export (no {MARKER}); --force replaces only an export"
        )


def write_tree(files: Mapping[str, str | bytes], out: Path, *, force: bool = False) -> None:
    """Write `files` (relative POSIX paths to content) to the folder `out`.

    The files go to a hidden folder beside `out` first and are moved into place in one step, so
    a failure leaves no half-written export and a replaced export stays whole until the swap.

    Raises:
        ExportError: If a path escapes the folder or the destination may not be written.
    """

    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ExportError(f"unsafe path in the export: {name!r}")
    check_destination(out, force=force)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".awa-export-", dir=out.parent))
    try:
        for name, content in files.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8", newline="\n")
            else:
                target.write_bytes(content)
        if out.exists():
            old = stage.with_name(stage.name + ".old")
            os.replace(out, old)
            os.replace(stage, out)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(stage, out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def find_secrets(files: Mapping[str, str | bytes]) -> list[str]:
    """The files that hold something shaped like a secret, as `path (kind)`, sorted.

    Args:
        files: The export, by relative path. Binary files are not scanned.
    """

    redactor = RegexRedactor.secrets()
    found = []
    for path, content in files.items():
        if path == "SHA256SUMS":
            continue
        text = content if isinstance(content, str) else content.decode("utf-8", errors="ignore")
        found += [f"{path} ({match.kind})" for match in redactor.find(text)]
    return sorted(set(found))


__all__ = ["ExportError", "ExportReport", "ExportUnsupportedError", "Finding", "export"]

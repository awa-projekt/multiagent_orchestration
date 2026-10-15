"""Write `.env.example` and `docs/reference/settings.md` from the settings registry.

    python scripts/gen_settings.py            # write both files
    python scripts/gen_settings.py --check    # exit 1 when a file differs (tests run this)

The registry (`awa.core.settings_registry.REGISTRY`) is the one list of every variable awa
reads; these files are generated from it so that the documentation cannot drift from the code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from awa.core.settings_registry import GROUPS, REGISTRY, SettingSpec  # noqa: E402

ENV_EXAMPLE = ROOT / ".env.example"
REFERENCE = ROOT / "docs" / "reference" / "settings.md"

# Settings only `awa setup` or the server writes, or that are internal: listed in the
# reference, left out of `.env.example` (a copied example would set them by hand).
_NOT_IN_EXAMPLE_GROUPS = frozenset({"services"})
_NOT_IN_EXAMPLE = frozenset({"AWA_PROJECT_DIR", "OPENAI_API_BASE"})


def _default(spec: SettingSpec) -> str:
    if spec.default is None:
        return ""
    return str(spec.default).lower() if isinstance(spec.default, bool) else str(spec.default)


def env_example() -> str:
    """The text of `.env.example`."""

    lines = [
        "# awa configuration. Copy to .env and fill in what you use; never commit .env.",
        "# Generated from the settings registry by scripts/gen_settings.py: do not edit by hand.",
        "#",
        "# A value is taken from the environment, else from this project's .env, else the",
        "# default. Every awa command reads the .env of the project folder (the folder with",
        "# blueprint.yaml, pyproject.toml [tool.awa] or .awa/). An empty value counts as unset,",
        "# so every line below may stay empty; fill in only what you use.",
        "# Secrets are never printed: awa env list and awa doctor show only whether they are set.",
        "# awa setup writes these values for you. Reference: docs/reference/settings.md",
    ]
    for group, title in GROUPS:
        if group in _NOT_IN_EXAMPLE_GROUPS:
            continue
        specs = [s for s in REGISTRY.values() if s.group == group and s.name not in _NOT_IN_EXAMPLE]
        if not specs:
            continue
        lines += ["", f"# --- {title} " + "-" * max(4, 72 - len(title))]
        for spec in specs:
            default = "" if spec.is_secret else _default(spec)
            note = f" Default: {default}." if default else ""
            lines.append(f"# {spec.doc}{note}")
            # Empty: an empty value counts as unset, so the default (or the provider chosen
            # from the credentials) applies until a value is filled in.
            lines.append(f"{spec.name}=")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def reference() -> str:
    """The text of `docs/reference/settings.md`."""

    parts = [
        "# Settings",
        "",
        "<!-- Generated from the settings registry by scripts/gen_settings.py: do not edit. -->",
        "",
        "Every setting awa reads, with its environment variable and default, generated from the",
        "code.",
        "",
        "```console",
        "awa env list                          # every setting, its value and where it comes from",
        "awa env set AWA_TRACE_LEVEL=full      # write one into the project .env",
        "```",
        "",
        "A value is taken from the process environment, else from the project `.env`, else the",
        "default ([Configuration](../develop/configuration.md)). awa never shows a secret value.",
        "A setting marked *confirm* decides where requests go or switches a protection:",
        "`awa env set` needs `--confirm` for it.",
    ]
    for group, title in GROUPS:
        specs = [s for s in REGISTRY.values() if s.group == group]
        if not specs:
            continue
        parts += ["", f"## {title}", "", "| Variable | What it does | Values (default) | Used by |"]
        parts.append("| --- | --- | --- | --- |")
        for spec in specs:
            values = ", ".join(f"`{c}`" for c in spec.choices) if spec.choices else ""
            if spec.type is bool:
                values = "`true`, `false`"
            elif spec.type in (int, float):
                values = "a number"
            default = _default(spec)
            shown = f"{values or 'text'} (`{default}`)" if default else values or "text"
            if spec.is_secret:
                shown = "secret"
            notes = []
            if spec.needs_confirmation:
                notes.append("*confirm*")
            if spec.aliases:
                notes.append("also read as " + ", ".join(f"`{a}`" for a in spec.aliases))
            if spec.deprecated:
                notes.append(f"deprecated: {spec.deprecated}")
            if spec.sdk and "SDK" not in spec.doc:
                notes.append(f"read by the `{spec.sdk}` SDK too")
            doc = spec.doc.removesuffix(".") + (f" ({'; '.join(notes)})" if notes else "") + "."
            used = ", ".join(spec.used_by) or "-"
            parts.append(f"| `{spec.name}` | {_cell(doc)} | {_cell(shown)} | {_cell(used)} |")
    parts += [
        "",
        "## Names your project declares",
        "",
        "A blueprint names further variables in its `*_env` keys: a webhook trigger's",
        "`secret_env`, a mailbox's `username_env` and `password_env`, `api_key_env`, and the",
        "variables of your connectors. awa treats them as secret, reads them from the project",
        "`.env` like the settings above, and `awa doctor` lists them.",
        "",
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when a file differs")
    args = parser.parse_args(argv)
    stale = []
    for path, text in ((ENV_EXAMPLE, env_example()), (REFERENCE, reference())):
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            continue
        stale.append(path.relative_to(ROOT).as_posix())
        if not args.check:
            path.write_text(text, encoding="utf-8", newline="\n")
    if args.check and stale:
        print(f"out of date: {', '.join(stale)}; run python scripts/gen_settings.py")
        return 1
    if stale:
        print(f"wrote {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

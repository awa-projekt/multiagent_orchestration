"""Model ids and product names are stated in one place; the provider table and the dated model
table in the docs name the same models."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from awa.core.providers import PROVIDERS, provider
from awa.core.settings import Settings
from awa.models.errors import KEY_VARIABLES, missing_credentials_message

ROOT = Path(__file__).resolve().parents[2]
MODELS_PAGE = ROOT / "docs" / "develop" / "models.md"

pytestmark = pytest.mark.skipif(not MODELS_PAGE.is_file(), reason="docs/ is in the repository only")

START = "<!-- current-models:start -->"
END = "<!-- current-models:end -->"

# What counts as a model id (or a version in a product name). Placeholders such as `gpt-<x>`
# or `my-model` never match. Patterns are applied case-insensitively except the o-series.
ID_PATTERNS = (
    re.compile(r"(?<![\w-])gpt-[0-9a-z][\w.\-]*", re.IGNORECASE),
    re.compile(r"(?<![\w./-])o[1-9](?:-(?:mini|pro|preview))?(?![\w\-]|\.\d)"),
    re.compile(r"(?<![\w-])claude-[0-9a-z][\w.\-]*", re.IGNORECASE),
    re.compile(r"(?<![\w-])gemini-[0-9a-z][\w.\-]*", re.IGNORECASE),
    re.compile(r"(?<![\w-])llama-?[0-9][\w.\-:]*", re.IGNORECASE),
    re.compile(r"(?<![\w-])mistral-[0-9a-z][\w.\-]*", re.IGNORECASE),
)
# Product names with a version, written as prose ("Claude Sonnet 4.5", "GPT-5", "Gemini 2.5").
PROSE_VERSION = re.compile(
    r"\b(?:Claude|Gemini|GPT)[ -](?:(?:Opus|Sonnet|Haiku|Fable|Pro|Flash)[ -])?\d+(?:\.\d+)?\b"
)
# Wrong or outdated product wording: a version number in "Azure OpenAI", the old Foundry name.
BANNED_WORDING = (
    re.compile(r"azure\s+openai\s+(?:service\s+)?v?\d", re.IGNORECASE),
    re.compile(r"azure\s+ai\s+foundry", re.IGNORECASE),
)
# Technology names that look like model ids but are not.
NOT_MODELS = {"gemini-api"}  # gemini-api: a URL path of Google's docs
# Strings this short in code are prefix tables ("o1", "gpt-"), not model names.
PREFIX_ENTRY_LENGTH = 6

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".toml", ".txt", ".html", ".tmpl", ".json", ".example"}
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "site", ".git", ".mypy_cache"}
SCANNED = (
    "README.md",
    "CONTRIBUTING.md",
    ".env.example",
    "pyproject.toml",
    "docs",
    "examples",
    "src",
    "scripts",
)


def _table() -> str:
    page = MODELS_PAGE.read_text(encoding="utf-8")
    assert START in page and END in page, "docs/develop/models.md lost the Current models markers"
    return page.split(START, 1)[1].split(END, 1)[0]


def _rows() -> list[dict[str, str]]:
    lines = [line for line in _table().splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _table_ids() -> set[str]:
    ids: set[str] = set()
    for row in _rows():
        ids.update(re.findall(r"`([^`]+)`", row["Id"]))
    return ids


def violations(text: str, allowed: set[str]) -> list[str]:
    """Return the model ids and wordings in `text` that are neither placeholders nor allowed."""

    found: list[str] = []
    for pattern in ID_PATTERNS:
        for match in pattern.finditer(text):
            literal = match.group(0).rstrip(".-:")
            if literal.lower() in NOT_MODELS or literal in allowed:
                continue
            found.append(literal)
    found += [match.group(0) for match in PROSE_VERSION.finditer(text)]
    for pattern in BANNED_WORDING:
        found += [match.group(0) for match in pattern.finditer(text)]
    return found


def _python_texts(path: Path) -> Iterator[str]:
    """Docstrings, other string constants and comments of a Python file."""

    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) > PREFIX_ENTRY_LENGTH
        ):
            yield node.value
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.string


def _files() -> Iterator[Path]:
    for entry in SCANNED:
        path = ROOT / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for found in sorted(path.rglob("*")):
                if (
                    found.is_file()
                    and not SKIP_DIRS & set(found.relative_to(ROOT).parts)
                    and (found.suffix == ".py" or found.suffix in TEXT_SUFFIXES)
                ):
                    yield found


def _texts(path: Path) -> Iterator[str]:
    if path.suffix == ".py":
        yield from _python_texts(path)
    else:
        text = path.read_text(encoding="utf-8")
        yield text.replace(_table(), "") if path == MODELS_PAGE else text


@pytest.mark.slow
def test_no_page_docstring_or_example_names_a_model_that_is_not_in_the_table() -> None:
    allowed = _table_ids()
    problems = []
    scanned = 0
    for path in _files():
        scanned += 1
        for text in _texts(path):
            for literal in violations(text, allowed):
                problems.append(f"{path.relative_to(ROOT).as_posix()}: {literal}")
    assert scanned > 100, "the scan found almost no files: the guard would pass without looking"
    assert not problems, (
        "model ids and versioned product names belong in the Current models table "
        "(docs/develop/models.md) or must be placeholders:\n" + "\n".join(sorted(set(problems)))
    )


def test_the_guard_flags_what_it_exists_for_and_accepts_placeholders() -> None:
    allowed = {"claude-sonnet-5-5", "gpt-6.1-sol"}
    for stale in (
        "model gpt-4.1",
        "openai:gpt-5-mini",
        "`claude-sonnet-4-5`",
        "gemini-2.5-flash",
        "model: o3-mini",
        "ollama:llama3.1:8b",
        "mistral-large-latest",
        "Azure OpenAI 4.0",
        "Azure AI Foundry",
        "Claude Sonnet 4.5",
        "GPT-5",
    ):
        assert violations(stale, allowed), stale
    for fine in (
        "anthropic:claude-sonnet-5-5",
        "azure_openai:gpt-6.1-sol",
        "azure_openai:<your-deployment>",
        "<provider>:<model>",
        "my-model: {input_per_mtok: 2.0}",
        "Azure OpenAI in Microsoft Foundry",
        "o2c.db",
        "gpt-<version>",
    ):
        assert not violations(fine, allowed), fine


def test_the_table_is_well_formed_and_dated() -> None:
    rows = _rows()
    assert rows, "the Current models table is empty"
    for row in rows:
        date.fromisoformat(row["Checked on"])  # a real date; its age is the release checklist's job
        assert re.search(r"\]\(https://[^)]+\)", row["Source"]), row
        assert re.fullmatch(r"`[^`]+`", row["Id"]), row
    providers = {row["Provider (prefix)"] for row in rows}
    assert any("anthropic:" in p for p in providers)
    assert any("openai:" in p and "azure_openai:" in p for p in providers)
    assert any("google_genai:" in p for p in providers)
    for prefix in providers:
        roles = {
            row["Role"].split(",")[0].split(" (")[0]
            for row in rows
            if row["Provider (prefix)"] == prefix
        }
        assert {"default", "fast", "strong"} <= roles, (prefix, roles)


def test_the_built_in_profile_defaults_are_the_anthropic_rows_of_the_table() -> None:
    wanted = {
        row["Role"]: row["Id"].strip("`")
        for row in _rows()
        if row["Provider (prefix)"].startswith("Anthropic")
    }
    fields = Settings.model_fields
    assert fields["default_model"].default == f"anthropic:{wanted['default']}"
    assert fields["fast_model"].default == f"anthropic:{wanted['fast']}"
    assert fields["strong_model"].default == f"anthropic:{wanted['strong']}"


def test_contributing_asks_to_check_for_a_newer_model_first() -> None:
    contributing = ROOT / "CONTRIBUTING.md"
    if contributing.is_file():
        assert "check for a newer one first" in contributing.read_text(encoding="utf-8")


def test_the_data_api_fragment_made_from_the_code_names_no_model_that_is_not_in_the_table() -> None:
    """The OpenAPI fragment is built from docstrings and examples of the server models (and of the
    library models they use), so it cannot name an id the page scan above did not see in the code;
    the generated text is scanned too, so a stale checked-in copy or a computed example is caught
    even when the source looks fine."""

    import importlib.util
    import json

    pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location(
        "gen_data_openapi", ROOT / "scripts" / "gen_data_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    text = json.dumps(generator.build(), ensure_ascii=False)

    assert len(text) > 10_000, "the fragment is almost empty: the guard would pass without looking"
    assert not violations(text, _table_ids())


_ROW = re.compile(
    r"^\| .*?\(`(\w+):`\)[^|]* \| (default|fast|strong)(?: \(preview\))? \| `([^`]+)` \|", re.M
)
_PROFILE_INDEX = {"default": 0, "fast": 1, "strong": 2}


def _documented() -> list[tuple[str, str, str]]:
    return _ROW.findall(MODELS_PAGE.read_text(encoding="utf-8"))


def test_the_docs_table_has_rows_to_compare() -> None:
    assert len(_documented()) >= 12


@pytest.mark.parametrize(("prefix", "profile", "model"), _documented())
def test_a_documented_model_is_the_one_in_the_provider_table(
    prefix: str, profile: str, model: str
) -> None:
    spec = provider(prefix)
    assert spec is not None
    if spec.name == "azure_openai":
        spec = provider("openai")
        assert spec is not None
    assert spec.profiles[_PROFILE_INDEX[profile]] == f"{spec.name}:{model}"


def test_the_error_message_names_the_key_variable_of_every_provider() -> None:
    for spec in PROVIDERS:
        if spec.key_variable:
            assert KEY_VARIABLES[spec.name] == spec.key_variable
            assert spec.key_variable in missing_credentials_message(spec.name)
    assert "GOOGLE_API_KEY" in missing_credentials_message("google")
    assert "GOOGLE_API_KEY" in missing_credentials_message("google_genai")

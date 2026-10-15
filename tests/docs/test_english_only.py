# ruff: noqa: SIM905
"""Guard: code, comments, texts and scripts are English; German only where it is the subject.

The rule: identifiers, comments, docstrings, error and log messages, CLI and UI texts,
test names, scripts and commit messages are English. German appears only in data that must be
German to test or demonstrate German behavior (the docs are guarded by `test_docs_english.py`).

The check scans every text file under `src`, `tests`, `scripts`, `examples` and the top-level
configuration. A file is flagged when it contains an umlaut or eszett, or a line with two
distinct common German function words, or one typical German business word (a small curated
list, matched on whole words). Flagged files must
be listed in `ALLOWED_GERMAN` (or match `ALLOWED_GLOBS`) with the reason. The list is kept tight:
an entry whose file no longer holds any German also fails, so the list cannot rot.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("src", "tests", "scripts", "examples")
SCANNED_SUFFIXES = {
    ".py", ".js", ".html", ".css", ".json", ".toml", ".yaml", ".yml", ".txt", ".csv", ".sh",
    ".tmpl", ".ini", ".cfg", ".md",
}  # fmt: skip
SKIPPED_PARTS = {"__pycache__", ".venv", ".venv-dev", "node_modules", ".mypy_cache", ".ruff_cache"}

# Why a file may hold German. Every entry names one of these (plus detail).
LEXICON = "functional German language support: word lists and patterns that recognize German input"
DATA = "German sample data: German amounts, headers, names or sentences used as input"
DOC_EXAMPLE = "docstring example that shows German-format data being profiled"
UNICODE = "non-ASCII round-trip data (umlauts must survive storage and transport)"

ALLOWED_GERMAN: dict[str, str] = {
    "tests/unit/test_toolkits_knowledge.py": "German sample passages for corpus language detection",
    # --- src: German recognition (no German user-facing text) ---
    "src/awa/testing/contracts/run_log.py": UNICODE + " (stored step results)",
    "src/awa/blueprints/_fit.py": LEXICON + " (bilingual keyword and column-name matching)",
    "src/awa/blueprints/_data_design.py": DOC_EXAMPLE,
    "src/awa/blueprints/_project.py": DOC_EXAMPLE,
    "src/awa/data/_infer.py": LEXICON + " (German month names, units, decimal-comma samples)",
    "src/awa/data/_injection.py": LEXICON + " (German prompt-injection phrases, language guess words)",
    "src/awa/data/_pdf.py": LEXICON + " (German document titles)",
    "src/awa/data/_pii.py": LEXICON + " (German column-name hints)",
    "src/awa/data/_render.py": LEXICON + " (German column words)",
    "src/awa/data/_stats.py": LEXICON + " (German totals-row words)",
    "src/awa/toolkits/documents.py": LEXICON + " (German stop words for search)",
    "src/awa/toolkits/knowledge.py": LEXICON + " (German stop words for search)",
    "src/awa/ui/_data_cards.py": DOC_EXAMPLE,
    "src/awa/ui/_plain.py": LEXICON + " (German column words for number display)",
    "src/awa/testing/contracts/artifact_repository.py": UNICODE,
    "src/awa/testing/contracts/event_queue.py": UNICODE,
    "src/awa/testing/contracts/run_store.py": UNICODE,
    # --- examples: German benchmark and use-case data ---
    "examples/benchmarks/core/data_gen.py": DATA + " (German-language benchmark inputs)",
    "examples/benchmarks/core/offline.py": DATA + " (reads the German benchmark inputs)",
    "examples/benchmarks/core/04_document_intake/tools.py": DATA + " (German VAT id column)",
    "examples/benchmarks/core/03_graphql_lookup/data/suppliers.json": DATA,
    "examples/usecases/sales-operations/README.md": DATA + " (German requests and names quoted)",
    "examples/usecases/sales-operations/cases.yaml": DATA + " (German requests of the sales team)",
    "examples/usecases/sales-operations/make_cases.py": DATA + " (German requests and answers)",
    "examples/usecases/sales-operations/make_data.py": DATA + " (German names, places, goods)",
    "examples/usecases/email-inbox-triage/cases.yaml": DATA + " (the German-mail case)",
    "examples/microsoft/testdata/mails/mails.yaml": DATA + " (the German offer request with 12,5 %)",
    "examples/usecases/order-to-cash-exceptions/make_data.py": DATA,
    # --- tests: German data for German behavior ---
    "tests/examples/test_benchmarks_core.py": DATA,
    "tests/examples/test_data_first_fixtures.py": DATA,
    "tests/examples/test_data_first_scenarios.py": DATA,
    "tests/examples/test_usecase_messages.py": DATA + " (German mail in, German reply out)",
    "tests/fixtures/html_golden/golden.json": UNICODE,
    "tests/integration/test_postgres_persistence.py": UNICODE,
    "tests/integration/test_sql_connector_dialects.py": UNICODE,
    "tests/_helpers/build_scripts.py": DATA,
    "tests/_helpers/data_scenarios/checks.py": DATA,
    "tests/_helpers/data_scenarios/generate.py": DATA,
    "tests/_helpers/data_scenarios/harness.py": DATA,
    "tests/_helpers/data_scenarios/scenarios.py": DATA,
    "tests/_helpers/data_fixtures.py": DATA,
    "tests/unit/test_automation_triggers.py": UNICODE,
    "tests/unit/test_automation_cron.py": UNICODE,
    "tests/unit/test_core_redaction.py": "a German sentence that must not be redacted",
    "tests/unit/test_core_util.py": UNICODE,
    "tests/unit/test_directory_registry.py": UNICODE,
    "tests/unit/test_live_scripts.py": DATA,
    "tests/unit/test_observability_otel_content.py": UNICODE,
    "tests/unit/test_server_uploads_api.py": DATA + " (German file names and cp1252 amounts)",
    "tests/unit/test_ui_data.py": DATA,
    "tests/unit/test_ui_diagram.py": DATA,
    "tests/unit/test_toolkits_data.py": DATA,
    "tests/unit/test_toolkits_documents.py": DATA,
    "tests/unit/test_blueprints_process_build.py": DATA + " (a German price-list column renamed)",
    "tests/docs/test_docs_english.py": LEXICON + " (the guard that finds German prose in the docs)",
    "tests/unit/test_skills_builtin.py": LEXICON + " (the guard that finds German words in skills)",
    "tests/unit/test_connectors_sharepoint.py": UNICODE + " (SharePoint file and folder names)",
}  # fmt: skip

# Whole test families whose file name says they are about German-format data.
ALLOWED_GLOBS: dict[str, str] = {
    "tests/unit/test_data_*.py": DATA,
    "tests/unit/test_blueprints_*data*.py": DATA,
    "tests/unit/test_blueprints_design.py": DATA,
    "tests/unit/test_blueprints_fit*.py": DATA,
    "tests/unit/test_blueprints_prompt.py": DATA,
    "tests/unit/test_cli_data.py": DATA,
    "tests/build_inputs/*/make_data.py": DATA + " (German scans among the generated invoices)",
    "tests/build_inputs/*/project/data/*": DATA + " (profile of the generated files; not tracked)",
}

_UMLAUT = re.compile("[äöüÄÖÜß]")
_FUNCTION_WORDS = frozenset(
    "der das und ist nicht ein eine einen einem mit von zu den dem des auf im nur auch als wie "
    "bei oder nach aus noch wird werden wurde sind haben hat kann sich sein ihre ihr ich wir bitte "
    "danke hallo vielen guten".split()
)
_BUSINESS_PREFIXES = (
    "rechnung", "lieferant", "bestellung", "fehlermeldung", "einstellungen", "nachricht",
)  # fmt: skip
_BUSINESS_EXACT = frozenset(
    "betrag netto brutto fuer ueber koennen koennten muessen geprueft pruefen zurueck "
    "schluessel speichern abbrechen passwort".split()
)
_WORD = re.compile(r"[A-Za-z]+")


def _text_files() -> list[Path]:
    found: list[Path] = []
    for name in SCANNED_DIRS:
        for path in (ROOT / name).rglob("*"):
            if path.suffix in SCANNED_SUFFIXES and not SKIPPED_PARTS.intersection(path.parts):
                if path == Path(__file__).resolve():
                    continue  # this file lists German words on purpose
                found.append(path)
    found += list(ROOT.glob("*.toml")) + list(ROOT.glob("*.ini"))
    return sorted(found)


def _holds_german(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    if _UMLAUT.search(text):
        return True
    words = set(_WORD.findall(text.lower()))
    if words & _BUSINESS_EXACT or any(w.startswith(_BUSINESS_PREFIXES) for w in words):
        return True
    if len(words & _FUNCTION_WORDS) < 2:
        return False  # cheap whole-file test before the per-line one
    return any(
        len(set(_WORD.findall(line.lower())) & _FUNCTION_WORDS) >= 2 for line in text.splitlines()
    )


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_allowed(rel: str) -> bool:
    if rel in ALLOWED_GERMAN:
        return True
    return any(Path(rel).match(pattern) for pattern in ALLOWED_GLOBS)


_FLAGGED: dict[str, bool] = {}


def _flagged() -> dict[str, bool]:
    if not _FLAGGED:
        for path in _text_files():
            _FLAGGED[_rel(path)] = _holds_german(path)
    return _FLAGGED


def test_german_text_appears_only_in_listed_files() -> None:
    offenders = sorted(rel for rel, german in _flagged().items() if german and not _is_allowed(rel))
    assert not offenders, (
        "German text found outside the allow-list. Translate it to English, or, if the file is "
        "German sample data or German language support, add it to ALLOWED_GERMAN with the reason:\n"
        + "\n".join(offenders)
    )


def test_allow_list_has_no_stale_entries() -> None:
    flagged = _flagged()
    stale = sorted(rel for rel in ALLOWED_GERMAN if not flagged.get(rel, False))
    assert not stale, "Remove from ALLOWED_GERMAN (file gone or no German left):\n" + "\n".join(
        stale
    )


def test_every_allow_list_entry_names_a_reason() -> None:
    reasons = {**ALLOWED_GERMAN, **ALLOWED_GLOBS}
    assert all(len(reason) > 15 for reason in reasons.values())


def test_the_web_app_has_no_german_translation_file() -> None:
    ui = ROOT / "src" / "awa" / "ui"
    assert not [p for p in ui.rglob("*") if p.name in {"de.json", "de.js"}]

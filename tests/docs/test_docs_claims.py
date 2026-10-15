"""Guard against overclaiming wording in the README, the docs, the examples and the source.

The goal of awa is one thing; the text must say only what has been measured or proven. This test
scans the Markdown files at the top of the repository (of a change log only its top section),
pyproject.toml, docs/**, examples/**/*.md and the text of src/awa (docstrings, comments and UI
strings) for phrases that promise more than the code, the tests or the measurements show. Matching is
case-insensitive and works per sentence, so an allowed exception must stand in the same sentence.

To fix a failure, reword the sentence so it carries its limit (which tool is gated, what is
configured, what was measured). A sentence that is true and backed by a named test may be added to
ALLOWED_SENTENCES with the evidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (ROOT / "docs").is_dir() or not (ROOT / "README.md").is_file(),
    reason="docs/ is in the repository only",
)

# Sentences that are true in code and named by a test. Each entry: a lower-case fragment of the
# sentence and the evidence that backs it.
ALLOWED_SENTENCES: dict[str, str] = {
    "refuses to answer": "a build refuses to answer a question about its own task (CHANGELOG)",
    "send_interview_invite` refuses candidates": "tests/examples/test_usecases.py, shortlist check",
    # The positioning: every run tree has default ceilings that cannot be switched off.
    "hard limits enforced in code, approvals before gated actions": (
        "tests/unit/test_runtime_loops.py, test_without_limits_a_run_tree_has_a_request_ceiling"
    ),
    "hard limits in code, approvals, eval cases": "the short positioning, same evidence",
    "run inside hard limits enforced by code": "the README opening, same evidence",
}

# A sentence that quotes a phrase in order to deny it.
_DENIAL = re.compile(
    r"do not (promise|say|claim|write)|don't (promise|say|claim)|never (promise|say)|"
    r"forbidden|guard phrase|not production[- ]ready|is not (error|bug)[- ]free",
    re.IGNORECASE,
)


def _lacks(*words: str) -> Callable[[str], bool]:
    return lambda s: not any(w in s for w in words)


# (phrase regex, predicate on the lower-case sentence: True means "this is a violation").
_ALWAYS: Callable[[str], bool] = lambda s: True  # noqa: E731
PHRASES: list[tuple[str, Callable[[str], bool]]] = [
    (r"(anything|before anything) it cannot undo", _ALWAYS),
    (r"nothing that changes data or sends something happens without", _ALWAYS),
    (r"nothing that needs a person's ok", _ALWAYS),
    (r"what awa never does without asking", _ALWAYS),
    (r"cannot talk its way past", _lacks("configured", "gated")),
    (r"always gated", _ALWAYS),
    (
        r"(every write waits|writes wait for (human )?approval|wait for human approval)",
        _lacks("by default", "configured"),
    ),
    (r"hard (limits|caps)", _lacks("you set", "configured", "default")),
    (
        r"(all enforced in code|are enforced in code)",
        _lacks("configured", " set ", "max_steps", "condition"),
    ),
    (r"every step is bounded", _ALWAYS),
    (
        r"(never appear in|never shows them|removes every credential|credentials never reach)",
        _ALWAYS,
    ),
    (r"(always treated as data|treats it as data only|treated as data only)", _ALWAYS),
    (r"nothing leaves this computer", _ALWAYS),
    (
        r"(proven starting point|complete, tested|tested systems|tested processes|"
        r"tested business processes|tested team)",
        _ALWAYS,
    ),
    (r"tested like the core", _ALWAYS),
    (r"what you see on the page is what you get", _ALWAYS),
    (r"(works out of the box|never fails because)", _ALWAYS),
    (r"until the tests pass", _lacks("limited", "bounded")),
    (r"(passes every check|all of it working|one real automation|ready-to-run)", _ALWAYS),
    (r"(a few minutes|takes a few minutes)", _lacks("depends on", "usually")),
    (r"(never loses|nothing that was waiting is lost|keeps waiting runs safe)", _ALWAYS),
    (r"survives a restart", _lacks("orderly", "normal stop")),
    (
        r"(exactly once|always safe|cannot hang or exhaust|cannot touch the numbers|"
        r"cannot receive them|are never followed|is never published)",
        _ALWAYS,
    ),
    (r"(production[- ]ready|fehlerfrei|error-free|bug-free)", _lacks("not ", "n't")),
    (r"(proves a lot|so it always looks good)", _ALWAYS),
    (r"(tested links?|tested, configurable links?|ready-made, tested link)", _ALWAYS),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), pred) for p, pred in PHRASES]


def _scanned_files() -> Iterator[Path]:
    yield from sorted(ROOT.glob("*.md"))
    yield ROOT / "pyproject.toml"
    for p in sorted((ROOT / "docs").rglob("*")):
        if p.is_file() and p.suffix in {".md", ".html", ".yml", ".yaml"}:
            yield p
    examples = ROOT / "examples"
    if examples.is_dir():
        yield from sorted(examples.rglob("*.md"))
    for p in sorted((ROOT / "src" / "awa").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".html", ".js"}:
            yield p


def _text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    # A change log has one section per version; older sections are history, so only the top
    # section, the current version, is scanned.
    headings = list(re.finditer(r"^## \[", text, re.MULTILINE))
    return text[: headings[1].start()] if len(headings) > 1 else text


def _sentences(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line number, sentence) with line breaks and list markers folded into spaces."""
    pattern = r"(?:[^\n]|\n(?![ \t]*\n))+?(?:[.!?](?=\s|$)|(?=\n[ \t]*\n)|$)"
    for match in re.finditer(pattern, text):
        raw = match.group(0)
        yield text.count("\n", 0, match.start()) + 1, re.sub(r"\s+", " ", raw).strip()


def find_violations(path: Path, text: str) -> list[str]:
    hits: list[str] = []
    for line, sentence in _sentences(text):
        low = sentence.lower().replace("\u2019", "'")
        if _DENIAL.search(low) or any(fragment in low for fragment in ALLOWED_SENTENCES):
            continue
        for pattern, violates in _COMPILED:
            if pattern.search(low) and violates(low):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {sentence[:140]}")
                break
    return hits


def test_no_overclaiming_phrases() -> None:
    hits: list[str] = []
    for path in _scanned_files():
        if path.name == "test_docs_claims.py":
            continue
        hits.extend(find_violations(path, _text(path)))
    assert not hits, "overclaiming wording found (reword with its limit):\n" + "\n".join(hits)


@pytest.mark.parametrize(
    ("sentence", "flagged"),
    [
        ("Every write waits for approval.", True),
        ("Connector writes wait for approval by default.", False),
        ("The agent cannot receive them.", True),
        ("This is production-ready.", True),
        ("This is not production-ready.", False),
        ("Setup takes a few minutes.", True),
        ("Setup usually takes a few minutes.", False),
        ("It repairs until the tests pass.", True),
        ("It repairs, bounded by rounds, until the tests pass.", False),
        ("Instructions in emails are never followed.", True),
        ("Do not promise that nothing leaves this computer.", False),
    ],
)
def test_guard_detects_phrases(sentence: str, flagged: bool) -> None:
    assert bool(find_violations(ROOT / "README.md", sentence)) is flagged

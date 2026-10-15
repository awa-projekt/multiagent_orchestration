"""The guide (docs/guide) is executed, not only read.

HOW THE GUIDE IS TESTED. Pages run in the order of `mkdocs.yml`, one after the other, in one
temporary work folder (the reader's `awa-guide-work`), with scripted models instead of a real one.
An HTML comment on the line above a fenced block says what the test does with it (the comment is
invisible on the page). Paths are relative to the folder the reader's terminal is in, except for
`do` and `tree`, which use the work folder (what the reader sees in the file explorer).

  <!-- guide:run [model=NAME] [input=a|b|c] [exit=N|N,N,...] [shot=NAME] -->
      A `console` block. Every `$ awa ...` line is run; the lines after it are the expected
      output (`...` inside a line = any text, a line of only `...` = any number of lines).
      `$ cd DIR` moves the terminal. `input` is what the reader types at prompts (echoed after
      the prompt like a terminal does; hidden input is not echoed). `RUN_ID` in a command is the
      last process run a page showed. `shot=NAME` makes the block the source of the terminal image
      NAME: scripts/capture_screenshots.py replays the guide and draws the commands with their
      real output, and the marks it has for NAME, as that image.
  <!-- guide:file PATH [append] -->      the block is the text the reader saves in PATH.
  <!-- guide:contains PATH -->           the block is an excerpt of PATH (it must appear in it).
  <!-- guide:tree PATH -->               the block lists the files under PATH, one per line.
  <!-- guide:python [model=NAME] [save=FILE] -->
      A `python` block that is run (`save` also writes it to FILE, as the reader does). The next
      block, marked <!-- guide:expect -->, is what it prints.
  <!-- guide:do copy SRC DST | delete PATH | cd PATH | replace PATH OLD NEW -->
      Something the reader does by hand: copy a file or folder (`awa:` in front of SRC means the
      awa folder), delete one, open a terminal in a folder, change a text with an editor.

A block without a comment is an illustration: it is only checked for syntax (YAML, Python, JSON)
and for real commands and options (`awa ...`). Volatile text (run ids, times, durations, token
counts, paths, the version) is replaced by a placeholder on both sides before the comparison.
`model=desk` is the scripted invoice desk, `model=bench:SCENARIO` a scripted scenario of
examples/benchmarks/core (`intake` is `bench:04_document_intake`), with its fake network.

The test also checks the book itself: the nav and the pages agree, every marker is known, every
page of the tour has its numbered steps and every other page its In code box, every image exists,
has alt text and (with marks) the generated legend under it, every internal link and anchor
resolves, US spelling, no repository address, no package-index claim, no model id, every `awa`
command and option is real, counts equal the code, the texts the pictures were made with are the
texts on the pages, and every element the capture script marks in the web app (an id or a class)
is in the web app's sources. The sources of this test are in the repository only, so it skips
without them.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import logging
import os
import re
import shlex
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from _helpers.guide_terminal import FakeTerminal
from _helpers.polling import SUBPROCESS_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
GUIDE = DOCS / "guide"
SHOTS = DOCS / "assets" / "screenshots" / "guide"
CAPTURE = ROOT / "scripts" / "capture_screenshots.py"

pytestmark = pytest.mark.skipif(
    not GUIDE.is_dir() or not CAPTURE.is_file(),
    reason="docs/ and scripts/ are in the repository only",
)

# ---------------------------------------------------------------------------------- the book


def guide_pages() -> list[Path]:
    """The pages of the Guide section of mkdocs.yml, in reading order."""

    class Loader(yaml.SafeLoader):
        """mkdocs.yml has python tags for the diagram fence; the nav needs none of them."""

    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda *_: None)
    nav = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader)["nav"]  # noqa: S506

    def paths(node: Any) -> Iterator[str]:
        if isinstance(node, str):
            yield node
        elif isinstance(node, list):
            for item in node:
                yield from paths(item)
        elif isinstance(node, dict):
            for value in node.values():
                yield from paths(value)

    section = next(
        v for item in nav if isinstance(item, dict) for k, v in item.items() if k == "Guide"
    )
    return [DOCS / name for name in paths(section)]


FENCE = re.compile(r"^(?P<indent> *)(?P<ticks>`{3,}|~{3,})(?P<info>.*)$")
MARK = re.compile(r"^\s*<!--\s*guide:(?P<action>\w+)(?P<args>.*?)\s*-->\s*$")


def _short_path(page: Path) -> str:
    return page.relative_to(ROOT).as_posix() if page.is_relative_to(ROOT) else page.name


@dataclass
class Block:
    page: Path
    line: int  # of the fence
    lang: str
    text: str
    action: str = ""  # run | file | python | expect | "" (an illustration)
    args: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{_short_path(self.page)}:{self.line}"

    def option(self, name: str, default: str = "") -> str:
        for arg in self.args:
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
        return default


@dataclass
class Do:
    page: Path
    line: int
    args: list[str]

    @property
    def where(self) -> str:
        return f"{_short_path(self.page)}:{self.line}"


def parse(page: Path) -> list[Block | Do]:
    """The fenced blocks and the by-hand actions of a page, in order."""

    lines = page.read_text(encoding="utf-8").splitlines()
    items: list[Block | Do] = []
    pending: tuple[str, list[str]] | None = None
    index = 0
    while index < len(lines):
        text = lines[index]
        mark = MARK.match(text)
        if mark:
            action, args = mark["action"], shlex.split(mark["args"])
            if action == "do":
                items.append(Do(page, index + 1, args))
                pending = None
            else:
                pending = (action, args)
            index += 1
            continue
        fence = FENCE.match(text)
        if fence:
            start, indent, ticks = index, len(fence["indent"]), fence["ticks"]
            body: list[str] = []
            index += 1
            while index < len(lines) and not re.match(
                rf"^ *{ticks[0]}{{{len(ticks)},}}\s*$", lines[index]
            ):
                body.append(
                    lines[index][indent:] if lines[index][:indent].strip() == "" else lines[index]
                )
                index += 1
            lang = fence["info"].strip().split()[0] if fence["info"].strip() else ""
            action, args = pending if pending else ("", [])
            items.append(Block(page, start + 1, lang, "\n".join(body), action, args))
            pending = None
            index += 1
            continue
        if text.strip():
            pending = None
        index += 1
    return items


# ---------------------------------------------------------------------------------- comparing output

WORK_NAME = "C:/awa-guide-work"  # what the pages show for the reader's work folder

VOLATILE = [
    (re.compile(r"\bDuration: [\d.]+ s\b"), "Duration: DURATION"),
    (re.compile(r"\blatency p50 / p95 \| \d+ ms / \d+ ms"), "latency p50 / p95 | N ms / N ms"),
    (
        re.compile(r"\bmodel call p50 / p95 \| \d+ ms / \d+ ms"),
        "model call p50 / p95 | N ms / N ms",
    ),
    # A prefix hash changes with every change of an agent's prompt.
    (re.compile(r"`[0-9a-f]{16}` \(stable\)"), "`PREFIX` (stable)"),
    (re.compile(r"\| \d+ \| (\d+) \|$", re.MULTILINE), r"| N | \1 |"),
    # The same in the terminal tables of `awa eval` (aligned columns instead of `|`).
    (re.compile(r"\blatency p50 / p95 +\d+ ms / \d+ ms"), "latency p50 / p95  N ms / N ms"),
    (re.compile(r"\bmodel call p50 / p95 +\d+ ms / \d+ ms"), "model call p50 / p95  N ms / N ms"),
    (re.compile(r"\b[0-9a-f]{16} \(stable\)"), "PREFIX (stable)"),
    (re.compile(r"(?<= )\d+ +(\d+)$", re.MULTILINE), r"N  \1"),
    (re.compile(r"(?<![=\d.])\b\d+ tokens\b"), "N tokens"),
    (re.compile(r"\btokens=\d+"), "tokens=N"),
    (re.compile(r"\(\d+ in, \d+ out\)"), "(N in, N out)"),
    (re.compile(r"\bproc_[0-9a-f]{6,}\b"), "proc_ID"),
    (re.compile(r"\bevt_[0-9a-f]{6,}\b"), "evt_ID"),
    (re.compile(r"\b(up|bld)_[0-9a-f]{12,}\b"), r"\1_ID"),
    (re.compile(r"\b(thread|run)_[0-9a-f]{12,}\b"), r"\1_ID"),
    (re.compile(r"\b[0-9a-f]{32}\b"), "HEX32"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?"), "TIME"),
    (re.compile(r"\((\d+(?:\.\d+)?) ?(?:ms|s)\)"), "(DURATION)"),
    (re.compile(r"\bafter \d+(?:\.\d+)? ?(?:ms|s)\b"), "after DURATION"),
    (re.compile(r", \d+(?:\.\d+)? ?(?:ms|s)\]"), ", DURATION]"),
    (re.compile(r", \d+(?:\.\d+)? ?(?:ms|s)$", re.MULTILINE), ", DURATION"),
    (re.compile(r"\btime \d+(?:\.\d+)? ?(?:ms|s)\b"), "time DURATION"),
    (re.compile(r"\] \d+ model call"), "] N model call"),
    (re.compile(r"\bawa \d+\.\d+\.\d+\b"), "awa VERSION"),
    (re.compile(r"\bsha256:[0-9a-f]{16}\b"), "sha256:HASH"),
    (re.compile(r"\bclaimed by \S+"), "claimed by WORKER"),
]


def normalize(text: str, work: Path | None = None) -> str:
    text = text.replace("\r\n", "\n")
    if work is not None:
        for form in {str(work), work.as_posix(), str(work).replace("\\", "\\\\")}:
            text = text.replace(form, WORK_NAME)
    text = text.replace("\\", "/")
    for pattern, replacement in VOLATILE:
        text = pattern.sub(replacement, text)
    # Column widths of terminal tables follow the values: compare the columns, not the padding.
    return "\n".join(_COLUMNS.sub("  ", line.rstrip()) for line in text.strip().split("\n"))


_COLUMNS = re.compile(r"(?<=\S) {2,}")


def matches(expected: str, actual: str) -> bool:
    """`...` inside an expected line matches any text; a line of only `...` any number of lines."""

    want, got = expected.split("\n"), actual.split("\n")

    def line_ok(pattern: str, line: str) -> bool:
        parts = [re.escape(p) for p in pattern.split("...")]
        return re.fullmatch(".*".join(parts), line) is not None

    def walk(i: int, j: int) -> bool:
        while i < len(want):
            if want[i].strip() == "...":
                return any(walk(i + 1, k) for k in range(j, len(got) + 1))
            if j >= len(got) or not line_ok(want[i], got[j]):
                return False
            i, j = i + 1, j + 1
        return j == len(got)

    return walk(0, 0)


# ---------------------------------------------------------------------------------- the work folder


def load_capture() -> Any:
    spec = importlib.util.spec_from_file_location("capture_screenshots", CAPTURE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_screenshots"] = module
    spec.loader.exec_module(module)
    return module


class Work:
    """The reader's work folder and the commands run in it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, capture: Any = None) -> None:
        self.root = root
        self.cwd = root
        self.monkeypatch = monkeypatch
        self.capture = capture or load_capture()
        self.shots: dict[str, list[tuple[str, str]]] = {}  # `shot=NAME` blocks: commands and output
        self.run_id = "proc_missing"  # the last process run a page showed; `RUN_ID` stands for it

    def resolve(self, name: str, base: Path | None = None) -> Path:
        if name.startswith("awa:"):
            return ROOT / name[4:]
        return (base or self.cwd) / name

    @contextlib.contextmanager
    def session(self, model: str, typed: list[str]) -> Iterator[FakeTerminal]:
        """Environment, scripted model, terminal and folder as the reader's command would see them."""

        import getpass

        from awa.core.settings import Settings
        from awa.models import ModelRegistry

        terminal = FakeTerminal(typed)
        env = {k: v for k, v in os.environ.items() if k.upper() in self.capture._KEEP}
        env.update(COLUMNS="92", NO_COLOR="1", PYTHONUTF8="1")
        with self.monkeypatch.context() as patch:
            patch.setattr(os, "environ", env)
            patch.setitem(Settings.model_config, "env_file", ".env")  # as outside the test suite
            patch.setattr(sys, "stdout", terminal)
            patch.setattr(sys, "stderr", terminal)
            patch.setattr(sys, "stdin", terminal.stdin)
            # Warnings of the logging module reach the terminal the way plain Python prints them (the
            # message alone), in the test run and in the capture run alike.
            log = logging.StreamHandler(terminal)
            log.setLevel(logging.WARNING)
            patch.setattr(logging.getLogger(), "handlers", [log])
            patch.setattr(logging.getLogger(), "level", logging.WARNING)
            patch.setattr(
                getpass,
                "getpass",
                lambda prompt="": (terminal.write(prompt + "\n"), terminal.read_line(False))[1],
            )
            registry = self.capture.scripted_registry(model, self.cwd)
            patch.setattr(ModelRegistry, "_default", registry)
            patch.chdir(self.cwd)
            with registry.network() if registry.network else contextlib.nullcontext():
                yield terminal

    def awa(self, argv: list[str], model: str, typed: list[str]) -> tuple[int, str, list[str]]:
        from awa.cli import main

        with self.session(model, typed) as terminal:
            try:
                code = main(argv)
            except SystemExit as stop:  # argparse exits for --help and for usage errors
                code = stop.code if isinstance(stop.code, int) else 1
            return code, terminal.getvalue(), terminal.typed

    def python(self, source: str, model: str) -> str:
        with self.session(model, []) as terminal:
            # a script run in its folder finds its neighbors
            self.monkeypatch.syspath_prepend(str(self.cwd))
            sys.modules.pop("tools", None)
            exec(compile(source, "<guide>", "exec"), {"__name__": "__guide__"})  # noqa: S102
            return terminal.getvalue()

    def do(self, action: Do) -> None:
        verb, *rest = action.args
        if verb == "copy":
            source, target = self.resolve(rest[0], self.root), self.resolve(rest[1], self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        elif verb == "cd":  # open a terminal in this folder
            self.cwd = self.resolve(rest[0], self.root)
        elif verb == "replace":  # change a text in a file with an editor
            target = self.resolve(rest[0], self.root)
            text = target.read_text(encoding="utf-8")
            assert rest[1] in text, f"{action.where}: {rest[0]} has no {rest[1]!r}"
            target.write_text(text.replace(rest[1], rest[2]), encoding="utf-8", newline="\n")
        elif verb == "delete":
            target = self.resolve(rest[0], self.root)
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        else:
            raise AssertionError(f"{action.where}: unknown action {verb!r}")


def split_transcript(text: str) -> list[tuple[str, str]]:
    """`$ command` lines and the expected output after each."""

    steps: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("$ "):
            steps.append((line[2:].strip(), []))
        elif steps:
            steps[-1][1].append(line)
        elif line.strip():
            raise AssertionError(f"text before the first `$ ` line: {line!r}")
    return [(command, "\n".join(output)) for command, output in steps]


def run_block(work: Work, block: Block) -> list[str]:
    """Run a `guide:run` block; return the problems."""

    problems: list[str] = []
    model = block.option("model", "desk")
    typed = block.option("input").split("|") if block.option("input") else []
    # One exit code for every command, or one per command (the last one repeats).
    exits = [int(code) for code in block.option("exit", "0").split(",")]
    for number, (command, expected) in enumerate(split_transcript(block.text)):
        want_exit = exits[min(number, len(exits) - 1)]
        command = command.replace("RUN_ID", work.run_id)
        words = shlex.split(command)
        if words[0] == "cd":
            work.cwd = work.resolve(words[1])
            continue
        if words[0] != "awa":
            problems.append(f"{block.where}: only `awa` and `cd` run; got {command!r}")
            continue
        code, output, typed = work.awa(words[1:], model, typed)
        if block.option("shot"):
            work.shots.setdefault(block.option("shot"), []).append((command, output))
        work.run_id = (re.findall(r"\bproc_[0-9a-f]+\b", output) or [work.run_id])[-1]
        if code != want_exit:
            problems.append(
                f"{block.where}: `{command}` ended with {code}, expected {want_exit}:\n{output}"
            )
            continue
        got, want = normalize(output, work.root), normalize(expected, work.root)
        if not matches(want, got):
            problems.append(
                f"{block.where}: output of `{command}` differs.\n--- page\n{want}\n--- actual\n{got}"
            )
    return problems


def run_guide(work: Work, pages: list[Path] | None = None) -> list[str]:
    problems: list[str] = []
    for page in pages or guide_pages():
        items = parse(page)
        for position, item in enumerate(items):
            if isinstance(item, Do):
                work.do(item)
                continue
            if item.action == "run":
                problems += run_block(work, item)
            elif item.action == "file":
                target = work.resolve(item.args[0])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open(
                    "a" if "append" in item.args else "w", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(item.text.rstrip("\n") + "\n")
            elif item.action == "contains":
                excerpt_of = work.resolve(item.args[0])
                if not excerpt_of.is_file():  # an earlier step failed; its problem says why
                    problems.append(f"{item.where}: {item.args[0]} was not written")
                    continue
                text = excerpt_of.read_text(encoding="utf-8").replace("\r\n", "\n")
                if item.text.strip("\n") not in text:
                    problems.append(
                        f"{item.where}: {item.args[0]} does not contain this text:\n{item.text}"
                    )
            elif item.action == "tree":
                root = work.resolve(item.args[0], work.root)
                found = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and ".awa" not in path.parts
                )
                if not matches(item.text.strip("\n"), "\n".join(found)):
                    problems.append(
                        f"{item.where}: the files under {item.args[0]} are:\n" + "\n".join(found)
                    )
            elif item.action == "python":
                expected = (
                    None  # the output belongs to the block right after, up to the next script
                )
                for later in items[position + 1 :]:
                    if isinstance(later, Block) and later.action in ("python", "expect"):
                        expected = later if later.action == "expect" else None
                        break
                if item.option("save"):  # the reader saves the script in a file, then runs it
                    (work.cwd / item.option("save")).write_text(
                        item.text.rstrip() + "\n", encoding="utf-8", newline="\n"
                    )
                try:
                    output = work.python(item.text, item.option("model", "desk"))
                except Exception as error:  # noqa: BLE001
                    problems.append(
                        f"{item.where}: the code failed: {type(error).__name__}: {error}"
                    )
                    continue
                if expected is not None and not matches(
                    normalize(expected.text, work.root), normalize(output, work.root)
                ):
                    problems.append(
                        f"{item.where}: printed text differs.\n"
                        f"--- page\n{expected.text}\n--- actual\n{output}"
                    )
    return problems


def test_the_guide_runs_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = Work(tmp_path / "awa-guide-work", monkeypatch)
    work.root.mkdir()
    problems = run_guide(work)
    assert not problems, "\n\n".join(problems)


# ---------------------------------------------------------------------------------- the book itself


def all_pages() -> list[Path]:
    return sorted(GUIDE.rglob("*.md"))


def text_of(page: Path) -> str:
    return page.read_text(encoding="utf-8")


def prose(text: str) -> str:
    """The page without fenced blocks and inline code (what a reader reads as sentences)."""

    text = re.sub(r"(?ms)^ *(`{3,}|~{3,}).*?^ *\1 *$", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def outside_fences(text: str) -> str:
    return re.sub(r"(?ms)^ *(`{3,}|~{3,}).*?^ *\1 *$", "", text)


def slug(heading: str) -> str:
    from markdown.extensions.toc import slugify

    return slugify(re.sub(r"[*_`]", "", heading), "-")


def anchors_of(page: Path) -> set[str]:
    text = outside_fences(text_of(page))
    found = {
        slug(m.group(1).split("{")[0].strip()) for m in re.finditer(r"(?m)^#{1,6} +(.+?)\s*$", text)
    }
    found |= set(re.findall(r"\{[^}]*#([\w.-]+)[^}]*\}", text))
    return found


def test_the_build_takes_the_files_the_guide_hands_it() -> None:
    """The tour (steps 3 and 4) and the page on your files say that awa builds on the files of the
    user, asks when the words do not fit them, and runs the process on each file. The tree must have it."""

    import inspect

    from awa.blueprints import build_project, run_project
    from awa.cli import COMMANDS

    taken = set(inspect.signature(build_project).parameters)
    assert {"data", "notes", "mask", "answers"} <= taken, (
        f"awa.blueprints.build_project takes {taken}"
    )
    assert "select" in inspect.signature(run_project).parameters
    assert "data" in COMMANDS


def test_the_guide_is_in_the_nav_and_every_page_is_there() -> None:
    listed = guide_pages()
    assert all(page.is_file() for page in listed), [p.name for p in listed if not p.is_file()]
    assert set(all_pages()) == {page for page in listed if page.is_relative_to(GUIDE)}, (
        "a page under docs/guide is not in the nav (or the reverse)"
    )
    assert listed[0] == GUIDE / "tour.md", "the guided tour opens the Guide"


def test_every_marker_is_known_and_next_to_what_it_acts_on() -> None:
    known = {"run", "file", "contains", "tree", "python", "expect", "do"}
    problems = []
    for page in all_pages():
        lines = text_of(page).splitlines()
        for number, line in enumerate(lines, start=1):
            mark = MARK.match(line)
            if not mark:
                continue
            if mark["action"] not in known:
                problems.append(f"{_short_path(page)}:{number}: unknown marker {mark['action']!r}")
            elif mark["action"] != "do":
                following = next((later for later in lines[number:] if later.strip()), "")
                if not FENCE.match(following):
                    problems.append(
                        f"{_short_path(page)}:{number}: marker without a block under it"
                    )
    assert not problems, "\n".join(problems)


def test_the_tour_is_one_linear_page_and_every_task_page_ends_in_code() -> None:
    """The tour has numbered steps in order and ends with what to read next; every other page of
    the Guide states its purpose in its first line and ends with an "In code" box."""

    tour = text_of(GUIDE / "tour.md")
    steps = [int(n) for n in re.findall(r"(?m)^## Step (\d+): ", tour)]
    assert steps == list(range(1, len(steps) + 1)) and len(steps) >= 10, steps
    assert "## What next" in tour
    problems = []
    for page in all_pages():
        if page.name == "tour.md":
            continue
        text = text_of(page)
        first = next(line for line in text.splitlines()[1:] if line.strip())
        if first.startswith(("```", "|", "!!!", "<!--", "-", "#")):
            problems.append(
                f"{_short_path(page)}: the first line after the title is not a sentence"
            )
        last_box = text.rfind("!!! ")
        if last_box < 0 or not text[last_box:].startswith('!!! example "In code"'):
            problems.append(f"{_short_path(page)}: does not end with an In code box")
    assert not problems, "\n".join(problems)


def test_every_image_exists_has_alt_text_and_its_legend() -> None:
    index = json.loads((SHOTS / "index.json").read_text(encoding="utf-8"))
    problems, used = [], set()
    for page in all_pages():
        lines = text_of(page).splitlines()
        for number, line in enumerate(lines, start=1):
            image = re.search(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)", line)
            if not image:
                continue
            where = f"{_short_path(page)}:{number}"
            target = (page.parent / image["path"]).resolve()
            if not target.is_file():
                problems.append(f"{where}: image {image['path']} is missing")
                continue
            if len(image["alt"].split()) < 5:
                problems.append(
                    f"{where}: the alt text of {image['path']} is too short to describe the picture"
                )
            if target.parent != SHOTS.resolve():
                continue
            name = target.stem
            used.add(name)
            marks = index.get(name, {}).get("marks", [])
            include = f'--8<-- "docs/assets/screenshots/guide/{name}.legend.txt"'
            under = next((later.strip() for later in lines[number:] if later.strip()), "")
            if marks and under != include:
                problems.append(f"{where}: the legend of {name} must follow the image: {include}")
            if not marks and under == include:
                problems.append(f"{where}: {name} has no marks but the page includes a legend")
            legend = SHOTS / f"{name}.legend.txt"
            expected = (
                "*In the picture:*\n\n"
                + "\n".join(f"{i}. {m}" for i, m in enumerate(marks, 1))
                + "\n"
            )
            if marks and (not legend.is_file() or legend.read_text(encoding="utf-8") != expected):
                problems.append(
                    f"{where}: {name}.legend.txt does not match the marks of index.json"
                )
    orphans = sorted({p.stem for p in SHOTS.glob("*.png")} - used)
    assert not orphans, f"images that no page shows: {orphans}"
    assert not problems, "\n".join(problems)


@pytest.mark.skipif(importlib.util.find_spec("markdown") is None, reason="needs the docs extra")
def test_every_internal_link_and_anchor_resolves() -> None:
    problems = []
    for page in all_pages():
        for number, line in enumerate(outside_fences(text_of(page)).splitlines(), start=1):
            for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", line):
                if re.match(r"(https?:|mailto:)", target):
                    continue
                path, _, anchor = target.partition("#")
                resolved = page if not path else (page.parent / path).resolve()
                where = f"{_short_path(page)}:{number}: {target}"
                if not resolved.exists():
                    problems.append(f"{where} does not exist")
                elif anchor and resolved.suffix == ".md" and anchor not in anchors_of(resolved):
                    problems.append(
                        f"{where}: no heading or id {anchor!r} in {_short_path(resolved)}"
                    )
    assert not problems, "\n".join(problems)


US_ONLY = [
    "colour", "colours", "behaviour", "behaviours", "organisation", "organise", "organised", "centre",
    "licence", "licences", "analyse", "analysed", "analysing", "catalogue", "catalogues", "favour",
    "favourable", "honour", "programme", "grey", "whilst", "learnt", "recognise", "recognised",
    "summarise", "summarised", "summarises", "customise", "initialise", "optimise", "judgement",
    "defence", "defences", "cheque", "labelled", "labelling", "travelled", "modelling", "neighbour",
    "neighbouring", "serialise", "serialised", "serialisable", "normalize", "normalised", "sanitise",
    "sanitised", "minimise", "minimisation", "stabilise", "utilise", "prioritise", "authorised",
    "artefact", "artefacts", "maths", "parameterised",
]  # fmt: skip
NOT_ALLOWED = [
    (r"github\.com|gitlab|bitbucket|git clone", "a repository address"),
    (r"pypi|pip install awa|uv add awa|from the package index", "a package-index claim"),
    (r"(?<![\w-])(?:gpt|claude|gemini|mistral)-[0-9a-z]", "a model id (use <provider>:<model>)"),
    (r"(?<![\w-])llama-?[0-9]", "a model id (use <provider>:<model>)"),
    (r"\bTODO\b|\bFIXME\b|\bTBD\b|coming soon|lorem ipsum", "an unfinished marker"),
]


def test_wording_us_spelling_no_repository_no_model_id() -> None:
    problems = []
    files = [*all_pages(), *SHOTS.glob("*.legend.txt")]
    for page in files:
        text = text_of(page) if page.suffix == ".md" else page.read_text(encoding="utf-8")
        for word in US_ONLY:
            if re.search(rf"\b{word}\b", prose(text), re.IGNORECASE):
                problems.append(f"{_short_path(page)}: British spelling {word!r}")
        for pattern, what in NOT_ALLOWED:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                problems.append(f"{_short_path(page)}: {what}: {match.group(0)!r}")
    assert not problems, "\n".join(problems)


def blocks_of(page: Path) -> list[Block]:
    return [item for item in parse(page) if isinstance(item, Block)]


def test_python_yaml_and_json_blocks_are_valid() -> None:
    problems = []
    for page in all_pages():
        for block in blocks_of(page):
            try:
                if block.lang == "python":
                    ast.parse(block.text)
                elif block.lang in ("yaml", "yml"):
                    yaml.safe_load(block.text)
                elif block.lang == "json":
                    json.loads(block.text)
            except (SyntaxError, yaml.YAMLError, ValueError) as error:
                problems.append(f"{block.where}: {block.lang} does not parse: {error}")
    assert not problems, "\n".join(problems)


def test_javascript_blocks_parse_and_the_endpoints_exist_in_the_contract() -> None:
    """The JavaScript of the web-frontend chapter is checked with `node --check` when node is there,
    and every endpoint the chapter names is one of the paths of the OpenAPI contract of the server."""

    import subprocess
    import tempfile

    page = GUIDE / "web-frontend.md"
    problems = []
    node = shutil.which("node")
    for block in blocks_of(page):
        if block.lang in ("js", "javascript") and node:
            with tempfile.TemporaryDirectory() as folder:
                script = Path(folder) / "block.mjs"
                script.write_text(block.text, encoding="utf-8")
                done = subprocess.run(
                    [node, "--check", str(script)], capture_output=True, text=True, check=False
                )
                if done.returncode != 0:
                    problems.append(
                        f"{block.where}: JavaScript does not parse: {done.stderr[-300:]}"
                    )
    contract = json.loads((DOCS / "develop" / "data-api.openapi.json").read_text(encoding="utf-8"))

    def shape(path: str) -> str:
        parts = path.strip("/").split("/")
        for index, part in enumerate(parts):
            is_value = "{" in part or part.isupper() or (index and parts[index - 1] == "files")
            if is_value:
                parts[index] = "{}"
        return "/" + "/".join(parts)

    known = {shape(path) for path in contract["paths"]} | {"/v1/health"}
    outside_code = re.sub(r"(?ms)^[ ]*```(?:python|js|text|json)\n.*?^[ ]*```", "", text_of(page))
    for path in sorted(
        set(re.findall(r"(?<![A-Za-z_{])/v1/[A-Za-z0-9_./{}-]*[A-Za-z0-9}]", outside_code))
    ):
        if shape(path) not in known:
            problems.append(
                f"{_short_path(page)}: {path} is not an endpoint of the OpenAPI contract"
            )
    assert not problems, "\n".join(problems)


JS_DRIVER = """
import fs from "node:fs";
const key = process.env.KEY;
const realFetch = globalThis.fetch;
globalThis.fetch = (url, options = {}) =>
  realFetch(url, { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${key}` } });
const dir = process.env.FILES;
const names = fs.readdirSync(dir).filter((n) => !n.endsWith(".db")).sort();  // the ledger is a system, not an upload
const files = names.map((n) => new File([fs.readFileSync(`${dir}/${n}`)], n, { type: "application/octet-stream" }));
const up = await upload(files);
console.log("upload", up.files.map((f) => `${f.name}:${f.kind}:${f.status}`).join(" "));
const started = await post("/v1/builds", { description: process.env.DESCRIPTION, upload_id: up.upload_id });
console.log("build", started.status);
const seen = [];
const last = await follow(started.build_id, (event) => seen.push(event.type));
console.log("events", seen.length, seen[0], seen.at(-1), "last", last);
console.log("again", await follow(started.build_id, (event) => seen.push(event.type), last));
const bad = new File([new Uint8Array([1, 2, 3])], "fake.pdf", { type: "application/pdf" });
try {
  await upload([bad]);
} catch (error) {
  console.log("error", error.message.split(":")[0]);
}
"""


def test_the_javascript_of_the_web_frontend_chapter_works_against_a_live_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The functions `upload`, `post` and `follow` of the web-frontend chapter are run by node against a real
    uvicorn server (scripted model): files go up, a build runs, its events are followed and
    followed again from the last number, and a refused upload is an Error with the code."""

    import subprocess
    import threading
    import time

    node = shutil.which("node")
    uvicorn = pytest.importorskip("uvicorn")
    if not node:
        pytest.skip("node is not installed")
    from awa.models import ModelRegistry
    from awa.server import ApiKeyAuthenticator, DataOptions, Principal, create_app

    capture = load_capture()
    registry = capture.scripted_registry("files")
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    key = "pick-a-long-random-secret"
    app = create_app(
        [],
        authenticator=ApiKeyAuthenticator(
            {key: Principal("web-app", tenant="acme", roles={"builder"})}
        ),
        data=DataOptions(root="awa-data", roles={"builder"}),
    )
    port = capture.free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        page = text_of(GUIDE / "web-frontend.md")
        code = "\n".join(re.findall(r"(?ms)^```js\n(.*?)^```", page))
        assert 'const API = "/awa";' in code
        script = tmp_path / "page.mjs"
        script.write_text(
            code.replace('const API = "/awa";', "const API = process.env.BASE;") + "\n" + JS_DRIVER,
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "BASE": f"http://127.0.0.1:{port}",
            "KEY": key,
            "FILES": str(GUIDE / "files"),
            "DESCRIPTION": capture.FILES_PROCESS,
        }
        done = subprocess.run(
            [node, str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    finally:
        server.should_exit = True
        thread.join(10)
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.splitlines() == [
        "upload INV-2041.pdf:pdf:ok INV-2042.pdf:pdf:ok purchase_orders.csv:csv:ok",
        "build queued",
        "events 15 build.started build.completed last 15",
        "again 15",
        "error awa.data.type_mismatch",
    ]


def test_mermaid_diagrams_are_well_formed_and_parse_when_the_checker_is_there() -> None:
    problems = []
    for page in all_pages():
        for block in blocks_of(page):
            if block.lang != "mermaid":
                continue
            text = block.text.strip()
            if not re.match(r"(flowchart|graph) (TD|TB|LR|RL|BT)\b", text):
                problems.append(
                    f"{block.where}: a diagram must start with flowchart and a direction"
                )
            for opening, closing in ("[]", "()", "{}"):
                if text.count(opening) != text.count(closing):
                    problems.append(f"{block.where}: unbalanced {opening}{closing}")
    assert not problems, "\n".join(problems)
    checker = os.environ.get("AWA_MERMAID_CHECK")  # a folder with check.mjs, jsdom and mermaid
    if checker and shutil.which("node") and (Path(checker) / "check.mjs").is_file():
        import subprocess

        for page in all_pages():
            if "```mermaid" in text_of(page):
                done = subprocess.run(
                    ["node", "check.mjs", str(page)],
                    cwd=checker,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert " 0 errors" in done.stdout, f"{_short_path(page)}: {done.stdout[-400:]}"


def help_text(words: list[str]) -> str:
    from awa.cli import main

    buffer = io.StringIO()
    with (
        contextlib.redirect_stdout(buffer),
        contextlib.redirect_stderr(buffer),
        contextlib.suppress(SystemExit),
    ):
        main([*words, "--help"])
    return buffer.getvalue()


def test_every_awa_command_and_option_in_the_guide_is_real() -> None:
    from awa.cli import COMMANDS

    problems = []
    for page in all_pages():
        for block in blocks_of(page):
            if block.lang not in ("console", "bash", "powershell", "shell", "text", ""):
                continue
            for line in block.text.splitlines():
                command = line[2:] if line.startswith("$ ") else (line if not block.action else "")
                if not command.startswith("awa "):
                    continue
                try:
                    words = shlex.split(command, comments=True)[1:]
                except ValueError:
                    continue
                while words and words[0] in ("--verbose", "--version"):
                    words = words[1:]
                if not words or words[0].startswith("-"):
                    continue
                name = words[0]
                where = f"{block.where}: {command}"
                if name not in COMMANDS:
                    problems.append(f"{where}: no such command {name!r}")
                    continue
                helped = help_text([name])
                depth = 1  # `awa setup services up`: follow the subcommands down
                while len(words) > depth and words[depth] in set(
                    re.findall(r"(?m)^    ([\w-]+)\s{2,}\S", helped)
                ):
                    depth += 1
                    helped = help_text(words[:depth])
                for token in words:
                    option = token.split("=")[0]
                    if re.fullmatch(r"--[a-z][\w-]*", option) and option not in helped:
                        problems.append(
                            f"{where}: {option} is not an option of `awa {' '.join(words[:2])}`"
                        )
    assert not problems, "\n".join(problems)


def test_tools_and_connectors_the_guide_names_exist() -> None:
    from awa.connectors import list_connectors
    from awa.toolkits import catalog

    tools = {entry.name for entry in catalog()}
    connectors = {c.name for c in list_connectors()}
    problems = []
    for page in all_pages():
        text = text_of(page)
        for name in re.findall(r"builtin:([a-z_]+)", text):
            if name not in tools:
                problems.append(f"{_short_path(page)}: builtin:{name} is not a builtin tool")
        for name in re.findall(r"^\s*use: (\w+)", text, re.MULTILINE):
            if name not in connectors:
                problems.append(f"{_short_path(page)}: use: {name} is not a connector")
    assert not problems, "\n".join(problems)


def test_the_counts_the_guide_quotes_match_the_code() -> None:
    spec = importlib.util.spec_from_file_location(
        "docs_facts_for_guide", ROOT / "tests" / "docs" / "test_docs_facts.py"
    )
    assert spec and spec.loader
    facts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(facts)
    truth, drift = facts._truth(), []
    for page in all_pages():
        for number, line in enumerate(text_of(page).splitlines(), 1):
            for fact, pattern, other in facts.CLAIMS:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    value = facts._number(match.group(1))
                    if value is not None and value != truth[fact] and value not in other:
                        drift.append(
                            f"{_short_path(page)}:{number}: {match.group(0)!r}, "
                            f"the code has {truth[fact]}"
                        )
    assert not drift, "\n".join(drift)


def test_the_texts_the_pictures_were_made_with_are_the_texts_on_the_pages() -> None:
    capture = load_capture()
    tour = " ".join(text_of(GUIDE / "tour.md").replace("> ", "").split())
    assert capture.GUIDE_DATA_NOTE in tour, "the note of step 3 differs from the capture script"
    assert capture.GUIDE_PROCESS in tour, (
        "the description of step 4 differs from the capture script"
    )
    assert capture.GUIDE_TRIGGER_INPUT in tour, (
        "the trigger text of step 9 differs from the capture script"
    )
    for name, data in capture.guide_sample_files().items():
        assert (GUIDE / "files" / name).read_bytes() == data, (
            f"{name} is not what the generator makes"
        )


def test_every_element_the_capture_marks_is_in_the_web_app() -> None:
    """The pictures mark elements of the web app by their id and class. When the interface
    renames one, this test names it, before anybody runs a browser."""

    import inspect
    import textwrap

    ui = ROOT / "src" / "awa" / "ui"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ui / "page.html", *sorted((ui / "js").glob("*.js"))]
        if path.is_file()
    )
    if not sources:
        pytest.skip("the web app sources are not in this tree")
    capture = load_capture()
    lost = []
    for name, function in inspect.getmembers(capture, inspect.isfunction):
        drives_a_page = {"page", "browser"} & set(inspect.signature(function).parameters)
        if not name.startswith("guide_") or not drives_a_page:
            continue
        selectors: list[str] = []
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(function)))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Mark"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                selectors.append(str(node.args[0].value))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for found in re.finditer(
                    r"""querySelector(?:All)?\(\s*(?P<q>['"])(?P<css>.+?)(?P=q)""", node.value
                ):
                    selectors.append(found["css"])
        for css in selectors:
            for token in re.findall(r"[#.]([A-Za-z][\w-]*)", re.sub(r"\[[^\]]*\]", "", css)):
                # "trigger-" + name
                built = re.escape(token.rsplit("-", 1)[0] + "-") + r"""["'`]\s*\+"""
                if (
                    not token.startswith("guide-")
                    and token not in sources
                    and not re.search(built, sources)
                ):
                    lost.append(f"{name}: {css!r} names {token}")
    assert not lost, (
        "the capture script marks elements the web app does not have; update the marks in "
        "scripts/capture_screenshots.py or the interface:\n" + "\n".join(sorted(set(lost)))
    )


def test_the_work_folder_in_the_pictures_is_the_one_the_pages_name() -> None:
    capture = load_capture()
    assert str(capture.guide_folder(None)).replace("\\", "/") in (WORK_NAME, "/tmp/awa-guide-work")

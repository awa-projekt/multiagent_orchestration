"""The README and the docs state facts about the code; this test keeps them true.

Counts (tools, connectors, skills, recipes, use cases, templates, examples) are derived from the
code and compared with every number the docs quote; the README's blueprint example is the real
output of `awa validate` and `awa show`; every relative link and image of the
README exists. The docs folder is part of the repository only, so the tests skip without it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import awa
from awa.blueprints import Blueprint
from awa.blueprints.scaffold import TEMPLATES
from awa.cli import COMMANDS, main
from awa.connectors import list_connectors
from awa.connectors._catalog import CORE_CONNECTORS
from awa.core.settings import Settings
from awa.skills import SkillRegistry
from awa.toolkits import catalog

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DEVELOPERS = ROOT / "DEVELOPERS.md"  # holds the blueprint example of the front page

pytestmark = pytest.mark.skipif(
    not (ROOT / "docs").is_dir() or not README.is_file(), reason="docs/ is in the repository only"
)

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
}  # fmt: skip
PAGES = [
    "*.md",
    "examples/README.md",
    "docs/index.md",
    "docs/reference",
    "docs/start",
    "docs/guide",
    "docs/about",
    "docs/develop",
]


def _folders(kind: str) -> list[Path]:
    return [p for p in (ROOT / "examples" / kind).iterdir() if (p / "blueprint.yaml").is_file()]


def _truth() -> dict[str, int]:
    return {
        "tools": len(catalog()),
        "connectors": len(list_connectors()),
        "core_connectors": len(CORE_CONNECTORS),
        "preview_connectors": len([c for c in list_connectors() if c.name not in CORE_CONNECTORS]),
        "skills": len(SkillRegistry.builtin()),
        "recipes": len(_folders("recipes")),
        "usecases": len(_folders("usecases")),
        "templates": len(TEMPLATES),
        "commands": len(COMMANDS),
        "examples": len(list((ROOT / "examples").glob("[0-9][0-9]_*.py"))),
        "names": len([n for n in awa.__all__ if not n.startswith("__")]),
        "toolkit_tools": len([e for e in catalog() if e.toolkit.name != "awa"]),
    }


# (fact, pattern with the number or number word in group 1, other values the phrase may mean)
CLAIMS = [
    ("tools", r"\b(\d+)\*{0,2} (?:builtin|built-in) tools", set()),
    # "Microsoft 365 connectors" names a product, not a count.
    (
        "connectors",
        r"(?<!Microsoft )\b(\d+)\*{0,2} (?:tested |configured )?connectors",
        {len(CORE_CONNECTORS), 12},
    ),
    ("core_connectors", r"\b(\w+) (?:other|core|generic) connectors", set()),
    ("preview_connectors", r"\b(\w+) (?:SaaS|preview) connectors", set()),
    ("skills", r"\b(\d+)\*{0,2} (?:builtin |built-in )?skills", set()),
    ("recipes", r"\b(\w+)\*{0,2} (?:complete )?recipes\b", set()),
    ("usecases", r"\b(\w+) use case(?:s| examples)\b", {6}),  # "the last six rows"
    ("examples", r"\b(\d+)\*{0,2} (?:examples|scripts)\b", set()),
    ("names", r"\b(\d+) everyday names", set()),
    ("templates", r"\b(\w+) (?:project )?templates\b", set()),
    ("commands", r"\b(\d+) (?:CLI )?commands\b", set()),
]


# Phrasings that span lines or lean on the words around them: the number is in group 1.
MULTILINE_CLAIMS = [
    ("toolkit_tools", r"\b(\d+) ready-made,\s+hardened\s+tools"),
    ("skills", r"\b(\d+) of them\s+\(`awa skills list`"),
]
OF_THEM = re.compile(r"\b(\d+) of them\b")


def _pages() -> list[Path]:
    found: list[Path] = []
    for entry in PAGES:
        path = ROOT / entry
        found += [path] if path.is_file() else sorted(path.rglob("*.md"))
    return found


VERSION_HEADING = re.compile(r"^## \[", re.MULTILINE)


def _text(page: Path) -> str:
    """The page's text; for a change log only the top section, the current version.

    An older section records the counts of its own version, so it is history, not a claim
    about the code of today.
    """

    text = page.read_text("utf-8")
    if len(headings := list(VERSION_HEADING.finditer(text))) > 1:
        return text[: headings[1].start()]
    return text


def _number(word: str) -> int | None:
    word = word.lower()
    return int(word) if word.isdigit() else WORDS.get(word)


def test_every_count_the_docs_quote_matches_the_code() -> None:
    truth = _truth()
    drift = []
    for page in _pages():
        for number, line in enumerate(_text(page).splitlines(), 1):
            for fact, pattern, other in CLAIMS:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    value = _number(match.group(1))
                    if value is not None and value != truth[fact] and value not in other:
                        where = f"{page.relative_to(ROOT).as_posix()}:{number}"
                        drift.append(f"{where}: {match.group(0)!r}, the code has {truth[fact]}")
    assert not drift, "\n".join(drift)


def test_counts_in_multiline_phrasings_and_of_them_match_the_code() -> None:
    """`ships 48 of them`, `34 ready-made, hardened tools`: the number may sit away from its noun."""

    truth = _truth()
    nouns = [("skill", "skills"), ("tool", "tools"), ("everyday names", "names")]
    drift = []
    for page in _pages():
        text = _text(page)
        where = page.relative_to(ROOT).as_posix()

        def line_of(match: re.Match[str], where: str = where, text: str = text) -> str:
            return f"{where}:{text.count(chr(10), 0, match.start()) + 1}"

        for fact, pattern in MULTILINE_CLAIMS:
            for match in re.finditer(pattern, text):
                if int(match.group(1)) != truth[fact]:
                    drift.append(
                        f"{line_of(match)}: {match.group(0)!r}, the code has {truth[fact]}"
                    )
        for match in OF_THEM.finditer(text):
            before = text[max(0, match.start() - 200) : match.start()].split("\n\n")[-1].lower()
            fact = next((fact for noun, fact in nouns if noun in before), None)
            if fact and int(match.group(1)) != truth[fact]:
                drift.append(
                    f"{line_of(match)}: {match.group(0)!r}, the code has {truth[fact]} {fact}"
                )
    assert not drift, "\n".join(drift)


def test_example_tools_name_only_catalog_connectors_or_their_own_instances() -> None:
    known = {c.name for c in list_connectors()}
    stale = []
    for tools in sorted((ROOT / "examples").rglob("tools.py")):
        blueprint = tools.parent / "blueprint.yaml"
        declared = blueprint.read_text("utf-8") if blueprint.is_file() else ""
        text = tools.read_text("utf-8")
        for match in re.finditer(r"`([a-z_]+)`(?: or `([a-z_]+)`)? connector\b", text):
            for name in filter(None, match.groups()):
                if name not in known and not re.search(rf"^\s+{name}:", declared, re.MULTILINE):
                    stale.append(f"{tools.relative_to(ROOT).as_posix()}: {name}")
    assert not stale, f"connectors that are neither in the catalog nor declared: {stale}"


def test_every_command_template_recipe_and_use_case_is_named_on_its_page() -> None:
    def page(path: str) -> str:
        return (ROOT / path).read_text("utf-8")

    missing = [
        f"awa {name}" for name in COMMANDS if f"awa {name}" not in page("docs/reference/cli.md")
    ]
    missing += [f"template {n}" for n in TEMPLATES if f"`{n}`" not in page("docs/reference/cli.md")]
    missing += [p.name for p in _folders("recipes") if p.name not in page("docs/guide/examples.md")]
    missing += [
        p.name for p in _folders("usecases") if p.name not in page("docs/guide/examples.md")
    ]
    missing += [c.name for c in list_connectors() if c.name not in page("docs/guide/connect.md")]
    assert not missing, f"not named on their docs page: {missing}"


def _readme_block(language: str, containing: str) -> str:
    text = DEVELOPERS.read_text("utf-8").replace("\r\n", "\n")
    for block in re.findall(rf"```{language}\n(.*?)```", text, re.DOTALL):
        if containing in block:
            return str(block)
    raise AssertionError(f"DEVELOPERS.md has no {language} block with {containing!r}")


def _example(tmp_path: Path) -> Path:
    path = tmp_path / "blueprint.yaml"
    path.write_text(_readme_block("yaml", "name: invoice-desk"), encoding="utf-8")
    return path


def test_the_readme_example_is_short_valid_and_loads_with_its_approval(tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    blueprint = _readme_block("yaml", "name: invoice-desk")
    assert len(blueprint.strip().splitlines()) <= 25
    path = _example(tmp_path)
    (tmp_path / "data").mkdir()  # the folder the README tells the reader to create
    issues = Blueprint.from_yaml(path).check()
    assert not issues, [str(issue) for issue in issues]
    system = awa.load(path, checkpointer=InMemorySaver())
    assert type(system).__name__ == "Sequential"


def test_the_readme_shows_the_real_validate_and_show_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _example(tmp_path)
    shown = _readme_block("console", "$ awa validate")
    assert main(["validate", str(path)]) == 0
    validated = capsys.readouterr().out
    assert main(["show", str(path)]) == 0
    described = capsys.readouterr().out
    assert (
        shown == f"$ awa validate blueprint.yaml\n{validated}$ awa show blueprint.yaml\n{described}"
    )


def test_the_readme_links_and_images_exist_and_stay_local() -> None:
    text = README.read_text("utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'src="([^"]+)"', text)
    missing = []
    for target in targets:
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if not (ROOT / target.split("#")[0]).exists():
            missing.append(target)
    assert not missing, f"broken README links: {missing}"
    # The project's own repository is the one address the README names (the install from GitHub).
    rest = text.lower().replace("https://github.com/awa-projekt/multiagent_orchestration", "")
    assert "github.com" not in rest and "pypi.org" not in rest
    assert "SDK of your choice" not in text


def test_the_readme_has_the_length_and_pictures_of_a_front_page() -> None:
    front = README.read_text("utf-8").split("\n## About\n", 1)[0]
    lines = front.splitlines()
    assert 150 <= len(lines) <= 240, len(lines)
    text = "\n".join(lines)
    assert "screenshots/ui-describe.png" in text
    runs = text.split("## See every run", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"^\d\. \*\*", runs, re.MULTILINE)) == 4, "one caption per picture"
    assert len(re.findall(r"!\[[^\]]+\]\(docs/assets/screenshots/tracing-", runs)) == 4
    assert re.findall(r"!\[[^\]]{10,}\]\(docs/assets/screenshots/", text), "images need alt text"


def test_the_front_pages_link_the_run_limits_and_safety_quotes_the_defaults() -> None:
    """The README and the home page link the run limits of the configuration reference instead of
    repeating them; the ceiling table of the safety page quotes the defaults of `Settings`, so a
    changed default fails here until that page says the new number."""

    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    for page, link in (
        (README, "docs/reference/settings.md#run-limits-and-loop-protection"),
        (ROOT / "docs" / "index.md", "reference/settings.md#run-limits-and-loop-protection"),
    ):
        text = page.read_text(encoding="utf-8")
        assert link in text, f"{page.name} links the run limits of the configuration reference"
        assert f"{defaults['run_max_tokens']:,}" not in text, f"{page.name} repeats a number"
    safety = (ROOT / "docs" / "develop" / "safety.md").read_text(encoding="utf-8")
    rows = re.findall(r"\| `AWA_(RUN_MAX_\w+)` \| ([\d,]+) \|", safety)
    assert len(rows) == 5, rows
    for setting, value in rows:
        assert int(value.replace(",", "")) == defaults[setting.lower()], setting


def test_snippets_use_awas_in_memory_checkpointer_not_the_plain_langgraph_one() -> None:
    """LangGraph's plain `InMemorySaver` returns a structured output as a dict once a run is
    resumed under `LANGGRAPH_STRICT_MSGPACK=true`; `awa.persistence.StateInMemorySaver` does not."""

    plain = re.compile(r"(?<![A-Za-z_])InMemorySaver\(|import InMemorySaver\b")
    code = [*(ROOT / "examples").rglob("*.py"), *(ROOT / "src" / "awa").rglob("*.py")]
    # The tool contract test graph keeps no structured output; the compat module defines both.
    # An export runs without awa: its generated tests use LangGraph's own saver on purpose.
    code = [
        p
        for p in code
        if p.name != "tool_contract.py" and not {"_compat", "upstream", "exporter"} & set(p.parts)
    ]
    found = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in [*_pages(), *code]
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if plain.search(line)
    ]
    assert not found, f"use awa.persistence.StateInMemorySaver: {found}"


def _extras_table() -> dict[str, tuple[list[str], str]]:
    """Rows of the extras table in `docs/develop/quickstart.md`: extra names -> (adds, text)."""

    text = (ROOT / "docs/develop/quickstart.md").read_text("utf-8")
    section = text.split("### Extras", 1)[1].split("\n###", 1)[0].split("\n## ", 1)[0]
    rows: dict[str, tuple[list[str], str]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 3 and cells[0] not in ("Extra", "---"):
            names = re.findall(r"`([^`]+)`", cells[0])
            rows[",".join(names)] = (re.findall(r"`([^`]+)`", cells[1]), cells[1])
    return rows


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0].lower()


def _included(requirement: str) -> list[str]:
    """The extras an `awa-agents[...]` requirement names, or none for any other package."""

    if _requirement_name(requirement) != "awa-agents":
        return []
    return re.search(r"\[(.*)\]", requirement).group(1).split(",")  # type: ignore[union-attr]


def _resolved(extras: dict[str, list[str]], extra: str) -> tuple[set[str], set[str]]:
    """The extras `extra` includes (itself too) and the package names they add."""

    names: set[str] = {extra}
    packages: set[str] = set()
    for requirement in extras[extra]:
        included = _included(requirement)
        if not included:
            packages.add(_requirement_name(requirement))
        for inner in included:
            inner_names, inner_packages = _resolved(extras, inner)
            names |= inner_names
            packages |= inner_packages
    return names, packages


def test_the_extras_table_equals_the_extras_of_pyproject() -> None:
    import tomllib

    extras = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "optional-dependencies"
    ]
    rows = _extras_table()
    named = {name for key in rows for name in key.split(",")}
    assert named == set(extras), f"the extras table and pyproject differ: {named ^ set(extras)}"
    for key, (adds, _) in rows.items():
        names = key.split(",")
        if len(names) > 1 or names[0] == "dev":
            continue  # a group row names its SDKs; `dev` also adds the test tools
        extra = names[0]
        if extra == "all":
            expected = set(_included(extras["all"][0]))
        else:
            expected = _resolved(extras, extra)[1]
        assert {_requirement_name(item) for item in adds} == expected, f"extra {extra!r}"
    everything = set(extras) - {"all", "dev", "studio", "docs"}
    assert _resolved(extras, "all")[0] - {"all"} == everything - {"testing"}
    assert _resolved(extras, "dev")[0] - {"all", "dev"} == everything, (
        "`dev` is every extra except studio and docs, as the table says"
    )


def test_each_dependency_of_the_extras_is_defined_once() -> None:
    import tomllib

    extras = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "optional-dependencies"
    ]
    defined = [
        _requirement_name(requirement)
        for requirements in extras.values()
        for requirement in requirements
        if not _included(requirement)
    ]
    assert sorted({name for name in defined if defined.count(name) > 1}) == []


def test_the_error_code_table_lists_every_exception_with_its_code() -> None:
    import awa.errors as exceptions

    page = (ROOT / "docs/reference/exceptions.md").read_text("utf-8")
    shown = set(re.findall(r"^\| `(awa\.[a-z_.]+)` \| `(\w+)` \| (yes|no) \|$", page, re.MULTILINE))
    expected = {
        (
            getattr(exceptions, name).code,
            name,
            "yes" if getattr(exceptions, name).retryable else "no",
        )
        for name in exceptions.__all__
    }
    assert shown == expected, f"error code table differs from the code: {shown ^ expected}"
    tree = page[page.index("```text\nAwaError") + len("```text\n") :].split("```", 1)[0]
    missing = [name for name in exceptions.__all__ if name not in tree]
    assert not missing, f"not in the exception tree of reference/exceptions.md: {missing}"


def test_the_install_page_says_what_a_wheel_lacks() -> None:
    page = (ROOT / "docs/start/install.md").read_text("utf-8")
    row = next(line for line in page.splitlines() if line.startswith("| A wheel file only |"))
    assert all(f"`{folder}/`" in row for folder in ("docs", "examples", "tests"))
    assert "mkdocs serve" in README.read_text("utf-8")


def test_the_skills_troubleshooting_row_quotes_the_invalid_name_error(tmp_path: Path) -> None:
    from awa.errors import SkillManifestError
    from awa.skills import Skill

    folder = tmp_path / "Bad_Name"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: Does x. Use when y.\n---\nbody\n", encoding="utf-8"
    )
    with pytest.raises(SkillManifestError) as caught:
        Skill.from_path(folder)
    page = _text(ROOT / "docs/develop/skills.md")
    row = re.search(r"\| `SkillManifestError: \.\.\. (.+?) \.\.\.` \|", page)
    assert row and row.group(1) in str(caught.value)


def test_the_tool_option_table_names_only_its_parameters() -> None:
    import inspect

    from awa.tools import tool

    text = _text(ROOT / "docs/develop/tools.md")
    table = text[text.index("| Option | Effect |") :].split("\n\n")[0]
    named = {
        name
        for row in table.splitlines()[2:]
        for name in re.findall(r"`([a-z_]+)(?:=[^`]*)?`", row.split("|")[1])
    }
    assert named and named <= set(inspect.signature(tool).parameters)


def test_every_runnable_reference_shows_run_sync_start_and_stream() -> None:
    import importlib

    from awa.runtime._run import RunApi

    directive = re.compile(r"^::: (awa(?:\.\w+)*)\n((?:    .*\n)*)", re.MULTILINE)
    seen = []
    for page in sorted((ROOT / "docs" / "reference").rglob("*.md")):
        for module_name, options in directive.findall(page.read_text(encoding="utf-8")):
            module = importlib.import_module(module_name)
            for name in getattr(module, "__all__", ()):
                target = getattr(module, name, None)
                if isinstance(target, type) and RunApi in target.__bases__:
                    seen.append(name)
                    assert "inherited_members: [run_sync, start, stream]" in options, (
                        page.name,
                        name,
                    )
    assert {"Agent", "Team", "AgentNetwork", "Workflow"} <= set(seen)


CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def test_no_markdown_file_holds_a_control_character() -> None:
    """A backslash and `a` in a path can turn into a bell character, which shows as nothing."""

    pages = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"), *(ROOT / "examples").rglob("*.md")]
    found = []
    for page in pages:
        text = page.read_text("utf-8")
        for match in CONTROL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{page.relative_to(ROOT).as_posix()}:{line}: {match.group(0)!r}")
    assert not found, found

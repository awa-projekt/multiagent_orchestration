"""The eight end-to-end scenarios of the data-first build, offline (`tests/_helpers/data_scenarios`).

Real files of the repository (and the generated ones) go in; the scenarios say what the profile
must show, which mistakes of a first design must be caught, what the `data:` section of the
blueprint must hold, what a build on the real files must leave behind, and (scenario 8) that a
description that does not fit the files ends with a question. The model is always scripted.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from _helpers.data_scenarios import generate, harness
from _helpers.data_scenarios.checks import BUDGET, CHECKS, VERDICT, Ctx, World, make_world
from _helpers.data_scenarios.harness import ScriptedBuild
from _helpers.data_scenarios.scenarios import SCENARIOS, DataEntry, Fact, Scenario
from awa.cli import main
from awa.data import DataProfile, DataRef, Privacy

pytestmark = pytest.mark.process

SUPPORT = Path(__file__).resolve().parents[1] / "_helpers"

SCRIPTED = [s for s in SCENARIOS if s.script and not s.question]  # scenarios 1 to 7


def ident(scenario: Scenario) -> str:
    return f"s{scenario.number}-{scenario.slug}"


# ------------------------------------------------------------------------ the files of a scenario


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("generated")
    generate.generate(root)
    return root


@pytest.fixture(scope="session")
def worlds(
    tmp_path_factory: pytest.TempPathFactory, generated: Path
) -> Callable[[Scenario], World]:
    made: dict[int, World] = {}

    def get(scenario: Scenario) -> World:
        if scenario.number not in made:
            base = tmp_path_factory.mktemp(f"s{scenario.number}")
            made[scenario.number] = make_world(scenario, generated, base)
        return made[scenario.number]

    return get


@pytest.fixture
def world(scenario: Scenario, worlds: Callable[[Scenario], World]) -> World:
    return worlds(scenario)


scenarios = pytest.mark.parametrize("scenario", SCENARIOS, ids=ident)
scripted = pytest.mark.parametrize("scenario", SCRIPTED, ids=ident)


# ------------------------------------------------------------------------ the data itself


def test_the_eight_scenarios_are_complete() -> None:
    assert [s.number for s in SCENARIOS] == list(range(1, 9))
    assert len({s.slug for s in SCENARIOS}) == 8
    assert all(s.description.endswith(".") and len(s.description) > 60 for s in SCENARIOS)
    wrong = SCENARIOS[7]
    assert wrong.question is not None and wrong.question.evidence == (
        "f1.t1.invoice_no",
        "f2.t1.reference",
    )
    assert all(s.question is None and s.script for s in SCENARIOS[:7])
    assert all(s.facts for s in SCENARIOS)


def test_every_file_a_scenario_names_exists_or_is_generated() -> None:
    for s in SCENARIOS:
        assert len(set(s.names)) == len(s.names), f"{s.slug}: two inputs share a file name"
        for spec in s.files:
            kind, _, relative = spec.partition(":")
            assert kind in ("repo", "gen"), spec
            if kind == "repo":
                assert (harness.REPO_ROOT / relative).is_file(), spec
            else:
                assert relative in generate.FILES, spec


def test_every_fact_names_a_file_of_its_scenario_and_a_known_operation() -> None:
    for s in SCENARIOS:
        for fact in s.facts:
            assert fact.file in s.names, (s.slug, fact.file)
            assert fact.op in {
                "eq", "in", "ge", "has", "len", "len_ge", "not_contains", "flag", "not_flag", "flag_at"
            }  # fmt: skip
        for entry in s.entries:
            assert entry.file in s.names, (s.slug, entry.file)
            assert entry.role in ("input", "reference", "output")


def test_the_fact_checker_says_yes_and_no_for_every_operation(
    worlds: Callable[[Scenario], World],
) -> None:
    # a fact that cannot fail proves nothing: every operation, its yes and its no
    from awa.data import make_flag

    data = worlds(SCENARIOS[1]).profile.model_copy(deep=True)
    orders = harness.find_file(data, "orders.csv")
    assert orders is not None
    orders.flags.append(make_flag("DF-W-TEST", "a test flag", "f1.t1.betrag_eur"))
    path = "tables.0.columns.Betrag_EUR"
    cases = [
        (Fact("orders.csv", "tables.0.rows", 8), True),
        (Fact("orders.csv", "tables.0.rows", 9), False),
        (Fact("orders.csv", "text.delimiter", (",", ";"), "in"), True),
        (Fact("orders.csv", "text.delimiter", (",", "|"), "in"), False),
        (Fact("orders.csv", "tables.0.rows", 8, "ge"), True),
        (Fact("orders.csv", "tables.0.rows", 9, "ge"), False),
        (Fact("orders.csv", "tables.0.columns", "Betrag_EUR", "has"), True),
        (Fact("orders.csv", "tables.0.columns", "Betrag", "has"), False),
        (Fact("orders.csv", "text.encoding", "1252", "has"), True),
        (Fact("orders.csv", "tables.0.columns", 6, "len"), True),
        (Fact("orders.csv", "tables.0.columns", 5, "len"), False),
        (Fact("orders.csv", "tables.0.columns", 6, "len_ge"), True),
        (Fact("orders.csv", "tables.0.columns", 7, "len_ge"), False),
        (Fact("orders.csv", "text.encoding", "utf", "not_contains"), True),
        (Fact("orders.csv", "text.encoding", "cp", "not_contains"), False),
        (Fact("orders.csv", "flags", "DF-W-TEST", "flag"), True),
        (Fact("orders.csv", "flags", "DF-W-OTHER", "flag"), False),
        (Fact("orders.csv", "flags", "DF-W-OTHER", "not_flag"), True),
        (Fact("orders.csv", "flags", "DF-W-TEST", "not_flag"), False),
        (Fact("orders.csv", "flags", "betrag_eur", "flag_at"), True),
        (Fact("orders.csv", "flags", "land", "flag_at"), False),
        (Fact("orders.csv", f"{path}.type", "decimal"), True),
        (Fact("orders.csv", f"{path}.nothing", "decimal"), False),
        (Fact("orders.csv", "tables.7.rows", 8), False),
        (Fact("orders.csv", "tables.0.columns.Betrag.type", "decimal"), False),
        (Fact("missing.csv", "kind", "csv"), False),
    ]
    for fact, holds in cases:
        assert (harness.check_fact(data, fact) is None) is holds, fact
    assert "is 8" in str(harness.check_fact(data, Fact("orders.csv", "tables.0.rows", 9)))

    # a column is found by its name, its key and any spelling of the name
    betrag = [
        harness.lookup(orders, f"tables.0.columns.{n}.index") for n in ("Betrag_EUR", "betrag_eur")
    ]
    assert betrag == [3, 3]


def test_a_value_is_data_when_it_has_a_digit_a_capital_or_a_sign() -> None:
    assert all(
        harness.data_like(v) for v in ("2024-0412", "1.428,00", "Rossi", "a@b.example", "B-1")
    )
    assert not any(harness.data_like(v) for v in ("hold", "manual entry", "billing"))


def test_the_scripted_model_plays_designer_architect_and_agent(
    worlds: Callable[[Scenario], World],
) -> None:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    from awa.blueprints import AcceptanceDesign, BlueprintDesign
    from awa.blueprints._project import TEST_DESIGNER_INSTRUCTIONS
    from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS

    scenario = SCENARIOS[1]
    fake = ScriptedBuild(scenario)

    def say(system: str, *rest: Any) -> Any:
        return fake.model.invoke([SystemMessage(system), HumanMessage("a brief"), *rest])

    design = say(TEST_DESIGNER_INSTRUCTIONS).tool_calls[0]
    assert design["name"] == "AcceptanceDesign" and AcceptanceDesign.model_validate(design["args"])
    first = say(ARCHITECT_INSTRUCTIONS).tool_calls[0]["args"]
    second = say(ARCHITECT_INSTRUCTIONS).tool_calls[0]["args"]
    assert BlueprintDesign.model_validate(first) and BlueprintDesign.model_validate(second)
    assert "`Betrag`" in first["agents"][0]["instructions"]  # the first draft has the mistake
    assert "`Betrag`" not in second["agents"][0]["instructions"]  # the repaired one has not
    assert fake.roles == ["designer", "architect", "architect"]
    assert fake.architect_prompts[1].startswith("a brief")

    case = scenario.script.cases[0]  # type: ignore[union-attr]
    agent = "You are the order desk."
    messages: list[Any] = [HumanMessage(case.request)]
    for call in case.calls:
        reply = fake.model.invoke([SystemMessage(agent), *messages])
        wanted = reply.tool_calls[0]
        assert wanted["name"] == call.tool
        if call.file:  # in a built project the files are below data/files
            assert wanted["args"]["path"] == "files/" + call.file
        messages += [reply, ToolMessage("result", tool_call_id=wanted["id"])]
    assert fake.model.invoke([SystemMessage(agent), *messages]).content == case.answer
    unknown = fake.model.invoke([SystemMessage(agent), HumanMessage("something else")])
    assert unknown.content == "unknown request" and isinstance(unknown, AIMessage)


# ------------------------------------------------------------------------ the scripted designs


@scripted
def test_the_scripted_design_passes_the_acceptance_and_blueprint_checks(scenario: Scenario) -> None:
    from awa.blueprints import AcceptanceDesign, BlueprintDesign
    from awa.blueprints._project import acceptance_problems

    script = scenario.script
    assert script is not None
    systems = {t[10:].replace(".", "_") for t in script.tools if t.startswith("connector:")}
    draft = AcceptanceDesign.model_validate(harness.acceptance_dict(scenario))
    assert [str(p) for p in acceptance_problems(draft, systems)] == []
    for wrong in (False, True):
        design = BlueprintDesign.model_validate(harness.blueprint_dict(scenario, wrong=wrong))
        # a database system's connector is added by the build
        errors = [
            str(i)
            for i in design.to_blueprint().check()
            if i.severity == "error" and "unknown connector" not in i.message
        ]
        assert errors == []


@scripted
def test_the_scripted_agents_read_the_real_files_and_satisfy_their_own_cases(world: World) -> None:
    script = world.scenario.script
    assert script is not None
    tools = harness.real_tools(world.folder)
    exposed = {
        t.removeprefix("builtin:")
        if t.startswith("builtin:")
        else t[10:].replace(".", "_")
        if t.startswith("connector:")
        else t
        for t in script.tools
    }
    steps = {str(a["name"]) for a in script.approvals}
    for case in script.cases:
        outputs = harness.run_real_calls(case, tools)  # a tool error here is a wrong script
        text = " ".join([*outputs, case.request])
        called = [call.tool for call in case.calls]
        assert set(called) <= exposed, (case.id, set(called) - exposed)
        for kind, value in case.expect:
            where = f"{case.id}: {kind} {value!r}"
            if kind == "contains":
                assert value in case.answer, where  # the scripted answer satisfies the case
                assert not harness.data_like(value) or value in text, where + " is not in the data"
            elif kind == "regex":
                assert re.search(value, case.answer), where
            elif kind == "tool_called":
                assert value in called, where
            elif kind == "tool_not_called":
                assert value not in called, where
            elif kind == "approval":
                assert value in called and value in harness.gated(script), where
            elif kind == "no_approval":
                assert value in steps, where
        assert harness.pauses(script, case) == len(case.decisions), case.id
        asked = {v for k, v in case.expect if k == "approval"}
        assert {tool for _, tool in case.decisions} <= asked, case.id


# ------------------------------------------------------------------------ the profile


def fact_params() -> list[Any]:
    return [
        pytest.param(s, f, id=f"s{s.number}:{f.file}:{f.op}:{f.path}")
        for s in SCENARIOS
        for f in s.facts
    ]


@pytest.mark.parametrize(("scenario", "fact"), fact_params())
def test_the_profile_shows_the_fact(
    scenario: Scenario, fact: Any, worlds: Callable[[Scenario], World]
) -> None:
    assert harness.check_fact(worlds(scenario).profile, fact) is None


@scenarios
def test_profiling_leaves_the_original_files_byte_identical(world: World) -> None:
    harness.profile_of(world.folder)
    assert harness.digests(world.folder) == world.before  # bytes and modification times


@scenarios
def test_the_profile_validates_is_deterministic_and_fast(world: World) -> None:
    text = world.profile.to_json()
    jsonschema.validate(json.loads(text), DataProfile.json_schema())
    assert DataProfile.from_json(text).to_json() == text
    times = []
    # warm runs; the best of three is the time of the profiler, not of the machine
    for _ in range(3):
        started = time.perf_counter()
        again = harness.profile_of(world.folder)
        times.append(time.perf_counter() - started)
        assert again.to_json() == text
    assert min(times) < 2.0, f"profiling {len(world.scenario.files)} files took {min(times):.1f} s"


@scenarios
def test_the_async_profile_is_the_same_document(world: World) -> None:
    from awa.data import aprofile

    again = asyncio.run(aprofile(world.folder, created=harness.CREATED))
    assert again.to_json() == world.profile.to_json()


@scenarios
def test_every_file_of_a_scenario_is_read_without_an_error(world: World) -> None:
    wanted = {f.path for f in harness.walk_files(world.profile)}
    assert {Path(f).name for f in wanted} >= set(world.scenario.names)
    bad = [(f.path, f.status) for f in harness.walk_files(world.profile) if f.status != "ok"]
    assert bad == []


# ------------------------------------------------------------------------ what a model may see


@scenarios
def test_the_model_view_fits_the_budget_and_drop_mode_shows_no_value(world: World) -> None:
    from awa.data import render_for_model

    view = render_for_model(world.profile, BUDGET)
    assert len(view) / 4 <= BUDGET
    assert render_for_model(world.profile, BUDGET) == view
    assert str(world.folder) not in view  # the real path is never shown
    dropped = render_for_model(world.profile, BUDGET, privacy=Privacy(mask="drop"))
    assert harness.leaked_values(world.profile, dropped) == []
    assert view.count("<<<DATA") == view.count("<<<END DATA>>>")


# ------------------------------------------------------------------------ the checks of a design


def mistakes() -> list[Any]:
    return [
        pytest.param(s, m, id=f"s{s.number}:{m.code}:{m.mention}")
        for s in SCENARIOS
        for m in s.mistakes
        if m.layer == "refs"
    ]


def as_ref(draft: Any) -> DataRef:
    return DataRef(
        file=draft.file, sheet=draft.sheet, columns=list(draft.columns), header_row=draft.header_row
    )


@pytest.mark.parametrize(("scenario", "mistake"), mistakes())
def test_a_wrong_reference_is_an_error_with_a_suggestion_and_the_right_one_passes(
    scenario: Scenario, mistake: Any, worlds: Callable[[Scenario], World]
) -> None:
    from awa.data import check_references

    data = worlds(scenario).profile
    issues = check_references(data, [as_ref(mistake.wrong)])
    errors = [i for i in issues if i.severity == "error"]
    assert [i.code for i in errors] == [mistake.code], issues
    assert mistake.mention in f"{errors[0].message} {errors[0].suggestion or ''}"
    assert [
        i for i in check_references(data, [as_ref(mistake.right)]) if i.severity == "error"
    ] == []


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.grounded or s.ungrounded], ids=ident
)
def test_values_that_are_in_the_data_are_grounded_and_invented_ones_are_not(world: World) -> None:
    s = world.scenario
    from awa.data import grounded

    assert [v for v in s.grounded if not grounded(v, world.profile)] == []
    assert [v for v in s.ungrounded if grounded(v, world.profile)] == []


# ------------------------------------------------------------------------ the data: section


def project_layout(scenario: Scenario, world: World, project: Path) -> None:
    """Copy the inputs where a built project keeps them: `data/files/<name>`, databases `data/`."""

    for name in scenario.names:
        target = project / "data" / (name if name.endswith(".db") else f"files/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((world.folder / name).read_bytes())


def blueprint_text(scenario: Scenario, entries: list[dict[str, Any]]) -> str:
    import yaml

    return yaml.safe_dump(
        {
            "name": "data_check",
            "description": "Checks the data section.",
            "entrypoint": "clerk",
            "agents": [
                {"name": "clerk", "description": "Reads.", "instructions": "Read the files."}
            ],
            "data": entries,
        },
        allow_unicode=True,
        sort_keys=False,
    )


def entry_dicts(scenario: Scenario) -> list[dict[str, Any]]:
    out = []
    for e in scenario.entries:
        path = e.file if e.file.endswith(".db") else f"files/{e.file}"
        item: dict[str, Any] = {
            "name": re.sub(r"\W+", "_", Path(e.file).stem).strip("_").lower(),
            "path": f"data/{path}",
            "role": e.role,
            "description": e.description,
        }
        if e.handling:
            item["handling"] = e.handling
        if e.expect:
            item["expect"] = dict(e.expect)
        out.append(item)
    return out


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.entries], ids=ident)
def test_the_data_section_is_valid_and_every_break_is_an_error(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = world.scenario
    project_layout(s, world, tmp_path)

    def errors(entries: list[dict[str, Any]]) -> str:
        """What `awa validate` says about a blueprint with these entries ('' when it is valid)."""

        (tmp_path / "blueprint.yaml").write_text(blueprint_text(s, entries), encoding="utf-8")
        code = main(["validate", str(tmp_path / "blueprint.yaml")])
        printed = capsys.readouterr().out
        return printed if code else ""

    base = entry_dicts(s)
    assert errors(base) == ""
    breaks = 0
    for index, entry in enumerate(base):
        expect = entry.get("expect", {})
        changes: list[tuple[str, dict[str, Any]]] = [("path", {"path": entry["path"] + ".missing"})]
        if expect.get("columns"):
            wrong = [expect["columns"][0][:-1], *expect["columns"][1:]]
            changes.append((expect["columns"][0], {"expect": {**expect, "columns": wrong}}))
        if "header_row" in expect:
            changes.append(
                ("header", {"expect": {**expect, "header_row": expect["header_row"] + 1}})
            )
        if "sheet" in expect:
            changes.append(("Blatt1", {"expect": {**expect, "sheet": "Blatt1"}}))
        if "decimal" in expect:
            changes.append(("decimal", {"expect": {**expect, "decimal": "."}}))
        for mention, change in changes:
            broken = [dict(e) for e in base]
            broken[index] = {**entry, **change}
            found = errors(broken)
            assert found, f"{entry['name']}: a break of {mention!r} was not found"
            assert any(word in found for word in (mention, entry["name"], entry["path"]))
            breaks += 1
    assert breaks >= len(base)


# ------------------------------------------------------------------------ checks of one scenario


def check_params() -> list[Any]:
    return [pytest.param(s, c, id=f"s{s.number}:{c.id}") for s in SCENARIOS for c in s.checks]


@pytest.mark.parametrize(("scenario", "item"), check_params())
def test_the_scenario_check_holds(
    scenario: Scenario,
    item: Any,
    worlds: Callable[[Scenario], World],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CHECKS[item.id](Ctx(worlds(scenario), tmp_path, monkeypatch))


def test_every_check_of_the_data_has_an_implementation_and_the_reverse() -> None:
    named = {c.id for s in SCENARIOS for c in s.checks}
    assert named == set(CHECKS)


# ------------------------------------------------------------------------ the offline build


@dataclass
class Built:
    report: Any
    steps: list[Any]
    out: Path
    fake: ScriptedBuild
    calls_at_profile: list[int]


_BUILT: dict[int, Built] = {}


async def built(world: World, base: Path) -> Built:
    """Build the scenario once with the scripted models and keep the result for the other tests."""

    s = world.scenario
    if s.number not in _BUILT:
        from awa.blueprints import build_project

        fake = ScriptedBuild(s)
        steps: list[Any] = []
        at_profile: list[int] = []

        def progress(step: Any) -> None:
            steps.append(step)
            if step.stage == "profile":
                at_profile.append(fake.model.call_count)

        out = base / f"project-{s.number}"
        report = await build_project(
            s.description,
            out_dir=out,
            model=fake.model,
            environment=None,
            data=[world.folder],
            notes=dict(s.notes) or None,
            on_progress=progress,
            max_rounds=2,
        )
        _BUILT[s.number] = Built(report, steps, out, fake, at_profile)
    return _BUILT[s.number]


async def cli(*argv: str) -> int:
    """`awa <argv>` from a test that already runs an event loop (a command may start its own)."""

    return await asyncio.to_thread(main, list(argv))


@scripted
async def test_the_build_on_the_real_files_passes_and_leaves_the_originals_alone(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    assert (run.report.passed, run.report.rounds) == (True, 1)
    assert harness.digests(world.folder) == world.before  # after the build and every evaluation


@scripted
async def test_the_build_profiles_first_and_ends_with_one_terminal_event(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    stages = [step.stage for step in run.steps]
    assert stages[0] == "profile"
    assert run.calls_at_profile and all(c == 0 for c in run.calls_at_profile)  # no call before
    assert stages[-1] == "done" and stages.count("done") == 1


@scripted
async def test_the_project_holds_the_users_files_and_nothing_invented(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    data = run.out / "data"
    found = {p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()}
    expected = {"PROFILE.json", "PROFILE.md"} | {
        n if n.endswith(".db") else f"files/{n}" for n in world.scenario.names
    }
    assert found == expected
    assert (data / "output").is_dir() and not list((data / "output").iterdir())
    for name in world.scenario.names:
        copy = data / (name if name.endswith(".db") else f"files/{name}")
        assert copy.read_bytes() == (world.folder / name).read_bytes()
    profile = DataProfile.from_json((data / "PROFILE.json").read_text(encoding="utf-8"))
    assert {Path(f.path).name for f in profile.files} == set(world.scenario.names)


@scripted
async def test_the_written_blueprint_validates_with_its_data_section(
    world: World, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.blueprints import Blueprint

    run = await built(world, tmp_path_factory.mktemp("builds"))
    blueprint = Blueprint.from_yaml(run.out / "blueprint.yaml")
    declared = {Path(entry.path).name for entry in blueprint.data}
    assert set(world.scenario.names) <= declared
    assert await cli("validate", str(run.out / "blueprint.yaml")) == 0
    capsys.readouterr()


@scripted
async def test_data_check_is_clean_on_the_built_project(
    world: World, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    assert await cli("data", "check", str(run.out)) == 0
    capsys.readouterr()


def _first_csv_with_columns(scenario: Scenario) -> DataEntry | None:
    return next(
        (e for e in scenario.entries if e.file.endswith(".csv") and e.expect.get("columns")), None
    )


@pytest.mark.parametrize("scenario", [s for s in SCRIPTED if _first_csv_with_columns(s)], ids=ident)
async def test_data_check_finds_a_renamed_column(
    world: World, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    entry = _first_csv_with_columns(world.scenario)
    assert entry is not None
    column = str(entry.expect["columns"][0])  # type: ignore[index]
    copy = run.out / "data" / "files" / entry.file
    original = copy.read_bytes()
    copy.write_bytes(original.replace(column.encode(), column[:-1].encode(), 1))
    try:
        assert await cli("data", "check", str(run.out), "--json") == 1
        assert column in capsys.readouterr().out
    finally:
        copy.write_bytes(original)


@scripted
async def test_the_cases_only_name_values_that_are_in_the_data_or_the_request(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    import yaml

    run = await built(world, tmp_path_factory.mktemp("builds"))
    cases = yaml.safe_load((run.out / "evals" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    tools = harness.real_tools(world.folder)
    script = world.scenario.script
    assert script is not None
    text = " ".join(
        out for case in script.cases for out in [*harness.run_real_calls(case, tools), case.request]
    )
    for case in cases:
        for scorer in case["scorers"]:
            kind, _, value = scorer.partition(":")
            if kind == "contains" and harness.data_like(value):
                assert value in text, (case["id"], scorer)


@pytest.mark.parametrize(
    "scenario", [s for s in SCRIPTED if any(m.layer == "draft" for m in s.mistakes)], ids=ident
)
async def test_a_mistake_in_the_first_draft_is_repaired_with_the_check_code_in_the_message(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    drafted = [m for m in world.scenario.mistakes if m.layer == "draft"]
    run = await built(world, tmp_path_factory.mktemp("builds"))
    prompts = run.fake.architect_prompts
    assert len(prompts) >= 2
    for mistake in drafted:
        assert mistake.code in prompts[1] and mistake.code not in prompts[0]


@scripted
async def test_the_architect_sees_the_digest_and_the_notes_and_the_designer_the_data(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    first = run.fake.architect_prompts[0]
    assert "<<<DATA" in first and all(name in first for name in world.scenario.names[:2])
    for note in world.scenario.notes.values():
        assert note in first
    assert run.fake.count("designer") >= 1


@scripted
async def test_the_build_leaves_the_originals_and_keeps_databases_in_the_data_folder(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run = await built(world, tmp_path_factory.mktemp("builds"))
    assert harness.digests(world.folder) == world.before
    for name in world.scenario.names:
        if name.endswith(".db"):  # an approved change reached the copy at most
            assert (run.out / "data" / name).is_file()


# ------------------------------------------------------------------------ scenario 8: ask, do not design

WRONG = SCENARIOS[7]
WRONG_QUESTION = {
    VERDICT: "question",
    "questions": [
        {
            "id": "q1",
            "text": "These files hold invoices and payments, not CVs: what shall the process do?",
            "evidence": list(WRONG.question.evidence),  # type: ignore[union-attr]
        }
    ],
}


async def test_the_wrong_description_ends_with_a_question_and_an_answer_resumes(
    worlds: Callable[[Scenario], World], tmp_path: Path
) -> None:
    from awa.blueprints import build_project

    world = worlds(WRONG)
    assert WRONG.question is not None
    asking = ScriptedBuild(WRONG, fit=WRONG_QUESTION)  # its only job before a design: the fit check
    out = tmp_path / "out"
    first = await build_project(
        WRONG.description, out_dir=out, model=asking.model, environment=None, data=[world.folder]
    )
    assert first.question is not None and first.stopped == "question"
    assert (
        asking.model.call_count <= 1 and asking.count("designer") + asking.count("architect") == 0
    )
    assert not (out / "blueprint.yaml").exists()
    known = {f"{t.id}.{c.key}" for _, t in world.profile.tables() for c in t.columns}
    assert all(q.evidence and set(q.evidence) <= known for q in first.question.questions)
    assert harness.digests(world.folder) == world.before

    resumed = ScriptedBuild(WRONG, fit={VERDICT: "ok", "questions": []})
    answers = {q.id: WRONG.question.answer for q in first.question.questions}
    second = await build_project(
        WRONG.description,
        out_dir=tmp_path / "resumed",
        model=resumed.model,
        environment=None,
        data=[world.folder],
        answers=answers,
    )
    assert second.question is None and second.passed
    brief = resumed.architect_prompts[0]
    assert WRONG.question.answer in brief  # the answer is labeled as the user's own statement
    assert re.search(r"(?i)\buser\b.{0,40}\b(answers?|statements?|says|said|words)\b", brief)


def test_the_command_line_asks_and_exits_with_2(
    worlds: Callable[[Scenario], World],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awa.blueprints

    build = awa.blueprints.build_project

    async def lazy_build(process: str, **options: Any) -> Any:
        # a model that thinks the description fits: the gate must ask all the same
        fake = ScriptedBuild(WRONG, fit={VERDICT: "ok", "questions": []})
        return await build(process, **{**options, "model": fake.model, "environment": None})

    monkeypatch.setattr(awa.blueprints, "build_project", lazy_build)
    folder, out = worlds(WRONG).folder, tmp_path / "out"
    code = main(
        ["build", WRONG.description, "--data", str(folder), "--out", str(out), "--no-input"]
    )
    printed = capsys.readouterr().out
    assert code == 2
    questions = json.loads(printed[printed.index("{") :])["questions"]
    assert questions and all(q["evidence"] for q in questions)
    assert not (out / "blueprint.yaml").exists()


# ------------------------------------------------------------------------ the command line on real files


@scenarios
def test_data_inspect_shows_the_profile_as_json_and_exactly_what_a_model_would_see(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["data", "inspect", str(world.folder), "--json"]) == 0
    shown = DataProfile.from_json(capsys.readouterr().out)
    assert [f.path for f in shown.files] == [f.path for f in world.profile.files]
    assert main(["data", "inspect", str(world.folder), "--model-view", "--mask", "drop"]) == 0
    text = capsys.readouterr().out
    assert harness.leaked_values(world.profile, text) == []
    assert harness.digests(world.folder) == world.before

"""Render a plan as the files of the exported project (nothing is written here)."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from datetime import date
from pathlib import Path
from pprint import pformat
from string import Template

from awa.core.providers import provider as provider_spec
from awa.exporter import _schema_code
from awa.exporter._plan import Plan
from awa.kernel import conditions

_HERE = Path(__file__).parent
_FRAMEWORK = ("langchain", "langchain-core", "langgraph", "python-dotenv")


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string: JSON escaping, which TOML reads, plus DEL."""

    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _template(file: str, /, **values: str) -> str:
    return Template((_HERE / "templates" / f"{file}.tmpl").read_text("utf-8")).substitute(values)


def _runtime(name: str) -> str:
    return (_HERE / "runtime_src" / name).read_text("utf-8")


def _identifier(name: str) -> str:
    """`name` as part of a Python identifier: `send-mail` -> `send_mail`."""

    return re.sub(r"\W+", "_", name).strip("_").lower() or "tool"


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _dependencies(plan: Plan) -> tuple[list[str], list[str]]:
    """The dependency ranges of `pyproject.toml` and the exact pins of `requirements.txt`."""

    names = [*_FRAMEWORK, *([plan.provider] if plan.provider else [])]
    ranges: list[str] = []
    pins: list[str] = []
    for name in names:
        version = _version(name)
        if version is None:
            ranges.append(name)
            continue
        major = int(version.split(".")[0])
        ranges.append(f"{name}>={version},<{major + 1}" if major else f"{name}>={version}")
        pins.append(f"{name}=={version}")
    return ranges, pins


def _settings(plan: Plan) -> str:
    limits = plan.limits
    lines = []
    if limits.max_tool_calls:
        lines.append(
            f'\nMAX_TOOL_CALLS = {limits.max_tool_calls}\n"""Tool calls one run may make."""'
        )
    if limits.max_total_tokens:
        lines.append(
            f'\nMAX_TOTAL_TOKENS = {limits.max_total_tokens}\n"""Tokens one run may use."""'
        )
    if limits.max_duration:
        lines.append(
            f"\nMAX_DURATION = {limits.max_duration!r}\n"
            '"""Seconds of model and tool calls one run may take."""'
        )
    if plan.compaction:
        tokens, keep = plan.compaction
        lines.append(
            f"\nCOMPACTION_TOKENS = {tokens}\nCOMPACTION_KEEP_MESSAGES = {keep}\n"
            '"""Older turns are summarised beyond this size; the last messages stay."""'
        )
    steps = min(plan.max_steps, limits.max_requests or plan.max_steps)
    return _template(
        "settings.py",
        title=plan.name,
        model=json.dumps(plan.model or "", ensure_ascii=False),
        model_kwargs=pformat(plan.model_kwargs, sort_dicts=False),
        max_steps=str(steps),
        limits="\n".join(lines),
    )


def _graph(plan: Plan) -> str:
    limits = plan.limits
    settings = ["MAX_STEPS", "MODEL", "MODEL_KWARGS"]
    middleware = ["ModelCallLimitMiddleware"]
    local: list[str] = []
    body = ['        ModelCallLimitMiddleware(run_limit=MAX_STEPS, exit_behavior="error"),']
    if limits.max_tool_calls:
        middleware.append("ToolCallLimitMiddleware")
        settings.append("MAX_TOOL_CALLS")
        body.append(
            '        ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS, exit_behavior="error"),'
        )
    if limits.max_total_tokens or limits.max_duration:
        local.append("from .limits import RunLimits")
        arguments = []
        if limits.max_total_tokens:
            settings.append("MAX_TOTAL_TOKENS")
            arguments.append("max_total_tokens=MAX_TOTAL_TOKENS")
        if limits.max_duration:
            settings.append("MAX_DURATION")
            arguments.append("max_duration=MAX_DURATION")
        body.append(f"        RunLimits({', '.join(arguments)}),")
    if plan.compaction:
        middleware.append("SummarizationMiddleware")
        settings += ["COMPACTION_KEEP_MESSAGES", "COMPACTION_TOKENS"]
        body.append(
            "        SummarizationMiddleware(\n"
            '            model, trigger=("tokens", COMPACTION_TOKENS),\n'
            '            keep=("messages", COMPACTION_KEEP_MESSAGES),\n'
            "        ),"
        )
    constants = [
        f'PROMPT = (Path(__file__).parent / "prompts" / "{plan.agent}.md").read_text(encoding="utf-8")'
    ]
    tools = []
    if plan.tools:
        local += ["from .discover import project_tools"]
        constants.append(f"TOOL_NAMES = {tuple(plan.tools)!r}")
        tools.append("*project_tools(project, TOOL_NAMES)")
    if plan.skills:
        local.append("from .skill_loader import skill_tools")
        constants.append('SKILLS = Path(__file__).parent / "skills"')
        constants.append(f"SKILL_NAMES = {tuple(plan.skills)!r}")
        tools.append(f"*skill_tools(SKILLS, SKILL_NAMES, load={plan.skill_loader})")
    if plan.gates:
        middleware.append("HumanInTheLoopMiddleware")
        local.append("from .approvals import interrupt_on")
        constants.append(f"GATES: dict[str, str | None] = {pformat(plan.gates, sort_dicts=False)}")
        body.append("        HumanInTheLoopMiddleware(interrupt_on(GATES, tools)),")
    response = ""
    if plan.output_schema:
        local.append("from .schemas import Output")
        response = "\n        response_format=Output,"
    imports = [
        "from pathlib import Path",
        "from typing import Any",
        "",
        "from langchain.agents import create_agent",
        "from langchain.agents.middleware import (\n"
        + "".join(f"    {name},\n" for name in sorted(middleware))
        + ")",
        "from langchain.chat_models import init_chat_model",
        "from langchain_core.language_models import BaseChatModel",
        "",
        *(["from . import tools as project"] if plan.tools else []),
        *sorted([*local, f"from .settings import {', '.join(sorted(settings))}"]),
    ]
    return _template(
        "graph.py",
        title=plan.name,
        imports="\n".join(imports),
        constants="\n".join(constants),
        tools=f"    tools: list[Any] = [{', '.join(tools)}]",
        middleware="    middleware = [\n" + "\n".join(body) + "\n    ]",
        response_format=response,
        agent=plan.agent,
    )


def _tests(plan: Plan) -> str:
    pkg = plan.package
    final = 'AIMessage(content="done")'
    imports = ["from langchain_core.messages import AIMessage"]
    blocks = []
    if plan.sample_output is not None:
        final = (
            'AIMessage(content="", tool_calls=[{"name": "Output", "args": SAMPLE, "id": "final"}])'
        )
        blocks.append(f"SAMPLE = {pformat(plan.sample_output)}\n")
    blocks.append(
        f"""
def test_the_graph_builds_without_credentials() -> None:
    from {pkg} import graph

    assert graph.graph is not None


def test_the_system_prompt_reaches_the_model() -> None:
    model = ScriptedChatModel(script=[{final}])

    result = build_graph(model).invoke({{"messages": [("user", "hello")]}})

    assert model.seen[0][0].content == PROMPT
    assert result["messages"][-1] is not None
"""
    )
    if plan.sample_output is not None:
        blocks.append(
            """
def test_the_answer_is_the_structured_output() -> None:
    model = ScriptedChatModel(script=[AIMessage(content="", tool_calls=[{"name": "Output", "args": SAMPLE, "id": "final"}])])

    result = build_graph(model).invoke({"messages": [("user", "hello")]})

    assert result["structured_response"].model_dump(mode="json", exclude_none=True) == SAMPLE
"""
        )
    if plan.sample_calls:
        imports += [
            "from langgraph.checkpoint.memory import InMemorySaver",
            "from langgraph.types import Command",
        ]
    for number, (tool, arguments) in enumerate(plan.sample_calls.items(), 1):
        call = f'AIMessage(content="", tool_calls=[{{"name": {tool!r}, "args": {arguments!r}, "id": "call-1"}}])'
        blocks.append(
            f"""
def test_{_identifier(tool)}_waits_for_a_person_{number}() -> None:
    model = ScriptedChatModel(script=[{call}, {final}])
    graph = build_graph(model, checkpointer=InMemorySaver())
    config = {{"configurable": {{"thread_id": "t1"}}}}

    paused = graph.invoke({{"messages": [("user", "go")]}}, config)
    request = paused["__interrupt__"][0].value["action_requests"][0]
    assert (request["name"], request["args"]) == ({tool!r}, {arguments!r})

    done = graph.invoke(Command(resume={{"decisions": [{{"type": "reject", "message": "no"}}]}}), config)
    answered = [m for m in done["messages"] if m.type == "tool" and m.name == {tool!r}]
    assert answered and answered[0].status == "error"
"""
        )
    header = (
        f'"""Offline tests of {plan.name}: a scripted model drives the real graph."""\n\n'
        "from __future__ import annotations\n\n"
        + "\n".join(sorted(imports))
        + "\n\n"
        + "\n".join(
            sorted(
                [
                    f"from {pkg}.graph import PROMPT, build_graph",
                    "from fakes import ScriptedChatModel",
                ]
            )
        )
        + "\n\n"
    )
    return header + "\n".join(blocks)


def _env_example(plan: Plan) -> str:
    provider = (plan.model or "").partition(":")[0]
    spec = provider_spec(provider) if provider else None
    if spec is not None:
        names = list(spec.needed(lambda _: None))
    else:
        names = [f"{provider.upper()}_API_KEY"] if provider else []
    lines = [
        "# Names only: put the values in .env, never in the repository.",
        "# MODEL=provider:model",
    ]
    return "\n".join([*lines, *(f"{name}=" for name in names)]) + "\n"


def render(plan: Plan, awa_version: str, today: date) -> dict[str, str | bytes]:
    """All files of the export, by relative path.

    Args:
        plan: What to export.
        awa_version: The awa version, recorded in `NOTICE.md`.
        today: The date, recorded in `NOTICE.md`.
    """

    pkg = f"src/{plan.package}"
    ranges, pins = _dependencies(plan)
    copied: dict[str, str | bytes] = {}
    if plan.gates:
        copied[f"{pkg}/approvals.py"] = _runtime("approvals.py")
        copied[f"{pkg}/conditions.py"] = Path(conditions.__file__).read_text("utf-8")
    if plan.limits.max_total_tokens or plan.limits.max_duration:
        copied[f"{pkg}/limits.py"] = _runtime("limits.py")
    if plan.skills:
        copied[f"{pkg}/skill_loader.py"] = _runtime("skill_loader.py")
    if plan.tools:
        copied[f"{pkg}/discover.py"] = _runtime("discover.py")
        assert plan.tool_file is not None
        copied[f"{pkg}/tools.py"] = plan.tool_file.read_bytes()
    for name, folder in plan.skill_folders.items():
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            hidden = any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
            if path.is_file() and not hidden:
                copied[f"{pkg}/skills/{name}/{relative.as_posix()}"] = path.read_bytes()
    files: dict[str, str | bytes] = {
        **copied,
        f"{pkg}/__init__.py": f'"""{plan.name}: exported from an awa blueprint (see NOTICE.md)."""\n',
        f"{pkg}/graph.py": _graph(plan),
        f"{pkg}/settings.py": _settings(plan),
        f"{pkg}/prompts/{plan.agent}.md": plan.prompt + "\n",
        "tests/fakes.py": (_HERE / "templates" / "fakes.py.tmpl").read_text("utf-8"),
        "tests/test_graph.py": _tests(plan),
        "run.py": _template("run.py", package=plan.package),
        "pyproject.toml": _template(
            "pyproject.toml",
            distribution=plan.package.replace("_", "-"),
            version=_toml_string(plan.version),
            description=_toml_string(plan.description or plan.name),
            dependencies=",\n".join(f'    "{item}"' for item in ranges) + ",",
            package=plan.package,
        ),
        "requirements.txt": "\n".join([*pins, "pytest"]) + "\n",
        ".env.example": _env_example(plan),
        "langgraph.json": json.dumps(
            {
                "dependencies": ["."],
                "graphs": {plan.agent: f"./{pkg}/graph.py:graph"},
                "env": ".env",
            },
            indent=2,
        )
        + "\n",
    }
    if plan.output_schema:
        files[f"{pkg}/schemas.py"] = (
            f'"""The structured output of {plan.agent}, generated from its output schema."""\n\n'
            "from __future__ import annotations\n\n"
            "from typing import Annotated, Any, Literal\n\n"
            "from pydantic import AllowInfNan, BaseModel, ConfigDict, Field, Strict\n\n\n"
            f"{_schema_code.model_source(plan.output_schema, 'Output')}\n"
        )
    approvals = (
        "Calls of " + ", ".join(f"`{t}`" for t in plan.gates) + " pause the run for a person. The "
        "graph needs a checkpointer (`run.py` uses an in-memory one; Studio supplies its own). "
        'Resume with `Command(resume={"decisions": [{"type": "approve"}]})`; `reject` takes a '
        "`message`, `edit` an `edited_action` with new arguments."
        if plan.gates
        else "This agent has no approval gates."
    )
    listed = [
        ("tools.py", "your tools, copied unchanged"),
        ("skills/", "the skills the agent loads on demand"),
    ]
    rows = "\n".join(
        f"| `src/{plan.package}/{name}` | {text} |"
        for name, text in listed
        if (name == "tools.py" and plan.tools) or (name == "skills/" and plan.skills)
    )
    files["README.md"] = _template(
        "README.md",
        title=plan.name,
        description=plan.description,
        blueprint=plan.source.name,
        package=plan.package,
        agent=plan.agent,
        files=rows,
        approvals=approvals,
        warnings=("\nLeft out of the export: " + "; ".join(plan.warnings) + ".\n")
        if plan.warnings
        else "",
    )
    files["NOTICE.md"] = _template(
        "NOTICE.md",
        awa_version=awa_version,
        date=today.isoformat(),
        blueprint=plan.source.name,
        name=plan.name,
        version=plan.version,
    )
    sums = "".join(
        f"{hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()}  {path}\n"
        for path, content in sorted(copied.items())
    )
    files["SHA256SUMS"] = sums
    return files

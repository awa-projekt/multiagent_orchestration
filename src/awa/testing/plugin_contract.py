"""The contract suites a replacement must pass before awa uses it (`parser/1`, `connector/1`, `tool/1`, `sql_guard/1`).

Every built-in passes its own suite; a suite runs offline and lists every case. `awa plugins
check` and the files of `awa new parser|connector|tool` use them.
"""

from __future__ import annotations

import hashlib
import inspect
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

from awa.plugins.switches import ComponentError, split_key

CaseStatus = Literal["passed", "failed", "skipped"]

SUITES: dict[str, str] = {
    "parser": "parser/1",
    "connector": "connector/1",
    "tool": "tool/1",
    "guard.sql_guard": "sql_guard/1",
}
"""The suite of each replaceable kind (or single component), with its version."""

_PARSE_LIMIT_S = 30.0


@dataclass(frozen=True)
class CaseResult:
    """One case of a suite: `name`, `status` (`passed`, `failed` or `skipped`) and why."""

    name: str
    status: CaseStatus
    message: str = ""


@dataclass(frozen=True)
class ContractResult:
    """The result of running a suite on one candidate.

    Attributes:
        key: The component, `KIND.NAME`.
        suite: The suite and its version, `parser/1`.
    """

    key: str
    suite: str
    cases: tuple[CaseResult, ...] = field(default_factory=tuple)
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        """Whether no case failed (and at least one ran)."""

        return any(case.status == "passed" for case in self.cases) and not any(
            case.status == "failed" for case in self.cases
        )

    @property
    def failed_case(self) -> CaseResult | None:
        """The first case that failed, or `None`."""

        return next((case for case in self.cases if case.status == "failed"), None)

    def summary(self) -> str:
        """One line per case, for a terminal or an assertion message."""

        lines = [f"{self.suite} on {self.key}: {'passed' if self.passed else 'FAILED'}"]
        for case in self.cases:
            suffix = f" - {case.message}" if case.message else ""
            lines.append(f"  {case.status:<7} {case.name}{suffix}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """The result as JSON-compatible data."""

        return {
            "key": self.key,
            "suite": self.suite,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "cases": [
                {"name": c.name, "status": c.status, "message": c.message} for c in self.cases
            ],
        }


def suite_of(key: str) -> str | None:
    """The suite a replacement of `key` must pass, or `None` when `key` cannot be replaced."""

    kind, name = split_key(key)
    return SUITES.get(f"{kind}.{name}") or (SUITES.get(kind) if kind != "guard" else None)


def verify(key: str, candidate: Any) -> ContractResult:
    """Run the suite of `key` on `candidate`, the object that would replace it; never raises for a failure.

    Args:
        key: `KIND.NAME`, for example `parser.csv` or `guard.sql_guard`.
        candidate: A parser function, a `ConnectorSpec` (or a function returning one), a tool (or a
            documented function), or a SQL check function.

    Raises:
        ComponentError: If `key` cannot be replaced (it has no suite).

    Example:
        ```python
        result = verify("parser.text", profile_text)
        assert result.passed, result.summary()
        ```
    """

    suite = suite_of(key)
    if suite is None:
        raise ComponentError(
            f"{key} cannot be replaced in this version (switch it on or off instead); "
            "replaceable: parsers, connectors, tools and guard.sql_guard"
        )
    kind, name = split_key(key)
    started = time.perf_counter()
    runner: Callable[[str, Any], Iterable[CaseResult]] = {
        "parser/1": _parser_cases,
        "connector/1": _connector_cases,
        "tool/1": _tool_cases,
        "sql_guard/1": _sql_guard_cases,
    }[suite]
    cases = tuple(runner(name, candidate))
    return ContractResult(key, suite, cases, int((time.perf_counter() - started) * 1000))


def _passed(name: str) -> CaseResult:
    return CaseResult(name, "passed")


def _failed(name: str, message: str) -> CaseResult:
    return CaseResult(name, "failed", message)


def _skipped(name: str, message: str) -> CaseResult:
    return CaseResult(name, "skipped", message)


def _outcome(name: str, ok: bool, message: str) -> CaseResult:
    return _passed(name) if ok else _failed(name, message)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


_SAMPLES: dict[str, tuple[str, bytes]] = {
    "csv": ("sample.csv", b"id,name,amount\n1,Alpha,10.5\n2,Beta,20\n3,Gamma,7.25\n"),
    "text": ("sample.txt", b"first line of the sample\nsecond line\nthird line\n"),
    "json": ("sample.json", b'{"orders": [{"id": 1, "amount": 10.5}, {"id": 2, "amount": 20}]}'),
    "mail": (
        "sample.eml",
        b"From: alice@example.com\r\nTo: bob@example.com\r\nSubject: Order 17\r\n"
        b"Date: Tue, 06 Oct 2026 10:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nPlease ship order 17.\r\n",
    ),
}
_TABULAR = frozenset({"csv"})
_GARBAGE = bytes(range(256)) * 8 + b"<<<not a valid file>>>" + bytes(range(255, -1, -1)) * 8


def _sample(kind: str, folder: Path) -> Path | None:
    if kind in _SAMPLES:
        name, data = _SAMPLES[kind]
        path = folder / name
        path.write_bytes(data)
        return path
    return None


def _parse_once(parser: Any, kind: str, path: Path) -> tuple[Any, float]:
    """Call `parser` on `path` like the profiler does, in a thread with the time limit."""

    from awa.data import FileProfile, Limits, ParseContext, Privacy
    from awa.runtime.clock import SystemClock

    limits = Limits()
    clock = SystemClock()
    data = path.read_bytes()
    profile = FileProfile(
        id="f1", path=path.name, sha256=hashlib.sha256(data).hexdigest(), size=len(data), kind=kind
    )

    def child(name: str, payload: bytes) -> FileProfile:
        return FileProfile(
            id="f1.a1",
            path=name,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            kind="unknown",
            status="unsupported",
        )

    ctx = ParseContext(
        limits,
        Privacy(),
        "f1",
        profile,
        clock.monotonic() + min(limits.parse_timeout_s, _PARSE_LIMIT_S),
        lambda event: None,
        child,
        clock,
    )
    outcome: list[tuple[bool, Any]] = []

    def work() -> None:
        try:
            outcome.append((True, parser(path, ctx)))
        except BaseException as error:  # noqa: BLE001 - reported as a failed case
            outcome.append((False, error))

    started = time.perf_counter()
    thread = threading.Thread(target=work, daemon=True, name="awa-contract-parse")
    thread.start()
    thread.join(_PARSE_LIMIT_S + 1.0)
    took = time.perf_counter() - started
    if not outcome:
        raise TimeoutError(f"the parser did not return within {_PARSE_LIMIT_S:g} s")
    ok, value = outcome[0]
    if not ok:
        raise value
    return value, took


def _identity_problem(result: Any, path: Path, kind: str) -> str | None:
    from awa.data import FileProfile

    if not isinstance(result, FileProfile):
        return f"returned {type(result).__name__}, not the FileProfile it was given"
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    if (result.id, result.path, result.sha256, result.kind) != ("f1", path.name, expected, kind):
        return "changed the identity of the file (id, path, sha256 or kind)"
    return None


def _parser_cases(kind: str, parser: Any) -> list[CaseResult]:
    cases: list[CaseResult] = []
    name = "is_a_function_of_path_and_context"
    try:
        parameters = inspect.signature(parser).parameters
        positional = [
            p for p in parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        ok = callable(parser) and (
            len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in parameters.values())
        )
    except (TypeError, ValueError):
        ok = False
    if not ok:
        return [_failed(name, "a parser is a function parser(path, ctx) -> FileProfile")]
    cases.append(_passed(name))
    with tempfile.TemporaryDirectory(prefix="awa-contract-") as folder:
        root = Path(folder)
        sample = _sample(kind, root)
        name = "profiles_a_valid_sample"
        if sample is None:
            cases.append(_skipped(name, f"the suite has no sample {kind} file"))
        else:
            cases.append(_check_sample(parser, kind, sample, name))
        for case_name, data in (
            ("survives_bad_content_without_raising", _GARBAGE),
            ("survives_an_empty_file_without_raising", b""),
        ):
            path = root / f"{case_name}.{kind}"
            path.write_bytes(data)
            try:
                result, _ = _parse_once(parser, kind, path)
            except BaseException as exc:  # noqa: BLE001 - the case reports it
                cases.append(
                    _failed(case_name, f"raised {_describe(exc)}; return ctx.fail(...) instead")
                )
                continue
            problem = _identity_problem(result, path, kind)
            cases.append(_outcome(case_name, problem is None, problem or ""))
    return cases


def _check_sample(parser: Any, kind: str, sample: Path, name: str) -> CaseResult:
    try:
        result, _ = _parse_once(parser, kind, sample)
    except BaseException as exc:  # noqa: BLE001 - the case reports it
        return _failed(name, f"raised {_describe(exc)}")
    problem = _identity_problem(result, sample, kind)
    if problem:
        return _failed(name, problem)
    if result.status not in ("ok", "partial"):
        codes = ", ".join(flag.code for flag in result.flags) or "no flag"
        return _failed(name, f"status {result.status} on a valid {kind} file ({codes})")
    if kind in _TABULAR and not result.tables:
        return _failed(name, f"found no table in a valid {kind} file")
    return _passed(name)


def _env_names(value: Any, found: set[str]) -> None:
    from awa.connectors import EnvVar

    if isinstance(value, EnvVar):
        found.add(value.name)
    elif isinstance(value, tuple | list):
        for item in value:
            _env_names(item, found)
    elif is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            _env_names(getattr(value, item.name), found)


def _connector_cases(name: str, candidate: Any) -> list[CaseResult]:
    from awa._internal.http_fake import MockTransport
    from awa._internal.upstream.tools import BaseTool
    from awa.connectors import ConnectorSpec
    from awa.connectors._catalog import build_tools, builtin_connector, connector_context

    case = "is_a_connector_spec"
    spec = candidate
    if not isinstance(spec, ConnectorSpec) and callable(spec):
        try:
            spec = spec()
        except Exception as exc:  # noqa: BLE001 - the case reports it
            return [_failed(case, f"calling it raised {_describe(exc)}")]
    if not isinstance(spec, ConnectorSpec):
        return [_failed(case, f"got {type(spec).__name__}; pass a ConnectorSpec")]
    cases = [_passed(case)]
    case = "keeps_the_name"
    cases.append(
        _outcome(
            case, spec.name == name, f"is named {spec.name!r}; blueprints use connector:{name}"
        )
    )
    case = "declares_what_it_can_do"
    declared = bool(spec.operations) or spec.factory is not None or spec.dynamic
    cases.append(_outcome(case, declared, "has no operations, no factory and is not dynamic"))
    case = "keeps_the_operations_of_the_built_in"
    builtin = builtin_connector(name)
    if builtin is None or not builtin.operations:
        cases.append(_skipped(case, "no built-in operations to keep"))
    else:
        missing = sorted(
            {op.name for op in builtin.operations} - {op.name for op in spec.operations}
        )
        message = f"misses {', '.join(missing)} (blueprints may name them)"
        cases.append(_outcome(case, not missing, message))
    case = "builds_its_tools_offline"
    if spec.factory is not None or spec.configure is not None or spec.dynamic:
        cases.append(_skipped(case, "builds its tools itself; needs the real system"))
        return cases
    names: set[str] = set()
    _env_names(spec.env, names)
    _env_names(spec.auth, names)
    env = dict.fromkeys(names, "https://contract.invalid")
    try:
        ctx = connector_context(spec, env=env, transport=MockTransport({}), dotenv=None)
        tools = build_tools(ctx)
    except Exception as exc:  # noqa: BLE001 - the case reports it
        cases.append(_failed(case, f"raised {_describe(exc)}"))
        return cases
    built = bool(tools) and all(isinstance(tool, BaseTool) for tool in tools)
    cases.append(_outcome(case, built, "built no tools"))
    return cases


def _tool_cases(name: str, candidate: Any) -> list[CaseResult]:
    from awa._internal.upstream.tools import BaseTool
    from awa.tools.decorator import tool as as_tool

    case = "is_a_tool"
    built = candidate
    if not isinstance(built, BaseTool):
        if not callable(built):
            return [_failed(case, f"got {type(built).__name__}; pass a tool or a function")]
        try:
            built = as_tool(built)
        except Exception as exc:  # noqa: BLE001 - the case reports it
            return [_failed(case, f"cannot be made a tool: {_describe(exc)}")]
    cases = [_passed(case)]
    case = "keeps_the_name"
    message = f"is named {built.name!r}; agents and blueprints call it {name!r}"
    cases.append(_outcome(case, built.name == name, message))
    case = "describes_itself_for_the_model"
    described = len((built.description or "").strip()) >= 10
    message = "needs a docstring or description of at least 10 characters"
    cases.append(_outcome(case, described, message))
    case = "has_an_argument_schema"
    try:
        schema = built.args
    except Exception as exc:  # noqa: BLE001 - the case reports it
        cases.append(_failed(case, _describe(exc)))
    else:
        cases.append(_outcome(case, isinstance(schema, dict), "no argument schema"))
    return cases


_REFUSED = (
    "DELETE FROM orders",
    "UPDATE orders SET amount = 0",
    "INSERT INTO orders VALUES (1, 2)",
    "DROP TABLE orders",
    "SELECT 1; DROP TABLE orders",
    "select * from orders; delete from orders",
    "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
    "CREATE TABLE copy AS SELECT * FROM orders",
    "SELECT * INTO copy FROM orders",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_terminate_backend(1)",
    "SELECT set_config('search_path', 'x', false)",
    "SELECT lo_import('/etc/passwd')",
    "SELECT pg_sleep(10)",
    "TRUNCATE orders",
    "EXPLAIN ANALYZE DELETE FROM orders",
    "COPY orders TO PROGRAM 'cat'",
    "MERGE INTO orders USING t ON 1 = 1 WHEN MATCHED THEN DELETE",
    "GRANT ALL ON orders TO public",
    "SET ROLE admin",
)
_ACCEPTED = (
    "SELECT id, amount FROM orders",
    "SELECT * FROM orders WHERE note = 'drop table orders'",
    "WITH totals AS (SELECT id, amount FROM orders) SELECT id FROM totals",
    "SELECT id FROM orders UNION SELECT id FROM archive",
    "SELECT id FROM orders -- a note; with a semicolon",
)


def _sql_guard_cases(name: str, candidate: Any) -> list[CaseResult]:
    case = "is_a_function_of_statement_and_dialect"
    if not callable(candidate):
        return [_failed(case, "a SQL check is a function check(statement, dialect=None)")]
    cases = [_passed(case)]
    for statement in _REFUSED:
        case = f"refuses: {statement}"
        try:
            candidate(statement, None)
        except Exception:  # noqa: BLE001 - refusing means raising
            cases.append(_passed(case))
        else:
            cases.append(_failed(case, "let a statement through that changes data or state"))
    for statement in _ACCEPTED:
        case = f"accepts: {statement}"
        try:
            candidate(statement, None)
        except Exception as exc:  # noqa: BLE001 - the case reports it
            cases.append(_failed(case, f"refused a read-only query ({_describe(exc)})"))
        else:
            cases.append(_passed(case))
    return cases


def builtin_object(key: str) -> Any:
    """The built-in object behind a replaceable part.

    Raises:
        ComponentError: If there is none.
    """

    kind, name = split_key(key)
    found: Any = None
    if kind == "parser":
        from awa.data._parse import builtin_parsers

        found = {k: p for k, p, _ in builtin_parsers()}.get(name)
    elif kind == "connector":
        from awa.connectors._catalog import builtin_connector

        found = builtin_connector(name)
    elif kind == "tool":
        from awa.toolkits import catalog

        found = _builtin_tool(catalog().get(name)) if name in catalog() else None
    elif f"{kind}.{name}" == "guard.sql_guard":
        from awa.toolkits._sql_guard import builtin_read_only_check

        found = builtin_read_only_check
    if found is None:
        raise ComponentError(f"{key} has no built-in object to check")
    return found


def _builtin_tool(entry: Any) -> Any:
    """A built-in tool's name, description and schema, without building its toolkit."""

    from awa._internal.upstream.tools import StructuredTool

    def stand_in(**kwargs: Any) -> str:
        return ""

    return StructuredTool.from_function(func=stand_in, name=entry.name, description=entry.summary)


__all__ = ["SUITES", "CaseResult", "ContractResult", "builtin_object", "suite_of", "verify"]

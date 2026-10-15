"""What the guards count: one function per baseline key, each returning its findings.

`tests/architecture/baseline.json` holds the number of findings per key. A guard test fails when
the count rises or falls without the baseline being lowered in the same commit (the ratchet);
`scripts/arch_baseline.py` regenerates the file after an intended improvement. Everything here
reads the source tree with `ast`, so nothing can fail by importing awa.
"""

from __future__ import annotations

import ast
import collections
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from architecture.scan import (
    HERE,
    REPO,
    ROLES,
    SRC,
    TESTS,
    Module,
    base_names,
    edges,
    matching_overrides,
    modules,
    ports,
    role_table,
    roles,
    strings,
)


@dataclass(frozen=True)
class Measure:
    find: Callable[[], list[str]]
    rule: str
    fix: str


MEASURES: dict[str, Measure] = {}


def measure(
    key: str, rule: str, fix: str
) -> Callable[[Callable[[], list[str]]], Callable[[], list[str]]]:
    def register(find: Callable[[], list[str]]) -> Callable[[], list[str]]:
        MEASURES[key] = Measure(functools.cache(find), rule, fix)
        return find

    return register


def _code() -> Iterator[Module]:
    """Every module that is not test support."""

    for module in modules().values():
        if roles()[module.name] != "testing" and module.package != "testing":
            yield module


# ============================================================================= GT1 roles


@measure(
    "GT1.table_problems",
    "R1",
    "give the package a role in tests/architecture/roles.toml [package], or the module an "
    "[[override]] (one role per module, no dead or overlapping patterns)",
)
def table_problems() -> list[str]:
    table = role_table()
    problems = []
    packages = {m.package for m in modules().values()} - {"awa"}
    for package in sorted(packages - set(table["package"])):
        problems.append(f"package {package!r} has no role: add it to roles.toml [package]")
    for package in sorted(set(table["package"]) - packages):
        problems.append(f"[package] {package!r} is not a package of awa (dead entry)")
    for module in modules().values():
        hits = matching_overrides(module)
        if len(hits) > 1:
            problems.append(
                f"src/{module.rel}: matches {len(hits)} overrides: {[h['path'] for h in hits]}"
            )
    for entry in table["override"]:
        if not any(fnmatch.fnmatch(m.rel, entry["path"]) for m in modules().values()):
            problems.append(f"override {entry['path']!r} matches no module (dead entry)")
    for exempt in table["budget_exempt"]:
        if not any(m.rel == exempt for m in modules().values()):
            problems.append(f"budget_exempt {exempt!r} is not a module (dead entry)")
    declared = [*table["package"].values(), *(entry["role"] for entry in table["override"])]
    problems += [f"unknown role {role!r}" for role in sorted(set(declared) - set(ROLES))]
    return problems


def _direction(importers: set[str], targets: set[str], *, cross_package: bool) -> list[str]:
    found = []
    for edge in edges():
        if not edge.internal or edge.kind == "typing":
            continue
        source, target = modules()[edge.source], modules().get(edge.target)
        if target is None or target.name == source.name:
            continue
        if (
            roles()[source.name] in importers
            and roles()[target.name] in targets
            and (not cross_package or source.package != target.package)
        ):
            found.append(
                f"{edge.where()} [{edge.kind}] {roles()[source.name]} -> "
                f"{target.name} ({roles()[target.name]})"
            )
    return found


@measure(
    "GT1.domain_to_adapter",
    "R2",
    "inject the backend protocol instead of importing the backend module; only roots choose backends",
)
def domain_to_adapter() -> list[str]:
    return _direction({"domain", "protocol"}, {"backend", "backend-kit"}, cross_package=False)


@measure(
    "GT1.domain_to_mixed",
    "R2 R3",
    "move the backend protocol out of the module that also holds its backends (a `ports` module), import that",
)
def domain_to_mixed() -> list[str]:
    return _direction({"domain", "protocol"}, {"mixed"}, cross_package=True)


@measure(
    "GT1.adapter_to_adapter",
    "R4",
    "backends never import each other; share through the family's backend-kit module",
)
def adapter_to_adapter() -> list[str]:
    return _direction({"backend", "mixed"}, {"backend"}, cross_package=False)


_ADAPTER_NAME = re.compile(r"^((InMemory|Sqlite|Postgres|Jsonl|File)[A-Z]\w*|StateInMemorySaver)$")
"""Names of the concrete backends (the in-memory TaskBoard and Blackboard are listed where they are built)."""
_DATA_MODELS = {"FileProfile", "SqliteInfo"}
"""Data classes of awa.data that start like a backend name but hold a profile, not a resource."""


@measure(
    "GT1.adapter_inherits_adapter",
    "R4",
    "compose or share a helper; a backend extends a backend protocol or a kit, never another backend",
)
def adapter_inherits_adapter() -> list[str]:
    adapter_roles = {"backend", "mixed"}
    defined = {
        node.name
        for m in modules().values()
        if roles()[m.name] in adapter_roles
        for node in m.tree.body
        if isinstance(node, ast.ClassDef) and _ADAPTER_NAME.match(node.name)
    }
    found = []
    for module in modules().values():
        if roles()[module.name] not in adapter_roles:
            continue
        for node in module.tree.body:
            if isinstance(node, ast.ClassDef):
                found += [
                    f"{module.where(node)} class {node.name}({base})"
                    for base in base_names(node)
                    if base in defined
                ]
    return found


@measure(
    "GT1.adapters_built_in_domain",
    "R16 R21",
    "take the backend protocol as an argument; only a root builds backends (a convenience default must warn)",
)
def adapters_built_in_domain() -> list[str]:
    found = []
    for module in _code():
        if roles()[module.name] not in {"domain", "protocol"}:
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func).rsplit(".", 1)[-1]
                if (_ADAPTER_NAME.match(name) and name not in _DATA_MODELS) or name in {
                    "TaskBoard",
                    "Blackboard",
                }:
                    found.append(f"{module.where(node)} {name}(...)")
    return found


@measure(
    "GT1.ports_outside_ports_modules",
    "R3",
    "define new backend protocols in a `ports` module of the consuming package",
)
def ports_outside_ports_modules() -> list[str]:
    port_modules = {name for name, role in roles().items() if role == "protocol"}
    return [
        p.qualname
        for p in ports()
        if p.qualname.rsplit(".", 1)[0] not in port_modules
        and p.qualname.split(".")[-2] not in {"ports", "_ports"}
    ]


@measure(
    "GT1.mixed_modules",
    "R1 R3",
    "split the module: the backend protocol into `ports`, each backend into its own module",
)
def mixed_modules() -> list[str]:
    return [name for name, role in roles().items() if role == "mixed"]


# ======================================================================== GT2 drivers

DRIVERS = re.compile(
    r"^(psycopg|psycopg_pool|asyncpg|sqlite3|pymysql|pymssql|smtplib|imaplib|poplib|urllib\.request|"
    r"http\.client|socket|ssl|subprocess|fastapi|starlette|uvicorn|opentelemetry|openai)(\.|$)"
)


@measure(
    "GT2.driver_in_domain",
    "R5",
    "move the driver call into a backend module (see tests/architecture/roles.toml) and call that",
)
def driver_in_domain() -> list[str]:
    return [
        f"{edge.where()} [{edge.kind}] import {edge.target} in a {roles()[edge.source]} module"
        for edge in edges()
        if not edge.internal
        and edge.kind != "typing"
        and DRIVERS.match(edge.target)
        and roles()[edge.source] in {"domain", "protocol", "facade"}
    ]


# ====================================================================== GT3 test awareness

_TEST_WORDING = re.compile(
    r"(for tests? only|local tests? only|for local tests?|\(local tests\)|test-only|"
    r"intended for tests|\bfor tests\b|\(tests\)|in tests\b)",
    re.I,
)
_TEST_NAMES = re.compile(
    r"(_OFFLINE|awa_offline|for_test|test_mode|testing_mode|allow_unsigned|ALLOW_MODEL)"
)
_FAKE_CLASS = re.compile(r"^(Fake|Mock|Stub|Scripted|Dummy)")
_ENV_PROBE = re.compile(r"(PYTEST_CURRENT_TEST|AWA_TEST|\"pytest\"|'pytest')")
_TEST_PACKAGES = ("pytest", "unittest", "mock")


@functools.cache
def _test_awareness() -> tuple[list[str], list[str]]:
    behavioral: list[str] = []
    wording: list[str] = []
    for module in _code():
        for edge in (e for e in edges() if e.source == module.name):
            target = edge.target
            if (
                target == "awa.testing"
                or target.startswith("awa.testing.")
                or target.split(".")[0] in _TEST_PACKAGES
            ):
                behavioral.append(f"src/{module.rel}:{edge.line} [{edge.kind}] imports {target}")
        for node in strings(module):
            text = node.value
            if _ENV_PROBE.search(text):
                behavioral.append(f"{module.where(node)} test probe string {text[:50]!r}")
            if _TEST_WORDING.search(text):
                behavioral.append(
                    f"{module.where(node)} test-only wording in a message {text[:50]!r}"
                )
        for node in ast.walk(module.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if _TEST_NAMES.search(node.name):
                    behavioral.append(f"{module.where(node)} test-aware name {node.name}")
                if isinstance(node, ast.ClassDef) and _FAKE_CLASS.match(node.name):
                    behavioral.append(f"{module.where(node)} class {node.name} outside awa.testing")
            elif isinstance(node, ast.arg) and _TEST_NAMES.search(node.arg):
                behavioral.append(f"{module.where(node)} test-aware argument {node.arg}")
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and _TEST_NAMES.search(node.target.id)
            ):
                behavioral.append(f"{module.where(node)} test-aware field {node.target.id}")
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and _TEST_NAMES.search(node.id)
            ):
                behavioral.append(f"{module.where(node)} test-aware name {node.id}")
            elif isinstance(node, ast.Compare):
                text = ast.unparse(node)
                if "sys.modules" in text and re.search(r"pytest|unittest|mock", text):
                    behavioral.append(
                        f"{module.where(node)} sys.modules probe for a test framework"
                    )
        wording += _docstring_wording(module)
    return behavioral, wording


def _docstring_wording(module: Module) -> list[str]:
    found = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                value = body[0].value.value
                if isinstance(value, str) and (hit := _TEST_WORDING.search(value)):
                    found.append(f"{module.where(body[0])} {hit.group(0)!r}")
    return found


@measure(
    "GT3.behavioral",
    "R11 R12 R15",
    "production code must not import awa.testing or pytest, probe for tests, or carry a field "
    "or message 'for tests': inject a backend protocol and let the test pass a fake (awa.testing)",
)
def test_awareness_behavioral() -> list[str]:
    return _test_awareness()[0]


@measure(
    "GT3.docstring_wording",
    "R11",
    "describe the backend protocol that a test injects ('pass a fake clock'), not a test mode",
)
def test_awareness_wording() -> list[str]:
    return _test_awareness()[1]


@measure(
    "GT3.test_doubles_outside_homes",
    "R12",
    "a double users could reuse belongs in awa.testing; a repository-only one in tests/_helpers/",
)
def test_doubles_outside_homes() -> list[str]:
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(TESTS).as_posix()
        if relative.startswith(("_helpers/", "architecture/")) or "__pycache__" in relative:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and _FAKE_CLASS.match(node.name):
                found.append(f"tests/{relative}:{node.lineno} class {node.name}")
    return found


# ============================================================================ GT4 wheel

WHEEL_ALLOW = (
    ("*.py", "code"),
    ("awa/py.typed", "typing marker"),
    ("awa/skills/builtin/**", "built-in skills (product content)"),
    ("awa/templates/**", "scaffold templates (product content)"),
    ("awa/exporter/templates/*.tmpl", "file templates of the exported project (product content)"),
    ("awa/blueprints/_catalog.json", "generated connector and tool catalog"),
    ("awa/data/profile.v1.schema.json", "the published JSON Schema of a data profile (generated)"),
    ("awa/ui/page.html", "the UI page"),
    ("awa/ui/page.css", "the stylesheet of the UI page (inlined into it when the page is served)"),
    ("awa/ui/js/*.js", "the scripts of the UI page (inlined into it when the page is served)"),
    ("awa/persistence/migrations/**", "the versioned SQL migrations of the data store"),
    ("awa/project_setup/compose.services.yaml", "the Compose file of `awa setup services`"),
)
"""The files that may sit under src/awa (hatch ships all of them): code, plus product data with a reason."""


WHEEL_EXCLUDED = tuple(
    tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"][
        "targets"
    ]["wheel"].get("exclude", [])
)
"""Files under src/awa that hatch leaves out of the wheel (`exclude` in pyproject.toml), such as lint settings."""


def wheel_allowed(relative: str) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (pattern.endswith("/**") and relative.startswith(pattern[:-2]))
        for pattern, _ in WHEEL_ALLOW
    )


@measure(
    "GT4.outside_allowlist",
    "R14 R25",
    "test and sample data live in tests/ or examples/; a real product file needs a line in "
    "WHEEL_ALLOW (tests/architecture/measures.py) with its reason",
)
def outside_allowlist() -> list[str]:
    found = []
    for path in sorted((SRC / "awa").rglob("*")):
        relative = path.relative_to(SRC).as_posix()
        if (
            path.is_file()
            and "__pycache__" not in relative
            and f"/src/{relative}" not in WHEEL_EXCLUDED
            and not wheel_allowed(relative)
        ):
            found.append(f"src/{relative}")
    return found


# ===================================================================== GT6 import effects

_IMPORT_EFFECTS_ALLOWED = {
    "awa._internal.upstream.agents": "importing langchain.agents registers atexit and binds a loopback socket (upstream)"
}


@functools.cache
def import_effects() -> dict[str, list[str]]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]),
    }
    result = subprocess.run(
        [sys.executable, str(HERE / "import_effects.py")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, f"the import probe failed:\n{result.stderr[-2000:]}"
    effects: dict[str, list[str]] = json.loads(result.stdout.splitlines()[-1])
    return effects


@measure(
    "GT6.modules_with_import_effects",
    "R19",
    "importing must define names only: register hooks and open files on first use, not at import",
)
def modules_with_import_effects() -> list[str]:
    return [
        f"{name}: {', '.join(events[:3])}"
        for name, events in sorted(import_effects().items())
        if name not in _IMPORT_EFFECTS_ALLOWED
    ]


# ================================================================ GT7 global mutable state

_MUTATORS = {
    "append", "add", "update", "clear", "pop", "popitem", "setdefault", "extend", "remove",
    "discard", "insert", "appendleft", "__setitem__", "increment",
}  # fmt: skip
_CONTAINERS = {
    "dict", "list", "set", "Counter", "defaultdict", "OrderedDict", "deque", "WeakKeyDictionary",
    "WeakValueDictionary", "WeakSet", "Lock", "RLock", "Event", "Semaphore", "ChainMap", "bytearray",
}  # fmt: skip
_SINGLETON_NAMES = {"_default", "_instance", "_singleton", "_registry", "_current", "_global"}


def _module_names(module: Module) -> dict[str, ast.expr | None]:
    names: dict[str, ast.expr | None] = {}
    for node in module.tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != "__all__":
                    names[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] = node.value
    return names


@measure("GT7.global_statements", "R18", "pass the state in, or hold it in a scoped object")
def global_statements() -> list[str]:
    return [
        f"{module.where(node)} global {', '.join(node.names)}"
        for module in modules().values()
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Global)
    ]


@measure(
    "GT7.class_singletons", "R18", "take the object as an argument; build the default at the edge"
)
def class_singletons() -> list[str]:
    found = []
    for module in modules().values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                for statement in node.body:
                    target = None
                    if isinstance(statement, ast.AnnAssign) and isinstance(
                        statement.target, ast.Name
                    ):
                        target = statement.target.id
                    elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                        only = statement.targets[0]
                        target = only.id if isinstance(only, ast.Name) else None
                    if target in _SINGLETON_NAMES:
                        found.append(f"{module.where(statement)} {node.name}.{target}")
    return found


@measure(
    "GT7.mutated_containers",
    "R18",
    "a registry needs unregister() and a context manager for tests; else pass it in",
)
def mutated_containers() -> list[str]:
    found = []
    for module in modules().values():
        names = _module_names(module)
        mutated: set[str] = set()
        for function in ast.walk(module.tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    if (
                        node.func.attr in _MUTATORS
                        and isinstance(owner, ast.Name)
                        and owner.id in names
                    ):
                        mutated.add(owner.id)
                elif isinstance(node, ast.Assign | ast.AugAssign | ast.Delete):
                    for target in (
                        node.targets if not isinstance(node, ast.AugAssign) else [node.target]
                    ):
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in names
                        ):
                            mutated.add(target.value.id)
        for name in sorted(mutated):
            value = names[name]
            literal = isinstance(
                value, ast.Dict | ast.List | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp
            )
            constructor = (
                isinstance(value, ast.Call)
                and ast.unparse(value.func).split(".")[-1] in _CONTAINERS
            )
            if literal or constructor:
                found.append(f"src/{module.rel}: {name} (mutated by functions)")
    return found


# ======================================================================== GT8 I/O in backends and roots

_ADAPTER_PACKAGES = {
    "persistence", "connectors", "mcp", "server", "cli", "ui",
    "toolkits", "automation", "observability", "blueprints", "testing", "_compat", "evals",
    "project_setup",
}  # fmt: skip
_CALLS = {
    "clock": re.compile(
        r"^(datetime\.(now|utcnow|today)|datetime\.datetime\.(now|utcnow|today)|"
        r"time\.(time|monotonic|perf_counter|time_ns|sleep|monotonic_ns)|asyncio\.sleep|"
        r"_time\.(time|monotonic|sleep))$"
    ),
    "ids": re.compile(
        r"^(uuid\.uuid[14]|uuid4|uuid1|secrets\.\w+|random\.\w+|new_id|os\.urandom)$"
    ),
    "http": re.compile(
        r"^(httpx\.\w+|requests\.\w+|urllib\.request\.\w+|urlopen|aiohttp\.\w+|http\.client\.\w+|"
        r"socket\.\w+|HTTPSConnection|HTTPConnection)$"
    ),
    "files": re.compile(
        r"^(open|os\.(replace|remove|unlink|makedirs|mkdir|listdir|rename|scandir|fsync|rmdir|walk)|"
        r"shutil\.\w+|tempfile\.\w+|io\.open|os\.path\.(exists|isfile|isdir|getsize))$"
    ),
    "env": re.compile(r"^(os\.getenv|os\.environ\.\w+|os\.putenv)$"),
}
_PATH_IO = {
    "read_text", "write_text", "read_bytes", "write_bytes", "mkdir", "unlink", "rmdir", "touch",
    "iterdir", "glob", "rglob", "rename", "open",
}  # fmt: skip


def _is_backend(module: Module) -> bool:
    """An `_internal` module the role table marks as a backend (the HTTP client and its fake)."""

    return module.package == "_internal" and any(
        o["role"] in {"backend", "backend-kit", "testing"} for o in matching_overrides(module)
    )


@functools.cache
def _io_calls() -> dict[str, list[str]]:
    exempt = set(role_table()["budget_exempt"])
    found: dict[str, list[str]] = collections.defaultdict(list)
    for module in modules().values():
        if module.package in _ADAPTER_PACKAGES or module.rel in exempt or _is_backend(module):
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                text = ast.unparse(node.func)
                for category, pattern in _CALLS.items():
                    if pattern.match(text):
                        found[category].append(f"{module.where(node)} {text}(...)")
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _PATH_IO
                    and not text.startswith(("os.", "shutil."))
                ):
                    found["files"].append(f"{module.where(node)} {text}(...)")
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                found["env"].append(f"{module.where(node)} os.environ[...]")
    return found


@measure(
    "GT8.clock_calls_domain",
    "R20",
    "take a Clock (awa.runtime.clock) as an argument; a test passes FakeClock",
)
def clock_calls() -> list[str]:
    return _io_calls()["clock"]


@measure(
    "GT8.id_calls_domain",
    "R20",
    "take an IdSource (awa.runtime.ids) as an argument; a test passes FixedIds",
)
def id_calls() -> list[str]:
    return _io_calls()["ids"]


@measure(
    "GT8.env_files_http_domain",
    "R17 R20",
    "read environment, files and the network in a backend or root and hand the result in",
)
def env_files_http() -> list[str]:
    calls = _io_calls()
    return [*calls["env"], *calls["files"], *calls["http"]]


# ============================================================== GT10 configuration channels


@measure(
    "GT10.awa_names_outside_settings",
    "R17",
    "add the setting to core/settings.py (Settings) instead of reading an AWA_* variable elsewhere",
)
def awa_names_outside_settings() -> list[str]:
    names: dict[str, set[str]] = collections.defaultdict(set)
    for module in modules().values():
        if module.rel in ("awa/core/settings.py", "awa/core/settings_registry.py"):
            continue  # Settings and the settings registry are where the names live
        for node in strings(module):
            if re.fullmatch(r"AWA_[A-Z0-9_]+", node.value) and node.value != "AWA_":
                names[node.value].add(module.rel)
    return [f"{name}: {', '.join(sorted(files))}" for name, files in sorted(names.items())]


_ENV_CALLS = {"os.environ.get", "os.getenv", "os.environ.setdefault", "os.environ.pop"}


@measure(
    "GT10.dynamic_env_reads",
    "R17",
    "read the environment in Settings, core/dotenv.Environment or a root; pass values in",
)
def dynamic_env_reads() -> list[str]:
    found = []
    for module in modules().values():
        for node in ast.walk(module.tree):
            dynamic_call = (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in _ENV_CALLS
                and not (node.args and isinstance(node.args[0], ast.Constant))
            )
            dynamic_item = (
                isinstance(node, ast.Subscript)
                and ast.unparse(node.value) == "os.environ"
                and not isinstance(node.slice, ast.Constant)
            )
            if dynamic_call or dynamic_item:
                found.append(f"{module.where(node)} {ast.unparse(node)[:60]}")
    return found

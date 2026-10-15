"""The settings registry is the one list: generated files match it, `Settings` and the code
read only names it has.

Before: `.env.example` and the configuration page listed only `Settings`; ten and more
variables were documented nowhere.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from awa.core.settings import Settings
from awa.core.settings_registry import REGISTRY, declared_env, is_denied, spec

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "awa"

pytestmark = pytest.mark.skipif(
    not (ROOT / "scripts" / "gen_settings.py").is_file(),
    reason="scripts/ is in the repository only",
)


def _generator() -> object:
    location = ROOT / "scripts" / "gen_settings.py"
    module_spec = importlib.util.spec_from_file_location("gen_settings", location)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["gen_settings"] = module
    module_spec.loader.exec_module(module)
    return module


def test_the_generated_files_match_the_registry_byte_for_byte() -> None:
    generator = _generator()
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == generator.env_example()  # type: ignore[attr-defined]
    reference = ROOT / "docs" / "reference" / "settings.md"
    assert reference.read_text(encoding="utf-8") == generator.reference()  # type: ignore[attr-defined]


def test_every_settings_field_has_a_spec() -> None:
    for name, field in Settings.model_fields.items():
        variable = field.alias or f"AWA_{name.upper()}"
        assert spec(variable) is not None, variable


def test_secrets_and_targets_are_marked() -> None:
    assert REGISTRY["ANTHROPIC_API_KEY"].is_secret
    assert REGISTRY["AWA_POSTGRES_DSN"].is_secret
    assert not REGISTRY["AWA_DEFAULT_MODEL"].is_secret
    assert REGISTRY["OPENAI_BASE_URL"].needs_confirmation
    assert spec("AZURE_OPENAI_DEPLOYMENT") is REGISTRY["AZURE_OPENAI_DEPLOYMENT_NAME"]
    assert all(is_denied(n) for n in ("PATH", "PYTHONPATH", "HTTPS_PROXY", "GIT_DIR"))
    assert not is_denied("OPENAI_API_KEY")


def test_declared_names_come_from_any_env_key(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text(
        "name: d\nagents: [{name: a}]\ntriggers:\n"
        "  - name: hook\n    webhook: {secret_env: HOOK_SECRET}\n"
        "  - name: mail\n    mailbox: {host: h, username_env: BOX_USER, password_env: BOX_PW}\n",
        encoding="utf-8",
    )
    found = declared_env(tmp_path)
    assert set(found) == {"HOOK_SECRET", "BOX_USER", "BOX_PW"}
    assert all(item.secret for item in found.values())


# The modules that read the process environment by design: the loader itself, `Settings`'
# sources, and process-level names that are no settings.
_ALLOWED_READS = {
    "awa/core/config.py": "the loader",
    "awa/core/dotenv.py": "Environment and the Windows account of icacls",
    "awa/cli/_doctor.py": "CONDA_PREFIX and SYSTEMROOT (the interpreter and icacls)",
    "awa/cli/_env.py": "TERM and MSYSTEM (whether a hidden prompt works)",
    "awa/cli/_serve.py": "hands the reload options to the reloaded process",
    "awa/server/_blueprint_app.py": "reads the reload options",
    "awa/observability/_trace_encode.py": "collects values to redact from traces",
    "awa/ui/_project.py": "the web app's own environment mapping",
    "awa/blueprints/_connect.py": "awa connect exports what it saved for this process",
    "awa/core/redaction.py": "a docstring example",
}


def _environment_reads(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        text = ast.unparse(node) if isinstance(node, ast.Call | ast.Subscript) else ""
        if (
            isinstance(node, ast.Call)
            and text.startswith(("os.environ.get(", "os.getenv("))
            or isinstance(node, ast.Subscript)
            and ast.unparse(node.value) == "os.environ"
        ):
            lines.append(node.lineno)
    return lines


@pytest.mark.slow
def test_no_module_reads_the_environment_around_the_loader() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC.parent).as_posix()
        if relative in _ALLOWED_READS or "__pycache__" in path.parts:
            continue
        for line in _environment_reads(ast.parse(path.read_text(encoding="utf-8"))):
            found.append(f"{relative}:{line}")
    assert not found, (
        "read variables through awa.core.config.env_value (the project .env counts too): "
        + ", ".join(found)
    )

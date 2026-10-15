"""The kernel imports no other awa module: it stays readable, testable and exportable on its own."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOWED_ROOTS = {
    "langchain",
    "langchain_core",
    "langgraph",
    "langsmith",
    "pydantic",
    "typing_extensions",
}


def _imports(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):  # lazy imports and TYPE_CHECKING blocks count
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield "." * node.level + (node.module or "")


def test_kernel_imports_no_awa_module() -> None:
    for path in (SRC / "awa" / "kernel").rglob("*.py"):
        for name in _imports(ast.parse(path.read_text("utf-8"))):
            root = name.lstrip(".").split(".")[0]
            assert (
                name.startswith(".") or root in ALLOWED_ROOTS or root in sys.stdlib_module_names
            ), (path, name)


def test_every_kernel_module_imports_without_awa(tmp_path: Path) -> None:
    shutil.copytree(SRC / "awa" / "kernel", tmp_path / "kernel_copy")
    code = (
        "import sys, importlib, pkgutil; sys.modules['awa'] = None; import kernel_copy; "
        "[importlib.import_module('kernel_copy.' + m.name) "
        "for m in pkgutil.iter_modules(kernel_copy.__path__)]"
    )
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True)

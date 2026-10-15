"""Every script of the learning path in `examples/` explains a missing model key in one line."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers.examples import EXAMPLES_DIR, LEARNING_PATH, load_example
from _helpers.polling import SUBPROCESS_TIMEOUT


@pytest.mark.parametrize("name", LEARNING_PATH)
def test_example_exits_with_one_line_message_without_credentials(name: str, tmp_path: Path) -> None:
    pytest.importorskip("langchain_anthropic")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWA_") and not key.endswith("_API_KEY")
    }
    env.update({"ANTHROPIC_API_KEY": "", "AWA_DEFAULT_MODEL": "anthropic:claude-sonnet-5-5"})
    completed = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / f"{name}.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip().splitlines() == [
        "Set ANTHROPIC_API_KEY to run this example with 'anthropic:claude-sonnet-5-5', or choose "
        "another model with AWA_DEFAULT_MODEL=provider:model."
    ]


def test_missing_model_setup_names_the_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langchain_openai")
    support = load_example("_support")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert support.missing_model_setup("openai:gpt-6.1-sol") is None
    monkeypatch.delenv("OPENAI_API_KEY")
    assert "OPENAI_API_KEY" in support.missing_model_setup("openai:gpt-6.1-sol")
    assert "pip install" in support.missing_model_setup("mistralai:mistral-large-latest")
    assert support.missing_model_setup("my-custom-profile") is None


def test_missing_model_setup_accepts_every_sign_in_the_library_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("langchain_openai")
    support = load_example("_support")
    for name in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)
    entra = {
        "AZURE_OPENAI_AUTH": "entra",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "chat",
        "AZURE_TENANT_ID": "t",
        "AZURE_CLIENT_ID": "c",
        "AZURE_CLIENT_SECRET": "s",
    }
    for name, value in entra.items():
        monkeypatch.setenv(name, value)
    assert support.missing_model_setup("azure_openai:chat") is None

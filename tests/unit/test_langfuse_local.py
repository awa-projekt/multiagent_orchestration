"""examples/observability/langfuse_local.py: Langfuse's compose file made safe for one computer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "examples" / "observability"))

import langfuse_local  # noqa: E402

COMPOSE = """services:
  langfuse-worker:
    ports:
      - 127.0.0.1:3030:3030
  langfuse-web:
    ports:
      - 3000:3000
  minio:
    ports:
      - 9090:9000
      - 127.0.0.1:9091:9001
"""


def test_every_published_port_is_bound_to_this_computer() -> None:
    changed = langfuse_local.local_ports(COMPOSE, 3310)

    assert "      - 127.0.0.1:3310:3000" in changed
    assert "      - 127.0.0.1:9090:9000" in changed
    assert "      - 127.0.0.1:3030:3030" in changed
    ports = [line.strip() for line in changed.splitlines() if line.strip().startswith("- ")]
    assert ports and all(line.startswith("- 127.0.0.1:") for line in ports)


def test_secrets_are_random_and_none_is_a_placeholder() -> None:
    first, second = langfuse_local.settings(3000), langfuse_local.settings(3000)

    for name in ("NEXTAUTH_SECRET", "SALT", "ENCRYPTION_KEY", "POSTGRES_PASSWORD", "REDIS_AUTH",
                 "MINIO_ROOT_PASSWORD", "LANGFUSE_INIT_PROJECT_SECRET_KEY",
                 "LANGFUSE_INIT_USER_PASSWORD"):  # fmt: skip
        assert first[name] != second[name], name
        assert first[name] not in ("mysecret", "mysalt", "postgres", "miniosecret", "clickhouse")
    assert len(first["ENCRYPTION_KEY"]) == 64  # 256 bits as hex, as Langfuse requires
    assert first["DATABASE_URL"].endswith(f":{first['POSTGRES_PASSWORD']}@postgres:5432/postgres")
    assert first["LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY"] == first["MINIO_ROOT_PASSWORD"]
    assert first["TELEMETRY_ENABLED"] == "false"


def test_main_writes_the_env_files_once_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")

    assert langfuse_local.main(["--folder", str(tmp_path), "--port", "3310"]) == 0
    values = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    )
    awa = (tmp_path / "awa-langfuse.env").read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert f"LANGFUSE_SECRET_KEY={values['LANGFUSE_INIT_PROJECT_SECRET_KEY']}" in awa
    assert "AWA_LANGFUSE_TRACING=true" in awa and "LANGFUSE_BASE_URL=http://localhost:3310" in awa
    secret_names = ("SECRET", "PASSWORD", "SALT", "KEY", "REDIS_AUTH", "DATABASE_URL")
    shown = [k for k, v in values.items() if any(s in k for s in secret_names) and v in printed]
    assert shown == []
    assert b"\r\n" not in (tmp_path / "docker-compose.yml").read_bytes()
    assert langfuse_local.main(["--folder", str(tmp_path)]) == 1  # never overwrites the .env


def test_main_without_a_compose_file_says_what_to_do(tmp_path: Path) -> None:
    assert langfuse_local.main(["--folder", str(tmp_path)]) == 2


def test_quoted_and_commented_ports_are_bound_too() -> None:
    compose = (
        "services:\n  web:\n    ports:\n      - \"3000:3000\"  # web UI\n      - '9090:9000'\n"
    )

    changed = langfuse_local.local_ports(compose, 3310)

    assert '      - "127.0.0.1:3310:3000"  # web UI' in changed
    assert "      - '127.0.0.1:9090:9000'" in changed
    assert langfuse_local.unbound_ports(changed) == []


def test_main_refuses_a_compose_file_with_ports_it_cannot_bind(tmp_path: Path) -> None:
    compose = (
        "services:\n  web:\n    ports: ['3000:3000']\n  db:\n    ports:\n      - target: 5432\n"
    )
    (tmp_path / "docker-compose.yml").write_text(compose, encoding="utf-8")

    assert langfuse_local.main(["--folder", str(tmp_path)]) == 2
    assert not (tmp_path / ".env").exists()

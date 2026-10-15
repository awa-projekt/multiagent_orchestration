"""Prepare Langfuse's Docker Compose file for this computer: secrets of your own, ports on localhost.

Langfuse's `docker-compose.yml` ships placeholder passwords (marked `# CHANGEME`) and publishes the
web UI and the object store on every network interface. This script, run in the folder of that
file, writes a `.env` next to it with random secrets, a first user and a project with API keys,
binds every published port to 127.0.0.1, and writes `awa-langfuse.env` with the three lines your
awa project needs. It uses only the standard library and prints no secret.

    python langfuse_local.py                 # in the folder with docker-compose.yml
    python langfuse_local.py --port 3000     # the port of the web UI on this computer
    docker compose up -d

Run it once: an existing `.env` is never overwritten (the database was made with its passwords).
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

_PORT = re.compile(r"""^(\s*-\s*)(["']?)(?:127\.0\.0\.1:)?(\d+):(\d+)\2(\s*#.*)?$""")
_PORTS_KEY = re.compile(r"^(\s*)ports:(.*)$")


def local_ports(compose: str, web_port: int) -> str:
    """`compose` with every published port bound to 127.0.0.1 and the web UI on `web_port`."""

    lines = []
    for line in compose.splitlines():
        found = _PORT.match(line)
        if found:
            prefix, quote, host, inside, comment = found.groups()
            if inside == "3000":
                host = str(web_port)
            line = f"{prefix}{quote}127.0.0.1:{host}:{inside}{quote}{comment or ''}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def unbound_ports(compose: str) -> list[str]:
    """The lines publishing a port that are not in the form `- 127.0.0.1:HOST:CONTAINER`."""

    unbound: list[str] = []
    indent: int | None = None
    for line in compose.splitlines():
        stripped = line.split("#", 1)[0].strip()
        key = _PORTS_KEY.match(line)
        if key:
            indent = len(key.group(1))
            if stripped != "ports:":
                unbound.append(stripped)
        elif indent is not None and stripped:
            if len(line) - len(line.lstrip()) <= indent and not stripped.startswith("-"):
                indent = None
            elif not (_PORT.match(line) and "127.0.0.1:" in stripped):
                unbound.append(stripped)
    return unbound


def settings(web_port: int) -> dict[str, str]:
    """The values of the `.env` of the compose folder: random secrets, a project and its keys."""

    def secret(n: int = 32) -> str:
        return secrets.token_hex(n)

    database, clickhouse, redis, minio = secret(16), secret(16), secret(16), secret(16)
    return {
        "NEXTAUTH_URL": f"http://localhost:{web_port}",
        "NEXTAUTH_SECRET": secret(),
        "SALT": secret(),
        "ENCRYPTION_KEY": secret(),
        "TELEMETRY_ENABLED": "false",
        "POSTGRES_PASSWORD": database,
        "DATABASE_URL": f"postgresql://postgres:{database}@postgres:5432/postgres",
        "CLICKHOUSE_PASSWORD": clickhouse,
        "REDIS_AUTH": redis,
        "MINIO_ROOT_PASSWORD": minio,
        "LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY": minio,
        "LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY": minio,
        "LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY": minio,
        "LANGFUSE_INIT_ORG_ID": "local-org",
        "LANGFUSE_INIT_ORG_NAME": "local",
        "LANGFUSE_INIT_PROJECT_ID": "awa-local",
        "LANGFUSE_INIT_PROJECT_NAME": "awa local",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + secret(12),
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + secret(16),
        "LANGFUSE_INIT_USER_EMAIL": "admin@example.com",
        "LANGFUSE_INIT_USER_NAME": "admin",
        "LANGFUSE_INIT_USER_PASSWORD": secret(12),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", default=".", help="the folder with docker-compose.yml")
    parser.add_argument("--port", type=int, default=3000, help="port of the web UI (default 3000)")
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    compose = folder / "docker-compose.yml"
    env = folder / ".env"
    if not compose.is_file():
        print(f"No docker-compose.yml in {folder.resolve()}: download it first.", file=sys.stderr)
        return 2
    if env.exists():
        print(f"{env} exists already; it is kept (delete it and the volumes to start over).")
        return 1
    text = local_ports(compose.read_text(encoding="utf-8"), args.port)
    left = unbound_ports(text)
    if left:
        print(
            f"Bind these ports by hand as - 127.0.0.1:HOST:CONTAINER first: {', '.join(left)}",
            file=sys.stderr,
        )
        return 2
    compose.write_text(text, encoding="utf-8", newline="\n")
    values = settings(args.port)
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    env.write_text(lines, encoding="utf-8", newline="\n")
    (folder / "awa-langfuse.env").write_text(
        "AWA_LANGFUSE_TRACING=true\n"
        f"LANGFUSE_PUBLIC_KEY={values['LANGFUSE_INIT_PROJECT_PUBLIC_KEY']}\n"
        f"LANGFUSE_SECRET_KEY={values['LANGFUSE_INIT_PROJECT_SECRET_KEY']}\n"
        f"LANGFUSE_BASE_URL=http://localhost:{args.port}\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Wrote .env (secrets, first user, project keys) and awa-langfuse.env; ports bound to "
        f"127.0.0.1. Next: docker compose up -d, then open http://localhost:{args.port} and sign "
        "in with LANGFUSE_INIT_USER_EMAIL and LANGFUSE_INIT_USER_PASSWORD from .env."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""What the installers trust: locked dependencies by hash, a pinned and checkable uv, a home of their own.

The POSIX installer runs against a fake `uv` (and `curl`) that only log their arguments, so nothing
is downloaded; the shell is the real one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_installers import BASH, MARKER, POSIX, SCRIPTS

REPO = SCRIPTS.parent
GIT_USR_BIN = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"), "Git", "usr", "bin")
#: Git for Windows' `bin/bash` puts its own `curl` first on PATH; its plain `usr/bin/bash` keeps the fakes first.
SHELL = shutil.which("bash", path=str(GIT_USR_BIN)) or BASH
FAKE_UV = r"""#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  venv) mkdir -p "$2/bin"
        printf '#!/bin/sh\nexit 0\n' > "$2/bin/python"
        printf '#!/bin/sh\necho awa 1.0.0\n' > "$2/bin/awa"
        chmod +x "$2/bin/python" "$2/bin/awa" ;;
  export) while [ $# -gt 0 ]; do [ "$1" = "-o" ] && echo "pkg==1" > "$2"; shift; done ;;
esac
exit 0
"""
FAKE_CURL = r"""#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -*) ;; *) echo "$1" > "$FAKE_URL" ;; esac
  shift
done
printf 'mkdir -p "$UV_INSTALL_DIR"\ncp "%s" "$UV_INSTALL_DIR/uv"\n' "$FAKE_UV" > "$out"
"""

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def program(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def without_uv() -> str:
    """The PATH of this process without any folder that holds an `uv`."""

    folders = os.environ["PATH"].split(os.pathsep)
    return os.pathsep.join(p for p in folders if not shutil.which("uv", path=p))


def install(tmp_path: Path, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the POSIX installer; a fake `uv` is first on PATH unless `PATH` is given."""

    assert SHELL is not None
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    if "PATH" not in env:
        program(fake / "uv", FAKE_UV)
        env["PATH"] = f"{fake.as_posix()}{os.pathsep}{os.environ['PATH']}"
    env["PATH"] += f"{os.pathsep}{Path(SHELL).parent}"
    options = ["--yes", "--no-shortcuts", "--no-launch", "--home", (tmp_path / "awa").as_posix()]
    options += ["--project", (tmp_path / "proj").as_posix(), *extra]
    return subprocess.run(
        [SHELL, str(POSIX[0]), *options],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "HOME": (tmp_path / "home").as_posix(),
            "FAKE_LOG": (tmp_path / "uv.log").as_posix(),
            **env,
        },
    )


def uv_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "uv.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_locked_dependencies_are_installed_by_hash_and_awa_without_them(tmp_path: Path) -> None:
    run = install(tmp_path, "--source", REPO.as_posix())

    assert run.returncode == 0, run.stderr
    calls = uv_calls(tmp_path)
    export = next(call for call in calls if call.startswith("export "))
    assert "--frozen" in export and "--no-dev" in export and "--extra all" in export
    assert any("pip install" in c and "--require-hashes -r" in c for c in calls)
    assert any("pip install" in c and "--no-deps" in c for c in calls)
    assert not any("--upgrade" in call for call in calls)


def test_dependencies_resolve_freely_only_when_asked_or_without_a_lock(tmp_path: Path) -> None:
    asked = install(tmp_path, "--source", REPO.as_posix(), "--latest")
    calls = uv_calls(tmp_path)

    assert asked.returncode == 0, asked.stderr
    assert not any(call.startswith("export ") or "--require-hashes" in call for call in calls)
    assert any("--upgrade" in call and call.endswith("[all]") for call in calls)

    other = tmp_path / "other"
    other.mkdir()
    wheel = other / "awa_agents-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"x")
    unlocked = install(other, "--source", wheel.as_posix())
    assert unlocked.returncode == 0, unlocked.stderr
    assert "not checked by hash" in unlocked.stdout


def test_a_home_the_installer_did_not_make_is_left_alone(tmp_path: Path) -> None:
    home = tmp_path / "awa"
    (home / "env").mkdir(parents=True)
    (home / "env" / "precious.txt").write_text("mine", encoding="utf-8")

    run = install(tmp_path, "--source", REPO.as_posix())

    assert run.returncode == 1 and "did not make" in run.stderr
    assert (home / "env" / "precious.txt").read_text(encoding="utf-8") == "mine"
    assert uv_calls(tmp_path) == [] and not (home / MARKER).exists()


def download_environment(tmp_path: Path) -> tuple[dict[str, str], str]:
    """A PATH without uv whose `curl` fetches an install script (returned) that installs a fake uv."""

    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    program(fake / "curl", FAKE_CURL)
    uv = program(fake / "uvprogram", FAKE_UV)
    env = {
        "PATH": f"{fake.as_posix()}{os.pathsep}{without_uv()}",
        "FAKE_UV": uv.as_posix(),
        "FAKE_URL": (tmp_path / "url.txt").as_posix(),
    }
    return env, f'mkdir -p "$UV_INSTALL_DIR"\ncp "{uv.as_posix()}" "$UV_INSTALL_DIR/uv"\n'


def test_a_downloaded_uv_is_pinned_and_runs_only_when_its_hash_matches(tmp_path: Path) -> None:
    env, script = download_environment(tmp_path)
    source = ("--source", REPO.as_posix())

    wrong = install(tmp_path, *source, "--uv-sha256", "0" * 64, **env)
    assert wrong.returncode == 1 and "Nothing was run" in wrong.stderr
    assert uv_calls(tmp_path) == []

    digest = hashlib.sha256(script.encode()).hexdigest().upper()  # upper case is accepted
    ok = install(tmp_path, *source, "--uv-version", "9.9.9", "--uv-sha256", digest, **env)
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "url.txt").read_text(encoding="utf-8").strip() == (
        "https://astral.sh/uv/9.9.9/install.sh"
    )
    assert "matches --uv-sha256" in ok.stdout and uv_calls(tmp_path)


def test_the_default_uv_download_names_its_version(tmp_path: Path) -> None:
    env, _ = download_environment(tmp_path)

    run = install(tmp_path, "--source", REPO.as_posix(), **env)

    assert run.returncode == 0, run.stderr
    shown = (tmp_path / "url.txt").read_text(encoding="utf-8").strip()
    assert shown.startswith("https://astral.sh/uv/0.") and shown.endswith("/install.sh")
    assert "is not verified" in run.stdout  # said, not hidden


def test_the_uv_it_is_given_is_used_and_nothing_is_downloaded(tmp_path: Path) -> None:
    env, _ = download_environment(tmp_path)

    run = install(tmp_path, "--source", REPO.as_posix(), "--uv", env["FAKE_UV"], **env)

    assert run.returncode == 0, run.stderr
    assert not (tmp_path / "url.txt").exists() and uv_calls(tmp_path)
    missing = install(tmp_path, "--source", REPO.as_posix(), "--uv", (tmp_path / "none").as_posix())
    assert missing.returncode == 1 and "is not an executable file" in missing.stderr

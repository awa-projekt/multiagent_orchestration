"""The double-click installers: syntax, shared POSIX script, and (opt-in) a real Windows install."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WINDOWS = SCRIPTS / "install-windows.cmd"
POSIX = (SCRIPTS / "install-linux.sh", SCRIPTS / "install-macos.command")


def embedded_powershell() -> str:
    text = WINDOWS.read_text(encoding="utf-8")
    return text[text.index("#POWERSHELL") :]


def working_bash() -> str | None:
    """A bash that runs (on Windows, `bash` may be a WSL launcher without a distribution)."""

    git_bash = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"), "Git", "bin")
    candidates = [shutil.which("bash"), shutil.which("bash", path=str(git_bash))]
    for candidate in filter(None, candidates):
        probe = subprocess.run([candidate, "-c", "exit 0"], capture_output=True, check=False)
        if probe.returncode == 0:
            return candidate
    return None


BASH = working_bash()


def test_macos_and_linux_run_the_same_script() -> None:
    linux, macos = (path.read_bytes() for path in POSIX)

    assert linux == macos
    assert linux.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r\n" not in linux


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize("script", POSIX, ids=lambda path: path.name)
def test_posix_installer_parses_and_explains_itself(script: Path) -> None:
    assert BASH is not None
    assert subprocess.run([BASH, "-n", str(script)], check=False).returncode == 0
    shown = subprocess.run(
        [BASH, str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert shown.returncode == 0
    assert "--source PATH" in shown.stdout
    assert "never downloaded" in shown.stdout


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_installer_prints_the_path_line_for_a_terminal() -> None:
    """The installer never changes PATH; it prints the line that puts `awa` on it, for its own
    install folder (also with `--home`)."""

    assert BASH is not None
    script = POSIX[0].read_text(encoding="utf-8")
    line = next(line for line in script.splitlines() if "export PATH=" in line)
    shown = subprocess.run(
        [BASH, "-c", f"ENV_DIR='/home/a b/awa/env'\n{line}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert shown.stdout == '    export PATH="/home/a b/awa/env/bin:$PATH"\n'


def test_the_windows_installer_prints_the_path_line_for_powershell() -> None:
    printed = next(line for line in embedded_powershell().splitlines() if "$env:PATH = " in line)
    script = f'$EnvDir = "C:\\work\\a b\\awa\\env"\n{printed}\n'
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("needs PowerShell")
    shown = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )

    assert '    $env:PATH = "C:\\work\\a b\\awa\\env\\Scripts;$env:PATH"' in shown.stdout


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize("folder", ["/usr", "/usr/bin"])
def test_posix_installer_prints_an_absolute_path_without_a_doubled_slash(folder: str) -> None:
    assert BASH is not None
    script = POSIX[0].read_text(encoding="utf-8")
    function = next(line for line in script.splitlines() if line.startswith("absolute()"))
    shown = subprocess.run(
        [BASH, "-c", function + '\nabsolute "$1"', "bash", folder],
        capture_output=True,
        text=True,
        check=False,
    )

    assert shown.stdout == folder


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_installer_stops_before_any_download_without_a_local_awa(tmp_path: Path) -> None:
    assert BASH is not None
    lonely = tmp_path / "scripts" / "install-linux.sh"
    lonely.parent.mkdir()
    shutil.copyfile(POSIX[0], lonely)
    run = subprocess.run(
        [BASH, str(lonely), "--yes", "--home", str(tmp_path / "home")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 1
    assert "no local awa found" in run.stderr
    assert not (tmp_path / "home").exists()


MARKER = ".awa-installer"


def posix_uninstall(
    home: Path, *extra: str, yes: bool = True, **env: str
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    options = ["--uninstall", "--home", home.as_posix()]
    options += ["--project", (home.parent / "proj").as_posix()]
    options += ["--shortcut-dir", (home.parent / "links").as_posix(), *extra]
    return subprocess.run(
        [BASH, str(POSIX[0]), *(["--yes"] if yes else []), *options],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **env},
    )


def installed_home(base: Path) -> Path:
    """A home folder as the installer leaves it, with a start shortcut next to it."""

    home = base / "home"
    for part in ("env", "python", "uv"):
        (home / part).mkdir(parents=True)
        (home / part / "file.txt").write_text("x", encoding="utf-8")
    (home / MARKER).write_text("made by the awa installer", encoding="utf-8")
    (base / "proj").mkdir()
    (base / "links").mkdir()
    shortcut = "awa.command" if sys.platform == "darwin" else "awa.desktop"
    (base / "links" / shortcut).write_text("x", encoding="utf-8")
    return home


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_uninstall_removes_only_what_the_installer_made(tmp_path: Path) -> None:
    home = installed_home(tmp_path)
    (home / "notes.txt").write_text("mine", encoding="utf-8")

    run = posix_uninstall(home)

    assert run.returncode == 0, run.stderr
    assert sorted(path.name for path in home.iterdir()) == ["notes.txt"]  # the rest of it stays
    assert not list((tmp_path / "links").iterdir()) and (tmp_path / "proj").is_dir()
    (tmp_path / "clean").mkdir()
    only_ours = installed_home(tmp_path / "clean")
    assert posix_uninstall(only_ours).returncode == 0 and not only_ours.exists()
    assert "nothing to remove" in posix_uninstall(only_ours).stdout


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_uninstall_leaves_a_folder_alone_that_the_installer_did_not_make(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "env").mkdir(parents=True)
    (home / "documents.txt").write_text("precious", encoding="utf-8")

    run = posix_uninstall(home)  # --yes does not turn a stranger's folder into awa's

    assert run.returncode == 1 and "was not made by this installer" in run.stderr
    assert (home / "env").is_dir() and (home / "documents.txt").is_file()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_uninstall_does_not_assume_yes_when_nobody_can_answer(tmp_path: Path) -> None:
    home = installed_home(tmp_path)

    run = posix_uninstall(home, yes=False)

    assert run.returncode == 1 and "pass --yes" in run.stderr
    assert (home / "env").is_dir()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_installer_refuses_a_home_that_is_not_a_folder_of_its_own(tmp_path: Path) -> None:
    inside = tmp_path / "home"
    elsewhere = tmp_path.parent / "elsewhere"
    cases = (
        (Path("/"), elsewhere, "root folder"),
        (tmp_path, elsewhere, "not your home folder"),
        (inside, inside / "proj", "must not contain your project"),
    )
    for home, project, message in cases:
        run = posix_uninstall(home, "--project", project.as_posix(), HOME=tmp_path.as_posix())
        assert run.returncode == 1 and message in run.stderr, (home, run.stderr)


def written_shortcut(system: str, awa: str, project: str, target: Path) -> list[str]:
    """The lines the POSIX installer writes as the start shortcut of `system`."""

    assert BASH is not None
    script = POSIX[0].read_text(encoding="utf-8")
    start = script.index("desktop_quote() {")
    functions = script[start : script.index("\nwhile [ $# -gt 0 ]", start)]
    snippet = target.with_suffix(".sh")  # a file: bash.exe on Windows mangles quotes in -c text
    snippet.write_text(functions + '\nwrite_shortcut "$1"\n', encoding="utf-8", newline="\n")
    shown = subprocess.run(
        [BASH, str(snippet), str(target)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SYSTEM": system, "AWA": awa, "PROJECT": project},
    )
    assert shown.returncode == 0, shown.stderr
    return target.read_text(encoding="utf-8").splitlines()


def desktop_exec_words(value: str) -> list[str]:
    """Split an `Exec=` value as the Desktop Entry rules do: file escapes, then quoted words."""

    value = value.replace("\\\\", "\\")
    words: list[str] = []
    word, quoted, index = "", False, 0
    while index < len(value):
        char = value[index]
        if quoted and char == "\\":
            index += 1
            word += value[index]
        elif char == '"':
            quoted = not quoted
        elif char == " " and not quoted:
            words.append(word)
            word = ""
        elif char == "%" and value[index + 1 : index + 2] == "%":
            word += "%"
            index += 1
        else:
            word += char
        index += 1
    return [*words, word]


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_shortcuts_survive_paths_with_quotes_dollars_backticks_and_percent(
    tmp_path: Path,
) -> None:
    awa = "/opt/my awa/$HOME/`id`/50%/it's/awa"
    project = '/srv/me/My "Docs" & $(rm -rf x)\\folder/awa'

    command = written_shortcut("macos", awa, project, tmp_path / "start.command")
    assert shlex.split(command[1]) == ["exec", awa, "ui", "--project", project]

    entry = written_shortcut("linux", awa, project, tmp_path / "start.desktop")
    [line] = [line for line in entry if line.startswith("Exec=")]
    assert desktop_exec_words(line.removeprefix("Exec=")) == [awa, "ui", "--project", project]


def test_the_installers_install_awa_only_from_the_local_copy() -> None:
    for text in (embedded_powershell(), POSIX[0].read_text(encoding="utf-8")):
        assert '"awa-agents[all]"' not in text  # no index download of awa itself
        assert "pyproject.toml" in text
        assert "awa_agents-*.whl" in text
        assert "--reinstall-package" in text


def test_the_windows_installer_is_one_crlf_file_that_runs_its_own_powershell() -> None:
    raw = WINDOWS.read_bytes()

    assert raw.count(b"\n") == raw.count(b"\r\n")  # cmd.exe needs CRLF line endings
    head = raw[: raw.index(b"#POWERSHELL")].decode()
    assert "-ExecutionPolicy Bypass" in head  # the user never changes a policy
    assert "exit /b" in head
    assert "https://astral.sh/uv/$UvVersion/install.ps1" in embedded_powershell()  # pinned
    assert "UV_NO_MODIFY_PATH" in embedded_powershell()


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_the_windows_installer_stops_before_any_download_without_a_local_awa(
    tmp_path: Path,
) -> None:
    lonely = tmp_path / "scripts" / "install-windows.cmd"
    lonely.parent.mkdir()
    shutil.copyfile(WINDOWS, lonely)
    home = tmp_path / "home"
    run = subprocess.run(
        f'cmd /c ""{lonely}" --yes --home "{home}""',
        env={**os.environ, "AWA_NO_PAUSE": "1"},
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert run.returncode == 1
    assert "no local awa found" in run.stdout
    assert not home.exists()


def windows_uninstall(home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    options = f'--uninstall --yes --home "{home}" --project "{home.parent / "proj"}"'
    options += f' --shortcut-dir "{home.parent / "links"}" {" ".join(extra)}'
    return subprocess.run(
        f'cmd /c ""{WINDOWS}" {options}"',
        env={**os.environ, "AWA_NO_PAUSE": "1"},
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_the_windows_uninstall_removes_only_what_the_installer_made(tmp_path: Path) -> None:
    home = installed_home(tmp_path)
    (tmp_path / "links" / "awa.lnk").write_text("x", encoding="utf-8")
    (home / "notes [1].txt").write_text("mine", encoding="utf-8")

    run = windows_uninstall(home)

    assert run.returncode == 0, run.stdout
    assert sorted(path.name for path in home.iterdir()) == ["notes [1].txt"]
    assert not (tmp_path / "links" / "awa.lnk").exists() and (tmp_path / "proj").is_dir()
    (tmp_path / "clean").mkdir()
    only_ours = installed_home(tmp_path / "clean")
    assert windows_uninstall(only_ours).returncode == 0 and not only_ours.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_the_windows_uninstall_leaves_a_folder_alone_that_the_installer_did_not_make(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "env").mkdir(parents=True)
    (home / "documents.txt").write_text("precious", encoding="utf-8")

    run = windows_uninstall(home)

    assert run.returncode == 1 and "was not made by this installer" in run.stdout
    assert (home / "env").is_dir() and (home / "documents.txt").is_file()
    for refused in (Path(os.environ["USERPROFILE"]), Path(tmp_path.anchor), home / "inner"):
        extra = ("--project", str(home / "inner" / "p")) if refused == home / "inner" else ()
        run = windows_uninstall(refused, *extra)
        assert run.returncode == 1 and "--home must" in run.stdout, (refused, run.stdout)


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_the_windows_installer_does_not_touch_a_home_it_did_not_make(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "env").mkdir(parents=True)
    (home / "env" / "precious.txt").write_text("mine", encoding="utf-8")
    source = Path(__file__).resolve().parents[2]

    run = subprocess.run(
        f'cmd /c ""{WINDOWS}" --yes --source "{source}" --home "{home}" --project "{tmp_path / "p"}""',
        env={**os.environ, "AWA_NO_PAUSE": "1"},
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert run.returncode == 1 and "did not make" in run.stdout
    assert (home / "env" / "precious.txt").read_text(encoding="utf-8") == "mine"
    assert not (home / MARKER).exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
def test_the_embedded_powershell_parses() -> None:
    check = (
        "$errors = $null; [Management.Automation.Language.Parser]::ParseInput("
        "[Console]::In.ReadToEnd(), [ref]$null, [ref]$errors) | Out-Null; "
        "$errors | ForEach-Object { $_.Message }; exit $errors.Count"
    )
    parsed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", check],
        input=embedded_powershell(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert parsed.returncode == 0, parsed.stdout


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("AWA_TEST_INSTALLER"),
    reason="installs awa from this folder and a local wheel; set AWA_TEST_INSTALLER=1 (Windows)",
)
def test_the_windows_installer_installs_updates_and_uninstalls(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run([uv, "build", "--wheel", "-o", str(tmp_path / "dist")], check=True)
    wheel = next((tmp_path / "dist").glob("*.whl"))
    home, project, links = tmp_path / "home dir", tmp_path / "my project", tmp_path / "links"
    options = f'--yes --home "{home}" --project "{project}" --shortcut-dir "{links}"'
    env = {**os.environ, "AWA_NO_PAUSE": "1"}

    def installer(arguments: str) -> None:
        command = f'"{WINDOWS}" {arguments}'
        subprocess.run(f'cmd /c "{command}"', env=env, check=True, stdin=subprocess.DEVNULL)

    # The default installs from the awa folder around scripts\; the second run updates
    # from a locally built wheel.
    for source in ("", f'--source "{wheel}"'):
        installer(f"{options} {source} --no-launch")
        awa = home / "env" / "Scripts" / "awa.exe"
        version = subprocess.run([awa, "--version"], capture_output=True, text=True, check=True)
        assert version.stdout.startswith("awa ")
        assert (links / "awa.lnk").is_file()
        assert (home / "python").is_dir()  # a private Python, not the system's
        assert project.is_dir()

    installer(f"{options} --uninstall")
    assert not home.exists()
    assert not (links / "awa.lnk").exists()
    assert project.is_dir()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_posix_installer_makes_a_relative_folder_absolute_before_it_exists(tmp_path: Path) -> None:
    assert BASH is not None
    script = POSIX[0].read_text(encoding="utf-8")
    function = next(line for line in script.splitlines() if line.startswith("from_here()"))
    shown = subprocess.run(
        [BASH, "-c", function + '\nfrom_here "$1"', "bash", "new/home"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )

    assert shown.stdout.startswith("/") and shown.stdout.endswith("/new/home")

"""`.env` files: the one parser, atomic validated writing, and `Environment`."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import awa._internal.filelock as filelock_module
from awa.core._envparse import parse_env_file
from awa.core.dotenv import (
    MAX_VALUE_CHARS,
    Environment,
    read_env_file,
    remove_env_names,
    write_env_file,
)
from awa.core.settings_registry import declared_env
from awa.errors import ConfigurationError


def test_read_env_file_returns_every_assignment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\nA=1\nexport B='two words'\nEMPTY=\nBARE\n", encoding="utf-8")

    assert read_env_file(path) == {"A": "1", "B": "two words", "EMPTY": "", "BARE": ""}
    assert read_env_file(tmp_path / "missing") == {}
    assert read_env_file(tmp_path) == {}  # a directory is no file


def test_write_env_file_replaces_in_place_and_appends(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep me\nexport A=old\nB=keep\n", encoding="utf-8")

    written = write_env_file(path, {"C": "new", "A": " fresh ", "D": "  "})

    assert written == ["A", "C"]
    assert path.read_text(encoding="utf-8") == "# keep me\nA=fresh\nB=keep\nC=new\n"
    assert not (tmp_path / "..env.tmp").exists()


def test_write_env_file_updates_every_assignment_of_a_name(tmp_path: Path) -> None:
    # A name that appears twice reads as its LAST value: updating only the first would leave the
    # old value in force and the new one silently ignored.
    path = tmp_path / ".env"
    path.write_text("A=old\nB=keep\nexport A=older\n", encoding="utf-8")

    write_env_file(path, {"A": "new"})

    assert path.read_text(encoding="utf-8") == "A=new\nB=keep\nA=new\n"
    assert read_env_file(path) == {"A": "new", "B": "keep"}


def test_write_env_file_quotes_values_that_need_it(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    values = {"PLAIN": "https://x.example/a/b", "SPACE": "two words", "BOTH": 'it\'s "x" \\'}

    write_env_file(path, values)

    assert read_env_file(path) == values
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["PLAIN=https://x.example/a/b", "SPACE='two words'"]


@pytest.mark.parametrize(
    "value", ["plain", "with space #hash", 'it\'s "quoted"', "back\\slash $HOME", "a'b\\\"c"]
)
def test_written_values_read_back_exactly(tmp_path: Path, value: str) -> None:
    path = tmp_path / ".env"

    write_env_file(path, {"KEY": value})

    assert read_env_file(path)["KEY"] == value


def test_write_env_file_creates_the_file_and_skips_blank_updates(tmp_path: Path) -> None:
    path = tmp_path / ".env"

    assert write_env_file(path, {"A": ""}) == []
    assert not path.exists()
    assert write_env_file(str(path), {"A": "1"}) == ["A"]
    assert path.read_text(encoding="utf-8") == "A=1\n"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_write_env_file_is_readable_by_the_owner_only(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env_file(path, {"A": "1"})

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("1BAD", "x"),
        ("BAD-NAME", "x"),
        ("A" * 129, "x"),
        ("OK", "line\nbreak"),
        ("OK", "nul\0"),
        ("OK", "x" * (MAX_VALUE_CHARS + 1)),
        ("OK", 5),
    ],
)
def test_write_env_file_rejects_invalid_names_and_values(
    tmp_path: Path, name: str, value: object
) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        write_env_file(path, {name: value})  # type: ignore[dict-item]
    assert path.read_text(encoding="utf-8") == "A=1\n"


def test_environment_prefers_the_mapping_then_the_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=file\nB=file\nC=\n", encoding="utf-8")
    env = Environment({"A": "mapping", "B": "  "}, dotenv=path)

    assert env.get("A") == "mapping"
    assert env.get("B") == "file"
    assert env.get("C") is None and not env.is_set("C")
    assert env.get("MISSING") is None


def test_environment_reads_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWA_CORE_DOTENV_TEST", "process")

    assert Environment(dotenv=None).get("AWA_CORE_DOTENV_TEST") == "process"
    assert Environment(dotenv=tmp_path / "missing").get("NOT_SET_ANYWHERE_X") is None


def test_environment_renames_and_rereads_a_changed_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("JIRA2_URL=one\n", encoding="utf-8")
    env = Environment({}, dotenv=path, renames={"JIRA_URL": "JIRA2_URL"})

    assert env.name("JIRA_URL") == "JIRA2_URL"
    assert env.get("JIRA_URL") == "one"
    path.write_text("JIRA2_URL=two\n", encoding="utf-8")
    stamp = path.stat().st_mtime + 5
    os.utime(path, (stamp, stamp))
    assert env.get("JIRA_URL") == "two"
    assert "dotenv=" in repr(env)


@pytest.mark.skipif(os.name != "nt", reason="ACLs are Windows")
def test_write_env_file_is_readable_by_the_current_user_only_on_windows(tmp_path: Path) -> None:
    import subprocess

    path = tmp_path / ".env"
    write_env_file(path, {"A": "1"})
    write_env_file(path, {"B": "2"})  # replaced in place: the restriction survives

    listing = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=True
    ).stdout
    entries = [line.split(":")[-2].strip() for line in listing.splitlines() if "(" in line]
    assert [entry.split("\\")[-1].lower() for entry in entries] == [os.environ["USERNAME"].lower()]


def test_read_env_file_keeps_dollar_braces_literal(tmp_path: Path) -> None:
    # A password or token may contain `${...}`; it must reach the connector unchanged.
    path = tmp_path / ".env"
    path.write_text('DB_PASS=ab${HOME}cd\nTOKEN="x${NOPE}y"\nPLAIN=$HOME\n', encoding="utf-8")

    assert read_env_file(path) == {"DB_PASS": "ab${HOME}cd", "TOKEN": "x${NOPE}y", "PLAIN": "$HOME"}
    assert Environment(env={}, dotenv=path).get("DB_PASS") == "ab${HOME}cd"


@pytest.mark.parametrize(
    "value",
    ["a${B}c", "it's ${B}", 'x${NOPE}y"z', "${A}${B}", "tail\\", "C:\\dir\\ ${X}", "p'w'd${"],
)
def test_written_values_with_interpolation_syntax_read_back_exactly(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / ".env"

    write_env_file(path, {"KEY": value, "B": "other"})

    assert read_env_file(path)["KEY"] == value


@pytest.mark.parametrize("value", ["a\\b", r"one\ntwo-literal", "ends\\\\"])
def test_written_backslashes_read_back_exactly(tmp_path: Path, value: str) -> None:
    path = tmp_path / ".env"

    write_env_file(path, {"KEY": value})

    assert read_env_file(path)["KEY"] == value


def test_a_set_pseudonym_key_is_kept_unless_confirmed(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env_file(path, {"AWA_PSEUDONYM_KEY": "first"})

    assert write_env_file(path, {"AWA_PSEUDONYM_KEY": "second", "A": "1"}) == ["A"]
    assert read_env_file(path) == {"AWA_PSEUDONYM_KEY": "first", "A": "1"}
    write_env_file(path, {"AWA_PSEUDONYM_KEY": "second"}, confirm_overwrite=True)
    assert read_env_file(path)["AWA_PSEUDONYM_KEY"] == "second"


def test_concurrent_writers_never_lose_a_change(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: write_env_file(path, {f"NAME_{i}": str(i)}), range(24)))

    assert read_env_file(path) == {f"NAME_{i}": str(i) for i in range(24)}
    assert not (tmp_path / "..env.lock").exists()


def test_write_waits_out_a_lock_file_windows_refuses_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file another writer is still deleting (PermissionError on Windows) is retried."""

    real_open = os.open
    calls = {"lock": 0}

    def flaky_open(path, flags, *args):  # type: ignore[no-untyped-def]
        if str(path).endswith(".lock"):
            calls["lock"] += 1
            if calls["lock"] == 1:
                raise PermissionError("lock file is being deleted")
        return real_open(path, flags, *args)

    monkeypatch.setattr(filelock_module.os, "open", flaky_open)
    assert write_env_file(tmp_path / ".env", {"A": "1"}) == ["A"]
    assert calls["lock"] == 2
    assert read_env_file(tmp_path / ".env") == {"A": "1"}


def test_write_raises_permission_error_once_the_lock_deadline_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file that stays refused surfaces its PermissionError after the deadline."""

    real_open = os.open

    def refusing_open(path, flags, *args):  # type: ignore[no-untyped-def]
        if str(path).endswith(".lock"):
            raise PermissionError("access denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(filelock_module.os, "open", refusing_open)
    monkeypatch.setattr(filelock_module, "_LOCK_SECONDS", 0.2)
    with pytest.raises(PermissionError):
        write_env_file(tmp_path / ".env", {"A": "1"})
    assert not (tmp_path / ".env").exists()


_CORPUS = (
    "# a comment\n"
    "export TOKEN=abc\n"
    "QUOTED='single value'\n"
    'DOUBLE="double # not a comment"\n'
    "EMPTY=\n"
    "BARE\n"
    "TOKEN=second\n"
    "TRAIL=value # a trailing comment\n"
)


def test_values_and_lines_of_a_corpus(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_CORPUS, encoding="utf-8", newline="\n")

    values, lines = parse_env_file(path)

    assert values["TOKEN"] == "second"
    assert values["QUOTED"] == "single value"
    assert values["DOUBLE"] == "double # not a comment"
    assert values["EMPTY"] == ""
    assert values["BARE"] == ""
    assert values["TRAIL"] == "value"
    assert lines["TOKEN"] == 7  # the last assignment wins, in the value and in the line
    assert lines["QUOTED"] == 3
    assert "BARE" not in lines  # a name without `=` has no assignment line
    assert read_env_file(path) == values


def test_a_byte_order_mark_and_windows_line_ends_do_not_change_the_result(tmp_path: Path) -> None:
    plain = tmp_path / "plain.env"
    plain.write_text(_CORPUS, encoding="utf-8", newline="\n")
    windows = tmp_path / "windows.env"
    windows.write_bytes(b"\xef\xbb\xbf" + _CORPUS.replace("\n", "\r\n").encode("utf-8"))

    assert parse_env_file(windows) == parse_env_file(plain)


def test_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "absent.env") == ({}, {})
    assert read_env_file(tmp_path / "absent.env") == {}


def test_a_file_that_is_not_utf8_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes("NAME=caf\u00e9\n".encode("cp1252"))

    with pytest.raises(ConfigurationError, match=r"\.env is not UTF-8 text.*save it as UTF-8"):
        parse_env_file(path)


def test_only_the_parser_calls_dotenv_values() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "awa"
    callers = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "import dotenv_values" in path.read_text(encoding="utf-8")
    ]

    assert callers == ["core/_envparse.py"]


def test_removing_and_writing_names_at_the_same_time_loses_no_change(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env_file(path, {f"DROP_{n}": "x" for n in range(8)})
    errors: list[BaseException] = []

    def drop(n: int) -> None:
        try:
            remove_env_names(path, [f"DROP_{n}"])
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def add(n: int) -> None:
        try:
            write_env_file(path, {f"KEEP_{n}": "y"}, mode="merge")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=fn, args=(n,)) for n in range(8) for fn in (drop, add)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert read_env_file(path) == {f"KEEP_{n}": "y" for n in range(8)}


def test_a_blueprint_with_an_alias_bomb_declares_no_variables(tmp_path: Path) -> None:
    levels = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for index, previous in enumerate("abcdefghij"):
        levels.append(
            f"{chr(ord('b') + index)}: &{chr(ord('b') + index)} [{', '.join('*' + previous for _ in range(9))}]"
        )
    (tmp_path / "blueprint.yaml").write_text(
        "\n".join(levels) + "\nsecret_env: HOOK\n", encoding="utf-8"
    )

    assert declared_env(tmp_path) == {}


def test_a_blueprint_names_its_variables(tmp_path: Path) -> None:
    (tmp_path / "blueprint.yaml").write_text("name: n\nsecret_env: HOOK_SECRET\n", encoding="utf-8")

    assert set(declared_env(tmp_path)) == {"HOOK_SECRET"}

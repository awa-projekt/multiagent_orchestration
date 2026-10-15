"""`awa.data.profile()`: finding files, per-file isolation under stress, progress, limits, the parser registry, and reproducible output."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import jsonschema
import pytest

from _helpers.data_fixtures import ORDERS_CSV, REPO, write_text
from _helpers.polling import SUBPROCESS_TIMEOUT
from awa.data import (
    DataError,
    DataProfile,
    Limits,
    ParseContext,
    PathNotAllowedError,
    ProfileEvent,
    TooLargeError,
    TooManyFilesError,
    aprofile,
    ingest,
    parsers_override,
    profile,
    register_parser,
)
from awa.data._isolate import ISOLATED_PARSERS
from awa.data._model import FileProfile
from awa.data._parse import PARSERS, parser_for
from awa.testing import FakeClock

CREATED = "2026-10-05T00:00:00Z"


def test_a_real_german_csv_is_profiled_with_its_format_decisions() -> None:
    p = profile(ORDERS_CSV)

    (file,) = p.files
    assert (file.id, file.path, file.kind, file.status) == ("f1", "orders.csv", "csv", "ok")
    assert file.sha256 == hashlib.sha256(ORDERS_CSV.read_bytes()).hexdigest()
    assert file.size == ORDERS_CSV.stat().st_size
    assert file.text is not None
    assert (file.text.encoding, file.text.delimiter, file.text.bom) == ("cp1252", ";", False)
    (table,) = file.tables
    assert table.id == "f1.t1" and table.header_row == 1 and table.rows_exact
    assert [c.name for c in table.columns] == [
        "Bestellnr",
        "Lieferant",
        "Land",
        "Betrag_EUR",
        "Wunschliefertermin",
        "Status",
    ]
    amount = p.find_column("orders.csv", "betrag_eur")
    assert amount is not None and amount.type == "decimal"
    assert amount.format is not None and amount.format.decimal == ","
    assert (amount.min, amount.max) == (880.75, 23400.0)
    assert amount.examples[:2] == ["4.250,00", "5.980,50"]
    key = p.find_column("orders.csv", "Bestellnr")
    assert key is not None and key.unique and key.type == "text"
    assert table.rows == 8 and len(table.sample_rows) == 3
    assert p.summary.files == 1 and p.summary.rows == 8


def test_utf8_with_bom_comma_delimiter_and_blank_header_cells(tmp_path: Path) -> None:
    path = write_text(tmp_path, "a.csv", "﻿id,name,,name\r\n1,Ann,x,\r\n2,Bob,y,z\r\n\r\n", "utf-8")

    (file,) = profile(path).files

    assert file.text is not None
    assert (file.text.encoding, file.text.bom, file.text.delimiter, file.text.newline) == (
        "utf-8-sig",
        True,
        ",",
        "crlf",
    )
    table = file.tables[0]
    assert [(c.name, c.key) for c in table.columns] == [
        ("id", "id"),
        ("name", "name"),
        ("column_3", "column_3"),
        ("name", "name_2"),
    ]
    assert table.rows == 2
    assert table.columns[0].type == "integer" and table.columns[0].unique
    assert table.columns[3].nullable and table.columns[3].null_share == 0.5


def test_ragged_rows_and_quoted_newlines_do_not_break_the_profile(tmp_path: Path) -> None:
    path = write_text(tmp_path, "r.csv", 'a;b\n1;"x\ny"\n2\n3;4;5\n')

    (file,) = profile(path).files

    table = file.tables[0]
    assert table.rows == 3
    assert table.columns[1].examples == ["x\ny", "4"]


def test_folders_globs_and_several_paths_are_sorted_with_relative_names(tmp_path: Path) -> None:
    write_text(tmp_path, "in/b.csv", "x\n1\n")
    write_text(tmp_path, "in/sub/a.csv", "x\n1\n")
    write_text(tmp_path, "bank.csv", "x\n1\n")
    write_text(tmp_path, "notes.bin", "x")

    folder = profile([tmp_path / "in", tmp_path / "bank.csv"], created=CREATED)
    pattern = profile(str(tmp_path / "in" / "*.csv"))
    nested = profile(str(tmp_path / "**" / "*.csv"))

    assert [(f.id, f.path) for f in folder.files] == [
        ("f1", "bank.csv"),
        ("f2", "in/b.csv"),
        ("f3", "in/sub/a.csv"),
    ]
    assert [f.path for f in pattern.files] == ["b.csv"]
    assert len(nested.files) == 3
    assert folder.created == CREATED


def test_the_same_files_give_the_same_json(tmp_path: Path) -> None:
    write_text(tmp_path, "x.csv", "a;b\n1;2\n")

    first = profile(tmp_path, created=CREATED).to_json()
    second = profile(tmp_path, created=CREATED).to_json()

    assert first == second


def test_originals_are_only_read(tmp_path: Path) -> None:
    path = write_text(tmp_path, "x.csv", "a;b\n1;2\n")
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    profile(path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.csv"]


def test_a_bad_file_does_not_fail_the_run(tmp_path: Path) -> None:
    write_text(tmp_path, "good.csv", "a;b\n1;2\n")
    write_text(tmp_path, "empty.csv", "\n\n")
    write_text(tmp_path, "blob.bin", "\x00\x01")

    p = profile(tmp_path)

    by_name = {f.path: f for f in p.files}
    assert by_name["good.csv"].status == "ok"
    assert by_name["empty.csv"].status == "error"
    assert [f.code for f in by_name["empty.csv"].flags] == ["DF-E-EMPTY"]
    assert by_name["blob.bin"].status == "unsupported" and by_name["blob.bin"].kind == "unknown"
    assert [f.code for f in by_name["blob.bin"].flags] == ["DF-W-UNSUPPORTED-KIND"]
    assert (p.summary.errors, p.summary.warnings) == (1, 1)


def test_a_crashing_parser_marks_only_its_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(path: Path, ctx: ParseContext) -> FileProfile:
        raise RuntimeError(f"secret content of {path.name}")

    monkeypatch.setitem(PARSERS, "csv", boom)
    write_text(tmp_path, "a.csv", "a\n1\n")

    (file,) = profile(tmp_path).files

    assert file.status == "error"
    (flag,) = file.flags
    assert flag.code == "DF-E-PARSE-FAILED"
    assert "RuntimeError" in flag.message and "secret" not in flag.message


def test_a_parser_that_runs_too_long_is_stopped_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow(path: Path, ctx: ParseContext) -> FileProfile:
        while True:
            time.sleep(0.005)
            ctx.check_deadline()

    monkeypatch.setitem(PARSERS, "csv", slow)
    write_text(tmp_path, "a.csv", "a\n1\n")

    (file,) = profile(tmp_path, limits=Limits(parse_timeout_s=0.05)).files

    assert [f.code for f in file.flags] == ["DF-E-TIMEOUT"]


def test_a_missing_library_gives_an_error_flag_with_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def needs_library(path: Path, ctx: ParseContext) -> FileProfile:
        assert ctx.library_missing("awa_no_such_library")
        return ctx.profile

    monkeypatch.setitem(PARSERS, "csv", needs_library)
    write_text(tmp_path, "a.csv", "a\n1\n")

    (file,) = profile(tmp_path).files

    assert file.status == "error"
    assert file.flags[0].code == "DF-E-MISSING-EXTRA"
    assert "files" in file.flags[0].message and "pip install" in file.flags[0].message


def test_embedded_files_are_profiled_as_children_up_to_three_levels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = PARSERS["csv"]

    def nesting(path: Path, ctx: ParseContext) -> FileProfile:
        if ctx.file_id.count(".") < 5:  # keep nesting until the limit stops it
            ctx.profile.children.append(ctx.child_profile("../in ner?.csv", b"x;y\n1;2\n"))
        return original(path, ctx)

    monkeypatch.setitem(PARSERS, "csv", nesting)
    write_text(tmp_path, "a.csv", "a\n1\n")

    (file,) = profile(tmp_path).files

    child = file.children[0]
    assert (child.id, child.path, child.kind, child.status) == ("f1.a1", "in ner_.csv", "csv", "ok")
    deepest = child.children[0].children[0].children[0]
    assert deepest.id == "f1.a1.a1.a1.a1" and deepest.status == "unsupported"
    assert deepest.flags[0].code == "DF-W-NESTED-TOO-DEEP"


def test_a_parser_may_be_any_callable_for_example_a_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def parse(label: str, path: Path, ctx: ParseContext) -> FileProfile:
        ctx.flag("DF-I-LABEL", label)
        return ctx.profile

    monkeypatch.setitem(PARSERS, "csv", functools.partial(parse, "from a partial"))
    write_text(tmp_path, "a.csv", "a\n1\n")

    (file,) = profile(tmp_path).files

    assert file.status == "ok" and file.flags[0].message == "from a partial"


def test_register_parser_refuses_a_second_parser_for_a_kind_unless_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("awa.data._parse.PARSERS", dict(PARSERS))
    assert parser_for("csv") is not None and parser_for("klingon") is None

    def mine(path: Path, ctx: ParseContext) -> FileProfile:
        return ctx.profile

    register_parser("klingon", mine)
    assert parser_for("klingon") is mine
    with pytest.raises(ValueError, match="replace=True"):
        register_parser("klingon", mine)
    register_parser("klingon", mine, replace=True)


def test_progress_events_come_in_order_and_carry_no_file_content(tmp_path: Path) -> None:
    write_text(tmp_path, "a.csv", "name\nSECRET-VALUE\n")
    write_text(tmp_path, "b.bin", "x")
    events: list[ProfileEvent] = []

    profile(tmp_path, on_progress=events.append)

    assert [(e.file, e.stage) for e in events] == [
        (None, "start"),
        ("f1", "start"),
        ("f1", "detect"),
        ("f1", "parse"),
        ("f1", "done"),
        ("f2", "start"),
        ("f2", "detect"),
        ("f2", "done"),
        (None, "done"),
    ]
    done = events[4]
    assert (done.path, done.kind, done.status, done.flags) == ("a.csv", "csv", "ok", 0)
    assert "SECRET-VALUE" not in "".join(e.model_dump_json() for e in events)


def test_bad_arguments_raise_a_data_error(tmp_path: Path) -> None:
    write_text(tmp_path, "a.csv", "a\n1\n")
    write_text(tmp_path, "inside/b.csv", "a\n1\n")

    with pytest.raises(DataError, match="no path"):
        profile([])
    with pytest.raises(PathNotAllowedError, match="does not exist"):
        profile(tmp_path / "missing.csv")
    with pytest.raises(PathNotAllowedError, match="matches no file"):
        profile(str(tmp_path / "*.xyz"))
    with pytest.raises(PathNotAllowedError, match="outside the allowed folder"):
        profile(tmp_path / "a.csv", root=tmp_path / "inside")
    assert profile(tmp_path / "inside" / "b.csv", root=tmp_path).files[0].path == "inside/b.csv"
    assert PathNotAllowedError.code == "awa.data.path_not_allowed"


def test_symbolic_links_are_never_followed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Creating real links needs a privilege on Windows, so the files merely report being links.
    write_text(tmp_path, "real/a.csv", "a\n1\n")
    write_text(tmp_path, "real/link.csv", "a\n1\n")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "link.csv")

    p = profile(tmp_path / "real")

    assert [f.path for f in p.files] == ["a.csv"]
    assert [f.code for f in p.flags] == ["DF-W-SYMLINK-SKIPPED"]
    with pytest.raises(PathNotAllowedError, match="symbolic link"):
        profile(tmp_path / "real" / "link.csv")
    with pytest.raises(PathNotAllowedError, match="matches no file"):
        profile(str(tmp_path / "real" / "link*"))


def test_limits_on_files_bytes_and_rows(tmp_path: Path) -> None:
    write_text(tmp_path, "a.csv", "x\n" + "1\n" * 10)
    write_text(tmp_path, "b.csv", "x\n1\n")

    with pytest.raises(TooManyFilesError):
        profile(tmp_path, limits=Limits(max_files=1))
    with pytest.raises(TooLargeError):
        profile(tmp_path, limits=Limits(max_total_bytes=10))

    p = profile(tmp_path, limits=Limits(max_file_bytes=10))
    assert {f.path: f.status for f in p.files} == {"a.csv": "error", "b.csv": "ok"}
    assert p.files[0].flags[0].code == "DF-E-TOO-LARGE"

    sampled = profile(tmp_path / "a.csv", limits=Limits(max_rows_exact=4)).files[0]
    assert sampled.status == "partial" and sampled.flags[0].code == "DF-W-SAMPLED"
    # beyond max_rows_exact the rows are still counted exactly; only the statistics are sampled
    assert sampled.tables[0].rows == 10 and sampled.tables[0].rows_exact
    assert sampled.tables[0].sampled and 0 < (sampled.tables[0].rows_seen or 0) <= 4


NO_EXTRAS = """
import importlib.abc, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {"openpyxl", "pypdf", "docx", "fastapi", "uvicorn", "multipart"}:
            raise ModuleNotFoundError(name)

sys.meta_path.insert(0, Block())
from awa.data import ingest, profile

p = profile(sys.argv[1])
assert p.files[0].status == "ok" and p.files[0].tables[0].rows == 8, p.files[0]
assert [c.name for c in ingest(sys.argv[1], sys.argv[2])] == ["orders.csv"]
assert not {"openpyxl", "pypdf", "docx", "fastapi"} & set(sys.modules)
print("no extras needed")
"""


def test_awa_data_profiles_and_ingests_a_csv_without_any_extra(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-c", NO_EXTRAS, str(ORDERS_CSV), str(tmp_path / "copy")],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )

    assert run.returncode == 0 and "no extras needed" in run.stdout, run.stderr[-2000:]


def test_profile_takes_its_time_from_the_clock_it_is_given() -> None:
    clock = FakeClock()
    assert profile(ORDERS_CSV, clock=clock).created == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_time_limit_of_a_file_follows_the_given_clock() -> None:
    clock = FakeClock()
    seen: list[float] = []

    def slow(path: Path, ctx: ParseContext) -> FileProfile:
        seen.append(ctx.deadline or 0.0)
        clock.advance(seconds=Limits().parse_timeout_s + 1)
        ctx.check_deadline()
        return ctx.profile

    with parsers_override({"csv": slow}):
        result = profile(ORDERS_CSV, clock=clock)
    assert result.files[0].status == "error"
    assert seen == [Limits().parse_timeout_s]


ISOLATED = "_helpers.data_isolated_parsers:profile_mixed"


def test_a_parser_that_never_checks_its_deadline_is_abandoned_and_cannot_change_the_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release, finished = threading.Event(), threading.Event()

    def stubborn(path: Path, ctx: ParseContext) -> FileProfile:
        release.wait(10)
        ctx.profile.status = "ok"
        ctx.flag("DF-I-LATE", "written after the run moved on")
        finished.set()
        return ctx.profile

    monkeypatch.setitem(PARSERS, "csv", stubborn)
    write_text(tmp_path, "a.csv", "a\n1\n")
    started = time.monotonic()

    (file,) = profile(tmp_path, limits=Limits(parse_timeout_s=0.2)).files
    release.set()
    assert finished.wait(5)

    assert time.monotonic() - started < 3
    assert file.status == "error"
    assert [flag.code for flag in file.flags] == ["DF-E-TIMEOUT"]


def test_isolated_parsers_run_in_one_reused_worker_process_and_noise_stays_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        PARSERS, "csv", importlib.import_module(ISOLATED.split(":")[0]).profile_mixed
    )
    monkeypatch.setattr("awa.data._profile.ISOLATED_PARSERS", {ISOLATED})
    for name in ("a.csv", "b.csv", "c.csv"):
        write_text(tmp_path, name, "x\n1\n")

    p = profile(tmp_path, limits=Limits(parse_timeout_s=20))

    pids = {file.flags[0].message for file in p.files}
    assert [f.status for f in p.files] == ["ok", "ok", "ok"]
    assert len(pids) == 1 and pids != {f"pid {os.getpid()}"}
    assert "noise" not in p.to_json()


def test_a_looping_crashing_or_failing_worker_costs_only_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        PARSERS, "csv", importlib.import_module(ISOLATED.split(":")[0]).profile_mixed
    )
    monkeypatch.setattr("awa.data._profile.ISOLATED_PARSERS", {ISOLATED})
    for name in ("a_ok.csv", "b_loop.csv", "c_raise.csv", "d_exit.csv", "e_ok.csv"):
        write_text(tmp_path, name, "x\n1\n")
    started = time.monotonic()

    p = profile(tmp_path, limits=Limits(parse_timeout_s=1.0))

    by = {f.path: f for f in p.files}
    assert time.monotonic() - started < 30
    assert by["b_loop.csv"].flags[0].code == "DF-E-TIMEOUT"
    assert "ValueError" in by["c_raise.csv"].flags[0].message
    assert "secret" not in p.to_json()
    assert "stopped unexpectedly" in by["d_exit.csv"].flags[0].message
    assert by["a_ok.csv"].status == "ok" and by["e_ok.csv"].status == "ok"
    assert by["a_ok.csv"].flags[0].message != by["e_ok.csv"].flags[0].message  # a new worker


def test_the_start_up_of_a_worker_does_not_count_against_the_files_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a busy machine a new Python process needs seconds to import awa; the file gets its own
    time limit only once the worker has said it is ready."""

    monkeypatch.setitem(
        PARSERS, "csv", importlib.import_module(ISOLATED.split(":")[0]).profile_mixed
    )
    monkeypatch.setattr("awa.data._profile.ISOLATED_PARSERS", {ISOLATED})
    slow_start = "import time; time.sleep(2.5); from awa.data._worker import main; main()"
    monkeypatch.setattr("awa.data._isolate._WORKER_ARGS", ("-c", slow_start))
    write_text(tmp_path, "a_ok.csv", "x\n1\n")

    (file,) = profile(tmp_path, limits=Limits(parse_timeout_s=1.0)).files

    assert file.status == "ok", file.flags


@pytest.mark.parametrize(
    "worker", ["import sys; sys.exit(3)", "print('not json', flush=True); input()"]
)
def test_a_worker_that_cannot_start_is_an_error_for_the_file_not_a_hang(
    worker: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        PARSERS, "csv", importlib.import_module(ISOLATED.split(":")[0]).profile_mixed
    )
    monkeypatch.setattr("awa.data._profile.ISOLATED_PARSERS", {ISOLATED})
    monkeypatch.setattr("awa.data._isolate._WORKER_ARGS", ("-c", worker))
    write_text(tmp_path, "a_ok.csv", "x\n1\n")
    started = time.monotonic()

    (file,) = profile(tmp_path).files

    assert time.monotonic() - started < 20
    assert (file.status, [f.code for f in file.flags]) == ("error", ["DF-E-PARSE-FAILED"])
    assert "could not start" in file.flags[0].message


def test_an_office_file_that_is_a_zip_bomb_is_refused_before_its_parser_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def parser(path: Path, ctx: ParseContext) -> FileProfile:
        calls.append(path.name)
        return ctx.profile

    monkeypatch.setitem(PARSERS, "xlsx", parser)
    bomb = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("xl/workbook.xml", "x")
        archive.writestr("xl/big.xml", b"0" * 5_000_000)

    (file,) = profile(bomb).files

    assert calls == [] and file.kind == "xlsx" and file.status == "error"
    assert file.flags[0].code == "DF-E-ZIP-UNSAFE" and "unreasonable size" in file.flags[0].message


async def test_the_async_twin_returns_the_same_profile_and_keeps_the_loop_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow(path: Path, ctx: ParseContext) -> FileProfile:
        time.sleep(0.3)  # blocks its thread, not the event loop
        return ctx.profile

    write_text(tmp_path, "a.csv", "a;b\n1;2\n")
    expected = profile(tmp_path, created=CREATED).to_json()
    monkeypatch.setitem(PARSERS, "csv", slow)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    slow_result = await aprofile(tmp_path, created=CREATED)
    beat.cancel()
    monkeypatch.undo()
    fast = await aprofile(tmp_path, created=CREATED)

    assert ticks >= 5 and slow_result.files[0].tables == []
    assert fast.to_json() == expected


async def test_cancelling_the_async_run_stops_it_before_the_next_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[str] = []
    gate = threading.Event()

    def slow(path: Path, ctx: ParseContext) -> FileProfile:
        started.append(path.name)
        gate.wait(5)
        return ctx.profile

    monkeypatch.setitem(PARSERS, "csv", slow)
    for name in ("a.csv", "b.csv", "c.csv"):
        write_text(tmp_path, name, "x\n1\n")

    task = asyncio.create_task(aprofile(tmp_path))
    while not started:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.sleep(0.3)  # the worker thread finishes its file and sees the cancellation

    assert started == ["a.csv"]


def _example_data_folders() -> list[Path]:
    folders = sorted(REPO.glob("examples/usecases/*/data")) + sorted(
        REPO.glob("examples/benchmarks/core/*/data")
    )
    assert len(folders) >= 15
    return folders


@pytest.mark.slow
@pytest.mark.parametrize("folder", _example_data_folders(), ids=lambda p: p.parent.name)
def test_every_real_example_folder_profiles_without_error_and_validates(folder: Path) -> None:
    p = profile(folder, created=CREATED)

    document = json.loads(p.to_json())
    jsonschema.validate(document, DataProfile.json_schema(), jsonschema.Draft202012Validator)
    assert DataProfile.from_json(p.to_json()) == p
    assert p.to_json() == profile(folder, created=CREATED).to_json()  # deterministic
    assert [f.id for f in p.files] == [f"f{n}" for n in range(1, len(p.files) + 1)]
    assert p.summary.files == len(list(folder.rglob("*"))) - len(
        [d for d in folder.rglob("*") if d.is_dir()]
    )
    for file in p.files:
        original = folder / file.path
        assert file.sha256 == hashlib.sha256(original.read_bytes()).hexdigest()
        assert file.status in ("ok", "partial", "error", "unsupported")
        if file.status == "error":
            assert any(flag.severity == "error" for flag in file.flags), file.path


def test_office_readers_run_in_the_killable_worker() -> None:
    assert {"awa.data._excel:profile_xlsx", "awa.data._docx:profile_docx"} <= ISOLATED_PARSERS


ENTRY_TIME = (2026, 3, 4, 5, 6, 8)  # zip entries have two-second resolution


def _descendants(file: FileProfile) -> list[FileProfile]:
    return [node for child in file.children for node in [child, *_descendants(child)]]


def _eml_with_a_nested_eml() -> bytes:
    inner = EmailMessage()
    inner["From"], inner["To"], inner["Subject"] = "a@example.com", "b@example.com", "inner"
    inner.set_content("inner body")
    inner.add_attachment(b"a;b\n1;2\n", maintype="text", subtype="csv", filename="rows.csv")
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = "c@example.com", "d@example.com", "outer"
    outer.set_content("outer body")
    outer.add_attachment(inner, filename="inner.eml")
    return outer.as_bytes()


def test_embedded_files_of_any_parser_have_no_modification_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Independent of the mail reader: whatever a parser embeds is profiled from a temporary
    copy whose time must not reach the profile."""

    def with_children(path: Path, ctx: ParseContext) -> FileProfile:
        ctx.profile.children.append(ctx.child_profile("inside.csv", b"x;y\n1;2\n"))
        ctx.profile.children.append(ctx.child_profile("deeper.csv", b"x;y\n3;4\n"))
        return ctx.profile

    monkeypatch.setitem(PARSERS, "text", with_children)
    write_text(tmp_path, "outer.txt", "just text\n")

    first = profile(tmp_path, created=CREATED)
    time.sleep(1.1)  # the time of a copy has second resolution
    second = profile(tmp_path, created=CREATED)

    (outer,) = first.files
    assert outer.mtime is not None  # a real file keeps its time
    assert [(c.id, c.mtime) for c in outer.children] == [("f1.a1", None), ("f1.a2", None)]
    assert first.to_json() == second.to_json()


def test_the_copies_of_ingest_keep_the_time_of_their_source(tmp_path: Path) -> None:
    source = write_text(tmp_path, "in/a.csv", "x\n1\n")
    stamp = datetime(2025, 1, 2, 3, 4, 6, tzinfo=UTC).timestamp()
    os.utime(source, (stamp, stamp))
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("dated.csv", ENTRY_TIME), "x\n1\n")
        bundle.writestr(zipfile.ZipInfo("zero.csv", (1980, 0, 0, 0, 0, 0)), "x\n2\n")

    copies = {c.name: c for c in ingest([source, archive], tmp_path / "out")}

    assert copies["in/a.csv"].path.stat().st_mtime == stamp
    assert (
        copies["pack/dated.csv"].path.stat().st_mtime
        == datetime(*ENTRY_TIME, tzinfo=UTC).timestamp()
    )
    assert (
        copies["pack/zero.csv"].path.stat().st_mtime == datetime(1980, 1, 1, tzinfo=UTC).timestamp()
    )


def test_a_zip_with_a_nested_mail_profiles_to_the_same_json_after_a_second_ingest(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "mailbox.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("outer.eml", ENTRY_TIME), _eml_with_a_nested_eml())
        bundle.writestr(zipfile.ZipInfo("sub/rows.csv", ENTRY_TIME), "a;b\n1;2\n")
    dest = tmp_path / "data"

    def ingest_and_profile() -> str:
        ingest(archive, dest)
        return profile(dest, created=CREATED, root=dest).to_json()

    first = ingest_and_profile()
    for path in sorted(dest.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    time.sleep(1.1)  # the extraction time of the second ingest differs by a second
    second = ingest_and_profile()

    assert first == second
    nodes = [n for f in profile(dest, created=CREATED).files for n in _descendants(f)]
    assert len(nodes) >= 2 and {node.mtime for node in nodes} == {None}

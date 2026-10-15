"""`scripts/capture_screenshots.py`: the parts that need no browser."""

from __future__ import annotations

import importlib.util
import re
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "capture_screenshots.py"


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_screenshots", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- exit codes


def test_a_waiting_worker_is_accepted_only_where_runs_wait(capture: ModuleType) -> None:
    argv = ["worker", "blueprint.yaml", "--once"]
    capture.check_exit(argv, capture.WORKER_WAITING, "", (capture.WORKER_WAITING,))
    capture.check_exit(argv, 0, "", (0,))
    with pytest.raises(SystemExit, match="ended with 2"):
        capture.check_exit(argv, 2, "two runs wait", (0,))
    with pytest.raises(SystemExit, match="ended with 1"):
        capture.check_exit(argv, 1, "", (capture.WORKER_WAITING,))


# ---------------------------------------------------------------- redaction


def _masks(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def test_paths_inside_the_demo_folders_stay_readable(capture: ModuleType) -> None:
    patterns = capture.redact_patterns([Path("C:/awa-guide-work"), Path("/tmp/demo")])
    assert not _masks(patterns, r"C:\awa-guide-work\invoice-desk")
    assert not _masks(patterns, r"c:\AWA-GUIDE-WORK")
    assert _masks(patterns, r"C:\awa-guide-work-other\x")
    assert _masks(patterns, r"D:\projects\invoice-desk")
    assert _masks(patterns, "someone@example.com")


def test_without_neutral_windows_folders_every_path_is_masked(capture: ModuleType) -> None:
    assert capture.redact_patterns([Path("/tmp/demo")]) == capture.REDACT_PATTERNS
    assert _masks(capture.redact_patterns([]), r"C:\awa-guide-work\invoice-desk")


def test_the_page_is_restored_after_the_shot_even_when_it_fails(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    page: Any = object.__new__(capture.Page)
    page.js = lambda expression: calls.append(expression)

    def failing_capture(*_args: Any) -> Path:
        calls.append("capture")
        raise SystemExit("the page has changed")

    page._capture = failing_capture
    monkeypatch.setattr(capture, "REDACT_LITERALS", [])
    with pytest.raises(SystemExit):
        page.shot("ui-test")
    assert capture.LIGHT_THEME_JS in calls[:2]
    assert calls.index("capture") < len(calls) - 1
    assert calls[-1] == capture._UNREDACT_JS.strip()
    assert "box.dataset.original" in capture._REDACT_JS
    assert "__awaRedactedInputs.push([el, el.value])" in capture._REDACT_JS


def test_without_redaction_the_page_is_left_alone(
    capture: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    page: Any = object.__new__(capture.Page)
    page.js = lambda expression: calls.append(expression)
    page._capture = lambda *_args: Path("x.png")
    monkeypatch.setattr(capture, "REDACT_LITERALS", None)
    assert page.shot("ui-test") == Path("x.png")
    assert calls[-1] == capture.LIGHT_THEME_JS  # after the blur, nothing to undo


# ---------------------------------------------------------------- frames


def test_one_fixed_frame_per_picture_type(capture: ModuleType) -> None:
    assert (capture.APP_FRAME.width, capture.APP_FRAME.height) == (1280, 800)
    assert (capture.PHONE_FRAME.width, capture.PHONE_FRAME.height) == (390, 844)
    assert capture.PHONE_FRAME.mobile and not capture.APP_FRAME.mobile
    assert capture.WIDTH == capture.GUIDE_WIDTH == capture.APP_FRAME.width
    assert set(capture.PHONE_PAGES) <= set(capture.PLAIN_TWINS)


def test_the_frame_starts_below_the_sticky_header(capture: ModuleType) -> None:
    frame = capture.APP_FRAME
    assert capture.frame_scroll(120, 110, 3000, frame) == 0  # fits at the page top
    assert capture.frame_scroll(1000, 110, 3000, frame) == 874
    assert capture.frame_scroll(2900, 110, 3000, frame) == 2200  # never past the page end
    assert capture.frame_scroll(500, 110, 600, frame) == 0  # a page shorter than the frame


# ---------------------------------------------------------------- the size optimiser


def _png(width: int, height: int, noisy: bool) -> bytes:
    import io
    import random

    from PIL import Image

    image = Image.new("RGB", (width, height), (255, 255, 255))
    if noisy:
        rng = random.Random(7)
        image.putdata(
            [
                (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(width * height)
            ]
        )
    else:
        for x in range(0, width, 40):
            for y in range(height):
                image.putpixel((x, y), (0, 113, 227))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def _pixels(png: bytes) -> bytes:
    import io

    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGB").tobytes()


def test_a_picture_with_few_colours_is_optimised_without_loss(capture: ModuleType) -> None:
    original = _png(1280, 800, noisy=False)
    assert len(original) > capture.MAX_PICTURE_BYTES
    small = capture.optimise_png(original, "ui-test")
    assert len(small) <= capture.MAX_PICTURE_BYTES
    assert _pixels(small) == _pixels(original)


def test_a_picture_that_stays_too_large_stops_the_capture(capture: ModuleType) -> None:
    with pytest.raises(SystemExit, match="ui-noise: .* over 250 KB"):
        capture.optimise_png(_png(600, 600, noisy=True), "ui-noise")


def test_a_small_picture_is_never_made_larger(capture: ModuleType) -> None:
    original = _png(40, 40, noisy=False)
    assert len(capture.optimise_png(original, "ui-tiny")) <= len(original)


def test_local_paths_are_shortened_to_neutral_ones(capture: ModuleType) -> None:
    privacy: Any = object.__new__(capture.Privacy)
    privacy.replacements = [(r"D:\work\someone\venv", r"C:\demo\.venv")]
    assert privacy.clean(r"in d:\WORK\someone\venv\Lib") == r"in C:\demo\.venv\Lib"


# ---------------------------------------------------------------- the web app


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # uvicorn exits
def test_a_taken_port_stops_the_capture_instead_of_hanging(capture: ModuleType) -> None:
    pytest.importorskip("uvicorn")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        return None

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        failed = pytest.raises(SystemExit, match=f"did not start on 127.0.0.1:{port}")
        with failed, capture.serve_app(app, port):
            pass


def test_tracing_is_captured_when_named_even_with_every_other_group(capture: ModuleType) -> None:
    assert "tracing" not in capture.chosen_groups(None)
    assert capture.chosen_groups(list(capture.GROUPS)) == list(capture.GROUPS)

"""`vision_ocr`: scanned PDF pages go to a vision model as images; text pages never do."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pypdf")

from awa.testing import FunctionChatModel, ModelCall  # noqa: E402
from awa.toolkits import document_toolkit, vision_ocr  # noqa: E402
from awa.toolkits.documents import pdf_text  # noqa: E402


def scan(path: Path, angle: int = 0) -> None:
    """An image-only PDF: one JPEG page, no text layer."""

    image = Image.new("RGB", (400, 300), "white")
    image.rotate(angle, expand=True).save(path, "PDF", resolution=100)


def test_a_scanned_page_is_sent_as_an_image_and_read(tmp_path: Path) -> None:
    scan(tmp_path / "scan.pdf")
    seen: list[list[Any]] = []

    def respond(call: ModelCall) -> str:
        seen.append(list(call.messages[-1].content))
        return "Invoice no: INV-00042"

    model = FunctionChatModel(respond)
    [text] = pdf_text(tmp_path / "scan.pdf", ocr=vision_ocr(model))
    assert "INV-00042" in text and "legibility" in text  # plain text: legibility unknown
    blocks = seen[0]
    assert blocks[0]["type"] == "text" and blocks[1]["type"] == "image"
    assert blocks[1]["mime_type"] == "image/png" and blocks[1]["base64"]


def test_the_tool_says_the_page_was_read_by_ocr(tmp_path: Path) -> None:
    scan(tmp_path / "scan.pdf")
    model = FunctionChatModel(lambda call: "Total 12.00")
    [tool] = document_toolkit(tmp_path, include=["extract_pdf_text"], ocr=vision_ocr(model))
    assert "vision model" in tool.description
    out = tool.invoke({"path": "scan.pdf"})
    assert "read by OCR" in out and "Total 12.00" in out


def test_an_upright_turn_is_applied_before_reading_again() -> None:
    from awa.toolkits._vision import _prepared

    buffer = io.BytesIO()
    Image.new("L", (40, 10), 255).save(buffer, "PNG")
    data, mime = _prepared("x.png", buffer.getvalue(), 2000, turn=90) or (b"", "")
    assert mime == "image/png" and Image.open(io.BytesIO(data)).size == (10, 40)


def scans(path: Path, pages: int) -> None:
    """An image-only PDF with `pages` pages."""

    first, *rest = [Image.new("RGB", (200, 150), "white") for _ in range(pages)]
    first.save(path, "PDF", resolution=100, save_all=True, append_images=rest)


def test_every_page_read_counts_in_the_run_and_its_limits(tmp_path: Path) -> None:
    from awa.contracts import UsageLimits
    from awa.errors import UsageLimitExceededError
    from awa.runtime import RunContext, new_run_id, use_run_context

    scans(tmp_path / "three.pdf", 3)
    hook = vision_ocr(FunctionChatModel(lambda call: "Total 1.00"))
    ctx: RunContext[Any] = RunContext(run_id=new_run_id(), agent="reader", deps=None)
    with use_run_context(ctx):
        texts = pdf_text(tmp_path / "three.pdf", ocr=hook)
    assert all("Total 1.00" in text for text in texts)
    # per page: the structured attempt and the plain answer of this fake model
    assert ctx.usage.requests == 6 and ctx.usage.by_agent["reader"].requests == 6

    capped: RunContext[Any] = RunContext(
        run_id=new_run_id(), agent="reader", deps=None, limits=UsageLimits(max_requests=1)
    )
    with use_run_context(capped), pytest.raises(UsageLimitExceededError):
        pdf_text(tmp_path / "three.pdf", pages=[1], ocr=hook)


def overlapping(bound: int, answer: str) -> tuple[Callable[[ModelCall], str], list[int]]:
    """A model reply that waits until `bound` calls run at once (or 2 s), and the most seen."""

    import threading

    lock, full = threading.Lock(), threading.Event()
    busy = [0, 0]  # now, most

    def respond(call: ModelCall) -> str:
        with lock:
            busy[0] += 1
            busy[1] = max(busy[1], busy[0])
            if busy[1] >= bound:
                full.set()
        full.wait(2)
        with lock:
            busy[0] -= 1
        return answer

    return respond, busy


def test_pages_are_read_in_parallel_within_the_bound(tmp_path: Path) -> None:
    scans(tmp_path / "six.pdf", 6)
    respond, busy = overlapping(2, "page text")
    hook = vision_ocr(FunctionChatModel(respond), max_concurrency=2)
    texts = pdf_text(tmp_path / "six.pdf", ocr=hook)
    assert len(texts) == 6 and all("page text" in text for text in texts)
    assert busy[1] == 2


def test_many_pdfs_are_read_in_one_call_with_their_scans_in_parallel(tmp_path: Path) -> None:
    (tmp_path / "scans").mkdir()
    for index in range(5):
        scan(tmp_path / "scans" / f"inv{index}.pdf")
    respond, busy = overlapping(3, "Total 5.00")
    hook = vision_ocr(FunctionChatModel(respond), max_concurrency=3)
    [tool] = document_toolkit(tmp_path, include=["extract_pdfs_text"], ocr=hook)
    out = tool.invoke({"paths": ["scans/*.pdf"]})
    assert [f"=== scans/inv{i}.pdf ===" in out for i in range(5)] == [True] * 5
    assert out.count("Total 5.00") == 5 and busy[1] == 3


def test_a_batch_names_files_inside_the_root_only(tmp_path: Path) -> None:
    scan(tmp_path / "a.pdf")
    [tool] = document_toolkit(
        tmp_path, include=["extract_pdfs_text"], ocr=vision_ocr(FunctionChatModel(lambda c: "x"))
    )
    assert tool.invoke({"paths": ["../*.pdf"]}).startswith("Error:")
    assert "no PDF file matches" in tool.invoke({"paths": ["none/*.pdf"]})
    assert tool.invoke({"paths": ["a.pdf", "*.pdf"]}).count("=== a.pdf ===") == 1


def test_vision_calls_are_model_responses_of_the_run(tmp_path: Path) -> None:
    import asyncio

    from awa.contracts.events import ModelResponseEvent
    from awa.evals.report import CaseCaching
    from awa.runtime import RunContext, new_run_id, use_run_context

    scans(tmp_path / "two.pdf", 2)
    ctx: RunContext[Any] = RunContext(run_id=new_run_id(), agent="reader", deps=None)
    seen: list[Any] = []
    ctx.events.subscribe(seen.append)
    with use_run_context(ctx):
        pdf_text(tmp_path / "two.pdf", ocr=vision_ocr(FunctionChatModel(lambda c: "t")))
    asyncio.run(ctx.events.flush())
    responses = [e for e in seen if isinstance(e, ModelResponseEvent)]
    assert len(responses) == ctx.usage.requests == 4 and not any(e.content for e in responses)
    assert CaseCaching.from_events(seen).model_calls == 4


def test_the_vision_model_setting_names_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.models import ModelRegistry

    monkeypatch.setenv("AWA_VISION_MODEL", "vision-test")
    chosen = FunctionChatModel(lambda call: "from the vision profile")
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    registry.register("vision-test", chosen)
    from awa.toolkits.documents import ScannedPage

    page = ScannedPage(1, b"%PDF-1.4", ())
    assert "from the vision profile" in vision_ocr()(page)


def test_a_scan_read_by_an_agent_is_a_generation_span_in_its_trace(tmp_path: Path) -> None:
    import asyncio

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from awa import Agent
    from awa.observability import OpenTelemetrySubscriber
    from awa.testing import FakeChatModel, tool_call

    scans(tmp_path / "two.pdf", 2)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = OpenTelemetrySubscriber(provider.get_tracer("test"), langfuse=True)
    vision = FunctionChatModel(lambda call: "Total 7.00")
    tools = document_toolkit(tmp_path, include=["extract_pdf_text"], ocr=vision_ocr(vision))
    model = FakeChatModel([tool_call("extract_pdf_text", {"path": "two.pdf"}), "done"])
    agent = Agent("reader", instructions="Read.", model=model, tools=tools, hooks=[tracing])
    asyncio.run(agent.run("Read two.pdf"))
    tracing.close()

    spans = list(exporter.get_finished_spans())
    kinds = [(s.attributes or {}).get("langfuse.observation.type") for s in spans]
    generations = [s for s, kind in zip(spans, kinds, strict=True) if kind == "generation"]
    assert kinds.count("tool") == 1
    # The agent's two turns; each page twice (structured, then plain: the fake returns text).
    assert len(generations) == 2 + 2 * 2
    assert {s.context.trace_id for s in spans} == {spans[0].context.trace_id}

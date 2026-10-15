"""Event recording and the hash-chained `AuditLog` with its sinks."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from awa.agents import Agent
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    AuditEvent,
    DelegationEvent,
    ErrorEvent,
    GuardrailBlockedEvent,
    ModelFallbackEvent,
    ModelRetryEvent,
    TextDeltaEvent,
    ToolCallEvent,
)
from awa.contracts.usage import Usage
from awa.observability import (
    ACTOR_METADATA_KEY,
    GENESIS_HASH,
    REQUEST_ID_METADATA_KEY,
    TENANT_METADATA_KEY,
    AuditChainError,
    AuditLog,
    AuditLogSink,
    AuditRecord,
    EventLogger,
    EventRecorder,
    InMemoryAuditSink,
    JsonlAuditSink,
    RegexRedactor,
    audit_node,
    describe_event,
    read_audit_file,
    verify_chain,
)
from awa.observability import audit as audit_module
from awa.observability.audit import sort_audit_trail
from awa.runtime.context import EventEmitter, RunContext, use_run_context
from awa.team import Team
from awa.testing import FakeChatModel, tool_call

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _ids(run_id: str = "run_1", *, agent: str = "lead", depth: int = 0) -> dict[str, Any]:
    return {"run_id": run_id, "root_run_id": "run_1", "agent": agent, "depth": depth}


def _started(run_id: str = "run_1", *, at: datetime = T0, **kwargs: Any) -> AgentStartedEvent:
    return AgentStartedEvent(**_ids(run_id, **kwargs), timestamp=at, input_preview="hi")


def _finished(run_id: str = "run_1", *, at: datetime, **kwargs: Any) -> AgentFinishedEvent:
    usage = Usage(requests=2, input_tokens=10, output_tokens=5, tool_calls=1)
    return AgentFinishedEvent(**_ids(run_id, **kwargs), timestamp=at, usage=usage, duration_ms=500)


async def test_event_recorder_records_in_order_and_filters_by_type() -> None:
    recorder = EventRecorder()
    emitter = EventEmitter()
    emitter.subscribe(recorder)

    await emitter.emit(_started())
    await emitter.emit(TextDeltaEvent(**_ids(), delta="Hel"))
    await emitter.emit(_finished(at=T0 + timedelta(seconds=1)))

    assert recorder.types() == ["agent_started", "text_delta", "agent_finished"]
    assert [e.delta for e in recorder.of_type(TextDeltaEvent)] == ["Hel"]
    assert len(recorder) == 3
    assert [e.type for e in recorder] == recorder.types()

    recorder.clear()
    assert recorder.events == []


def test_event_logger_logs_at_the_configured_level_and_skips_text_deltas(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = EventLogger(level="DEBUG")

    with caplog.at_level(logging.DEBUG, logger="awa.events"):
        logger(_started())
        logger(TextDeltaEvent(**_ids(), delta="x"))

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.DEBUG
    assert record.name == "awa.events"
    assert "agent_started agent=lead" in record.getMessage()


def test_event_logger_can_include_text_deltas(caplog: pytest.LogCaptureFixture) -> None:
    logger = EventLogger(include_text_deltas=True)

    with caplog.at_level(logging.INFO, logger="awa.events"):
        logger(TextDeltaEvent(**_ids(), delta="chunk"))

    assert "delta='chunk'" in caplog.records[0].getMessage()


def test_event_logger_rejects_an_unknown_level() -> None:
    with pytest.raises(ValueError, match="logging level.*DEBUG, INFO, WARNING, ERROR, CRITICAL"):
        EventLogger(level="LOUD")


def test_describe_event_summarises_tool_and_error_events() -> None:
    call = ToolCallEvent(**_ids(), tool_name="search", call_id="c1", arguments={"q": "x"})
    error = ErrorEvent(**_ids(), error_type="ValueError", message="bad\ninput")

    assert describe_event(call).endswith("tool=search call_id=c1")
    assert describe_event(error).endswith("error=ValueError: bad input")


def test_describe_event_summarises_model_retries_and_fallbacks() -> None:
    retry = ModelRetryEvent(
        **_ids(), model="openai:gpt-5", attempt=2, delay=1.5, error_type="E", kind="rate_limit"
    )
    fallback = ModelFallbackEvent(
        **_ids(), from_model="openai:gpt-5", to_model="fast", reason="circuit_open"
    )

    assert describe_event(retry).endswith("model=openai:gpt-5 attempt=2 kind=rate_limit delay=1.5s")
    assert describe_event(fallback).endswith("from=openai:gpt-5 to=fast reason=circuit_open")


def test_event_logger_accepts_lowercase_level_names(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="awa.events"):
        EventLogger(level="debug")(_started())

    assert [record.levelno for record in caplog.records] == [logging.DEBUG]


def test_describe_event_flattens_and_cuts_long_previews_at_120_characters() -> None:
    event = AgentStartedEvent(**_ids(), input_preview="word\n" * 60)

    description = describe_event(event)

    preview = description.split("input=", 1)[1]
    assert "\n" not in preview
    assert preview == repr("word " * 23 + "wo...")
    assert len(preview) == 120 + 2


def _chat_events() -> dict[str, Any]:
    from awa.contracts import AgentMessage
    from awa.contracts.events import (
        AgentMessageEvent,
        DelegationEvent,
        DelegationResultEvent,
        HandoffEvent,
        ToolResultEvent,
    )

    ids = _ids()
    return {
        "call": ToolCallEvent(**ids, tool_name="search", call_id="c1", arguments={"q": "x"}),
        "coordination": ToolCallEvent(**ids, tool_name="delegate_task", call_id="c2"),
        "transfer": ToolCallEvent(**ids, tool_name="transfer_to_billing", call_id="c3"),
        "failed": ToolResultEvent(
            **ids, tool_name="search", call_id="c1", content="ValueError: no\nway", is_error=True
        ),
        "succeeded": ToolResultEvent(**ids, tool_name="search", call_id="c1", content="fine"),
        "consult": DelegationEvent(
            **ids, target="ana", task="Check the invoice\nnow", mode="consult", correlation_id="d1"
        ),
        "task": DelegationEvent(
            **ids, target="ana", task="Check it", mode="delegate", correlation_id="d1"
        ),
        "result": DelegationResultEvent(**ids, target="ana", status="ok", correlation_id="d1"),
        "handoff": HandoffEvent(**ids, source="lead", target="billing", reason="refund"),
        "handoff_plain": HandoffEvent(**ids, source="lead", target="billing"),
        "message": AgentMessageEvent(
            **ids,
            message=AgentMessage(
                sender="lead", recipient="ana", kind="request", subject="Hello", body="b"
            ),
        ),
        "error": ErrorEvent(**ids, error_type="ValueError", message="bad\ninput"),
        "started": _started(),
    }


def test_chat_style_matches_the_former_chat_lines() -> None:
    lines = {name: describe_event(event, style="chat") for name, event in _chat_events().items()}

    assert lines == {
        "call": 'lead -> search({"q": "x"})',
        "coordination": None,
        "transfer": None,
        "failed": "lead <- search [error]: ValueError: no way",
        "succeeded": None,
        "consult": "lead asks ana: Check the invoice now",
        "task": "lead => ana: Check it",
        "result": "lead <= ana [ok]",
        "handoff": "lead >> billing: refund",
        "handoff_plain": "lead >> billing",
        "message": "lead -> ana [request]: Hello",
        "error": "! lead failed: ValueError: bad input",
        "started": None,
    }


def test_log_style_is_unchanged() -> None:
    events = _chat_events()

    assert (
        describe_event(events["call"])
        == "tool_call agent=lead depth=0 run=run_1 tool=search call_id=c1"
    )
    assert describe_event(events["handoff"]) == (
        "handoff agent=lead depth=0 run=run_1 source=lead target=billing"
    )
    assert describe_event(events["error"], style="log") == (
        "error agent=lead depth=0 run=run_1 error=ValueError: bad input"
    )


IDS: dict[str, Any] = {"run_id": "run_1", "root_run_id": "run_1", "agent": "a"}


def _started_audit_log(text: str = "Hello") -> AgentStartedEvent:
    return AgentStartedEvent(input_preview=text, **IDS)


def test_records_form_a_verifiable_chain() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    audit(_started_audit_log())
    audit(ToolCallEvent(tool_name="pay", call_id="c1", arguments={"amount": 5}, **IDS))
    audit(ErrorEvent(error_type="E", message="boom", **IDS))

    records = sink.read()
    assert [r.seq for r in records] == [0, 1, 2]
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].hash
    assert [r.event_type for r in records] == ["agent_started", "tool_call", "error"]
    result = verify_chain(records)
    assert result.valid and bool(result)
    assert result.checked == 3
    assert result.head_hash == records[-1].hash
    assert audit.head == (2, records[-1].hash)
    assert isinstance(sink, AuditLogSink)


def test_tampering_reordering_and_gaps_are_detected() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    for index in range(4):
        audit(ToolCallEvent(tool_name="t", call_id=f"c{index}", **IDS))
    records = sink.read()

    edited = records[1].model_copy(update={"data": {**records[1].data, "tool_name": "other"}})
    result = verify_chain([records[0], edited, *records[2:]])
    assert not result.valid and result.seq == 1 and "modified" in (result.error or "")

    gap = verify_chain([records[0], records[2], records[3]])
    assert not gap.valid and gap.seq == 2 and "missing" in (gap.error or "")

    swapped = verify_chain([records[0], records[2], records[1], records[3]])
    assert not swapped.valid

    # Re-hashing an edited record does not help: the next record no longer links.
    rehashed = edited.model_copy(update={"hash": edited.compute_hash()})
    broken = verify_chain([records[0], rehashed, *records[2:]])
    assert not broken.valid and broken.seq == 2 and "link" in (broken.error or "")

    forged_start = records[0].model_copy(update={"prev_hash": "f" * 64})
    forged_start = forged_start.model_copy(update={"hash": forged_start.compute_hash()})
    assert not verify_chain([forged_start])


def test_segments_verify_against_an_expected_previous_hash() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    for index in range(3):
        audit(ToolCallEvent(tool_name="t", call_id=f"c{index}", **IDS))
    records = sink.read()

    assert verify_chain(records[1:], expected_prev_hash=records[0].hash).valid
    assert not verify_chain(records[2:], expected_prev_hash=records[0].hash).valid
    assert not verify_chain([{"seq": "x"}]).valid
    assert verify_chain([r.model_dump(mode="json") for r in records]).valid


def test_default_types_and_content_minimisation() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    audit(TextDeltaEvent(delta="hi", **IDS))  # not recorded by default
    audit(_started_audit_log("Refund order 7 for jane@example.com"))
    audit(DelegationEvent(target="b", task="secret task", correlation_id="d1", **IDS))

    started, delegation = sink.read()
    assert started.data["input_preview"] == "[omitted: 35 chars]"
    assert delegation.data["task"] == "[omitted: 11 chars]"
    assert delegation.data["target"] == "b"

    full = InMemoryAuditSink()
    AuditLog(full, include_content=True, redactor=RegexRedactor.pii())(
        _started_audit_log("Refund order 7 for jane@example.com")
    )
    assert full.records[0].data["input_preview"] == "Refund order 7 for [REDACTED:email]"

    only_errors = InMemoryAuditSink()
    AuditLog(only_errors, event_types={"error"})(_started_audit_log())
    assert only_errors.records == []


def test_reasons_notes_and_error_messages_are_kept_only_by_length() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)

    audit(ErrorEvent(error_type="ValueError", message="customer Jane lost", **IDS))
    audit(ApprovalDecidedEvent(tool="pay", decision="reject", note="Jane is a friend", **IDS))
    audit(GuardrailBlockedEvent(guardrail="pii", stage="output", reason="Jane's mail", **IDS))

    error, decided, blocked = sink.read()
    assert (
        error.data["message"] == "[omitted: 18 chars]" and error.data["error_type"] == "ValueError"
    )
    assert decided.data["note"] == "[omitted: 16 chars]" and decided.data["decision"] == "reject"
    assert blocked.data["reason"] == "[omitted: 11 chars]"


def test_tool_arguments_are_kept_but_redacted() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    audit(
        ToolCallEvent(
            tool_name="login",
            call_id="c",
            arguments={"user": "jane", "password": "hunter2", "note": "Bearer abcdefgh1234"},
            **IDS,
        )
    )

    arguments = sink.records[0].data["arguments"]
    assert arguments["user"] == "jane"
    assert arguments["password"] == "[REDACTED:secret]"
    assert arguments["note"] == "Bearer [REDACTED:bearer_token]"
    raw = InMemoryAuditSink()
    AuditLog(raw, redactor=None)(
        ToolCallEvent(tool_name="t", call_id="c", arguments={"password": "x"}, **IDS)
    )
    assert raw.records[0].data["arguments"]["password"] == "x"


def test_identity_comes_from_run_context_metadata() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    ctx: RunContext[None] = RunContext(
        run_id="run_1",
        agent="a",
        deps=None,
        metadata={
            ACTOR_METADATA_KEY: {"id": "alice", "tenant": "acme"},
            REQUEST_ID_METADATA_KEY: "req-1",
        },
    )
    with use_run_context(ctx):
        audit(_started_audit_log())
        audit(ApprovalDecidedEvent(tool="pay", decision="approve", reviewer="bob", **IDS))
    ctx.metadata[TENANT_METADATA_KEY] = "globex"
    with use_run_context(ctx):
        custom = audit.record("invoice_released", {"invoice": "INV-1", "token": "abc"})

    first, decided, _ = sink.read()
    assert (first.actor, first.tenant, first.request_id) == ("alice", "acme", "req-1")
    assert decided.actor == "bob"  # the reviewer made the decision
    assert custom.tenant == "globex"
    assert custom.run_id == "run_1"
    assert custom.data == {"invoice": "INV-1", "token": "[REDACTED:secret]"}
    assert verify_chain(sink.read()).valid


def test_the_same_event_object_is_recorded_once() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    event = _started_audit_log()
    audit(event)
    audit(event)
    assert len(sink.records) == 1


def test_a_failing_sink_does_not_advance_the_chain() -> None:
    class Flaky(InMemoryAuditSink):
        fail = True

        def write(self, record: AuditRecord) -> None:
            if self.fail:
                self.fail = False
                raise OSError("disk full")
            super().write(record)

    sink = Flaky()
    audit = AuditLog(sink)
    audit(_started_audit_log())  # logged, not raised
    audit(_started_audit_log())

    [record] = sink.records
    assert record.seq == 0 and record.prev_hash == GENESIS_HASH
    with pytest.raises(OSError):
        sink.fail = True
        audit.record("custom", {})


def test_jsonl_sink_persists_rotates_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "awa.jsonl"
    sink = JsonlAuditSink(path, max_bytes=900)
    audit = AuditLog(sink)
    for index in range(8):
        audit(ToolCallEvent(tool_name="t", call_id=f"c{index}", arguments={"i": index}, **IDS))

    files = sink.files()
    assert len(files) > 1
    assert files[-1] == path
    assert all(f.name.startswith("awa.") for f in files)
    records = sink.read()
    assert [r.seq for r in records] == list(range(8))
    assert verify_chain(records).valid
    first = list(read_audit_file(files[0]))
    assert first[0].seq == 0

    # A new log on the same file continues the chain.
    resumed = AuditLog(JsonlAuditSink(path, max_bytes=900))
    assert resumed.head == (7, records[-1].hash)
    resumed(_started_audit_log())
    assert verify_chain(JsonlAuditSink(path).read()).valid

    # Editing a stored line breaks verification.
    lines = files[0].read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["data"]["tool_name"] = "forged"
    lines[0] = json.dumps(record)
    files[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not verify_chain(JsonlAuditSink(path).read()).valid


def test_jsonl_sink_files_and_folder_are_made_for_the_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[tuple[str, int]] = []
    real_open, real_mkdir = os.open, Path.mkdir

    def spy_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        modes.append(("file", mode))
        return real_open(path, flags, mode, **kwargs)

    def spy_mkdir(self: Path, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        modes.append(("folder", mode))
        real_mkdir(self, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    previous = os.umask(0o022)
    try:
        AuditLog(JsonlAuditSink(tmp_path / "audit" / "awa.jsonl", fsync=False))(
            _started_audit_log()
        )
    finally:
        os.umask(previous)

    assert ("file", 0o600) in modes and ("folder", 0o700) in modes
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "audit").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "audit" / "awa.jsonl").stat().st_mode) == 0o600


def test_jsonl_sink_restricts_each_new_file_to_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted: list[Path] = []
    monkeypatch.setattr(audit_module, "restrict_to_owner", restricted.append)
    log = AuditLog(JsonlAuditSink(tmp_path / "awa.jsonl", fsync=False, max_bytes=1))

    log(_started_audit_log())
    log(_started_audit_log())

    assert restricted == [tmp_path / "awa.jsonl", tmp_path / "awa.jsonl"]
    assert (tmp_path / "awa.000001.jsonl").exists()


def test_jsonl_sink_on_a_new_file(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "a.jsonl", fsync=False)
    assert sink.last_record() is None
    assert sink.read() == []
    with pytest.raises(ValueError):
        JsonlAuditSink(tmp_path / "b.jsonl", max_bytes=0)


def test_a_log_resumes_a_file_that_ends_with_a_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(JsonlAuditSink(path, fsync=False))
    audit(ToolCallEvent(tool_name="t", call_id="c0", **IDS))
    audit(ToolCallEvent(tool_name="t", call_id="c1", **IDS))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("   \n")

    resumed = AuditLog(JsonlAuditSink(path, fsync=False))

    assert resumed.head == audit.head


def test_audit_node_feeds_the_chain_with_the_run_id() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    ctx: RunContext[None] = RunContext(run_id="run_9", agent="graph", deps=None)
    with use_run_context(ctx), audit_node("summarise", agent_name="writer", sink=audit):
        pass
    with pytest.raises(RuntimeError), audit_node("fail", sink=audit):
        raise RuntimeError("x")

    done, failed = sink.read()
    assert done.event_type == "node_completed"
    assert done.run_id == "run_9"
    assert done.agent == "writer"
    assert failed.event_type == "node_failed"
    assert failed.run_id is None
    assert verify_chain(sink.read()).valid


async def test_audit_log_as_hook_records_a_team_run() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    lead = Agent(
        "lead",
        description="Leads.",
        model=FakeChatModel([tool_call("delegate_task", {"agent": "a", "task": "x"}), "Done."]),
    )
    member = Agent("a", description="A.", model=FakeChatModel(["from a"]), hooks=[audit])
    await Team("desk", orchestrator=lead, members=[member], hooks=[audit]).run("Go")

    types = [record.event_type for record in sink.read()]
    assert types.count("agent_started") == 3
    assert "delegation" in types and "delegation_result" in types
    assert "tool_call" in types
    assert types[-1] == "agent_finished"
    assert len(types) == len({(r.run_id, r.event_type, r.data.get("call_id")) for r in sink.read()})
    assert verify_chain(sink.read()).valid
    started = next(r for r in sink.read() if r.agent == "a" and r.event_type == "agent_started")
    assert started.parent_run_id is not None


def test_a_log_whose_first_records_were_removed_is_not_valid() -> None:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink)
    for index in range(4):
        audit(ToolCallEvent(tool_name="t", call_id=f"c{index}", **IDS))
    records = sink.read()

    assert verify_chain(records).valid
    cut = verify_chain(records[2:])
    assert not cut.valid
    assert cut.seq == 2
    assert cut.error is not None
    assert "starts at record 2, not 0" in cut.error
    assert "expected_prev_hash" in cut.error


def _jsonl_with_four_records(tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(JsonlAuditSink(path, fsync=False))
    for index in range(4):
        audit(ToolCallEvent(tool_name="t", call_id=f"c{index}", **IDS))
    return path


def test_a_damaged_line_is_named_with_file_and_line(tmp_path: Path) -> None:
    path = _jsonl_with_four_records(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1][:20]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditChainError, match=r"audit\.jsonl.*line 2"):
        list(read_audit_file(path))
    with pytest.raises(AuditChainError, match="line 2"):
        JsonlAuditSink(path).read()

    result = verify_chain(JsonlAuditSink(path).read(tolerant=True))
    assert not result.valid
    assert result.checked == 1
    assert result.error is not None
    assert "line 2" in result.error


def test_a_torn_last_line_names_the_fix_at_start_up(tmp_path: Path) -> None:
    path = _jsonl_with_four_records(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:-30], encoding="utf-8")

    with pytest.raises(AuditChainError, match=r"line 4.*(remove|repair|move)"):
        AuditLog(JsonlAuditSink(path))


def _event(node_name: str, created_at: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt-{node_name}-{created_at.timestamp()}",
        event_type="node_completed",
        node_name=node_name,
        created_at=created_at,
    )


def test_sort_uses_declared_order_not_completion_order() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # `medical` finished *after* `insurance` in wall-clock time (a real race
    # between parallel branches), but the declared topology order still wins.
    insurance_finished_first = _event("insurance", t0)
    medical_finished_second = _event("medical", t0.replace(second=1))

    ordered = sort_audit_trail(
        [insurance_finished_first, medical_finished_second], declared_order=["medical", "insurance"]
    )

    assert [event.node_name for event in ordered] == ["medical", "insurance"]


def test_undeclared_nodes_sort_after_declared_ones_and_stay_stable() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    events = [_event("unknown_b", t0), _event("known", t0), _event("unknown_a", t0)]

    ordered = sort_audit_trail(events, declared_order=["known"])

    assert [event.node_name for event in ordered] == ["known", "unknown_b", "unknown_a"]


def test_same_node_keeps_relative_order_across_repeated_events() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    first_attempt = _event("writer", t0)
    retry_attempt = _event("writer", t0.replace(second=5))

    ordered = sort_audit_trail([retry_attempt, first_attempt], declared_order=["writer"])

    assert ordered == [retry_attempt, first_attempt]

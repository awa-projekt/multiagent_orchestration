"""Guardrails, and output checks in retry mode."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from _helpers.polling import TIMEOUT_FACTOR
from awa.agents import Agent
from awa.errors import ConfigurationError
from awa.guardrails import (
    BlocklistGuardrail,
    GuardrailResult,
    GuardrailTripwireError,
    LengthGuardrail,
    MustIncludeGuardrail,
    NonEmptyFieldsGuardrail,
    PIIGuardrail,
    SchemaGuardrail,
    retries_on_fail,
    run_guardrails,
    run_input_guardrails,
    run_output_guardrails,
)
from awa.kernel.redaction import PII_PATTERNS
from awa.testing import FakeChatModel, structured_output


class _Payload(BaseModel):
    title: str
    score: float


class _RevisionContext(BaseModel):
    task: str
    evidence: list[str] = []
    notes: str = ""


def test_length_guardrail_passes_within_bounds() -> None:
    guardrail = LengthGuardrail(min_length=3, max_length=10)

    result = guardrail.check("hello")

    assert result.passed is True


def test_length_guardrail_blocks_too_short() -> None:
    guardrail = LengthGuardrail(min_length=5)

    result = guardrail.check("hi")

    assert result.passed is False
    assert result.severity == "block"
    assert "below the minimum" in (result.reason or "")


def test_length_guardrail_blocks_too_long() -> None:
    guardrail = LengthGuardrail(max_length=3)

    result = guardrail.check("hello")

    assert result.passed is False
    assert "exceeds the maximum" in (result.reason or "")


def test_blocklist_guardrail_blocks_forbidden_term_case_insensitively() -> None:
    guardrail = BlocklistGuardrail(["forbidden"])

    result = guardrail.check("this text contains a FORBIDDEN word")

    assert result.passed is False
    assert "forbidden" in (result.reason or "")


def test_blocklist_guardrail_passes_clean_text() -> None:
    guardrail = BlocklistGuardrail(["forbidden"])

    result = guardrail.check("this text is clean")

    assert result.passed is True


def test_schema_guardrail_passes_valid_dict() -> None:
    guardrail = SchemaGuardrail(_Payload)

    result = guardrail.check({"title": "ok", "score": 1.0})

    assert result.passed is True


def test_schema_guardrail_blocks_invalid_dict() -> None:
    guardrail = SchemaGuardrail(_Payload)

    result = guardrail.check({"title": "ok", "score": "not-a-number-that-is-invalid"})

    assert result.passed is False
    assert result.severity == "block"


def test_run_guardrails_all_pass_returns_passing_result() -> None:
    result = run_guardrails(
        "a clean sentence", [LengthGuardrail(min_length=1), BlocklistGuardrail(["forbidden"])]
    )

    assert result.passed is True


def test_run_guardrails_returns_first_blocking_failure_in_declared_order() -> None:
    guardrails = [
        LengthGuardrail(min_length=1, name="length"),
        BlocklistGuardrail(["forbidden"], name="blocklist"),
    ]

    result = run_guardrails("forbidden", guardrails)

    assert result.passed is False
    assert result.guardrail_name == "blocklist"


def test_run_guardrails_stops_at_the_first_failure_not_the_last() -> None:
    guardrails = [
        BlocklistGuardrail(["forbidden"], name="first_blocklist"),
        BlocklistGuardrail(["another"], name="second_blocklist"),
    ]

    result = run_guardrails("forbidden and another", guardrails)

    assert result.guardrail_name == "first_blocklist"


def test_run_guardrails_prefers_block_severity_over_earlier_warning() -> None:
    class _WarnGuardrail:
        name = "warn_guard"

        def check(self, candidate: object) -> GuardrailResult:
            return GuardrailResult(passed=False, severity="warn", guardrail_name=self.name)

    guardrails = [_WarnGuardrail(), BlocklistGuardrail(["forbidden"], name="blocker")]

    result = run_guardrails("forbidden", guardrails)

    assert result.guardrail_name == "blocker"
    assert result.severity == "block"


def test_run_guardrails_returns_warning_when_nothing_blocks() -> None:
    class _WarnGuardrail:
        name = "warn_guard"

        def check(self, candidate: object) -> GuardrailResult:
            return GuardrailResult(passed=False, severity="warn", guardrail_name=self.name)

    result = run_guardrails("anything", [_WarnGuardrail()])

    assert result.passed is False
    assert result.severity == "warn"
    assert result.guardrail_name == "warn_guard"


def test_run_guardrails_empty_list_passes() -> None:
    result = run_guardrails("anything", [])

    assert result.passed is True


def test_run_guardrails_raise_on_block_raises_tripwire_error() -> None:
    guardrails = [BlocklistGuardrail(["forbidden"], name="blocker")]

    with pytest.raises(GuardrailTripwireError) as excinfo:
        run_guardrails("forbidden", guardrails, raise_on_block=True)

    assert excinfo.value.result.guardrail_name == "blocker"
    assert excinfo.value.result.passed is False


def test_run_guardrails_raise_on_block_does_not_raise_on_warning() -> None:
    class _WarnGuardrail:
        name = "warn_guard"

        def check(self, candidate: object) -> GuardrailResult:
            return GuardrailResult(passed=False, severity="warn", guardrail_name=self.name)

    result = run_guardrails("anything", [_WarnGuardrail()], raise_on_block=True)

    assert result.passed is False
    assert result.severity == "warn"


def test_run_guardrails_raise_on_block_false_by_default() -> None:
    guardrails = [BlocklistGuardrail(["forbidden"], name="blocker")]

    result = run_guardrails("forbidden", guardrails)

    assert isinstance(result, GuardrailResult)
    assert result.passed is False


def test_run_input_guardrails_tags_result_stage_input() -> None:
    result = run_input_guardrails("clean text", [LengthGuardrail(min_length=1)])

    assert result.passed is True
    assert result.stage == "input"


def test_run_output_guardrails_tags_result_stage_output() -> None:
    result = run_output_guardrails("clean text", [LengthGuardrail(min_length=1)])

    assert result.passed is True
    assert result.stage == "output"


def test_run_input_guardrails_raise_on_block_carries_input_stage() -> None:
    with pytest.raises(GuardrailTripwireError) as excinfo:
        run_input_guardrails("forbidden", [BlocklistGuardrail(["forbidden"])], raise_on_block=True)

    assert excinfo.value.result.stage == "input"
    assert excinfo.value.result.passed is False


def test_run_output_guardrails_raise_on_block_carries_output_stage() -> None:
    with pytest.raises(GuardrailTripwireError) as excinfo:
        run_output_guardrails("forbidden", [BlocklistGuardrail(["forbidden"])], raise_on_block=True)

    assert excinfo.value.result.stage == "output"
    assert excinfo.value.result.passed is False


def test_run_output_guardrails_does_not_raise_by_default() -> None:
    result = run_output_guardrails("forbidden", [BlocklistGuardrail(["forbidden"])])

    assert result.passed is False
    assert result.stage == "output"


def test_non_empty_fields_guardrail_passes_when_fields_are_populated() -> None:
    guardrail = NonEmptyFieldsGuardrail(["evidence"])
    context = _RevisionContext(task="t", evidence=["source 1"])

    result = guardrail.check(context)

    assert result.passed is True


def test_non_empty_fields_guardrail_blocks_an_empty_list_field() -> None:
    guardrail = NonEmptyFieldsGuardrail(["evidence"])
    context = _RevisionContext(task="t", evidence=[])

    result = guardrail.check(context)

    assert result.passed is False
    assert result.severity == "block"
    assert "evidence" in (result.reason or "")


def test_non_empty_fields_guardrail_blocks_an_empty_string_field() -> None:
    guardrail = NonEmptyFieldsGuardrail(["notes"])
    context = _RevisionContext(task="t", notes="")

    result = guardrail.check(context)

    assert result.passed is False
    assert "notes" in (result.reason or "")


def test_non_empty_fields_guardrail_does_not_flag_falsy_but_valid_values() -> None:
    class _NumericContext(BaseModel):
        score: float
        active: bool

    guardrail = NonEmptyFieldsGuardrail(["score", "active"])

    result = guardrail.check(_NumericContext(score=0.0, active=False))

    assert result.passed is True


def test_non_empty_fields_guardrail_blocks_a_field_not_on_the_model() -> None:
    guardrail = NonEmptyFieldsGuardrail(["does_not_exist"])
    context = _RevisionContext(task="t")

    result = guardrail.check(context)

    assert result.passed is False
    assert "not a field" in (result.reason or "")


def test_non_empty_fields_guardrail_blocks_a_non_basemodel_candidate() -> None:
    guardrail = NonEmptyFieldsGuardrail(["evidence"])

    result = guardrail.check({"evidence": ["source 1"]})

    assert result.passed is False
    assert "not a BaseModel" in (result.reason or "")


def test_non_empty_fields_guardrail_reports_every_problem_field_at_once() -> None:
    guardrail = NonEmptyFieldsGuardrail(["evidence", "notes"])
    context = _RevisionContext(task="t", evidence=[], notes="")

    result = guardrail.check(context)

    assert result.passed is False
    assert "evidence" in (result.reason or "")
    assert "notes" in (result.reason or "")


def test_non_empty_fields_guardrail_wires_into_run_output_guardrails() -> None:
    context = _RevisionContext(task="t", evidence=[])

    with pytest.raises(GuardrailTripwireError) as excinfo:
        run_output_guardrails(context, [NonEmptyFieldsGuardrail(["evidence"])], raise_on_block=True)

    assert excinfo.value.result.stage == "output"


# ------------------------------------------------------------------ normalised blocklist


@pytest.mark.parametrize(
    "candidate",
    [
        "my pass\u200bword is x",  # zero-width space inside the term
        "my \uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44 is x",  # fullwidth letters
        "my PASSWORD is x",
        "my pa\u00adssword is x",  # soft hyphen
        "the \ufb01nance report",  # ligature
        "MA\u00df",  # sharp s folds to "ss"
    ],
)
def test_blocklist_sees_through_invisible_and_compatibility_characters(candidate: str) -> None:
    terms = ["password", "finance", "mass"]

    assert BlocklistGuardrail(terms).check(candidate).blocked


def test_blocklist_normalises_its_terms_too() -> None:
    assert BlocklistGuardrail(["\uff53\uff45\uff43\uff52\uff45\uff54"]).check("a secret").blocked
    assert BlocklistGuardrail(["sec\u200bret"]).check("a secret").blocked
    assert BlocklistGuardrail(["secret"]).check("nothing here").passed


def test_blocklist_does_not_fold_look_alike_letters_of_another_script() -> None:
    assert BlocklistGuardrail(["password"]).check("p\u0430ssword").passed  # Cyrillic a


@pytest.mark.parametrize("terms", ["password", [""], ["ok", "\u200b"]])
def test_blocklist_refuses_a_bare_string_and_an_empty_term(terms: object) -> None:
    with pytest.raises(ConfigurationError):
        BlocklistGuardrail(terms)  # type: ignore[arg-type]


# ------------------------------------------------------------------ PII


PII_CASES = {
    "email": ("jane.doe@example.com", "jane.doe at example"),
    "iban": ("DE89 3704 0044 0532 0130 00", "DE89 3704 0044 0532 0130 01"),
    "credit_card": ("4111 1111 1111 1111", "4111 1111 1111 1112"),
    "ipv4": ("192.168.10.200", "192.168.10.300"),
    "ipv6": ("2001:db8:85a3::8a2e:370:7334", "2001:db8:85a3::8a2e:370:zzzz"),
    "phone": ("+49 151 23456789", "+49"),
}


@pytest.mark.parametrize("kind", sorted(PII_CASES))
def test_pii_kind_finds_the_value_but_not_the_near_miss(kind: str) -> None:
    value, near_miss = PII_CASES[kind]
    guardrail = PIIGuardrail(kinds={kind})

    assert guardrail.check(f"contact: {value}.").blocked
    assert guardrail.check(f"\u00bb{value}\u00ab").blocked  # surrounded by non-ASCII quotes
    assert guardrail.check(f"contact: {near_miss}.").passed


def test_pii_scans_numbers_and_bytes() -> None:
    guardrail = PIIGuardrail(kinds={"credit_card", "email"})

    assert guardrail.check({"card": 4111111111111111}).blocked
    assert guardrail.check({"mail": b"jane.doe@example.com"}).blocked
    assert guardrail.check({"count": 3, "flag": True}).passed
    redacting = PIIGuardrail(mode="redact", kinds={"credit_card"})
    result = redacting.check({"card": 4111111111111111})
    assert result.blocked and "cannot be redacted" in (result.reason or "")


def test_pii_cases_cover_every_built_in_kind() -> None:
    assert set(PII_CASES) == {pattern.kind for pattern in PII_PATTERNS}


def test_pii_sees_through_invisible_characters() -> None:
    result = PIIGuardrail(kinds={"email"}).check("write to jane@exa\u200bmple.com please")

    assert result.blocked and "email (1)" in (result.reason or "")


def test_pii_scans_and_redacts_mapping_keys() -> None:
    guardrail = PIIGuardrail(mode="redact", kinds={"email"})

    result = guardrail.check({"jane@example.com": "ok", "other@example.com": "ok", "note": "x"})

    assert result.passed and "email (2)" in (result.reason or "")
    assert result.replacement == {"[REDACTED:email]": "ok", "[REDACTED:email]#": "ok", "note": "x"}


def test_pii_redacts_nested_keys_but_not_the_field_names_of_a_model() -> None:
    guardrail = PIIGuardrail(mode="redact", kinds={"email"})

    nested = guardrail.check({"rows": [{"jane@example.com": 1}]})
    model = guardrail.check(_Payload(title="mail jane@example.com", score=1.0))

    assert nested.replacement == {"rows": [{"[REDACTED:email]": 1}]}
    assert model.replacement == _Payload(title="mail [REDACTED:email]", score=1.0)


@pytest.mark.parametrize("candidate", [("jane@example.com", "x"), {"jane@example.com"}])
def test_pii_checks_tuple_and_set_candidates(candidate: object) -> None:
    assert PIIGuardrail(kinds={"email"}).check(candidate).blocked


def test_pii_scans_a_megabyte_quickly() -> None:
    import time

    text = ("lorem ipsum dolor sit amet, order 42 shipped. " * 30_000)[:1_000_000]
    started = time.perf_counter()

    result = PIIGuardrail().check(text)

    assert result.passed
    assert time.perf_counter() - started < 2.0 * TIMEOUT_FACTOR


# ------------------------------------------------------------------ schema reasons


class _Item(BaseModel):
    name: str
    qty: int


class _Order(BaseModel):
    id: int
    items: list[_Item]


def test_schema_reason_names_error_types_and_known_fields_only() -> None:
    result = SchemaGuardrail(_Order).check({"id": "x", "items": [{"name": 1}], "secret@x.io": 1})

    reason = result.reason or ""
    assert result.blocked
    assert "id: int_parsing" in reason
    assert "items.0.qty: missing" in reason
    assert "items.0.name: string_type" in reason
    assert "secret" not in reason


def test_schema_reason_hides_keys_the_candidate_controls() -> None:
    class Open(BaseModel):
        tags: dict[str, int]

    result = SchemaGuardrail(Open).check({"tags": {"jane@example.com": "x"}})

    assert "jane" not in (result.reason or "")
    assert "tags.<field>" in (result.reason or "")


# ------------------------------------------------------------------ runners


class _Raises:
    name = "raises"

    def check(self, candidate: object) -> GuardrailResult:
        raise RuntimeError("leaks jane@example.com")


class _Redacts:
    name = "redacts"

    def check(self, candidate: object) -> GuardrailResult:
        return GuardrailResult(
            passed=True, guardrail_name=self.name, replacement=str(candidate).upper()
        )


class _Blocks:
    name = "blocks"

    def check(self, candidate: object) -> GuardrailResult:
        return GuardrailResult(passed=False, reason="no", guardrail_name=self.name)


def test_a_guardrail_that_raises_blocks_without_leaking_its_message() -> None:
    result = run_guardrails("x", [_Raises()])

    assert result.blocked and result.guardrail_name == "raises"
    assert result.reason == "the check raised RuntimeError"
    with pytest.raises(GuardrailTripwireError, match="RuntimeError"):
        run_guardrails("x", [_Raises()], raise_on_block=True)


def test_a_replacement_followed_by_a_block_is_a_block() -> None:
    result = run_guardrails("abc", [_Redacts(), _Blocks()])

    assert result.blocked and result.replacement is None


def test_the_block_event_is_redacted_truncated_and_names_the_tool_stage() -> None:
    from awa.contracts import GuardrailBlockedEvent
    from awa.runtime import RunContext, use_run_context

    class Verbose:
        name = "verbose"

        def check(self, candidate: object) -> GuardrailResult:
            reason = "token sk-ant-api03-" + "a" * 40 + " " + "x" * 1000
            return GuardrailResult(passed=False, reason=reason, guardrail_name=self.name)

    ctx: RunContext[None] = RunContext(run_id="r", agent="a", deps=None)
    with use_run_context(ctx):
        run_output_guardrails("c", [Verbose()], tool="lookup")

    [event] = ctx.events._pending
    assert isinstance(event, GuardrailBlockedEvent)
    assert event.stage == "tool" and event.tool == "lookup"
    assert "sk-ant-api03" not in event.reason and len(event.reason) <= 300


class Report(BaseModel):
    title: str
    items: list[str]


def test_contract_checks_retry_by_default_and_safety_checks_block() -> None:
    assert retries_on_fail(MustIncludeGuardrail(["A"]))
    assert retries_on_fail(SchemaGuardrail(Report))
    assert retries_on_fail(NonEmptyFieldsGuardrail(["items"]))
    assert not retries_on_fail(BlocklistGuardrail(["secret"]))
    assert not retries_on_fail(MustIncludeGuardrail(["A"], on_fail="block"))
    with pytest.raises(ValueError, match="on_fail"):
        MustIncludeGuardrail(["A"], on_fail="later")  # type: ignore[arg-type]


def test_must_include_names_the_missing_terms() -> None:
    guardrail = MustIncludeGuardrail(["STATUS:", "Ticket"])

    assert guardrail.check("status: ok, ticket T-1").passed
    result = guardrail.check("all fine")
    assert result.blocked
    assert "'STATUS:'" in (result.reason or "") and "'Ticket'" in (result.reason or "")
    assert not MustIncludeGuardrail(["STATUS:"], case_sensitive=True).check("status:").passed


async def test_a_failed_check_is_sent_back_and_the_fixed_answer_passes() -> None:
    model = FakeChatModel(["no marker here", "STATUS: done"])
    agent = Agent("a", model=model, output_guardrails=[MustIncludeGuardrail(["STATUS:"])])

    result = await agent.run("Do it")

    assert result.output == "STATUS: done"
    retry = model.calls[1].messages[-1]
    assert "must_include" in str(retry.content)
    assert "'STATUS:'" in str(retry.content)


async def test_after_the_last_retry_the_check_blocks() -> None:
    model = FakeChatModel(["one", "two", "three"])
    agent = Agent(
        "a", model=model, output_guardrails=[MustIncludeGuardrail(["STATUS:"])], output_retries=2
    )

    with pytest.raises(GuardrailTripwireError, match="must_include"):
        await agent.run("Do it")
    assert len(model.calls) == 3


async def test_a_block_mode_check_never_reaches_the_model_again() -> None:
    model = FakeChatModel(["a secret"])
    agent = Agent("a", model=model, output_guardrails=[BlocklistGuardrail(["secret"])])

    with pytest.raises(GuardrailTripwireError):
        await agent.run("Do it")
    assert len(model.calls) == 1


async def test_a_structured_answer_is_checked_and_retried() -> None:
    model = FakeChatModel(
        [
            structured_output(Report, {"title": "t", "items": []}),
            structured_output(Report, {"title": "t", "items": ["x"]}),
        ]
    )
    agent = Agent(
        "a", model=model, output_type=Report, output_guardrails=[NonEmptyFieldsGuardrail(["items"])]
    )

    result = await agent.run("Report")

    assert result.output == Report(title="t", items=["x"])
    assert len(model.calls) == 2


def test_output_retries_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="output_retries"):
        Agent("a", model=FakeChatModel(["x"]), output_retries=-1)


async def test_concurrent_runs_of_one_agent_have_their_own_retry_budget() -> None:
    import asyncio

    from awa.testing import FunctionChatModel, ModelCall

    def answer(call: ModelCall) -> str:
        # Each run fails the check once, then fixes its answer.
        prompt = str(call.messages[1].content) if len(call.messages) > 1 else ""
        retried = "Your answer failed" in str(call.last_message.content)
        return f"STATUS: {prompt}" if retried else "no marker"

    agent = Agent(
        "a",
        model=FunctionChatModel(answer),
        output_guardrails=[MustIncludeGuardrail(["STATUS:"])],
        output_retries=1,
    )

    results = await asyncio.gather(*(agent.run(f"job {i}") for i in range(4)))

    assert all(r.output.startswith("STATUS:") for r in results)


async def test_the_retry_count_is_kept_in_the_state_not_on_the_middleware() -> None:
    from langchain_core.messages import AIMessage

    from awa.agents._stack import _GuardrailCheck
    from awa.kernel.output import RETRY_KEY, OutputRejected, OutputValidatorMiddleware

    middleware = OutputValidatorMiddleware(
        [_GuardrailCheck(MustIncludeGuardrail(["STATUS:"]))], max_retries=1
    )
    first = {"messages": [AIMessage("no marker", id="a1")]}
    second = {"messages": [AIMessage("no marker", id="a2")]}

    # Two runs outside any run context: each has its own budget of one retry.
    update_first = await middleware.aafter_agent(first, None)
    update_second = await middleware.aafter_agent(second, None)
    assert update_first is not None and update_first["jump_to"] == "model"
    assert update_second is not None and update_second["jump_to"] == "model"
    assert update_first[RETRY_KEY] == 1
    # The budget is used up once the state says so.
    spent = {"messages": [AIMessage("no marker")], RETRY_KEY: 1}
    with pytest.raises(OutputRejected):
        await middleware.aafter_agent(spent, None)


async def test_a_resumed_run_continues_the_retry_count() -> None:
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from awa.testing import tool_call
    from awa.tools import create_tool_approval_wrapper

    @tool
    def refund(amount: int) -> str:
        """Refund an amount."""

        return f"refunded {amount}"

    saver = InMemorySaver()
    guardrails = [MustIncludeGuardrail(["STATUS:"])]
    tools = [create_tool_approval_wrapper(refund)]
    before = Agent(
        "a",
        model=FakeChatModel(["no marker", tool_call("refund", {"amount": 5})]),
        tools=tools,
        output_guardrails=guardrails,
        output_retries=2,
        checkpointer=saver,
    )
    paused = await before.run("refund 5")
    assert paused.is_interrupted and paused.thread_id is not None

    # A fresh agent (another process) resumes: one retry was used, one is left.
    after_model = FakeChatModel(["still no marker", "again no marker"])
    after = Agent(
        "a",
        model=after_model,
        tools=tools,
        output_guardrails=guardrails,
        output_retries=2,
        checkpointer=saver,
    )
    with pytest.raises(GuardrailTripwireError, match="must_include"):
        await after.resume(paused.thread_id, {"action": "approve"})
    assert len(after_model.calls) == 2

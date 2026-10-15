"""The Try it page in plain words: a run's events as a timeline, answers and approvals as cards
(the raw JSON stays available as `details`)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from awa._internal.logging import get_logger
from awa._internal.upstream.messages import AIMessage, ToolMessage
from awa._internal.util import preview
from awa.contracts.events import (
    DelegationEvent,
    HandoffEvent,
    RunCompletedEvent,
    RunEvent,
    StepEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    rejection_note,
)
from awa.contracts.results import RunResult
from awa.server._serialize import jsonable, sse_frame
from awa.ui._plain import describe_call, format_value, humanize, view_of

if TYPE_CHECKING:
    from awa.ui._project import Project
    from awa.ui._tools import ToolInfo

_log = get_logger("ui")
_PREVIEW = 600
_COORDINATION_TOOLS = frozenset(
    {"delegate_task", "consult_peer", "check_delegations", "wait_for_delegations"}
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def text_of(value: Any) -> str:
    """`value` as text: a string as it is, anything else as indented JSON."""

    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(jsonable(value), ensure_ascii=False, indent=2)


def clip(value: Any, limit: int = _PREVIEW) -> str:
    """`value` as text of at most `limit` characters."""

    return preview(text_of(value), limit, ellipsis="…")


def _redacted_tree(value: Any, redact: Callable[[str], str]) -> Any:
    """`value` with every key and text passed through `redact`."""

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(str(key)): _redacted_tree(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_tree(item, redact) for item in value]
    return value


def structured(output: Any) -> dict[str, Any] | list[Any] | None:
    """The output as data when it is structured: a model, a dict or list, or text that is JSON."""

    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    if isinstance(output, str):
        text = output.strip()
        fenced = _FENCE.match(text)
        text = fenced.group(1) if fenced else text
        if not text.startswith(("{", "[")):
            return None
        try:
            output = json.loads(text)
        except ValueError:
            return None
    found = jsonable(output)
    return found if isinstance(found, dict | list) and found else None


def _is_coordination(tool: str) -> bool:
    return tool in _COORDINATION_TOOLS or tool.startswith("transfer_to_")


class Narrator:
    """Tells one run in plain words (`delta`, `activity`, `done`, `approval` frames)."""

    def __init__(self, project: Project) -> None:
        self._project = project
        self._tools: dict[str, ToolInfo] = project.tools()

    def _label(self, tool: str) -> tuple[str, dict[str, Any]]:
        info = self._tools.get(tool)
        return (info.label, dict(info.fields)) if info else (humanize(tool), {})

    def frame(self, event: RunEvent) -> bytes | None:
        """The frame for one run event, or `None` when the page shows nothing for it."""

        if isinstance(event, TextDeltaEvent):
            return sse_frame("delta", {"text": event.delta}) if event.depth == 0 else None
        if isinstance(event, ToolCallEvent):
            return self._call(event.call_id, event.agent, event.tool_name, event.arguments)
        if isinstance(event, ToolResultEvent):
            return self._done(event.call_id, event.tool_name, event.content, event.is_error)
        if isinstance(event, DelegationEvent):
            task = self._project.redacted(clip(event.task, 120))
            return self._note(f"{humanize(event.agent)} asked {humanize(event.target)}: {task}")
        if isinstance(event, HandoffEvent):
            return self._note(f"{humanize(event.source)} handed over to {humanize(event.target)}")
        if isinstance(event, StepEvent) and event.status != "started":
            return self._step(event)
        return None

    def _step(self, event: StepEvent) -> bytes:
        """One sentence for a step that is being checked, waits for a person, passed, failed, is
        tried again, was skipped or turned the item down."""

        title = event.title or humanize(event.step)
        words = {
            "checking": "is being checked",
            "needs_approval": "waits for a person to decide",
            "checked": "passed its checks",
            "failed": "failed a check",
            "retrying": "is tried again",
            "skipped": "was skipped",
            "rejected": "turned the item down",
        }
        text = f"Step {event.index + 1} of {event.total}, {title} ({humanize(event.agent)}), "
        text += words.get(event.status, event.status)
        if event.attempt is not None and event.status in ("checking", "retrying", "failed"):
            text += f" (try {event.attempt})"
        if event.status in ("failed", "retrying", "skipped", "rejected") and event.message:
            text += f": {self._project.redacted(clip(event.message, 200))}"
        return self._note(text)

    def _note(self, text: str) -> bytes:
        return sse_frame("activity", {"text": text, "done": True})

    def _call(self, call_id: str, agent: str, tool: str, arguments: dict[str, Any]) -> bytes | None:
        if _is_coordination(tool):
            return None
        label, fields = self._label(tool)
        safe = jsonable(arguments)
        redact = self._project.redacted
        data = {
            "id": call_id,
            "agent": humanize(agent),
            "tool": tool,
            "text": redact(describe_call(label, safe, fields)),
            "details": redact(clip(safe, 2000)),
        }
        return sse_frame("activity", data)

    def _done(self, call_id: str, tool: str, content: str, failed: bool) -> bytes | None:
        if _is_coordination(tool):
            return None
        note = rejection_note(content)
        data = {
            "id": call_id,
            "tool": tool,
            "ok": not failed and note is None,
            "rejected": note is not None,
            "result": self._project.redacted(clip(content if note is None else note)),
        }
        return sse_frame("activity_done", data)

    def replay(self, result: RunResult[Any]) -> Iterator[bytes]:
        """The tool calls a run made without streaming (a run continued after an approval)."""

        for message in result.new_messages():
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    args = call.get("args", {})
                    frame = self._call(str(call.get("id", "")), result.agent, call["name"], args)
                    if frame:
                        yield frame
            elif isinstance(message, ToolMessage):
                failed = getattr(message, "status", "success") == "error"
                content = text_of(message.content)
                frame = self._done(message.tool_call_id, message.name or "", content, failed)
                if frame:
                    yield frame

    def outcome(self, result: RunResult[Any]) -> bytes:
        """The final frame of a run: `approval` when it waits for a decision, else `done`."""

        if not result.is_interrupted:
            data: dict[str, Any] = {
                "thread_id": result.thread_id,
                "agent": result.agent,
                "text": self._project.redacted(text_of(result.output)),
            }
            found = structured(result.output)
            if found is not None:
                data["view"] = view_of(_redacted_tree(found, self._project.redacted))
            return sse_frame("done", data)
        pending = [self.card(item["value"], str(item.get("id", ""))) for item in result.interrupts]
        return sse_frame("approval", {"thread_id": result.thread_id, "pending": pending})

    def _step_card(self, call: dict[str, Any], item_id: str) -> dict[str, Any]:
        """The card of a step approval: it names the step and the agent, shows the step's result
        and why a person is asked."""

        redact = self._project.redacted
        step = str(call.get("step", ""))
        title = str(call.get("title") or humanize(step))
        agent = str(call.get("agent", ""))
        output = call.get("output")
        who = ""
        if call.get("by"):
            who = f" Who decides: {', '.join(str(role) for role in call['by'])}."
        reason = str(call.get("reason") or "A check of this step asks a person to decide.")
        return {
            "id": item_id,
            "tool": str(call.get("tool", f"step:{step}")),
            "label": f"Step '{title}' ({humanize(agent)})",
            "summary": redact(
                f"Step '{title}' of the process has a result, made by {humanize(agent)}. "
                "The process continues with the next step only if you approve."
            ),
            "fields": _step_card_rows(output, redact),
            "why": redact(f"{reason}{who}"),
            "args": redact(clip(output, 2000)),
            "step": step,
            "agent": agent,
            "kind": "step_approval",
        }

    def card(self, value: Any, item_id: str = "") -> dict[str, Any]:
        """One open approval (`value`: the paused call's `tool` and `args`) and why it waits."""

        call = value if isinstance(value, dict) else {"request": value}
        if call.get("kind") == "step_approval":
            return self._step_card(call, item_id)
        tool = str(call.get("tool", "an action"))
        asked = call.get("args", call)
        args = asked if isinstance(asked, dict) else {"request": asked}
        label, meta = self._label(tool)
        redact = self._project.redacted
        rows = []
        for name, argument in args.items():
            hint = meta.get(name, {})
            description = str(hint.get("description", ""))
            shown = format_value(str(name), argument, description=description, siblings=args)
            rows.append(
                {
                    "name": str(name),
                    "title": hint.get("title") or humanize(str(name)),
                    "description": description,
                    "value": redact(shown),
                }
            )
        info = self._tools.get(tool)
        return {
            "id": item_id,
            "tool": tool,
            "label": label,
            "summary": redact(describe_call(label, args, meta, listed=False)),
            "fields": rows,
            "why": self._project.approval_reason(tool, info),
            "args": redact(clip(asked, 2000)),
        }


def _step_card_rows(output: Any, redact: Any) -> list[dict[str, Any]]:
    shown = output if isinstance(output, dict) else {"result": output}
    return [
        {
            "name": str(name),
            "title": humanize(str(name)),
            "description": "",
            "value": redact(format_value(str(name), value, description="", siblings=shown)),
        }
        for name, value in shown.items()
    ]


def explained(project: Project, exc: BaseException, action: str) -> dict[str, str]:
    """`exc` explained for the page (`message`, `fix`, and when there is one a `link`, its
    `link_text` and `detail`), logged as a failed `action`; credentials are removed."""

    found = project.explain(exc)
    _log.warning("ui %s failed: %s", action, found.message)
    data = {"message": found.message, "fix": found.fix}
    for name in ("link", "link_text", "detail"):
        if value := getattr(found, name):
            data[name] = value
    return data


class Conversation:
    """The runs of the Try it page; a decision sent twice (a double click) runs once."""

    def __init__(self, project: Project) -> None:
        self._project = project
        self._deciding: set[str] = set()

    def _limits(self) -> dict[str, Any]:
        """The blueprint's `limits:` as the `usage_limits` option of a run (empty without)."""

        limits = self._project.usage_limits()
        return {} if limits is None else {"usage_limits": limits}

    async def chat(self, message: str, thread_id: str) -> AsyncIterator[bytes]:
        """Run `message` on the thread and yield the page's frames (an `error` frame, never an
        exception, when the run fails)."""

        project = self._project
        try:
            system = project.system()
            narrator = Narrator(project)
            stream = getattr(system, "stream", None)
            if not callable(stream):
                result = await system.run(message, thread_id=thread_id, **self._limits())
                yield narrator.outcome(result)
                return
            async for event in stream(message, thread_id=thread_id, **self._limits()):
                if isinstance(event, RunCompletedEvent) and event.depth == 0:
                    yield narrator.outcome(event.result)
                    return
                frame = narrator.frame(event)
                if frame is not None:
                    yield frame
        except Exception as exc:  # noqa: BLE001 - shown in the conversation with the fix
            yield sse_frame("error", explained(project, exc, "run"))

    def claim(self, thread_id: str) -> bool:
        """Reserve the decision on `thread_id`; `False` when one is already on its way."""

        if thread_id in self._deciding:
            return False
        self._deciding.add(thread_id)
        return True

    async def decide(
        self, thread_id: str, decisions: Sequence[tuple[str, bool, str]]
    ) -> AsyncIterator[bytes]:
        """Continue a paused run with `(interrupt id, approve, note)` decisions; the reservation
        of `claim` ends with the stream."""

        project = self._project
        answers = {
            item: {"action": "approve"}
            if approve
            else {"action": "reject", "note": note or "Rejected by the operator."}
            for item, approve, note in decisions
        }
        decision: Any = next(iter(answers.values())) if len(answers) == 1 else answers
        try:
            resume = getattr(project.system(), "resume", None)
            if not callable(resume):
                raise RuntimeError("This system cannot continue after an approval")
            result = await resume(thread_id, decision, **self._limits())
            narrator = Narrator(project)
            for frame in narrator.replay(result):
                yield frame
            yield narrator.outcome(result)
        except Exception as exc:  # noqa: BLE001 - shown in the conversation with the fix
            yield sse_frame("error", explained(project, exc, "run"))
        finally:
            self._deciding.discard(thread_id)

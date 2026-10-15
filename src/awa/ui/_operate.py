"""Use it: the triggers in words, recent runs, health, "Keep it running" and a run on a sample."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.util import to_timedelta
from awa.automation import SqliteRunStore
from awa.automation._time import utcnow
from awa.automation.cron import CronExpression
from awa.automation.models import TERMINAL_STATUSES, ProcessRun
from awa.automation.store import hold_triggers
from awa.errors import ConfigurationError, InvalidCronError, ProcessRunStateError
from awa.ui._plain import humanize

if TYPE_CHECKING:
    from awa.automation.automation import Automation
    from awa.blueprints._automation import TriggerBlueprint
    from awa.ui._project import Project

CHECKPOINTS_FILE = "checkpoints.db"
_RECENT = 20
_SUMMARY = 240
_STATUS = {
    "queued": ("Waiting to start", "warning"),
    "running": ("Running now", "warning"),
    "waiting": ("Waiting for a decision", "warning"),
    "succeeded": ("Done", "ready"),
    "failed": ("Failed", "missing"),
    "dead_lettered": ("Failed after every retry", "missing"),
    "cancelled": ("Stopped by a person", "warning"),
    "rejected": ("Turned down by a special case or a person", "warning"),
}
_DAYS = {
    "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
    "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
}  # fmt: skip
_NUMBERED = dict(zip("1234567", _DAYS, strict=True)) | {"0": "SUN"}
_PAYLOAD_FIELD = re.compile(r"\{payload\.([A-Za-z_]\w*)")
_SAMPLE_FIELDS = {"file_drop": ("name", "text"), "mailbox": ("from", "subject", "text")}
_UNITS = ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second"))


def _every(seconds: float) -> str:
    """`900` as `Every 15 minutes`."""

    for size, unit in _UNITS:
        if seconds >= size and seconds % size == 0:
            count = int(seconds // size)
            return f"Every {unit}" if count == 1 else f"Every {count} {unit}s"
    return f"Every {seconds:g} seconds"


def cron_words(expression: str, timezone: str | None = None) -> str:
    """A cron expression as a sentence: `0 6 * * MON-FRI` as `Every weekday at 06:00`.

    What it cannot phrase comes back as `On the schedule "<expression>"`; it never raises.

    Example:
        ```python
        cron_words("*/15 * * * *")  # 'Every 15 minutes'
        cron_words("0 8,18 * * MON-FRI")  # 'Every weekday at 08:00 and 18:00'
        ```
    """

    text = " ".join(str(expression).split())
    zone = f" ({timezone})" if timezone else ""
    unknown = f'On the schedule "{text}"{zone}'
    try:
        CronExpression(text)
    except InvalidCronError:
        return unknown
    fields = text.split(" ")
    if len(fields) != 5 or fields[2:4] != ["*", "*"]:
        return unknown
    minute, hour, _, _, weekday = fields
    days = _days(weekday)
    if not days:
        return unknown
    spoken = _times_of(minute, hour)
    if spoken is None:
        return unknown
    if spoken[0] == "every":
        return (
            spoken[1] if weekday == "*" else f"{days}, {spoken[1][0].lower()}{spoken[1][1:]}"
        ) + zone
    return f"{days} at {spoken[1]}{zone}"


def _times_of(minute: str, hour: str) -> tuple[str, str] | None:
    """The minute and hour fields as `("at", "06:00")` or `("every", "Every 15 minutes")`;
    `None` when they are not a minute step, a whole hour or a list of daily times."""

    step = re.fullmatch(r"\*/(\d+)", minute)
    if hour == "*" and (minute == "*" or (step and 60 % int(step.group(1)) == 0)):
        size = int(step.group(1)) if step else 1
        return "every", "Every minute" if size == 1 else f"Every {size} minutes"
    if not (minute.isdigit() and int(minute) < 60):
        return None
    past = int(minute)
    if hour == "*":
        return "every", ("Every hour" if past == 0 else f"Every hour at {past} minutes past")
    if all(item.isdigit() and int(item) < 24 for item in hour.split(",")):
        times = [f"{int(item):02d}:{past:02d}" for item in hour.split(",")]
        return "at", " and ".join([", ".join(times[:-1]), times[-1]] if len(times) > 2 else times)
    return None


def _days(field: str) -> str:
    """The weekday field as `Every day`, `Every weekday` or `Every Monday and Friday` (`""` if odd)."""

    if field == "*":
        return "Every day"
    names = []
    for part in field.upper().split(","):
        low, _, high = part.partition("-")
        low, high = _NUMBERED.get(low, low), _NUMBERED.get(high, high)
        if low not in _DAYS or (high and high not in _DAYS):
            return ""
        order = list(_DAYS)
        names += order[order.index(low) : order.index(high or low) + 1]
    if not names:
        return ""
    if names == list(_DAYS)[:5]:
        return "Every weekday"
    if names == list(_DAYS):
        return "Every day"
    shown = [_DAYS[n] for n in dict.fromkeys(names)]
    return "Every " + " and ".join([", ".join(shown[:-1]), shown[-1]] if len(shown) > 2 else shown)


def trigger_words(trigger: TriggerBlueprint, automation: str) -> str:
    """What starts a run, as a sentence."""

    if trigger.schedule is not None:
        schedule = trigger.schedule
        if schedule.cron:
            return cron_words(schedule.cron, schedule.timezone)
        seconds = to_timedelta(schedule.every, what="every")
        return _every(seconds.total_seconds() if seconds else 0)
    if trigger.webhook is not None:
        return (
            "When another system sends a signed web request to "
            f"/v1/automations/{automation}/webhooks/{trigger.name}."
        )
    if trigger.file_drop is not None:
        drop = trigger.file_drop
        return f"When a file ({drop.pattern}) is put into the folder {drop.directory}."
    if trigger.mailbox is not None:
        return f"When an email arrives in {trigger.mailbox.folder} on {trigger.mailbox.host}."
    assert trigger.queue is not None  # exactly one source is set
    return f"When a message arrives on the queue {trigger.queue.name}."


def sample_fields(trigger: TriggerBlueprint, default_input: str) -> list[dict[str, str]]:
    """The values a sample run asks for: the fields the trigger's input names, else the usual
    fields of its kind, else one `message`. A schedule asks for nothing (it supplies the date)."""

    if trigger.schedule is not None:
        return []
    named = list(dict.fromkeys(_PAYLOAD_FIELD.findall(trigger.input or default_input)))
    names = named or list(_SAMPLE_FIELDS.get(trigger.kind, ())) or ["message"]
    return [{"name": name, "label": humanize(name)} for name in names]


def status_words(status: str) -> tuple[str, str]:
    """`(text, chip class)` of a run status."""

    return _STATUS.get(status, (status, "warning"))


def run_view(run: ProcessRun, project: Project) -> dict[str, Any]:
    """One run in plain words: its status, when, what started it, and what came of it."""

    text, chip = status_words(run.status)
    summary = ""
    fix = ""
    if run.error:
        summary = project.redacted(run.error)[:_SUMMARY]
        fix = project.explain(RuntimeError(run.error)).fix
    elif run.output:
        summary = project.redacted(run.output)[:_SUMMARY]
    return {
        "id": run.id,
        "trigger": run.trigger or "",
        "status": run.status,
        "status_text": text,
        "chip": chip,
        "created": run.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
        "finished": run.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if run.status in TERMINAL_STATUSES
        else "",
        "summary": summary,
        "fix": fix,
        "open_approvals": sum(1 for w in run.waits if not w.answered and w.kind == "approval"),
        "attempts": run.attempts,
    }


@asynccontextmanager
async def local_runs(runs_file: Path) -> AsyncIterator[SqliteRunStore]:
    """The run store of the project's local data file, closed afterwards."""

    store = SqliteRunStore(runs_file)
    try:
        yield store
    finally:
        await store.close()


def worker_alive(runs_file: Path) -> bool:
    """Whether an `awa serve` or `awa worker` runs the triggers of the local run store: the
    lock it holds on `<store>.triggers` cannot be taken by this process."""

    try:
        with hold_triggers(runs_file):
            return False
    except ProcessRunStateError:
        return True


class Operation:
    """Long-term operation of one project: triggers, runs, health and runs on a sample."""

    def __init__(self, project: Project) -> None:
        self._project = project

    def triggers(self) -> list[dict[str, Any]]:
        """Each trigger of the blueprint: its name, kind, a sentence, and the sample it takes."""

        project = self._project
        if not project.blueprint_file.is_file():
            return []
        blueprint = project.blueprint()
        section = blueprint.automation_section()
        if section is None:
            return []
        name = section.name or blueprint.name
        return [
            {
                "name": item.name,
                "kind": item.kind.replace("_", "-"),
                "what": trigger_words(item, name),
                "enabled": item.enabled,
                "input": item.input or section.input,
                "sample": sample_fields(item, section.input),
            }
            for item in section.triggers
        ]

    def keep_running(self) -> dict[str, Any]:
        """The "Keep it running" panel: the one command, where the state lives, what is kept after a
        restart and how to see problems."""

        project = self._project
        webhook = any(t["kind"] == "webhook" for t in self.triggers())
        command = "serve" if webhook else "worker"
        runs = project.runs_file.relative_to(project.directory).as_posix()
        return {
            "command": f"awa {command} blueprint.yaml",
            "why": (
                "It receives web requests and runs everything"
                if webhook
                else "It watches the triggers and runs the process for each event"
            ),  # fmt: skip
            "where": [
                {"path": runs, "text": "every run, its status and its history"},
                {
                    "path": (Path(runs).parent / CHECKPOINTS_FILE).as_posix(),
                    "text": "runs that wait for an approval, so they continue after a restart",
                },
                {"path": ".env", "text": "your keys and addresses (never part of the download)"},
                {"path": "blueprint.yaml", "text": "the design and its triggers"},
            ],
            "restart": [
                "Every run is written to the data file before its work starts, so a restart loses nothing.",
                "Work that was running when the computer stopped is tried again.",
                "Runs that wait for an approval stay paused until someone decides.",
                "Schedules that were missed while it was off are caught up (the latest tick).",
                "Stop it with Ctrl+C: running work gets 30 seconds to finish, the rest goes back to the queue.",
            ],
            "problems": [
                {"title": "Failed runs", "command": "awa runs list --status failed"},
                {"title": "Runs that failed every retry", "command": "awa runs dlq"},
                {"title": "What one run did, step by step", "command": "awa runs trace RUN_ID"},
                {"title": "Start a failed run again", "command": "awa runs retry RUN_ID"},
            ],
            "one_each": "Run only one of awa serve and awa worker per project folder: the second refuses to start.",
        }

    async def overview(self) -> dict[str, Any]:
        """The recent runs (newest first) and a health reading."""

        project = self._project
        triggers = self.triggers()
        runs: list[ProcessRun] = []
        if project.runs_file.is_file():
            async with local_runs(project.runs_file) as store:
                runs = await store.list(limit=_RECENT)
        views = [run_view(run, project) for run in runs]
        counts = {status: sum(1 for run in runs if run.status == status) for status in _STATUS}
        failed = counts["failed"] + counts["dead_lettered"]
        alive = worker_alive(project.runs_file) if triggers else False
        return {
            "runs": views,
            "counts": counts,
            "health": self._health(bool(triggers), alive, failed, len(runs)),
        }

    @staticmethod
    def _health(has_triggers: bool, alive: bool, failed: int, total: int) -> dict[str, Any]:
        if not has_triggers:
            state, text = (
                "ondemand",
                (
                    "This system starts only when you ask it (Try it, or a run on a sample below). "
                    "It has no triggers yet."
                ),
            )
        elif alive:
            state, text = "running", "Running: a worker is waiting for events."
        else:
            state, text = (
                "stopped",
                (
                    "Not running: nothing is waiting for events yet, so scheduled and incoming work "
                    "is not picked up. Start it with the command under Keep it running."
                ),
            )
        problems = (
            f"{failed} of the last {total} runs failed. See what to do under Keep it running."
            if failed
            else ""
        )
        return {"state": state, "text": text, "problems": problems, "worker_running": alive}

    @asynccontextmanager
    async def _automation(self) -> AsyncIterator[Automation]:
        """The automation of the blueprint, built as `awa worker` builds it on the local store,
        so any local worker can continue a run paused here."""

        from awa.automation.blueprint import build_automation, load_automation_blueprint
        from awa.blueprints._resolve import file_modules_scope
        from awa.core.settings import Settings
        from awa.persistence import open_checkpointer

        project = self._project
        blueprint, spec = load_automation_blueprint(project.blueprint_file)
        if spec is None or not spec.triggers:
            raise ConfigurationError(
                "This system has no triggers yet. Add one under Start it automatically."
            )
        local = Settings(
            _env_file=None,
            checkpointer_backend="sqlite",
            checkpointer_path=str(project.runs_file.with_name(CHECKPOINTS_FILE)),
        )
        threads = blueprint.resolved_entrypoint() != "workflow" or bool(
            blueprint.needs_checkpointer()
        )
        async with local_runs(project.runs_file) as store, open_checkpointer(local) as checkpointer:
            with file_modules_scope():
                system = await blueprint.abuild(
                    tools=project.loaded_tools(),
                    model_registry=project.model_registry,
                    checkpointer=checkpointer if threads else None,
                    allow_imports=True,
                )
            yield build_automation(
                blueprint,
                system,
                spec=spec,
                store=store,
                base_dir=project.directory,
                usage_limits=blueprint.usage_limits(),
            )

    async def run_sample(self, trigger: str, values: Mapping[str, str]) -> dict[str, Any]:
        """Fire `trigger` once with the sample `values` and run it now, like `awa trigger run`.

        Raises:
            ConfigurationError: If there is no such trigger or no triggers at all.
        """

        from awa.automation.models import TriggerEvent
        from awa.automation.triggers import Schedule

        project = self._project
        async with self._automation() as automation:
            try:
                fired = automation.trigger(trigger)
            except Exception:  # noqa: BLE001 - the library names its own error; say it plainly
                known = ", ".join(t["name"] for t in self.triggers())
                raise ConfigurationError(
                    f"There is no trigger {trigger!r} (known: {known})"
                ) from None
            payload = {key: str(value) for key, value in values.items() if str(value).strip()}
            now = utcnow()
            if isinstance(fired, Schedule):
                base = fired.event_for(fired.next_after(now - timedelta(days=366)), now=now)
                event = base.model_copy(update={"payload": {**base.payload, **payload}})
            else:
                event = TriggerEvent(source=fired.name, payload=payload, received_at=now)
            submission = await automation.submit(event, trigger=fired, actor="web app")
            ran = await automation.run_pending(worker="awa-ui", limit=1)
            run = next((r for r in ran if r.id == submission.run.id), submission.run)
        return {**run_view(run, project), "created_new": submission.created}

    async def continue_run(self, run_id: str) -> dict[str, Any]:
        """Run now what is ready, as `awa worker --once` does, and report `run_id`.

        Raises:
            ConfigurationError: If a worker runs the triggers or the run is not found.
        """

        project = self._project
        if worker_alive(project.runs_file):
            raise ConfigurationError(
                "A worker is running and continues the run itself; give it a moment"
            )
        async with self._automation() as automation:
            if await automation.store.get(run_id) is None:
                raise ConfigurationError(f"There is no run {run_id!r}")
            await automation.run_pending(worker="awa-ui")
            run = await automation.store.get(run_id)
        assert run is not None
        return run_view(run, project)

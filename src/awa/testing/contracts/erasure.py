"""The contract of erasure: after `erase_subject`, nothing names the person in any awa backend.

The suite seeds a whole stack with two people, erases the first and scans the backends with a
raw reader (`ErasureContract.mentions`); implement `opened` for a stack and `mentions` for how to
look into it. A stack with a process run store also implements `seed_process_run`, so the suite
checks that the person's process runs are erased too.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from awa._internal.upstream.checkpoint import BaseCheckpointSaver, BaseStore
from awa.errors import ConfigurationError
from awa.memory import AgentMemory, MemoryScope
from awa.persistence.artifacts import ArtifactRepository
from awa.persistence.erasure import ErasureReport, erase_subject
from awa.persistence.retention import SubjectIndex
from awa.persistence.runlog import PseudonymKey, RunLog, Scope
from awa.persistence.threads import BaseThreadIndex, ThreadOwner
from awa.testing.contracts._base import AdapterContract, Place

if TYPE_CHECKING:
    from awa.automation.store import RunStore

KEY = PseudonymKey(b"erasure-contract-key-32-bytes!!!")
SCOPE = Scope("acme", "invoices")
NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
APPROVALS = ("awa", "tool_approval")
PROFILES = ("crm", "profiles", "{subject_label}")
EXPORTS = "export:{subject}:"


@dataclass
class ErasureStack:
    """Every backend one erasure reaches.

    Attributes:
        run_log: The run log (built with `KEY` unless the suite asked for no pseudonym key).
        run_store: The process run store, if the stack has one.
        location: Whatever `ErasureContract.mentions` needs to look into the backends.
    """

    store: BaseStore
    checkpointer: BaseCheckpointSaver[Any]
    artifacts: ArtifactRepository
    run_log: RunLog
    threads_index: BaseThreadIndex
    run_store: RunStore | None = None
    location: Any = None


class ErasureContract(AdapterContract):
    """Run this against a stack of backends: implement `opened` and `mentions`.

    A stack with a `run_store` also implements `seed_process_run`.
    """

    def opened(
        self, place: Place, *, pseudonym: bool = True
    ) -> AbstractAsyncContextManager[ErasureStack]:
        """An async context manager yielding a stack on `place`; with `pseudonym=False` the run log has no key."""

        raise NotImplementedError

    async def mentions(self, stack: ErasureStack, needle: str) -> list[str]:
        """Where `needle` appears in the stack, looking at the raw data (rows, files, items)."""

        raise NotImplementedError

    async def seed_process_run(self, stack: ErasureStack, person: str) -> str | None:
        """Give `person` a process run in `stack.run_store` and return its id; `None` without one.

        Raises:
            NotImplementedError: If the stack has a run store and this is not implemented.
        """

        if stack.run_store is not None:
            raise NotImplementedError(
                "the stack has a run store: implement seed_process_run to create a process run "
                "for the person, so the suite checks that erasure removes it"
            )
        return None

    async def seed(self, stack: ErasureStack, person: str) -> dict[str, str]:
        """Give `person` data in every backend; return the strings that identify it."""

        thread, run = f"thread-{person}", f"run-{person}"
        memory = AgentMemory(stack.store, scope="user")
        await memory.remember(f"{person} prefers EUR", owner=MemoryScope(agent="a", user_id=person))
        await stack.store.aput(APPROVALS, f"refund:{person}", {"action": "approve"})
        await stack.store.aput(
            ("crm", "profiles", person), "profile", {"name": f"Customer {person}"}
        )
        await _checkpoint(stack.checkpointer, thread, person)
        await stack.artifacts.put(f"contract:{person}", {"owner": person})
        await stack.artifacts.put(f"export:{person}:2026", {"rows": 3})
        await stack.run_log.open_run(SCOPE, run, now=NOW, initiator=person)
        await stack.run_log.request_approval(SCOPE, _approval(run))
        approval = (await stack.run_log.approvals(SCOPE, run))[0]
        await stack.run_log.decide_approval(
            SCOPE, approval.approval_id, decision="approved", reviewer=person, reason=None, now=NOW
        )
        await stack.threads_index.claim("desk", f"served-{person}", ThreadOwner("acme", person))
        await _checkpoint(stack.checkpointer, f"served-{person}", None)
        process = await self.seed_process_run(stack, person)
        await SubjectIndex(stack.store).link(
            person, threads=[thread], artifacts=[f"contract:{person}"]
        )
        identity = {
            "id": person,
            "hmac": KEY.subject(person),
            "thread": thread,
            "run": run,
            "served": f"served-{person}",
        }
        if process is not None:
            identity["process"] = process
        return identity

    async def erase(self, stack: ErasureStack, person: str, **options: Any) -> ErasureReport:
        arguments: dict[str, Any] = {
            "store": stack.store,
            "checkpointer": stack.checkpointer,
            "artifacts": stack.artifacts,
            "run_log": stack.run_log,
            "scopes": [SCOPE],
            "threads_index": stack.threads_index,
            "run_store": stack.run_store,
            "namespaces": [PROFILES],
            "artifact_prefixes": [EXPORTS],
        }
        return await erase_subject(person, **{**arguments, **options})

    async def found(self, stack: ErasureStack, identity: dict[str, str]) -> dict[str, list[str]]:
        return {
            what: places
            for what, needle in identity.items()
            if (places := await self.mentions(stack, needle))
        }

    async def test_nothing_names_the_person_afterwards_and_the_other_person_is_untouched(
        self, place: Place
    ) -> None:
        async with self.opened(place) as stack:
            alice = await self.seed(stack, "alice")
            bob = await self.seed(stack, "bob")
            assert set(await self.found(stack, alice)) == set(alice)

            report = await self.erase(stack, "alice")

            assert report.ok, report.errors
            assert await self.found(stack, alice) == {}
            assert set(await self.found(stack, bob)) == set(bob)
            assert report.deleted["runs"] == 1 and report.deleted["served_threads"] == 1
            assert report.deleted["process_runs"] == int("process" in alice)
            assert report.deleted["memories"] == 1
            assert report.deleted["links"] == 2 and report.deleted["approvals"] == 1
            assert report.deleted["store_items"] == 1
            assert sorted(report.ids["threads"]) == ["served-alice", "thread-alice"]

    async def test_a_dry_run_deletes_nothing_and_reports_the_ids_of_the_real_run(
        self, place: Place
    ) -> None:
        async with self.opened(place) as stack:
            alice = await self.seed(stack, "alice")

            preview = await self.erase(stack, "alice", dry_run=True)
            assert set(await self.found(stack, alice)) == set(alice)
            report = await self.erase(stack, "alice")

            assert preview.dry_run and preview.ok, preview.errors
            assert preview.ids == report.ids and preview.deleted == report.deleted

    async def test_a_second_call_reports_zeros(self, place: Place) -> None:
        async with self.opened(place) as stack:
            await self.seed(stack, "alice")
            await self.erase(stack, "alice")

            again = await self.erase(stack, "alice")

            assert again.ok, again.errors
            assert again.total == 0

    async def test_without_a_pseudonym_key_the_runs_are_reported_and_the_rest_is_erased(
        self, place: Place
    ) -> None:
        async with self.opened(place, pseudonym=False) as stack:
            alice = await self.seed(stack, "alice")

            report = await self.erase(stack, "alice")

            assert any(e.startswith("runs:") and "pseudonym key" in e for e in report.errors)
            assert report.deleted["runs"] == 0
            for key in {"thread", "process"} & alice.keys():
                assert await self.mentions(stack, alice[key]) == []
            assert await self.mentions(stack, alice["run"]) != []

    async def test_links_stay_while_their_backend_is_missing_and_a_second_call_completes(
        self, place: Place
    ) -> None:
        async with self.opened(place) as stack:
            alice = await self.seed(stack, "alice")
            assert await stack.artifacts.exists("contract:alice")

            first = await self.erase(stack, "alice", artifacts=None)
            kept = await SubjectIndex(stack.store).links("alice")
            second = await self.erase(stack, "alice")

            assert any(e.startswith("artifacts:") for e in first.errors)
            assert kept.artifacts == ("contract:alice",) and kept.threads == ()
            assert second.ok, second.errors
            assert not await SubjectIndex(stack.store).links("alice")
            assert not await stack.artifacts.exists("contract:alice")
            assert not await stack.artifacts.exists("export:alice:2026")
            assert alice["thread"] and await self.mentions(stack, alice["hmac"]) == []

    async def test_an_unsafe_template_raises_before_anything_is_deleted(self, place: Place) -> None:
        async with self.opened(place) as stack:
            alice = await self.seed(stack, "alice")

            for options in (
                {"namespaces": [("crm", "profiles")]},
                {"namespaces": [()]},
                {"artifact_prefixes": ["export:"]},
            ):
                try:
                    await self.erase(stack, "alice", **options)
                except ConfigurationError:
                    continue
                raise AssertionError(f"{options} was accepted")

            assert set(await self.found(stack, alice)) == set(alice)


def _approval(run: str) -> Any:
    from awa.persistence.runlog import Approval

    return Approval(run_id=run, step_key=None, requested_at=NOW, reasons=("Large.",))


async def _checkpoint(saver: BaseCheckpointSaver[Any], thread: str, subject: str | None) -> None:
    checkpoint: Any = {
        "v": 4,
        "id": "000001",
        "ts": NOW.isoformat(),
        "channel_values": {"a": thread},
        "channel_versions": {"a": "000001"},
        "versions_seen": {},
        "updated_channels": None,
    }
    metadata: Any = {"source": "input", "step": 1}
    if subject is not None:
        metadata["awa.subject"] = subject
    config: Any = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    saved = await saver.aput(config, checkpoint, metadata, {"a": "000001"})
    await saver.aput_writes(saved, [("a", thread)], "task")


__all__ = ["ErasureContract", "ErasureStack"]

"""An erasure contract that gives each person a process run in the stack's run store."""

from __future__ import annotations

from awa.automation import ProcessRun
from awa.testing.contracts import ErasureContract, ErasureStack


class WithProcessRuns(ErasureContract):
    """`ErasureContract` whose stacks have a process run store."""

    async def seed_process_run(self, stack: ErasureStack, person: str) -> str | None:
        assert stack.run_store is not None
        run = ProcessRun(id=f"proc-{person}", automation="invoices", actor=person, thread_id=None)
        await stack.run_store.create(run)
        return run.id

"""The contract of an `AuditLogSink`: an append-only store that lets a hash chain continue."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

import pytest

from awa.observability.audit import AuditLog, AuditLogSink
from awa.testing.contracts._base import AdapterContract, Place


@dataclass(frozen=True)
class AuditSinkCapabilities:
    """What a sink promises beyond the base contract (`survives_restart`: a reopened sink holds the records)."""

    survives_restart: bool = False


class AuditSinkContract(AdapterContract):
    """Run this against an `AuditLogSink`: implement `opened`."""

    capabilities = AuditSinkCapabilities()

    def opened(self, place: Place) -> AbstractContextManager[AuditLogSink]:
        """A context manager yielding a sink on `place`. Opening twice is a restart."""

        raise NotImplementedError

    def test_an_empty_sink_has_no_last_record(self, place: Place) -> None:
        with self.opened(place) as sink:
            assert sink.last_record() is None

    def test_the_last_record_is_the_one_written_last(self, place: Place) -> None:
        with self.opened(place) as sink:
            audit = AuditLog(sink)
            records = [audit.record("step", {"n": n}) for n in range(3)]

            assert sink.last_record() == records[-1]
            assert [r.seq for r in records] == [0, 1, 2]
            assert records[1].prev_hash == records[0].hash

    def test_a_new_log_on_the_same_sink_continues_the_chain(self, place: Place) -> None:
        with self.opened(place) as sink:
            first = AuditLog(sink).record("step", {"n": 0})
            second = AuditLog(sink).record("step", {"n": 1})

            assert second.seq == 1 and second.prev_hash == first.hash
            assert sink.last_record() == second

    def test_the_chain_survives_a_restart(self, place: Place) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("a single-process sink: a restart starts empty")
        with self.opened(place) as first:
            kept = AuditLog(first).record("step", {"n": 0})
        with self.opened(place) as second:
            assert second.last_record() == kept
            resumed = AuditLog(second).record("step", {"n": 1})
            assert resumed.seq == 1 and resumed.prev_hash == kept.hash
            assert second.last_record() == resumed

    def test_a_single_process_sink_starts_empty_after_a_restart(self, place: Place) -> None:
        if self.capabilities.survives_restart:
            pytest.skip("a durable sink keeps its records")
        with self.opened(place) as first:
            AuditLog(first).record("step", {"n": 0})
        with self.opened(place) as second:
            assert second.last_record() is None


__all__ = ["AuditSinkCapabilities", "AuditSinkContract"]

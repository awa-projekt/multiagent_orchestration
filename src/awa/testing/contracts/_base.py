"""What every contract suite shares: where a test keeps its data, and known deviations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

from awa.testing.doubles import FakeClock, FixedIds


@dataclass(frozen=True)
class Place:
    """Where one test keeps its data: a fresh folder, a unique lowercase name and, if needed, a DSN."""

    path: Path
    name: str
    dsn: str | None = None


class AdapterContract:
    """Base of the suites. A subclass is one backend run against one suite.

    Attributes:
        adapter: The backend class this suite runs; awa checks that every backend of a protocol is
            run by its suite.
        known_deviations: Test name -> why this backend differs. The test must fail (passing fails
            the run); pin the actual behavior in a test of its own.
    """

    adapter: ClassVar[type | None] = None
    known_deviations: ClassVar[dict[str, str]] = {}

    @pytest.fixture
    def place(self, tmp_path: Path) -> Place:
        return Place(tmp_path, f"c{uuid4().hex[:12]}")

    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def ids(self) -> FixedIds:
        return FixedIds()

    @pytest.fixture(autouse=True)
    def _expect_known_deviation(self, request: pytest.FixtureRequest) -> None:
        reason = self.known_deviations.get(request.node.originalname)
        if reason is not None:
            request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))

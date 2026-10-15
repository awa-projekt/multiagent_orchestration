"""What `awa.blueprints.build_project` hands back, as the build-engine tests need it.

Repository-only test support (rule R12): `FakeReport` has the fields and methods of a build report
that `awa.server._data_blueprints.BlueprintsEngine` reads, without running a build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from awa.contracts.usage import Usage


@dataclass
class FakeReport:
    passed: bool = True
    pass_rate: float = 0.9
    rounds: int = 2
    stopped: str = "target_reached"
    target_pass_rate: float = 0.8
    flaky: tuple[str, ...] = ("c2",)
    usage: Usage = field(default_factory=lambda: Usage(requests=14))
    question: Any = None

    def summary(self) -> str:
        return "shop: 3 of 3 eval cases pass.\nProject folder: C:/srv/secret/project\nNext: read README.md"

    @property
    def eval_report(self) -> Any:
        @dataclass
        class Case:
            case_id: str

        class Report:
            @staticmethod
            def failures() -> list[Case]:
                return [Case("c9"), Case("c9"), Case("c4")]

        return Report()

    @property
    def design(self) -> Any:
        class Design:
            assumptions = ("amounts in euro",)

        return Design()

"""The terminal the guide test types into: output with the typed lines echoed after a prompt.

Repository-only test support (rule R12): `FakeTerminal` stands in for a person at a terminal for
`tests/docs/test_guide.py`.
"""

from __future__ import annotations

import io
from typing import Any


class FakeTerminal(io.StringIO):
    """What the reader sees: the output (prompts too) and the typed lines echoed after a prompt."""

    def __init__(self, typed: list[str]) -> None:
        super().__init__()
        self.typed = list(typed)
        self.stdin = _Stdin(self)

    def read_line(self, echo: bool) -> str:
        if not self.typed:
            raise EOFError
        line = self.typed.pop(0)
        if echo:
            self.write(line + "\n")
        return line


class _Stdin:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        try:
            return self.terminal.read_line(echo=True) + "\n"
        except EOFError:
            return ""

    def read(self, *_: Any) -> str:
        return ""

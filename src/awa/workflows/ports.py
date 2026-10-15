"""Backend protocols of the workflows: what a workflow needs from its caller."""

from __future__ import annotations

from typing import Protocol

__all__ = ["StepDocuments"]


class StepDocuments(Protocol):
    """What a step needs from its documents; implemented by the caller and called in a worker thread."""

    def problems(self) -> list[str]:
        """What is wrong with the documents now, one plain sentence each; empty when all is well."""

        ...

    def block(self) -> str:
        """The text appended to the step's input that tells the agent about its documents."""

        ...

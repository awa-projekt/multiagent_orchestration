"""The interfaces the connector engine is written against.

`TokenProvider` is how the engine gets a bearer token without knowing how it was obtained; its
backends are the built-in OAuth 2.0 grants of the engine, the Microsoft user sign-in
(`awa.connectors._microsoft_auth`) and the Microsoft certificate sign-in
(`awa.connectors._microsoft_cert`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from awa.connectors._engine import ConnectorContext

__all__ = ["Identity", "TokenProvider"]

Identity = Literal["app", "user"]
"""Whose rights a token carries: the application's own, or a signed-in person's."""


class TokenProvider(Protocol):
    """Hands the engine a bearer token; the engine never asks how it was obtained.

    The engine calls `token` before every request and `invalidate` after a 401, then asks again
    once. Implementations raise `ToolException` naming the setup step when they cannot produce a
    token; they must not log the token or return it anywhere but from `token`. `kind` (whose
    rights) is checked against `Operation.needs`.
    """

    kind: Identity

    def token(self, ctx: ConnectorContext) -> str:
        """A valid bearer token (cached until shortly before it expires)."""

    def invalidate(self, ctx: ConnectorContext) -> None:
        """Forget the cached token: the service rejected it."""

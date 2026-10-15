"""`replay_models`: record model responses once and replay them in later test runs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from awa.models.replay import ReplayScope, replaying
from awa.persistence.cache import InMemoryCacheStore, SqliteCacheStore
from awa.runtime.cache import Cache, CacheSettings

__all__ = ["replay_models"]


@contextmanager
def replay_models(
    path: str | Path | None = None, *, project: str = "tests", tenant: str = "local"
) -> Iterator[ReplayScope]:
    """Record and replay the model responses of agents run inside the block (see `awa.models.replay`).

    Args:
        path: SQLite file of the recordings; `None` keeps them in memory for the block only.
        project: Project name the recordings are kept under.
        tenant: Tenant the recordings are kept under.

    Yields:
        The `ReplayScope`; its `counts` say how many calls were replayed and recorded.

    Example:
        ```python
        @pytest.fixture
        def replayed(request):
            with replay_models(Path(request.path).with_suffix(".replay.db")) as scope:
                yield scope
        ```
    """

    store = InMemoryCacheStore() if path is None else SqliteCacheStore(path)
    with replaying(Cache(CacheSettings(), store, project=project), tenant=tenant) as scope:
        yield scope

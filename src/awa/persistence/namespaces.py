"""The store layer under awa's memory and privacy tools.

Everything awa keeps in a LangGraph `BaseStore` lives under a namespace starting with `"awa"`:
`("awa", "memory", ...)` (`AgentMemory`), `("awa", "tool_approval")` (remembered approvals) and
`("awa", "privacy", "subjects", <subject>)` (`SubjectIndex`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any
from urllib.parse import quote

from awa._internal.upstream.checkpoint import BaseStore, SearchItem
from awa.errors import ConfigurationError

MEMORY_NAMESPACE: tuple[str, ...] = ("awa", "memory")
"""Namespace prefix of `awa.memory.AgentMemory`."""

PRIVACY_NAMESPACE: tuple[str, ...] = ("awa", "privacy")
"""Namespace prefix of the subject index."""

_PAGE_SIZE = 100


def namespace_label(value: str) -> str:
    """Encode a non-empty `value` as one injective namespace label without `"."` (percent-encoded).

    Raises:
        ConfigurationError: If `value` is empty.

    Example:
        ```python
        assert namespace_label("jane.doe@example.com") == "jane%2Edoe%40example%2Ecom"
        ```
    """

    if not value:
        raise ConfigurationError("namespace labels must not be empty")
    return quote(value, safe="").replace(".", "%2E")


async def iter_namespace_items(
    store: BaseStore, namespace: tuple[str, ...], *, page_size: int = _PAGE_SIZE
) -> AsyncIterator[SearchItem]:
    """Yield every item stored exactly in `namespace` (not nested ones), page by page."""

    async for item in iter_prefix_items(
        store, namespace, page_size=page_size, predicate=lambda i: tuple(i.namespace) == namespace
    ):
        yield item


async def iter_prefix_items(
    store: BaseStore,
    prefix: tuple[str, ...],
    *,
    page_size: int = _PAGE_SIZE,
    filter: dict[str, Any] | None = None,  # noqa: A002 - the store's own argument name
    predicate: Callable[[SearchItem], bool] | None = None,
) -> AsyncIterator[SearchItem]:
    """Yield every item in `prefix` and the namespaces nested under it, page by page.

    Do not delete while iterating (offset paging would skip items); collect first and use
    `delete_items`.

    Args:
        filter: Match the stored value on these fields (the store's own `filter`).
        predicate: Yield only the items it accepts.

    Example:
        ```python
        stale = [item async for item in iter_prefix_items(store, MEMORY_NAMESPACE)]
        ```
    """

    offset = 0
    while True:
        page = await store.asearch(prefix, filter=filter, limit=page_size, offset=offset)
        for item in page:
            if predicate is None or predicate(item):
                yield item
        if len(page) < page_size:
            return
        offset += page_size


async def iter_namespaces(
    store: BaseStore, prefix: tuple[str, ...], *, page_size: int = _PAGE_SIZE
) -> AsyncIterator[tuple[str, ...]]:
    """Yield every namespace at or below `prefix`, page by page."""

    offset = 0
    while True:
        page = await store.alist_namespaces(prefix=prefix, limit=page_size, offset=offset)
        for namespace in page:
            yield tuple(namespace)
        if len(page) < page_size:
            return
        offset += page_size


async def delete_items(
    store: BaseStore, items: Iterable[SearchItem], *, dry_run: bool = False
) -> list[SearchItem]:
    """Delete `items` (collected beforehand) from `store` and return them once each; `dry_run` deletes none."""

    unique = list({(tuple(item.namespace), item.key): item for item in items}.values())
    if not dry_run:
        for item in unique:
            await store.adelete(tuple(item.namespace), item.key)
    return unique


__all__ = [
    "MEMORY_NAMESPACE",
    "PRIVACY_NAMESPACE",
    "delete_items",
    "iter_namespace_items",
    "iter_namespaces",
    "iter_prefix_items",
    "namespace_label",
]

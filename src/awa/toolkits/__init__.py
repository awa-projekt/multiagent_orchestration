"""Toolkits (extension): ready-made tools with timeouts, size limits, path confinement and approval.

Factories such as `web_toolkit`, `file_toolkit`, `data_toolkit` or `sql_toolkit` build LangChain
tools; `catalog()` lists them for blueprints (`builtin:<name>`, configured in `toolkits:`) with
category, risk, approval default and optional extra. Tools that write or send wait for approval by
default (`requires_approval=False` turns it off; approval needs a checkpointer). The PDF, Word and
Excel readers need the `files` extra.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa._internal.sandbox import Sandbox
    from awa.toolkits._catalog import CatalogEntry, ToolCatalog, Toolkit, ToolkitConfig, catalog
    from awa.toolkits._search import SearchFixture, search_fixture
    from awa.toolkits._vision import vision_ocr
    from awa.toolkits.communication import email_toolkit, template_toolkit, webhook_toolkit
    from awa.toolkits.data import data_toolkit
    from awa.toolkits.documents import chunk_text, document_toolkit, text_statistics
    from awa.toolkits.files import file_toolkit
    from awa.toolkits.knowledge import KnowledgeBase, KnowledgeHit, knowledge_toolkit
    from awa.toolkits.sql import check_read_only_sql, sql_toolkit
    from awa.toolkits.utilities import utility_toolkit
    from awa.toolkits.web import (
        DuckDuckGoSearchProvider,
        JSONSearchProvider,
        SearchBlockedError,
        SearchError,
        SearchFormatError,
        SearchProvider,
        SearchResult,
        html_to_text,
        web_toolkit,
    )

_HOMES = homes(
    {
        "awa._internal.sandbox": "Sandbox",
        "awa.toolkits._catalog": "CatalogEntry ToolCatalog Toolkit ToolkitConfig catalog",
        "awa.toolkits._search": "SearchFixture search_fixture",
        "awa.toolkits._vision": "vision_ocr",
        "awa.toolkits.communication": "email_toolkit template_toolkit webhook_toolkit",
        "awa.toolkits.data": "data_toolkit",
        "awa.toolkits.documents": "chunk_text document_toolkit text_statistics",
        "awa.toolkits.files": "file_toolkit",
        "awa.toolkits.knowledge": "KnowledgeBase KnowledgeHit knowledge_toolkit",
        "awa.toolkits.sql": "check_read_only_sql sql_toolkit",
        "awa.toolkits.utilities": "utility_toolkit",
        "awa.toolkits.web": (
            "DuckDuckGoSearchProvider JSONSearchProvider SearchBlockedError SearchError"
            " SearchFormatError SearchProvider SearchResult html_to_text web_toolkit"
        ),
    }
)

__all__ = [
    "CatalogEntry",
    "DuckDuckGoSearchProvider",
    "JSONSearchProvider",
    "KnowledgeBase",
    "KnowledgeHit",
    "Sandbox",
    "SearchFixture",
    "SearchBlockedError",
    "SearchError",
    "SearchFormatError",
    "SearchProvider",
    "SearchResult",
    "ToolCatalog",
    "Toolkit",
    "ToolkitConfig",
    "catalog",
    "check_read_only_sql",
    "chunk_text",
    "data_toolkit",
    "document_toolkit",
    "email_toolkit",
    "file_toolkit",
    "html_to_text",
    "knowledge_toolkit",
    "sql_toolkit",
    "template_toolkit",
    "text_statistics",
    "vision_ocr",
    "utility_toolkit",
    "search_fixture",
    "web_toolkit",
    "webhook_toolkit",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})

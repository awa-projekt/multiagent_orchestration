"""Modules copied unchanged into every export (see `awa.exporter`).

They import LangChain, LangGraph and the standard library only, never awa, so the export runs
without awa installed. `approvals.py` imports `conditions` as a sibling: the export copies
`awa.kernel.conditions` next to it. They are templates: `approvals` imports only inside an export.
"""

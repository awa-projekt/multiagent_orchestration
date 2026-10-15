"""The contract matrix of the builtin tools: one spec per tool, run by `tests/unit/test_tools_contract_*.py`.

`specs.SPECS` says, for each tool of `awa.toolkits.catalog()`, a valid call, an invalid call that
is refused with a message that names the fix, hostile calls (traversal, huge input, injection) and
what repeating the call does. `world.World` is the offline fixture world they run against: a
temporary folder with sample files, a local web server on loopback, an outbox for emails.
Everything is offline; nothing here proves the tools against a real web site, mail server or
database server.
"""

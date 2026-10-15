## What changes and why

<!-- One focused change. Link the issue it closes, if any (`Closes #123`). -->

## Checklist

- [ ] Tests cover the change and run offline (no real model requests).
- [ ] `python -m ruff check .`, `python -m ruff format --check src tests examples scripts`, `python -m mypy src` and `lint-imports` pass.
- [ ] `python -m pytest -n auto --ignore=tests/docs` and `python -m pytest -n auto tests/docs` pass.
- [ ] The relevant docs page is updated; `python scripts/gen_llms.py` was run after a docs change.
- [ ] No secrets, tokens or personal data in the diff.

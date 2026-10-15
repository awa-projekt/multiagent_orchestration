"""Hypothesis profiles for the property-based tests and fuzzers in this folder.

Pick a profile with the `HYPOTHESIS_PROFILE` environment variable:

- `default` (local runs): 50 examples per test, random seeds, failures are
  saved to the local `.hypothesis/` example database and replayed first.
- `ci`: 100 examples per test with `derandomize=True`, so every CI run tries
  the same inputs and a red build is reproducible with the same command. No
  example database is written.
- `thorough`: 2,000 examples per test with random seeds, for a nightly or
  pre-release fuzzing run (`HYPOTHESIS_PROFILE=thorough pytest tests/property`).

To replay a failure from CI locally, run the failing test with
`HYPOTHESIS_PROFILE=ci`; to reproduce a failure seen with random seeds, use
the `@reproduce_failure(...)` blob Hypothesis prints.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

# Wall-clock deadlines are flaky on shared CI runners and on Windows.
_UNTIMED = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])

settings.register_profile("default", _UNTIMED, max_examples=50)
settings.register_profile(
    "ci", _UNTIMED, max_examples=100, derandomize=True, database=None, print_blob=True
)
settings.register_profile("thorough", _UNTIMED, max_examples=2_000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

"""Eval cases and datasets, loadable from YAML or JSON Lines.

```yaml
name: support-triage
setup: fixtures.py:seed_orders      # optional: called with (case_dir, case) per case
cases:
  - id: order-status
    input: Where is order 1002?
    expected: shipped
    scorers: [contains, "delegated:order_specialist"]
  - id: refund-then-apology
    input: Refund order 1003 and send the customer an apology.
    decisions:
      - {action: approve}
      - {action: reject, tool: send_email, note: Too informal; rewrite it.}
```
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from awa.errors import EvalError

EvalInput = str | list[dict[str, Any]]
"""What a case sends to the system: a prompt, or a list of role/content messages."""

DECISION_ACTIONS = ("approve", "reject", "edit")
"""The actions of a human decision (`{"action": ...}`), as `Agent.resume` takes them."""

_YAML_SUFFIXES = (".yaml", ".yml", ".json")


class EvalCase(BaseModel):
    """One input to evaluate a system on.

    Attributes:
        id: Unique id within its dataset; reports and comparisons match cases by it.
        input: A prompt, or a list of `{"role": ..., "content": ...}` messages.
        expected: What the scorers compare the output with (text, a mapping of output fields, a
            list of phrases, ...).
        scorers: Scorer specifications added to those passed to `evaluate` (`"tool:lookup_order"`).
        decisions: Human decisions for the run's approval requests, in the order asked, as
            `Agent.resume` takes them (`{"action": "approve"}`, `{"action": "reject", "note": ...}`,
            `{"action": "edit", "args": {...}}`; `"approve"`/`"reject"` for short). The run is
            resumed with each in turn; without one left it ends paused. A decision with a `tool`
            answers the first request for that tool wherever it comes; with `"repeat": true`
            every one (at most one repeating decision per tool).
        setup: A function preparing this case's folder (`<file.py>:<function>` or
            `<module>:<function>`), run after the dataset's and called with `(case_dir, case)`;
            like the dataset's, it is code that evaluating runs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    input: EvalInput
    expected: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    scorers: tuple[str, ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    setup: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _text_id(cls, value: Any) -> Any:
        return (
            str(value) if isinstance(value, int | float) and not isinstance(value, bool) else value
        )

    @field_validator("tags", mode="before")
    @classmethod
    def _one_tag(cls, value: Any) -> Any:
        return (value,) if isinstance(value, str) else value

    @field_validator("setup")
    @classmethod
    def _setup_spec(cls, value: str | None) -> str | None:
        return _setup_spec(value)

    @field_validator("decisions", mode="before")
    @classmethod
    def _normalize_decisions(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str | Mapping) or not isinstance(value, Iterable):
            raise ValueError("decisions must be a list of decisions")
        decisions = tuple(_decision(item, index) for index, item in enumerate(value))
        tools = [decision["tool"] for decision in decisions if decision.get("repeat")]
        twice = sorted({tool for tool in tools if tools.count(tool) > 1})
        if twice:
            raise ValueError(f"more than one repeating decision for tool {', '.join(twice)}")
        return decisions

    @field_validator("input")
    @classmethod
    def _input_not_empty(cls, value: EvalInput) -> EvalInput:
        if isinstance(value, str) and not value.strip():
            raise ValueError("input must not be empty")
        if isinstance(value, list):
            if not value:
                raise ValueError("input must contain at least one message")
            for message in value:
                if "content" not in message or ("role" not in message and "type" not in message):
                    raise ValueError("every input message needs a 'role' and a 'content'")
        return value


class Dataset(BaseModel):
    """An ordered collection of `EvalCase`s with unique ids.

    Attributes:
        setup: A function preparing every case's folder before its run (`<file.py>:<function>` or
            `<module>:<function>`, called with `(case_dir, case)`); see `awa.runtime.case_dir`.
            Evaluating imports and runs it, so evaluate only datasets whose code you trust.

    Example:
        ```python
        refunds = Dataset.from_yaml("cases.yaml").filter("refunds")
        ```
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "dataset"
    cases: tuple[EvalCase, ...] = ()
    setup: str | None = None

    @field_validator("setup")
    @classmethod
    def _setup_spec(cls, value: str | None) -> str | None:
        return _setup_spec(value)

    @model_validator(mode="after")
    def _unique_ids(self) -> Dataset:
        ids = [case.id for case in self.cases]
        duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate case ids: {', '.join(duplicates)}")
        return self

    @classmethod
    def from_records(
        cls,
        records: Iterable[Mapping[str, Any]],
        *,
        name: str = "dataset",
        setup: str | None = None,
        base: Path | None = None,
    ) -> Dataset:
        """Build a dataset from case mappings; a missing `id` becomes `case-<n>` (1-based).

        A relative `<file.py>` in a `setup` resolves against `base`.

        Raises:
            EvalError: If a record is not a valid case.
        """

        cases: list[EvalCase] = []
        for index, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                raise EvalError(f"Case {index} must be a mapping, got {type(record).__name__}")
            data = {"id": f"case-{index}", **record}
            if isinstance(data.get("setup"), str):
                data["setup"] = _anchored(data["setup"], base)
            try:
                cases.append(EvalCase.model_validate(data))
            except ValidationError as exc:
                raise EvalError(f"Invalid case {data['id']!r}: {_first_error(exc)}") from None
        try:
            return cls(name=name, cases=tuple(cases), setup=_anchored(setup, base))
        except ValidationError as exc:
            raise EvalError(f"Invalid dataset {name!r}: {_first_error(exc)}") from None

    @classmethod
    def from_yaml(cls, source: str | Path, *, name: str | None = None) -> Dataset:
        """Load a dataset from a YAML (or JSON) file or YAML text: a list of cases, or `cases` and `name`.

        Args:
            source: A `Path`, a path string ending in `.yaml`, `.yml` or `.json` (or naming an existing
                file), or YAML text.
            name: Defaults to the document's `name`, then the file name.

        Raises:
            EvalError: If the file cannot be read or does not describe cases.
        """

        text, default_name, base = _read_source(source)
        setup: Any = None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise EvalError(f"Malformed dataset YAML: {exc}") from None
        if isinstance(data, Mapping):
            extra = sorted(set(data) - {"name", "cases", "setup"})
            if extra:
                raise EvalError(f"Unknown dataset keys: {', '.join(extra)}")
            default_name = str(data.get("name") or default_name)
            setup = data.get("setup")
            if setup is not None and not isinstance(setup, str):
                raise EvalError("The dataset's setup must be '<file.py>:<function>'")
            data = data.get("cases") or []
        if not isinstance(data, list):
            raise EvalError("A dataset must be a list of cases or a mapping with 'cases'")
        return cls.from_records(data, name=name or default_name, setup=setup, base=base)

    @classmethod
    def load(cls, path: str | Path, *, name: str | None = None) -> Dataset:
        """Load a YAML (or JSON) dataset file.

        Raises:
            EvalError: If the file cannot be read or is invalid.
        """

        return cls.from_yaml(Path(path), name=name)

    def to_yaml(self, path: str | Path | None = None) -> str:
        """Return the dataset as YAML (readable by `from_yaml`), and write it to `path` when given."""

        document: dict[str, Any] = {"name": self.name}
        if self.setup is not None:
            document["setup"] = self.setup
        document["cases"] = [
            case.model_dump(mode="json", exclude_defaults=True) for case in self.cases
        ]
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def filter(self, *tags: str, ids: Iterable[str] | None = None) -> Dataset:
        """The cases carrying any of `tags` (all when none are given) and, with `ids`, listed there."""

        wanted = set(ids) if ids is not None else None
        selected = tuple(
            case
            for case in self.cases
            if (not tags or any(tag in case.tags for tag in tags))
            and (wanted is None or case.id in wanted)
        )
        return Dataset(name=self.name, cases=selected, setup=self.setup)

    @property
    def tags(self) -> list[str]:
        """Every tag used by a case, sorted."""

        return sorted({tag for case in self.cases for tag in case.tags})

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[EvalCase]:  # type: ignore[override]  # iterates the cases, not pydantic's field pairs
        return iter(self.cases)

    def __getitem__(self, case_id: str) -> EvalCase:
        """Return the case with id `case_id`; `KeyError` if there is none."""

        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)


def _decision(value: Any, index: int) -> dict[str, Any]:
    """One decision in the resume format, from a mapping or an `approve`/`reject` shorthand."""

    where = f"decision {index + 1}"
    if isinstance(value, str):
        if value.strip().lower() not in ("approve", "reject"):
            raise ValueError(f"{where}: {value!r} is not 'approve' or 'reject'")
        return {"action": value.strip().lower()}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a mapping like {{action: approve}}")
    decision = {str(key): item for key, item in value.items()}
    action = decision.get("action")
    if action not in DECISION_ACTIONS:
        raise ValueError(f"{where} needs action approve, reject or edit, got {action!r}")
    if "note" in decision and not isinstance(decision["note"], str):
        raise ValueError(f"{where}: note must be text")
    if action == "edit" and not isinstance(decision.get("args"), Mapping):
        raise ValueError(f"{where}: an edit needs an 'args' mapping")
    if "tool" in decision:
        tool = decision["tool"]
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"{where}: tool must be the name of the tool it answers")
        decision["tool"] = tool.strip()
    if "repeat" in decision:
        if not isinstance(decision["repeat"], bool):
            raise ValueError(f"{where}: repeat must be true or false")
        if decision["repeat"] and "tool" not in decision:
            raise ValueError(f"{where}: repeat needs the tool whose requests it answers")
    return decision


def split_setup(spec: str) -> tuple[str, str]:
    """A setup spec as `(target, function)`, both stripped; the target is empty without a colon."""

    target, _, function = spec.rpartition(":")
    return target.strip(), function.strip()


def _setup_spec(value: str | None) -> str | None:
    if value is None:
        return None
    target, function = split_setup(value)
    if ":" not in value or not target or not function.isidentifier():
        raise ValueError(f"setup {value!r} must be '<file.py>:<function>' or '<module>:<function>'")
    return f"{target}:{function}"


def _anchored(setup: str | None, base: Path | None) -> str | None:
    """`setup` with a relative `<file.py>` made absolute against `base`."""

    if setup is None or base is None:
        return setup
    target, function = split_setup(setup)
    if ":" in setup and target.lower().endswith(".py") and not Path(target).is_absolute():
        return f"{(base / target).resolve()}:{function}"
    return setup


def _read_source(source: str | Path) -> tuple[str, str, Path | None]:
    """The dataset text, its default name and its folder (`None` for dataset text)."""

    if isinstance(source, Path) or _looks_like_path(source):
        path = Path(source)
        try:
            return path.read_text(encoding="utf-8"), path.stem, path.resolve().parent
        except OSError as exc:
            raise EvalError(f"Cannot read dataset file {str(path)!r}: {exc}") from None
    return source, "dataset", None


def _looks_like_path(text: str) -> bool:
    if "\n" in text:
        return False
    if text.lower().endswith(_YAML_SUFFIXES):
        return True
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}" if location else str(error["msg"])


__all__ = ["DECISION_ACTIONS", "Dataset", "EvalCase", "EvalInput"]

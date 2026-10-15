"""The "External systems" part of the Data step: declare, test, inspect and remove systems;
kinds and form fields come from the connector catalog."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from awa.blueprints._declare import (
    DATABASE_KINDS,
    Declared,
    Setting,
    declare_system,
    merged_env,
    read_declared_systems,
    refresh_system,
    remove_declared_system,
    system_config,
    system_settings,
    system_types,
    try_system,
)
from awa.blueprints._systems import DeclaredSystem, declared_tools, system_key
from awa.ui._connections import CATEGORY_TITLES, _field, check_values, instance_label, kind_of

if TYPE_CHECKING:
    from awa.ui._project import Project

_SHOWN_ROWS = 3


class SystemsStep:
    """The declared systems of one project and the form that adds one."""

    def __init__(self, project: Project) -> None:
        self._project = project

    def state(self) -> dict[str, Any]:
        """The cards of the declared systems and the kinds of system that can be added."""

        return {
            "systems": [self.card(system) for system in self.declared()],
            "types": [
                {
                    "connector": t.connector,
                    "title": t.title,
                    "category": t.category,
                    "group": CATEGORY_TITLES.get(t.category, t.category.capitalize()),
                    "summary": t.summary,
                }
                for t in system_types()
            ],
            "databases": [
                {"id": key, "title": title, "example": example}
                for key, (title, example) in DATABASE_KINDS.items()
            ],
        }

    def declared(self) -> list[DeclaredSystem]:
        return read_declared_systems(self._project.directory)

    def form(self, connector: str, name: str) -> dict[str, Any]:
        """The fields of a new system of kind `connector` called `name`, and how to get them."""

        from awa.connectors import get_connector

        key = self._key(name)
        config = system_config(connector, key)
        env = merged_env(self._project.environ, self._project.env_file, {})
        spec = get_connector(connector)
        return {
            "key": key,
            "fields": self._fields(system_settings(key, config, env), key),
            "setup": spec.setup.replace("<NAME>", key.upper()),
            "docs": spec.docs,
        }

    def test(self, connector: str, name: str, values: Mapping[str, str]) -> dict[str, Any]:
        """Test a system with the typed values over the saved ones; nothing is saved."""

        key = self._key(name)
        self._check_links(connector, key, values)
        outcome = try_system(
            self._project.directory,
            connector=connector,
            name=name,
            values=values,
            environ=self._project.environ,
            key=key,
            transport=self._project.transport,
        )
        return self._outcome(outcome, key)

    def save(
        self, connector: str, name: str, values: Mapping[str, str], *, samples: bool
    ) -> dict[str, Any]:
        """Test the system and, when the test passes, save its settings, read its structure and
        declare it (see `awa.blueprints._declare.declare_system`)."""

        key = self._key(name)
        self._check_links(connector, key, values)
        outcome = declare_system(
            self._project.directory,
            connector=connector,
            name=name,
            values=values,
            environ=self._project.environ,
            samples=samples,
            key=key,
            transport=self._project.transport,
        )
        if outcome.saved:
            self._project._refresh(outcome.saved)
        return self._outcome(outcome, key)

    def refresh(self, key: str) -> dict[str, Any]:
        """Read the structure of a declared system again."""

        system = refresh_system(
            self._project.directory,
            key,
            environ=self._project.environ,
            transport=self._project.transport,
        )
        return self.card(system)

    def remove(self, key: str) -> dict[str, Any]:
        """Forget a declared system (its settings stay in `.env`)."""

        remove_declared_system(self._project.directory, key)
        return self.state()

    def card(self, system: DeclaredSystem) -> dict[str, Any]:
        """A declared system as the page shows it: never a value of a setting."""

        env = merged_env(self._project.environ, self._project.env_file, {})
        tables = [
            {
                "name": table.name,
                "kind": table.kind,
                "rows": table.rows,
                "columns": list(table.columns),
                "sample": {
                    "columns": list(table.sample_columns),
                    "rows": [
                        ["" if value is None else str(value) for value in row]
                        for row in table.sample_rows[:_SHOWN_ROWS]
                    ],
                }
                if system.samples and table.sample_rows
                else None,
            }
            for table in system.tables
        ]
        reads = [op for op in system.operations if not op.write]
        return {
            "key": system.key,
            "name": system.name,
            "kind": system.kind,
            "connector": system.connector,
            "state": "live",
            "tables": tables,
            "operations": [
                {"name": op.name, "method": op.method, "path": op.path, "summary": op.summary}
                for op in reads
            ],
            "left_out": len(system.operations) - len(reads),
            "tools": list(declared_tools([system])),
            "warnings": list(system.warnings),
            "tested": system.tested,
            "samples": system.samples,
            "settings": [
                {
                    "name": name,
                    "label": instance_label(name, system.key),
                    "set": bool(env.get(name)),
                }
                for name in system.variables
            ],
        }

    def _key(self, name: str) -> str:
        """The key of the system called `name`: the existing one, or a new unique one."""

        clean = " ".join(name.split())
        declared = self.declared()
        same = next((s for s in declared if s.name == clean), None)
        return same.key if same else system_key(clean, {s.key for s in declared})

    def _fields(self, settings: list[Setting], key: str) -> list[dict[str, Any]]:
        env = merged_env(self._project.environ, self._project.env_file, {})
        return [
            _field(
                s.name,
                s.description,
                kind_of(s.name, secret=s.secret, example=s.example),
                required=s.required,
                is_set=bool(env.get(s.name)),
                hint=s.example,
                label=instance_label(s.name, key),
            )
            for s in settings
        ]

    def _check_links(self, connector: str, key: str, values: Mapping[str, str]) -> None:
        env = merged_env(self._project.environ, self._project.env_file, {})
        fields = self._fields(system_settings(key, system_config(connector, key), env), key)
        check_values(
            {k: v for k, v in values.items() if str(v).strip()},
            {item["name"]: item for item in fields},
        )

    def _outcome(self, outcome: Declared, key: str) -> dict[str, Any]:
        return {
            "key": key,
            "ok": outcome.ok,
            "message": self._project.redacted(outcome.message),
            "advice": outcome.advice,
            "warnings": [self._project.redacted(w) for w in outcome.warnings],
            "saved": outcome.saved,
            "system": self.card(outcome.system) if outcome.system is not None else None,
            "fields": self._fields(outcome.settings, key),
        }

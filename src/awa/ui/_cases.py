"""The Test step's case editor: change what an eval case sends and what it runs against."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from awa.blueprints.schema import Blueprint
from awa.errors import ConfigurationError
from awa.ui._plain import as_text

if TYPE_CHECKING:
    from awa.evals import EvalCase
    from awa.ui._project import Project

MAX_REQUEST = 20_000
_MODES = ("live", "stand-in")


class CaseEditor:
    """The eval cases of one project, as the Test step edits them.

    Changes are saved in `evals/cases.yaml` (`input`, `metadata.systems`), so `awa eval` runs the
    same case; expected values are not changed.
    """

    def __init__(self, project: Project) -> None:
        self._project = project

    def view(self) -> dict[str, Any]:
        """Every case with its request, the files it names and the systems it may switch, plus
        the project's files and the systems that can run on their stand-in per case."""

        project = self._project
        if not project.cases_file.is_file():
            return {"exists": False, "cases": [], "files": [], "systems": []}
        files = self._project.files_in("data/files")
        switchable = self.switchable()
        cases = []
        for case in project.dataset().cases:
            text = as_text(case.input)
            chosen = dict((case.metadata or {}).get("systems") or {})
            cases.append(
                {
                    "id": case.id,
                    "input": text,
                    "files": [name for name in files if _names(text, name)],
                    "checks": list(case.scorers),
                    "systems": {key: chosen.get(key, "live") for key in switchable},
                }
            )
        return {
            "exists": True,
            "cases": cases,
            "files": files,
            "systems": [{"key": key, "name": name} for key, name in switchable.items()],
        }

    def switchable(self) -> dict[str, str]:
        """The systems a case may run on their stand-in instead: connected after the build, with
        their stand-in still in `data/` (by key, with their name)."""

        from awa.blueprints._project_files import parse_steps

        project = self._project
        try:
            connectors = project.blueprint().connectors
        except Exception:  # noqa: BLE001 - no system: nothing to switch
            return {}
        found: dict[str, str] = {}
        for step in parse_steps(project.guide()):
            key = str(step.get("key") or "")
            if not key or (connectors.get(key) or {}).get("use") in (None, "workspace"):
                continue
            if _stand_in(project, key) is not None:
                found[key] = str(step.get("system") or key)
        return found

    def save(self, case_id: str, request: str, systems: Mapping[str, str]) -> dict[str, Any]:
        """Save a changed request and system choice of one case in `evals/cases.yaml`.

        Raises:
            ConfigurationError: If something named is unknown or the request is empty or too long.
        """

        from awa.evals import Dataset

        project = self._project
        text = request.strip()
        if not text or len(text) > MAX_REQUEST:
            raise ConfigurationError(
                f"The request must hold some text (at most {MAX_REQUEST} characters)"
            )
        switchable = self.switchable()
        for key, mode in systems.items():
            if key not in switchable or mode not in _MODES:
                raise ConfigurationError(
                    f"{key!r} cannot be switched for one case (only systems that were connected "
                    "after the build and still have their stand-in)"
                )
        dataset = project.dataset()
        try:
            case = dataset[case_id]
        except KeyError:
            raise ConfigurationError(f"There is no eval case {case_id!r}") from None
        metadata = dict(case.metadata or {})
        chosen = {key: mode for key, mode in systems.items() if mode == "stand-in"}
        if chosen:
            metadata["systems"] = chosen
        else:
            metadata.pop("systems", None)
        updated = case.model_copy(update={"input": text, "metadata": metadata})
        cases = tuple(updated if c.id == case_id else c for c in dataset.cases)
        Dataset(name=dataset.name, cases=cases).to_yaml(project.cases_file)
        return {"saved": True, "case": next(c for c in self.view()["cases"] if c["id"] == case_id)}

    def blueprint_for(self, case: EvalCase) -> Blueprint | None:
        """The blueprint this case runs on: the project's, with the systems the case put on
        their stand-in switched back (`None` when the case changes nothing)."""

        chosen = (case.metadata or {}).get("systems") or {}
        keys = [key for key, mode in chosen.items() if mode == "stand-in"]
        keys = [key for key in keys if key in self.switchable()]
        if not keys:
            return None
        return on_stand_ins(self._project, keys)


def on_stand_ins(project: Project, keys: list[str]) -> Blueprint:
    """The project's blueprint with the systems `keys` back on their stand-ins: the connector
    entry of the stand-in, and the agents' tools renamed back (`CONNECT.md` knows the renames)."""

    from awa.blueprints._project_files import parse_steps

    blueprint = project.blueprint()
    steps = {str(step.get("key")): step for step in parse_steps(project.guide())}
    connectors = dict(blueprint.connectors)
    back: dict[str, list[str]] = {}
    for key in keys:
        config = _stand_in(project, key)
        if config is None:
            continue
        connectors[key] = config
        for stand_in, targets in (steps[key].get("switch") or {}).items():
            for target in targets or ():
                back.setdefault(str(target), []).append(str(stand_in))
            if not targets:  # the live system had no counterpart: the stand-in tool comes back
                back.setdefault(f"connector:{key}", []).append(str(stand_in))

    def renamed(references: list[str]) -> list[str]:
        result: list[str] = []
        for reference in references:
            for target in back.get(reference, [reference]):
                if target not in result:
                    result.append(target)
        return result

    agents = [a.model_copy(update={"tools": renamed(list(a.tools))}) for a in blueprint.agents]
    approvals = [
        a.model_copy(update={"tool": back.get(a.tool, [a.tool])[0]}) for a in blueprint.approvals
    ]
    return blueprint.model_copy(
        update={"connectors": connectors, "agents": agents, "approvals": approvals}
    )


def _stand_in(project: Project, key: str) -> dict[str, Any] | None:
    """The workspace entry of `key`'s stand-in, when its data is still in `data/`."""

    data = project.directory / "data"
    if (data / f"{key}.db").is_file():
        return {"use": "workspace", "root": "data", "database": f"{key}.db"}
    if (data / key).is_dir():
        return {"use": "workspace", "root": f"data/{key}"}
    return None


def _names(text: str, name: str) -> bool:
    lowered = text.casefold()
    return name.casefold() in lowered or name.rsplit("/", 1)[-1].casefold() in lowered

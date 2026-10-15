"""The Data step: the user's files in the project, their profile, notes and the masking choice."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awa.data import DataError, DataProfile, FileProfile, Privacy, ProfileEvent, profile
from awa.errors import ConfigurationError
from awa.ui._data_cards import file_card, pending_card
from awa.ui._data_folder import megabytes
from awa.ui.ports import FileStore, StoredFile

FORMAT = "awa.ui.data/v1"
MASKS = ("none", "placeholders", "drop")
MAX_NOTE = 1000
Emit = Callable[[str, dict[str, Any]], None]
DATA_STATUS = {
    "awa.data.too_large": 413,
    "awa.data.too_many_files": 413,
    "awa.data.type_mismatch": 415,
    "awa.data.unsupported_type": 415,
    "awa.data.zip_unsafe": 422,
}
"""The HTTP status of a refused file, by the code of the `awa.data` error."""


@dataclass(frozen=True)
class DataRequest:
    """What a build is given: the stored files, what the user wants done with each, the mask.

    Attributes:
        notes: What must happen with a file, keyed the way a build looks files up.
        mask: What the model sees of personal data: `none`, `placeholders` or `drop`.
    """

    paths: list[Path]
    notes: dict[str, str] = field(default_factory=dict)
    mask: str = "none"


def _write_json(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step, so a reader never sees half a file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class DataStep:
    """The files of one project and everything the Data page shows about them.

    Notes and the masking choice are kept in `.awa/data.json` (`awa.ui.data/v1`); a file written
    by a newer awa is refused, not overwritten.
    """

    def __init__(self, store: FileStore, profile_file: Path, settings_file: Path) -> None:
        self.store = store
        self._profile_file = profile_file
        self._settings_file = settings_file

    def _settings(self) -> dict[str, Any]:
        if not self._settings_file.is_file():
            return {"format": FORMAT, "mask": "none", "notes": {}}
        try:
            found = json.loads(self._settings_file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConfigurationError(
                f"{self._settings_file.name} is not valid JSON: {exc}"
            ) from None
        if found.get("format") != FORMAT:
            raise ConfigurationError(
                f"{self._settings_file.name} was written by a different awa version "
                f"({found.get('format')!r}); move it away to start again"
            )
        return {
            "format": FORMAT,
            "mask": found.get("mask", "none"),
            "notes": found.get("notes", {}),
        }

    def _save(self, settings: dict[str, Any]) -> None:
        _write_json(self._settings_file, json.dumps(settings, indent=2, ensure_ascii=False))

    def _profile(self) -> DataProfile | None:
        if not self._profile_file.is_file():
            return None
        try:
            return DataProfile.from_json(self._profile_file.read_text(encoding="utf-8"))
        except ValueError:
            return None  # damaged or from another version: analyze again

    def state(self) -> dict[str, Any]:
        """Everything the Data page and the Describe page show: limits, files with their cards
        and the masking choice."""

        stored = self.store.listing()
        settings = self._settings()
        profiled: dict[str, FileProfile] = {}
        found = self._profile()
        if found is not None:
            profiled = {f.path: f for f in found.files}
        cards = [self._card(item, profiled.get(item.name), settings["notes"]) for item in stored]
        return {
            "files": cards,
            "mask": settings["mask"],
            "analysed": bool(cards) and all(card["status"] != "new" for card in cards),
            "used_text": megabytes(self.store.used()),
            "limits": {
                "file": megabytes(self.store.max_file_bytes),
                "total": megabytes(self.store.max_total_bytes),
                "files": self.store.max_files,
                "kinds": sorted(self.store.kinds),
            },
        }

    @staticmethod
    def _card(item: StoredFile, found: FileProfile | None, notes: dict[str, str]) -> dict[str, Any]:
        note = notes.get(item.name, "")
        if found is not None and found.sha256 == item.sha256:
            return file_card(found, note)
        return pending_card(item.name, item.size, item.kind, note)

    async def receive(self, name: str, chunks: AsyncIterator[bytes]) -> list[dict[str, Any]]:
        """Store an uploaded file (see `FileStore.receive`); the files are analyzed afterwards."""

        return [
            {"name": item.name, "size_text": megabytes(item.size), "kind": item.kind}
            for item in await self.store.receive(name, chunks)
        ]

    def analyse(self, emit: Emit) -> dict[str, Any]:
        """Profile every stored file, keep the result in `data/PROFILE.json` and return the page's
        state; `emit("progress", {"message": ...})` reports each file as it is read."""

        settings = self._settings()
        if not self.store.listing():
            self._profile_file.unlink(missing_ok=True)
            return self.state()

        def report(event: ProfileEvent) -> None:
            if event.file is not None and event.stage == "start":
                emit("progress", {"message": f"Reading {event.path or event.file}"})

        result = profile(
            [self.store.root],
            root=self.store.root,
            privacy=Privacy(mask=settings["mask"]),
            on_progress=report,
        )
        _write_json(self._profile_file, result.to_json())
        return self.state()

    def remove(self, name: str) -> dict[str, Any]:
        """Delete a stored file, its note and its entry in the profile."""

        self.store.remove(name)
        settings = self._settings()
        if settings["notes"].pop(name, None) is not None:
            self._save(settings)
        found = self._profile()
        if found is not None:
            found.files = [f for f in found.files if f.path != name]
            if found.files:
                _write_json(self._profile_file, found.to_json())
            else:
                self._profile_file.unlink(missing_ok=True)
        return self.state()

    def set_note(self, name: str, text: str) -> None:
        """Keep what the user wants done with the stored file `name` (empty text removes it).

        Raises:
            DataError: If `name` is not a stored file or the text is too long.
        """

        if name not in {item.name for item in self.store.listing()}:
            raise DataError(f"{name!r}: there is no such file in your data")
        if len(text) > MAX_NOTE:
            raise DataError(f"The note is longer than {MAX_NOTE} characters; shorten it")
        settings = self._settings()
        if text.strip():
            settings["notes"][name] = text.strip()
        else:
            settings["notes"].pop(name, None)
        self._save(settings)

    def mask(self) -> str:
        """What the AI sees of personal data: `none`, `placeholders` or `drop`."""

        return str(self._settings()["mask"])

    def set_mask(self, mask: str) -> None:
        """Choose what the AI sees of personal data: `none`, `placeholders` or `drop`."""

        if mask not in MASKS:
            raise DataError(f"{mask!r} is not a masking mode; use one of {', '.join(MASKS)}")
        settings = self._settings()
        settings["mask"] = mask
        self._save(settings)

    def request(self) -> DataRequest | None:
        """The stored files, notes and masking mode a build takes (`None` without files).

        A note is keyed by its file's base name, or by a glob ending in its relative name when
        two files share one, as a build resolves names relative to the folder it was given.
        """

        stored = self.store.listing()
        if not stored:
            return None
        settings = self._settings()
        bases = [item.name.rsplit("/", 1)[-1] for item in stored]
        notes = {}
        for item in stored:
            text = settings["notes"].get(item.name)
            if text:
                base = item.name.rsplit("/", 1)[-1]
                notes[base if bases.count(base) == 1 else "*" + item.name] = text
        return DataRequest(
            paths=[self.store.root / item.name for item in stored],
            notes=notes,
            mask=settings["mask"],
        )

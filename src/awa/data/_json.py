"""JSON and JSON Lines files (`json`): the schema as key paths (`customer.id`, `items[].sku`)
over sampled records; an object of eight or more objects is a table keyed by id (`*`)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from awa.data._injection import injection_flags
from awa.data._model import FileProfile, Flag, JsonField, JsonInfo
from awa.data._parse import ParseContext

KINDS = ("json",)

MAX_PATHS = 300
MAX_PATH_DEPTH = 12
MAX_ENUM_VALUES = 12
_TRACKED_VALUES = 50
_FORMAT_PROBES = 200
_KEYED_MIN = 8
_SCANNED_STRINGS = 20_000
_MIN_SCANNED_CHARS = 12
_MAX_VARIANT_GROUPS = 20

ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FORMATS = (
    ("datetime", ISO_DATETIME),
    ("date", ISO_DATE),
    ("email", re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")),
    ("uuid", re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")),
    ("url", re.compile(r"https?://\S+")),
    ("number_text", re.compile(r"[-+]?\d[\d.,' ]*\d|\d")),
)
_TIME_NAME = re.compile(r"(?:date|time|stamp|_at$|^ts$|created|updated|modified|datum|zeit)", re.I)
_EPOCH = {
    "epoch_seconds": (946_684_800, 4_102_444_800),
    "epoch_millis": (946_684_800_000, 4_102_444_800_000),
}
_TYPE_ORDER = ("string", "integer", "number", "boolean", "null", "object", "array")
_TYPE_NAMES = {bool: "boolean", int: "integer", float: "number", str: "string", dict: "object"}


class _Path:
    """What was seen at one key path."""

    __slots__ = (
        "epoch",
        "formats",
        "in_array",
        "overflow",
        "parent",
        "present",
        "strings",
        "texts",
        "types",
    )

    def __init__(self, parent: str, in_array: bool) -> None:
        self.parent, self.in_array = parent, in_array
        self.types: set[str] = set()
        self.present = self.texts = 0
        self.strings: Counter[str] = Counter()
        self.overflow = False
        self.formats: Counter[str] = Counter()
        self.epoch: Counter[str] = Counter()


class _Tally:
    """Collects paths, depth and injection hints over the visited records."""

    def __init__(self, ctx: ParseContext) -> None:
        self.ctx = ctx
        self.paths: dict[str, _Path] = {}
        self.objects: Counter[str] = Counter()
        self.depth = 0
        self.cut_paths = False
        self.too_deep = False
        self.keys: set[str] = set()
        self.scanned: set[str] = set()
        self.hints: dict[tuple[str, str | None], Flag] = {}
        self.visited = 0

    def visit(self, root: Any, depth: int) -> None:
        """Walk `root` (iteratively: nesting is attacker controlled) and note everything in it."""

        self.visited += 1
        if self.visited % 500 == 0:
            self.ctx.check_deadline()
        stack = [(root, "", depth)]
        while stack:
            value, path, level = stack.pop()
            if not isinstance(value, dict | list):
                continue
            self.depth = max(self.depth, level)
            children: list[tuple[Any, str, int]] = []
            if isinstance(value, dict):
                self.objects[path] += 1
                keyed = len(value) >= _KEYED_MIN and all(
                    isinstance(v, dict) for v in value.values()
                )
                for key, item in value.items():
                    name = "*" if keyed else key
                    self.keys.add(name)
                    child = f"{path}.{name}" if path else name
                    self._note(child, path, False, item, level)
                    children.append((item, child, level + 1))
            else:
                child = f"{path}[]"
                for item in value:
                    self._note(child, path, True, item, level)
                    children.append((item, child, level + 1))
            stack.extend(reversed(children))

    def _note(self, path: str, parent: str, in_array: bool, value: Any, level: int) -> None:
        if level > MAX_PATH_DEPTH:
            self.too_deep = True
            return
        seen = self.paths.get(path)
        if seen is None:
            if len(self.paths) >= MAX_PATHS:
                self.cut_paths = True
                return
            seen = self.paths[path] = _Path(parent, in_array)
        kind = _type_name(value)
        seen.types.add(kind)
        seen.present += 1
        if kind == "string":
            self._note_string(path, seen, value)
        elif kind in ("integer", "number") and _TIME_NAME.search(path.rpartition(".")[2]):
            for name, (low, high) in _EPOCH.items():
                if low <= value <= high:
                    seen.epoch[name] += 1

    def _note_string(self, path: str, seen: _Path, text: str) -> None:
        if seen.texts < _FORMAT_PROBES and text:
            seen.texts += 1
            probed = text.strip()
            seen.formats[
                next((name for name, rule in _FORMATS if rule.fullmatch(probed)), "text")
            ] += 1
        if text in seen.strings or len(seen.strings) < _TRACKED_VALUES:
            seen.strings[text] += 1
        else:
            seen.overflow = True
        if (
            len(text) >= _MIN_SCANNED_CHARS
            and len(self.scanned) < _SCANNED_STRINGS
            and text not in self.scanned
        ):
            self.scanned.add(text)
            for flag in injection_flags(text, f"{self.ctx.file_id}:{path}"):
                self.hints.setdefault((flag.code, flag.where), flag)

    def fields(self) -> list[JsonField]:
        limit = self.ctx.limits.max_chars_per_cell
        fields = []
        for path, seen in self.paths.items():
            parents = self.objects[seen.parent] if not seen.in_array else 0
            missing = 1 - seen.present / parents if parents else 0.0
            nonempty = sum(seen.formats.values())
            formats = {name for name, count in seen.formats.items() if count}
            fmt = next(iter(formats)) if len(formats) == 1 and nonempty else None
            if len(seen.epoch) == 1 and sum(seen.epoch.values()) >= 0.9 * seen.present:
                fmt = next(iter(seen.epoch))
            enum_like = (
                seen.types == {"string"}
                and not seen.overflow
                and len(seen.strings) <= MAX_ENUM_VALUES
                and seen.present >= 3 * len(seen.strings)
            )
            values = sorted(seen.strings.items(), key=lambda item: (-item[1], item[0]))
            fields.append(
                JsonField(
                    path=path,
                    types=sorted(seen.types, key=_TYPE_ORDER.index),
                    optional_share=round(max(missing, 0.0), 4),
                    enum=[value[:limit] for value, _ in values] if enum_like else [],
                    format=None if fmt == "text" else fmt,
                )
            )
        return fields

    def key_variants(self) -> list[list[str]]:
        groups: dict[str, set[str]] = {}
        for key in self.keys:
            groups.setdefault(re.sub(r"[\s_-]", "", key).lower(), set()).add(key)
        variants = sorted(sorted(spellings) for spellings in groups.values() if len(spellings) > 1)
        return variants[:_MAX_VARIANT_GROUPS]


def profile_json(path: Path, ctx: ParseContext) -> FileProfile:
    """Profile a JSON or JSON Lines file: key paths, types, optionality, enums, formats, depth."""

    if ctx.profile.size == 0:
        return ctx.fail("DF-E-EMPTY", "the file is empty")
    tally = _Tally(ctx)
    if _is_lines(path):
        records, seen, bad = _read_lines(path, ctx, tally)
        info = JsonInfo(top_level="jsonl", records=records, sampled=seen < records)
        if not seen or bad == seen:
            return ctx.fail("DF-E-PARSE-FAILED", "no line of the file is valid JSON")
        if bad:
            ctx.flag("DF-W-JSON-BAD-LINES", f"{bad} of {seen} lines read are not valid JSON")
    else:
        try:
            value = json.loads(_decode(path.read_bytes()))
        except RecursionError:
            return ctx.fail("DF-E-PARSE-FAILED", "the JSON is nested too deeply to read safely")
        except ValueError as error:
            return ctx.fail("DF-E-PARSE-FAILED", f"the file is not valid JSON ({error})")
        info = _from_value(value, ctx, tally)
    info.depth = tally.depth
    info.paths = tally.fields()
    info.key_variants = tally.key_variants()
    ctx.profile.json_doc = info
    if info.sampled:
        ctx.profile.status = "partial"
        ctx.flag(
            "DF-W-SAMPLED",
            f"the schema comes from an even sample of {ctx.limits.sample_rows} records",
        )
    if tally.cut_paths:
        ctx.profile.status = "partial"
        ctx.flag("DF-W-SAMPLED", f"only the first {MAX_PATHS} key paths are listed")
    if tally.too_deep:
        ctx.flag("DF-W-JSON-TOO-DEEP", f"key paths below {MAX_PATH_DEPTH} levels are not listed")
    ctx.profile.flags.extend(tally.hints.values())
    ctx.profile.flags.extend(injection_flags(" ".join(sorted(tally.keys)), ctx.file_id))
    return ctx.profile


def _from_value(value: Any, ctx: ParseContext, tally: _Tally) -> JsonInfo:
    if isinstance(value, dict):
        tally.visit(value, 1)
        return JsonInfo(top_level="object")
    if not isinstance(value, list):
        return JsonInfo(top_level="scalar")
    limit = ctx.limits.sample_rows
    stride = max(1, -(-len(value) // limit))
    chosen = value[::stride]
    info = JsonInfo(top_level="array", records=len(value), sampled=stride > 1)
    if sum(isinstance(item, dict) for item in chosen) * 2 >= len(chosen) and chosen:
        for record in chosen:
            if isinstance(record, dict):
                tally.visit(record, 2)
    else:
        tally.visit(chosen, 1)
    return info


def _type_name(value: Any) -> str:
    return "null" if value is None else _TYPE_NAMES.get(type(value), "array")


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _is_lines(path: Path) -> bool:
    """JSON Lines: named so, or the first lines are each a complete JSON object."""

    if path.suffix.lower() in (".jsonl", ".ndjson"):
        return True
    with path.open("rb") as handle:
        lines = [line for line in handle.read(1 << 20).splitlines() if line.strip()][:3]
    try:
        return len(lines) > 1 and all(isinstance(json.loads(_decode(line)), dict) for line in lines)
    except ValueError:
        return False


def _read_lines(path: Path, ctx: ParseContext, tally: _Tally) -> tuple[int, int, int]:
    """Count the records of a JSON Lines file and visit an even sample; `(records, seen, bad)`."""

    with path.open("rb") as handle:
        total = 0
        for line in handle:
            total += bool(line.strip())
            if total % 10_000 == 0:
                ctx.check_deadline()
    stride = max(1, -(-total // ctx.limits.sample_rows))
    seen = bad = index = 0
    with path.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            if index % stride == 0:
                seen += 1
                try:
                    record = json.loads(_decode(line))
                except (ValueError, RecursionError):
                    bad += 1
                else:
                    tally.visit(record, 2)
            index += 1
    return total, seen, bad

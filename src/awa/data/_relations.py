"""Join candidates between the columns of files, sheets and tables."""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import cached_property, lru_cache
from typing import NamedTuple

from awa._internal.text import stem
from awa.data._infer import id_named
from awa.data._model import (
    ColumnProfile,
    DataProfile,
    FileProfile,
    Flag,
    Relation,
    TableProfile,
    column_key,
    make_flag,
)
from awa.data.ports import ColumnValues, ValueSource

_COMPARABLE = frozenset({"text", "id", "enum", "integer"})
_GENERIC = frozenset({"id", "ids", "nr", "no", "num", "number", "nummer", "key", "code", "ref"})
_MAX_PAIRS = 250  # candidate pairs whose values are compared
_MAX_BUCKET = 60  # columns sharing one name word that are paired with each other
_MAX_COMPARED = 1000  # distinct values of the referencing side that are looked up
_LONG_TEXT = 60  # an example longer than this is free text, not an identifier
_FEW = 5  # distinct values needed for full value evidence
_NAME_LINK = 0.7  # name evidence from which two columns are compared at all
_MIN_VALUE = 0.5  # share of the values that must match (exactly or as a quoted key)
_FOLD = re.compile(r"[^0-9a-z]+")
_TOKEN = re.compile(r"[0-9A-Za-z]+")
_NUMBER = re.compile(r"[^\d]*(\d{3,})[^\d]*")
_CONTAINED, _NUMERIC = 0.6, 0.4  # weight of a value that contains a key / carries its number


class Links(NamedTuple):
    """What `analyze_links` found: the relations (strongest first) and the flags about them."""

    relations: list[Relation]
    flags: list[Flag]


class _Col(NamedTuple):
    id: str
    column: ColumnProfile
    table: TableProfile
    stems: frozenset[str]  # words of the table, sheet and file name (`customers` -> `customer`)
    core: frozenset[str]  # words of the column name besides `id`, `nr` and the like


class _Side:
    """The distinct values of one column, prepared once for every pair the column is in."""

    def __init__(self, values: frozenset[str]) -> None:
        self.values = values

    @cached_property
    def spread(self) -> list[str]:
        """The values to look up when this column refers to another one: sorted, thinned evenly
        to `_MAX_COMPARED` (the same input gives the same list)."""

        ordered = sorted(self.values)
        if len(ordered) <= _MAX_COMPARED:
            return ordered
        return [ordered[i * len(ordered) // _MAX_COMPARED] for i in range(_MAX_COMPARED)]

    @cached_property
    def folded(self) -> frozenset[str]:
        return frozenset(_fold(value) for value in self.values)

    @cached_property
    def numbers(self) -> frozenset[str]:
        """The digit runs of three or more digits that a key is written with."""

        return frozenset(m[1] for value in self.values if (m := _NUMBER.fullmatch(value)))


class _Match(NamedTuple):
    """How the distinct values of the referencing column meet the keys."""

    exact: int
    folded: int  # equal after ignoring case, spaces and punctuation
    contained: int  # holds a key as a word or a pair of words
    numeric: int  # holds the number of a key under another prefix
    total: int
    unmatched: list[str]


def analyze_links(profile: DataProfile, values: ValueSource | None = None) -> Links:
    """Relations between the tables of a profile, strongest first, and the quality flags about them.

    `values` supplies distinct column values; `None` reads the profiled files again and a column it
    cannot read is judged by name. Deterministic for the same profile and values.
    """

    found = _declared(profile)
    flags = _duplicate_files(profile)
    pairs = _candidates(_columns(profile))
    if not pairs:
        return Links(_ranked(found), flags)
    unread: dict[str, str] = {}
    if values is None:
        from awa.data._values import local_values

        values = local_values(profile, on_error=unread.__setitem__)
    ids = sorted({c.id for a, b, _ in pairs for c in (a, b)})
    read = values(ids)
    for table_id, reason in sorted(unread.items()):
        message = f"the values of table {table_id} could not be read again ({reason}); its links are judged by column name only"
        flags.append(make_flag("DF-W-VALUES-UNREAD", message, table_id))
    declared = {(r.from_column, r.to_column) for r in found}
    sides: dict[str, _Side] = {}
    for a, b, name in pairs:
        compared = _compare(a, b, name, read.get(a.id), read.get(b.id), sides)
        if compared is None or (compared[0].from_column, compared[0].to_column) in declared:
            continue
        found.append(compared[0])
        flags.extend(compared[1])
    return Links(_ranked(found), flags)


def relations(profile: DataProfile, values: ValueSource | None = None) -> list[Relation]:
    """The likely links between the columns of a profile, strongest first.

    `kind` is `foreign_key` (declared in SQLite, confidence 1.0), `value_overlap` (the share of
    distinct values found in a key column, ignoring case and punctuation) or `name_match` (names
    only; never 0.8). Use a link without asking only from confidence 0.8.

    Example:
        ```python
        from awa.data import profile, relations

        p = profile(["invoices.xlsx", "payments.csv"])
        for link in relations(p):
            print(link.from_column, "->", link.to_column, link.kind, link.confidence)
        # f2.t1.customer_id -> f1.t1.customer_id value_overlap 0.7
        # f2.t1.reference -> f1.t1.invoice_no value_overlap 0.43
        ```
    """

    return analyze_links(profile, values).relations


def with_relations(profile: DataProfile, values: ValueSource | None = None) -> DataProfile:
    """The profile with its relations, their flags and a recounted summary filled in."""

    links = analyze_links(profile, values)
    return DataProfile(
        awa_version=profile.awa_version,
        created=profile.created,
        limits=profile.limits,
        privacy=profile.privacy,
        files=profile.files,
        relations=links.relations,
        flags=[*profile.flags, *links.flags],
    )


def _words(key: str) -> frozenset[str]:
    return frozenset(stem(w) for w in key.split("_") if w)


@lru_cache(maxsize=1 << 16)  # the same text is folded for every pair its column is in
def _fold(text: str) -> str:
    return _FOLD.sub("", text.casefold())


def _columns(profile: DataProfile) -> list[_Col]:
    found = []
    for file, table in profile.tables():
        base = file.path.rsplit("/", 1)[-1]
        stems = _words(column_key(table.name or table.sheet or base))
        stems |= _words(column_key(base.rsplit(".", 1)[0]))
        for column in table.columns:
            long = any(len(example) > _LONG_TEXT for example in column.examples)
            counter = column.type == "integer" and not id_named(column.name)  # a quantity
            if column.type in _COMPARABLE and not long and not counter:
                core = _words(column.key) - _GENERIC
                found.append(_Col(f"{table.id}.{column.key}", column, table, stems, core))
    return found


def _name_score(a: _Col, b: _Col) -> float:
    """How well two column names fit: 1 equal, .9 same words besides `id`/`nr`, .85 a bare `id`
    in a table named after the other column, .7 very similar spelling, else 0."""

    ka, kb = a.column.key, b.column.key
    core_a, core_b = a.core, b.core
    if ka == kb and core_a:
        return 1.0
    if core_a and core_a == core_b:
        return 0.9
    if core_a and not core_b and core_a <= b.stems:
        return 0.85
    if core_b and not core_a and core_b <= a.stems:
        return 0.85
    if min(len(ka), len(kb)) >= 5 and SequenceMatcher(None, ka, kb).ratio() >= 0.85:
        return 0.7
    return 0.0


@lru_cache(maxsize=1 << 14)
def _folded_tokens(text: str) -> frozenset[str]:
    """Folded words and pairs of neighboring words of a text (`INV 3001` -> `inv3001`)."""

    words = _TOKEN.findall(text)
    folded = {_fold(w) for w in words} | {
        _fold(" ".join(words[i : i + 2])) for i in range(len(words) - 1)
    }
    return frozenset(t for t in folded if len(t) >= 4)


def _candidates(columns: list[_Col]) -> list[tuple[_Col, _Col, float]]:
    """Pairs of columns of different tables worth a look: names that fit, or a column that
    quotes the example keys of a unique column (`INV-3001 Mueller` next to `INV-3001`)."""

    pairs: dict[tuple[str, str], tuple[_Col, _Col, float]] = {}

    def add(a: _Col, b: _Col, name: float) -> None:
        if a.table.id != b.table.id:
            first, second = sorted((a, b), key=lambda c: c.id)
            pairs.setdefault((first.id, second.id), (first, second, name))

    by_word: dict[str, list[_Col]] = {}
    for col in columns:
        for word in sorted(col.core or col.stems):
            by_word.setdefault(word, []).append(col)
    for word in sorted(by_word):
        group = by_word[word][:_MAX_BUCKET]
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if (name := _name_score(a, b)) >= _NAME_LINK:
                    add(a, b, name)
    keys: dict[str, list[_Col]] = {}
    for col in columns:
        if col.column.unique and not col.column.nullable and col.column.type in ("text", "id"):
            for example in col.column.examples:
                if len(_fold(example)) >= 4:
                    keys.setdefault(_fold(example), []).append(col)
    for col in columns:
        if col.column.type in ("text", "id", "enum"):
            for example in col.column.examples:
                for token in _folded_tokens(example):
                    for key_col in keys.get(token, []):
                        add(col, key_col, _name_score(col, key_col))
    ranked = sorted(pairs.values(), key=lambda p: (-p[2], p[0].id, p[1].id))
    return ranked[:_MAX_PAIRS]


def _keylike(col: _Col, values: ColumnValues | None) -> bool:
    """Unique and never empty: by the profile, or by the values read."""

    declared = {k[0] for k in col.table.key_candidates if len(k) == 1}
    if col.column.key in declared or (col.column.unique and not col.column.nullable):
        return True
    return bool(
        values and values.complete and values.cells >= 2 and values.cells == len(values.values)
    )


def _orient(
    a: _Col, b: _Col, va: ColumnValues | None, vb: ColumnValues | None
) -> tuple[_Col, _Col]:
    """(referencing column, key column): the key side is the unique one, else the one with more
    distinct values, else the one that sorts first."""

    ka, kb = _keylike(a, va), _keylike(b, vb)
    if ka != kb:
        return (b, a) if ka else (a, b)
    size_a = len(va.values) if va else a.column.distinct or 0
    size_b = len(vb.values) if vb else b.column.distinct or 0
    if size_a != size_b:
        return (a, b) if size_a < size_b else (b, a)
    return (a, b) if a.id > b.id else (b, a)


def _match(source: Sequence[str], keys: _Side) -> _Match:
    exact = folded = contained = numeric = 0
    unmatched: list[str] = []
    for value in source:
        if value in keys.values:
            exact += 1
        elif _fold(value) in keys.folded:
            folded += 1
        elif not keys.folded.isdisjoint(_folded_tokens(value)):
            contained += 1
        elif keys.numbers and not keys.numbers.isdisjoint(
            _NUMBER.findall(value) + _TOKEN.findall(value)
        ):
            numeric += 1
        elif len(unmatched) < 3:
            unmatched.append(value)
    return _Match(exact, folded, contained, numeric, len(source), unmatched)


def _compare(
    a: _Col,
    b: _Col,
    name: float,
    va: ColumnValues | None,
    vb: ColumnValues | None,
    sides: dict[str, _Side],
) -> tuple[Relation, list[Flag]] | None:
    source, target = _orient(a, b, va, vb)
    vs, vt = (va, vb) if source is a else (vb, va)
    key = _keylike(target, vt)
    if not (vs and vt and vs.values and vt.values):  # no values to compare: names alone
        if name < 0.85 or not key:
            return None
        relation = Relation(
            from_column=source.id,
            to_column=target.id,
            kind="name_match",
            confidence=round(0.5 * name + 0.1, 4),
        )
        return relation, []
    side_s = sides.setdefault(source.id, _Side(vs.values))
    match = _match(side_s.spread, sides.setdefault(target.id, _Side(vt.values)))
    exact = (match.exact + match.folded) / match.total
    weighted = min(
        1.0, exact + (_CONTAINED * match.contained + _NUMERIC * match.numeric) / match.total
    )
    if weighted < _MIN_VALUE:  # the values disprove a link that the names suggest
        return None
    confidence = (
        0.55 * weighted * min(1.0, match.total / _FEW) + 0.35 * name + (0.10 if key else 0.0)
    )
    if not key and not _keylike(source, vs):
        confidence = min(confidence, 0.7)
    relation = Relation(
        from_column=source.id,
        to_column=target.id,
        kind="value_overlap",
        overlap=round(exact, 4),
        confidence=round(confidence, 4),
    )
    return relation, _quality_flags(relation, match, vs.complete and vt.complete)


def _quality_flags(relation: Relation, match: _Match, complete: bool) -> list[Flag]:
    where, to = relation.from_column, relation.to_column
    equal = match.exact + match.folded
    weak = match.contained + match.numeric
    flags = []
    if weak and equal / match.total < 0.8:
        message = (
            f"{weak} of {match.total} distinct values contain a key of {to} inside other text or under "
            f"another prefix ({equal} equal a key): extract the key before joining"
        )
        flags.append(make_flag("DF-W-REFERENCE-NEEDS-PARSING", message, where))
    if match.folded:
        message = (
            f"{match.folded} of {match.total} distinct values equal a key of {to} only after ignoring "
            "case, spaces and punctuation: normalize them before joining"
        )
        flags.append(make_flag("DF-W-KEY-SPELLING-DIFFERS", message, where))
    gone = match.total - equal - weak
    if complete and relation.confidence >= 0.6 and gone > 0:
        relation.unmatched = [value[:20] for value in match.unmatched[:2]]
        message = (
            f"{gone} of {match.total} distinct values have no counterpart in {to} "
            "(the relation lists a few of them)"
        )
        flags.append(make_flag("DF-W-ORPHAN-VALUES", message, where))
    return flags


def _declared(profile: DataProfile) -> list[Relation]:
    """Foreign keys declared in SQLite databases: certain, by column ids."""

    found = []
    for file in profile.files:
        if file.sqlite is None:
            continue
        by_name = {t.name: t for t in file.tables if t.name}
        for fk in file.sqlite.foreign_keys:
            table, target = by_name.get(fk.table), by_name.get(fk.ref_table)
            if table is None or target is None:
                continue
            for own, other in zip(fk.columns, fk.ref_columns, strict=False):
                if table.column(own) and target.column(other):
                    found.append(
                        Relation(
                            from_column=f"{table.id}.{column_key(own)}",
                            to_column=f"{target.id}.{column_key(other)}",
                            kind="foreign_key",
                            confidence=1.0,
                        )
                    )
    return found


def _duplicate_files(profile: DataProfile) -> list[Flag]:
    first: dict[str, FileProfile] = {}
    flags = []
    for file in profile.files:
        if not file.sha256 or file.status == "error":
            continue
        original = first.setdefault(file.sha256, file)
        if original is not file and file.size > 0:
            message = f"{file.path} has exactly the content of {original.path} ({original.id}); one of them is redundant"
            flags.append(make_flag("DF-W-DUPLICATE-FILE", message, file.id))
    return flags


def _ranked(found: list[Relation]) -> list[Relation]:
    return sorted(found, key=lambda r: (-r.confidence, r.from_column, r.to_column))

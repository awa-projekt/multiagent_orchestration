"""The contract of an `ArtifactRepository`: JSON values under string refs."""

from __future__ import annotations

import asyncio
import math
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

import pytest

from awa.persistence.artifacts import ArtifactRepository
from awa.testing.contracts._base import AdapterContract, Place

JSON_VALUES: list[Any] = [
    "plain text",
    "",
    {"nested": [1, 2.5, None, True, {"k": "v"}]},
    [],
    {},
    None,
    0,
    False,
    1e20,
    2**70,
    "NUL \x00 inside",
    "日本語 and \U0001f600 and Müller",
    {"b": 1, "a": 2},
    [[[[["deep"]]]]],
]
"""Values every backend must read back equal, including the ones a `jsonb` column would change."""

REFS = ["a:b", "a/b", "a b", "ü", "%", "_", "x" * 300, "report:42"]


@dataclass(frozen=True)
class ArtifactRepositoryCapabilities:
    """What a repository promises beyond the base contract.

    Attributes:
        survives_restart: A second repository opened on the same `Place` sees the artifacts.
        refs_with_nul: A ref may contain a NUL character. Without it (PostgreSQL) `put` raises
            `ValueError` and the other methods treat such a ref as unstored.
    """

    survives_restart: bool = False
    refs_with_nul: bool = False


class ArtifactRepositoryContract(AdapterContract):
    """Run this against an `ArtifactRepository`: implement `opened`."""

    capabilities = ArtifactRepositoryCapabilities()

    def opened(self, place: Place) -> AbstractAsyncContextManager[ArtifactRepository]:
        """An async context manager yielding a repository on `place`; opening twice is a restart."""

        raise NotImplementedError

    @pytest.mark.parametrize("value", JSON_VALUES, ids=lambda v: repr(v)[:20])
    async def test_a_value_reads_back_exactly_as_stored(self, place: Place, value: Any) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", value)

            assert await repository.get("ref") == value

    async def test_a_lone_surrogate_in_a_string_is_stored_and_read_back(self, place: Place) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", "broken \ud800 text")

            assert await repository.get("ref") == "broken \ud800 text"

    async def test_a_value_is_stored_as_json_tuples_become_lists_and_keys_text(
        self, place: Place
    ) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", {1: (1, 2), "b": True})

            assert await repository.get("ref") == {"1": [1, 2], "b": True}

    async def test_floats_keep_their_sign_and_integers_their_type(self, place: Place) -> None:
        async with self.opened(place) as repository:
            await repository.put("zero", -0.0)
            await repository.put("one", 1)
            await repository.put("one_float", 1.0)

            assert math.copysign(1, await repository.get("zero")) == -1
            assert type(await repository.get("one")) is int
            assert type(await repository.get("one_float")) is float

    async def test_object_keys_keep_their_order(self, place: Place) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", {"b": 1, "a": 2, "c": 3})

            assert list(await repository.get("ref")) == ["b", "a", "c"]

    @pytest.mark.parametrize("ref", REFS, ids=lambda r: r[:12])
    async def test_any_string_is_a_ref(self, place: Place, ref: str) -> None:
        async with self.opened(place) as repository:
            await repository.put(ref, {"ref": ref})

            assert await repository.exists(ref)
            assert await repository.get(ref) == {"ref": ref}
            assert await repository.list_refs() == [ref]

    async def test_a_ref_with_a_nul_character_is_a_ref_or_refused_with_value_error(
        self, place: Place
    ) -> None:
        async with self.opened(place) as repository:
            if self.capabilities.refs_with_nul:
                await repository.put("a\x00b", 1)

                assert await repository.get("a\x00b") == 1
                assert await repository.list_refs() == ["a\x00b"]
            else:
                with pytest.raises(ValueError, match="NUL"):
                    await repository.put("a\x00b", 1)

                assert not await repository.exists("a\x00b")
                assert not await repository.delete("a\x00b")
                assert await repository.list_refs("a\x00") == []
                with pytest.raises(KeyError):
                    await repository.get("a\x00b")

    async def test_a_missing_ref_raises_key_error_and_does_not_exist(self, place: Place) -> None:
        async with self.opened(place) as repository:
            with pytest.raises(KeyError):
                await repository.get("missing")

            assert not await repository.exists("missing")

    async def test_put_replaces_the_value(self, place: Place) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", {"v": 1})
            await repository.put("ref", {"v": 2})

            assert await repository.get("ref") == {"v": 2}
            assert await repository.list_refs() == ["ref"]

    async def test_a_value_is_stored_as_a_copy(self, place: Place) -> None:
        async with self.opened(place) as repository:
            value = {"list": [1]}
            await repository.put("ref", value)
            value["list"].append(2)
            first = await repository.get("ref")
            first["list"].append(3)

            assert await repository.get("ref") == {"list": [1]}

    async def test_delete_reports_whether_there_was_a_value_and_is_idempotent(
        self, place: Place
    ) -> None:
        async with self.opened(place) as repository:
            await repository.put("ref", 1)

            assert await repository.delete("ref") is True
            assert await repository.delete("ref") is False
            assert await repository.delete("never") is False
            assert not await repository.exists("ref")
            with pytest.raises(KeyError):
                await repository.get("ref")

    async def test_list_refs_is_sorted_and_filters_by_a_literal_prefix(self, place: Place) -> None:
        refs = ["run:b", "run:a", "other:z", "run_c", "RUN:d", "run%e"]
        async with self.opened(place) as repository:
            for ref in refs:
                await repository.put(ref, 1)

            assert await repository.list_refs("run:") == ["run:a", "run:b"]
            assert await repository.list_refs("run_") == ["run_c"]
            assert await repository.list_refs("run%") == ["run%e"]
            assert await repository.list_refs("nothing") == []
            assert await repository.list_refs() == sorted(refs)

    async def test_values_json_cannot_hold_are_refused(self, place: Place) -> None:
        async with self.opened(place) as repository:
            with pytest.raises(TypeError):
                await repository.put("ref", object())
            with pytest.raises(ValueError):
                await repository.put("ref", math.nan)
            with pytest.raises(ValueError):
                await repository.put("ref", {"x": math.inf})

            assert not await repository.exists("ref")

    async def test_concurrent_puts_of_different_refs_all_arrive(self, place: Place) -> None:
        async with self.opened(place) as repository:
            await asyncio.gather(*[repository.put(f"ref:{n:02d}", n) for n in range(20)])

            assert await repository.list_refs("ref:") == [f"ref:{n:02d}" for n in range(20)]

    async def test_artifacts_survive_a_restart(self, place: Place) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("a single-process repository: a restart starts empty")
        async with self.opened(place) as first:
            await first.put("kept", {"v": 1})
        async with self.opened(place) as second:
            assert await second.get("kept") == {"v": 1}

    async def test_a_single_process_repository_starts_empty_after_a_restart(
        self, place: Place
    ) -> None:
        if self.capabilities.survives_restart:
            pytest.skip("a durable repository keeps its artifacts")
        async with self.opened(place) as first:
            await first.put("lost", 1)
        async with self.opened(place) as second:
            assert await second.list_refs() == []


__all__ = ["ArtifactRepositoryCapabilities", "ArtifactRepositoryContract"]

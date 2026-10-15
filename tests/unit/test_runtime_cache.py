"""The cache settings block, the fail-open `Cache` and `InMemoryCacheStore` limits."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from awa.core.settings import Settings
from awa.errors import CacheStoreError
from awa.persistence.cache import InMemoryCacheStore, SqliteCacheStore, build_cache
from awa.runtime.cache import DEFAULT_SALT, Cache, CacheSettings, cache_key, tenant_of
from awa.runtime.ports import CacheScope


class _Broken:
    """A store whose every call fails, like a database that went away."""

    calls = 0

    def _fail(self, *args: object, **kwargs: object) -> None:
        _Broken.calls += 1
        raise CacheStoreError("database is gone")

    get = put = entries = clear = stats = close = _fail


def test_the_defaults_cache_tools_for_one_run() -> None:
    settings = CacheSettings()

    assert settings.uses("prompt") and settings.uses("tools") and settings.uses("steps")
    assert settings.tools_ttl is None and settings.store == "memory"


def test_the_master_switch_turns_every_cache_off() -> None:
    settings = CacheSettings(enabled=False)

    assert not any(settings.uses(k) for k in ("prompt", "tools", "steps"))
    assert settings.prompt_mode == "off"


def test_environment_values_win_over_the_blueprint_block() -> None:
    env = Settings(
        _env_file=None,
        cache_prompt="off",
        cache_tools_ttl=30,
        cache_steps=False,
        cache_store="sqlite",
        cache_path="x.db",
    )

    merged = CacheSettings(prompt="1h", steps=True).with_environment(env)

    assert (merged.prompt, merged.tools_ttl, merged.steps) == ("off", 30, False)
    assert (merged.store, merged.path) == ("sqlite", "x.db")
    assert CacheSettings().with_environment(Settings(_env_file=None)) == CacheSettings()


def test_unknown_keys_and_bad_values_are_refused() -> None:
    with pytest.raises(ValidationError):
        CacheSettings.model_validate({"prompts": "auto"})
    with pytest.raises(ValidationError):
        CacheSettings(prompt="2h")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CacheSettings(tools_ttl=0)


def test_keys_are_digests_that_hide_their_parts_and_ignore_mapping_order() -> None:
    key = cache_key("acme", "lookup", {"b": 1, "a": "sk-secret-value"})

    assert key == cache_key("acme", "lookup", {"a": "sk-secret-value", "b": 1})
    assert len(key) == 64 and "secret" not in key and "acme" not in key
    assert key != cache_key("globex", "lookup", {"a": "sk-secret-value", "b": 1})


def test_a_broken_store_is_a_miss_logged_once_and_never_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache = Cache(CacheSettings(), _Broken())  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="awa.cache"):
        assert cache.get("tools", "acme", "k") is None
        cache.put("tools", "acme", "k", b"v", ttl=None)
        assert cache.entries("examples", "acme") == []
        assert cache.get("tools", "acme", "k") is None

    assert _Broken.calls == 4
    assert len([r for r in caplog.records if "cache store failed" in r.message]) == 1
    assert cache.counts.as_dict() == {"tools": {"hits": 0, "misses": 2}}


def test_entries_of_two_tenants_never_meet() -> None:
    cache = Cache(CacheSettings(), InMemoryCacheStore(), project="invoices")
    cache.put("tools", "acme", "k", b"acme", ttl=None)

    assert cache.get("tools", "globex", "k") is None
    assert cache.get("tools", "acme", "k") == b"acme"
    assert cache.get("tools", None, "k") is None  # no tenant is the tenant "local"


def test_a_tenant_or_project_that_is_no_scope_part_is_hashed_not_rejected() -> None:
    cache = Cache(CacheSettings(), InMemoryCacheStore(), project="My Project/1")

    scope = cache.scope("tools", "tenant with spaces")

    assert isinstance(scope, CacheScope)
    assert scope.tenant != cache.scope("tools", "tenant-with-spaces").tenant
    assert tenant_of({"awa.tenant": "acme"}) == "acme" and tenant_of(None) == "local"


def test_without_a_salt_every_cache_object_uses_the_default_one() -> None:
    first, second = Cache(), Cache()  # a system built per run gets the same provider keys

    assert first.salt == second.salt == DEFAULT_SALT
    assert Cache(salt="s").salt == b"s"


def test_build_cache_opens_the_store_the_settings_name(tmp_path: Path) -> None:
    env = Settings(_env_file=None, cache_store="sqlite", cache_salt="pepper")

    cache = build_cache(CacheSettings(), project="invoices", base=tmp_path, settings=env)

    assert isinstance(cache.store, SqliteCacheStore)
    assert cache.store.path == tmp_path / ".awa" / "cache.db"
    assert cache.salt == b"pepper" and cache.project == "invoices"


def test_a_store_that_cannot_open_leaves_the_cache_without_a_store(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env = Settings(_env_file=None, cache_store="sqlite", cache_path=str(blocker / "c.db"))

    cache = build_cache(CacheSettings(), base=tmp_path, settings=env)

    assert cache.store is None
    assert not cache.uses("tools") and cache.uses("prompt")


def test_a_disabled_cache_opens_no_store(tmp_path: Path) -> None:
    env = Settings(_env_file=None, cache_enabled=False)

    assert build_cache(None, base=tmp_path, settings=env).store is None


def test_the_cache_file_is_made_private_to_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.core import dotenv

    restricted: list[Path] = []
    monkeypatch.setattr(dotenv, "restrict_to_owner", restricted.append)

    SqliteCacheStore(tmp_path / "cache.db")

    assert tmp_path / "cache.db" in restricted


SCOPE = CacheScope("local", "demo", "model")


def test_an_oversize_value_leaves_no_stale_value_behind() -> None:
    store = InMemoryCacheStore(max_bytes=10)
    store.put(SCOPE, "k", b"old")

    store.put(SCOPE, "k", b"x" * 11)

    assert store.get(SCOPE, "k") is None
    assert store.stats() == []


def test_a_value_that_fits_replaces_the_old_one_and_the_bytes_add_up() -> None:
    store = InMemoryCacheStore(max_bytes=10)
    store.put(SCOPE, "k", b"old")
    store.put(SCOPE, "k", b"newer")

    assert store.get(SCOPE, "k") == b"newer"
    [line] = store.stats()
    assert (line.entries, line.bytes) == (1, 5)


def test_the_oldest_entries_go_first_when_the_limits_are_reached() -> None:
    store = InMemoryCacheStore(max_entries=2)
    for key in ("a", "b", "c"):
        store.put(SCOPE, key, key.encode())

    assert [key for key, _ in store.entries(SCOPE)] == ["c", "b"]


def test_a_scope_part_with_a_trailing_line_break_is_refused() -> None:
    from awa.runtime.cache import _scope_part

    part = "acme\n"
    with pytest.raises(ValueError, match="Cache scope tenant"):
        CacheScope(part, "shop", "tools")
    assert _scope_part(part) != part

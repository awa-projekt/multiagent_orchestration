"""`awa._internal.util`, serialization, and the shared HTML tokenizer."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from awa._internal.html import MAX_ATTRIBUTES, HtmlEvents
from awa._internal.serialize import (
    check_yaml_size,
    dump_yaml,
    load_yaml,
    split_frontmatter,
    to_json,
)
from awa._internal.util import accepts_keyword, call_factory, elapsed_ms, homes, is_within, preview
from awa.data._mail import _html_text
from awa.toolkits._search import _ResultPage
from awa.toolkits.web import html_to_text


def test_accepts_keyword_sees_named_parameters_and_kwargs() -> None:
    def named(a: int, *, ctx: Any = None) -> None: ...

    def catch_all(a: int, **kwargs: Any) -> None: ...

    def neither(a: int, *args: Any) -> None: ...

    assert accepts_keyword(named, "ctx")
    assert accepts_keyword(catch_all, "ctx")
    assert not accepts_keyword(neither, "ctx")
    assert accepts_keyword(lambda ctx: None, "ctx")


def test_accepts_keyword_is_false_without_a_signature() -> None:
    class Opaque:
        @property
        def __signature__(self) -> Any:
            raise ValueError("no signature")

        def __call__(self) -> None: ...

    assert not accepts_keyword(Opaque(), "x")


async def test_call_factory_builds_from_sync_and_async_factories() -> None:
    class Built:
        pass

    async def build_async() -> Built:
        return built

    built = Built()
    assert await call_factory(built, Built) is built
    assert await call_factory(lambda: built, Built) is built
    assert await call_factory(build_async, Built) is built
    # Values that are not factories are returned as they are, for the caller to check.
    assert await call_factory([built], Built) == [built]


def test_preview_keeps_short_text_and_cuts_long_text() -> None:
    assert preview("short", 10) == "short"
    assert preview("0123456789", 10) == "0123456789"
    assert preview("0123456789A", 10) == "0123456..."
    assert len(preview("x" * 500)) == 200


def test_preview_can_flatten_whitespace_and_use_another_ellipsis() -> None:
    assert preview("a\n\n  b\tc", 100, flatten=True) == "a b c"
    assert preview("a\nb", 100) == "a\nb"
    assert preview("abcdefgh", 5, ellipsis="…") == "abcd…"
    assert preview(12345, 3, ellipsis="") == "123"  # type: ignore[arg-type]


def test_preview_rejects_a_limit_below_the_ellipsis() -> None:
    with pytest.raises(ValueError):
        preview("text", 2)


def test_elapsed_ms_counts_milliseconds() -> None:
    started = time.perf_counter() - 0.25

    assert 250 <= elapsed_ms(started) < 5000


def test_is_within_resolves_links_and_dots_and_includes_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    (outside / "secret.txt").write_text("y", encoding="utf-8")

    assert is_within(root, root) and is_within(root / "sub", root)
    assert is_within(root / "sub" / ".." / "file.txt", root)
    assert is_within(root / "missing.txt", root)  # need not exist
    assert not is_within(outside, root) and not is_within(root / ".." / "outside", root)
    assert not is_within(tmp_path / "root-neighbour", root)  # a name prefix is not containment
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links need a privilege on this system")
    assert not is_within(root / "link" / "secret.txt", root)  # a link out of the root leads out


def test_is_within_refuses_what_cannot_be_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Path, strict: bool = False) -> Path:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "resolve", broken)
    assert not is_within(Path("a"), Path("b"))


def test_dump_yaml_keeps_order_unicode_and_uses_block_style() -> None:
    data = {"name": "Büro", "instructions": "Line one.\nLine two.", "list": [1, 2]}

    text = dump_yaml(data)

    assert text.startswith("name: Büro\ninstructions: |-\n  Line one.\n  Line two.\n")
    assert yaml.safe_load(text) == data


def test_dump_yaml_wraps_at_the_width() -> None:
    words = " ".join(["word"] * 40)

    assert len(dump_yaml({"k": words}).splitlines()) > 1
    assert len(dump_yaml({"k": words}, width=1000).splitlines()) == 1


def test_load_yaml_parses_text_and_bytes() -> None:
    assert load_yaml("a: 1\nb: [x]") == {"a": 1, "b": ["x"]}
    assert load_yaml("﻿a: 1".encode()) == {"a": 1}
    assert load_yaml("") is None


def test_load_yaml_refuses_unsafe_tags() -> None:
    with pytest.raises(ValueError, match="Malformed blueprint YAML"):
        load_yaml("!!python/object/apply:os.system ['echo hi']", what="blueprint")


@pytest.mark.parametrize("text", ["a: [1, 2", "when: 2001-13-01"])
def test_load_yaml_reports_malformed_documents(text: str) -> None:
    with pytest.raises(ValueError, match="Malformed document YAML"):
        load_yaml(text)


def test_load_yaml_limits_the_size() -> None:
    with pytest.raises(ValueError, match="larger than 10 bytes"):
        load_yaml("a: 12345678901", max_bytes=10)
    with pytest.raises(ValueError, match="larger than 10 bytes"):
        load_yaml(b"a: 12345678901", max_bytes=10)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        load_yaml(b"a: \xff")


def test_load_yaml_counts_aliases_at_every_use() -> None:
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)},"
        f" *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)},"
        f" *{chr(97 + i)}, *{chr(97 + i)}]\n"
        for i in range(5)
    )

    with pytest.raises(ValueError, match="more than 100,000 values"):
        load_yaml(bomb)


def test_load_yaml_limits_the_nesting() -> None:
    with pytest.raises(ValueError, match="nests deeper than 3 levels"):
        load_yaml("a: {b: {c: {d: 1}}}", max_depth=3)
    with pytest.raises(ValueError, match="nests deeper"):
        load_yaml("[" * 5000 + "]" * 5000)


def test_check_yaml_size_accepts_small_data() -> None:
    check_yaml_size({"a": [1, {"b": 2}]}, max_values=5, max_depth=3)
    with pytest.raises(ValueError, match="more than 4 values"):
        check_yaml_size({"a": [1, {"b": 2}]}, max_values=4)


def test_split_frontmatter_returns_the_block_and_the_body() -> None:
    text = "﻿---\nname: review\n---\n# Review\n\nBody."

    assert split_frontmatter(text) == ("name: review", "# Review\n\nBody.")
    assert split_frontmatter("---\n---\n") == ("", "")


def test_split_frontmatter_without_a_block_returns_the_text() -> None:
    assert split_frontmatter("# Just markdown\n") == (None, "# Just markdown\n")
    assert split_frontmatter("") == (None, "")


def test_split_frontmatter_requires_a_closing_delimiter() -> None:
    with pytest.raises(ValueError, match="not closed"):
        split_frontmatter("---\nname: x\n# body")


def test_to_json_is_compact_and_strict_by_default() -> None:
    assert to_json({"a": [1, "ü"]}) == '{"a":[1,"ü"]}'
    with pytest.raises(ValueError):
        to_json(math.nan)


def test_to_json_options() -> None:
    assert to_json({"a": 1}, compact=False) == '{"a": 1}'
    assert to_json("ü", ensure_ascii=True) == '"\\u00fc"'
    assert to_json(math.inf, allow_nan=True) == "Infinity"
    assert to_json(object(), default=lambda value: "obj") == '"obj"'
    with pytest.raises(TypeError):
        to_json(object())


def test_split_frontmatter_keeps_unicode_line_separators_in_values() -> None:
    text = "---\r\nowner: 'a\u2028b\u2029c'\r\n---\r\nBody\r\n"

    assert split_frontmatter(text) == ("owner: 'a\u2028b\u2029c'", "Body")


FIXTURES = Path(__file__).parents[1] / "fixtures"
# The output of the earlier `html.parser`-based readers, kept as the reference the tokenizer of
# `awa._internal.html` must match.
GOLDEN = json.loads((FIXTURES / "html_golden" / "golden.json").read_text("utf-8"))
SEARCH_DIR = FIXTURES / "web_search"
SECONDS_PER_MB = 4.0  # the tokenizer needs about 0.3 to 1; the standard parser needs 20 to 100


class Recorder(HtmlEvents):
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("start", tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        self.events.append(("end", tag))

    def handle_data(self, data: str) -> None:
        self.events.append(("data", data))

    def handle_comment(self, data: str) -> None:
        self.events.append(("comment", data))


def events(html: str) -> list[tuple[object, ...]]:
    recorder = Recorder()
    recorder.parse(html)
    return recorder.events


# ---- the text of ordinary pages and mails matches the golden text


@pytest.mark.parametrize("name", sorted(GOLDEN["pages"]))
def test_web_text_of_ordinary_pages_matches_the_golden_text(name: str) -> None:
    page = GOLDEN["pages"][name]
    assert list(html_to_text(page)) == GOLDEN["web"][name]
    title, text, links = html_to_text(page, keep_links=True)
    assert [title, text, [list(pair) for pair in links]] == GOLDEN["web_links"][name]


@pytest.mark.parametrize("name", sorted(GOLDEN["mail"]))
def test_mail_text_of_ordinary_bodies_matches_the_golden_text(name: str) -> None:
    body = GOLDEN["pages"].get(name) or GOLDEN["mails"][name.removeprefix("mail_")]
    assert list(_html_text(body)) == GOLDEN["mail"][name]


@pytest.mark.parametrize("name", sorted(GOLDEN["search"]))
def test_search_result_pages_match_the_golden_hits(name: str) -> None:
    page = _ResultPage()
    page.parse((SEARCH_DIR / name).read_text(encoding="utf-8"))
    expected = GOLDEN["search"][name]
    assert page.hits == expected["hits"]
    assert (page.blocked, page.empty, page.next_form) == (
        expected["blocked"],
        expected["empty"],
        expected["next_form"],
    )


# ---- what the tokenizer reports


def test_tags_attributes_text_and_comments_come_in_order() -> None:
    html = "<P Class='a b' id=x hidden>Hi &amp; <b>bye</b><!-- note --></P>"
    assert events(html) == [
        ("start", "p", [("class", "a b"), ("id", "x"), ("hidden", None)]),
        ("data", "Hi & "),
        ("start", "b", []),
        ("data", "bye"),
        ("end", "b"),
        ("comment", " note "),
        ("end", "p"),
    ]


def test_attribute_values_resolve_references_and_keep_quotes_inside() -> None:
    assert events("<a href='?a=1&amp;b=2' title=\"it's\" alt=>")[0] == (
        "start",
        "a",
        [("href", "?a=1&b=2"), ("title", "it's"), ("alt", "")],
    )


def test_self_closing_tags_call_start_then_end_by_default() -> None:
    assert events("<br/><img src=x />") == [
        ("start", "br", []),
        ("end", "br"),
        ("start", "img", [("src", "x")]),
        ("end", "img"),
    ]


def test_an_unquoted_value_ending_in_a_slash_is_not_self_closing() -> None:
    assert events("<a href=/docs/>Docs</a>") == [
        ("start", "a", [("href", "/docs/")]),
        ("data", "Docs"),
        ("end", "a"),
    ]


def test_script_and_style_bodies_are_raw_text_until_their_end_tag() -> None:
    assert events("<script>if (a<b && c>d) '</p>'</SCRIPT>x<style>a>b{}</style>") == [
        ("start", "script", []),
        ("data", "if (a<b && c>d) '</p>'"),
        ("end", "script"),
        ("data", "x"),
        ("start", "style", []),
        ("data", "a>b{}"),
        ("end", "style"),
    ]


def test_an_unclosed_script_takes_the_rest_of_the_page() -> None:
    assert events("<script>var a = 1; <b>")[-1] == ("data", "var a = 1; <b>")


def test_stray_less_than_signs_are_text() -> None:
    assert events("a < b <3 and </ x") == [("data", "a < b <3 and </ x")]


def test_unterminated_comment_runs_to_the_end_and_declarations_are_dropped() -> None:
    assert events("<!DOCTYPE html><?xml x?><![CDATA[ y ]]>a<!-- open <b>") == [
        ("data", "a"),
        ("comment", " open <b>"),
    ]


def test_the_two_short_comment_forms_are_dropped_and_do_not_swallow_the_page() -> None:
    assert events("a<!-->b<!--->c<!---->d") == [
        ("data", "a"),
        ("data", "b"),
        ("data", "c"),
        ("comment", ""),
        ("data", "d"),
    ]


def test_a_tag_keeps_at_most_the_attribute_limit() -> None:
    page = "<a " + " ".join(f"k{i}=v" for i in range(MAX_ATTRIBUTES * 3)) + ">"
    attrs = events(page)[0][2]
    assert isinstance(attrs, list)
    assert len(attrs) == MAX_ATTRIBUTES


def test_nothing_is_fetched_or_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the tokenizer must not touch the network")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    text = html_to_text(
        "<img src='http://127.0.0.1:9/x'><script src='http://127.0.0.1:9/y'></script>"
        "<link rel=stylesheet href='http://127.0.0.1:9/z'><iframe src='http://127.0.0.1:9/w'></iframe>"
        "<p>ok</p>"
    )[1]
    assert text == "ok"


# ---- hostile input is read in time proportional to its size

_TABLE_ROW = "<tr><td>{0}</td><td>Name {0}</td><td>{0}.50</td></tr>\n"
HOSTILE = {
    "comments": "<!--" * 100_000,
    "tags without end": "<a" * 300_000,
    "quotes in tags": '<a "' * 300_000,
    "attributes without end": "<a " + "x=1 " * 300_000,
    "unterminated tag": "<div class=" + "x" * 1_000_000,
    "unterminated quote": '<a href="' + "a" * 1_000_000,
    "unterminated script": "<script>" + "var a=1;" * 200_000,
    "huge attribute list": "<p " + 'data-a="1" ' * 100_000 + ">text",
    "declaration": "<!DOCTYPE " + "a" * 1_000_000,
    "less-than signs": "<" * 300_000,
    "entities": "&amp;" * 300_000,
    "nested divs": "<div>" * 300_000,
    "table of 3.6 MB": "<table>"
    + "".join(_TABLE_ROW.format(i) for i in range(60_000))
    + "</table>",
}
READERS: dict[str, Callable[[str], object]] = {
    "html_to_text": lambda html: html_to_text(html, keep_links=True),
    "mail": _html_text,
    "search": lambda html: _ResultPage().parse(html),
}


@pytest.mark.slow
@pytest.mark.parametrize("reader", sorted(READERS))
@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_pages_take_linear_time(case: str, reader: str) -> None:
    if reader != "html_to_text" and case == "table of 3.6 MB":
        pytest.skip("covered by html_to_text; the readers share the tokenizer")
    html = HOSTILE[case]
    started = time.perf_counter()
    READERS[reader](html)
    seconds = time.perf_counter() - started
    megabytes = len(html) / 1_000_000
    assert seconds / megabytes < SECONDS_PER_MB, f"{case}: {seconds:.1f} s for {megabytes:.1f} MB"


@pytest.mark.slow
def test_a_million_nested_divs_and_stray_end_tags_are_read_flat() -> None:
    page = "<div>" * 1_000_000 + "deep" + "</div>" * 1_000_000 + "</p>" * 1_000_000
    started = time.perf_counter()
    assert html_to_text(page)[1] == "deep"
    assert _html_text(page)[0].strip() == "deep"
    assert (time.perf_counter() - started) / (2 * len(page) / 1_000_000) < SECONDS_PER_MB


def test_work_grows_with_size_not_with_its_square() -> None:
    """Four times a hostile input must not take sixteen times as long (ratio of two timings)."""

    def seconds(count: int) -> float:
        html = '<a "' * count
        started = time.perf_counter()
        html_to_text(html)
        return time.perf_counter() - started

    small, large = min(seconds(40_000) for _ in range(3)), seconds(160_000)
    assert large < 10 * max(small, 0.005)  # linear is about 4, quadratic about 16


def test_homes_maps_each_name_to_its_module() -> None:
    table = homes({"pkg.a": "One two", "pkg.b": " three\n four "})
    assert table == {"One": "pkg.a", "two": "pkg.a", "three": "pkg.b", "four": "pkg.b"}

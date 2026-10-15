"""The page of `awa ui` and its access model: token, bearer header, host and origin checks, headers."""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from awa.cli import build_parser
from awa.cli._ui import _free_port, _ui
from awa.errors import ConfigurationError
from awa.ui import create_ui_app
from awa.ui._app import page_source

PAGE = page_source()
SCRIPT = PAGE.split('<script nonce="{{nonce}}">', 1)[1].split("</script>", 1)[0]
MARKUP = PAGE.split("<script", 1)[0]


def test_the_scripts_are_inlined_in_file_name_order() -> None:
    parts = sorted(
        (p for p in files("awa.ui").joinpath("js").iterdir() if p.name.endswith(".js")),
        key=lambda p: p.name,
    )
    firsts = [part.read_text(encoding="utf-8").strip().splitlines()[0] for part in parts]

    assert "{{script}}" not in PAGE
    assert firsts[0] == '"use strict";'
    positions = [PAGE.index(first) for first in firsts]
    assert positions == sorted(positions)


def test_the_script_cannot_end_the_script_element_early() -> None:
    assert "</script" not in SCRIPT.lower()
    assert PAGE.count("</script>") == 1


def test_every_id_the_script_looks_up_exists_in_the_markup_or_is_made_by_the_script() -> None:
    wanted = set(re.findall(r"""\$\(["']#([\w-]+)""", SCRIPT))
    wanted |= set(re.findall(r"""getElementById\(["']([\w-]+)""", SCRIPT))
    present = set(re.findall(r'\bid="([\w-]+)"', MARKUP))
    made = set(re.findall(r"""\bid: ?["']([\w-]+)["']""", SCRIPT)) | set(
        re.findall(r"""\.id = ["']([\w-]+)["']""", SCRIPT)
    )

    assert sorted(wanted - present - made) == []


def test_every_page_in_the_menu_has_a_section_and_a_loader() -> None:
    pages = re.findall(r'<li><a href="#(\w+)">', MARKUP)
    sections = re.findall(r'<section id="(\w+)" hidden', MARKUP)
    listed = re.search(r"const PAGES = \[(.*?)\]", SCRIPT)
    loaders = re.search(r"const LOADERS = \{(.*?)\};", SCRIPT, flags=re.DOTALL)

    assert listed is not None and loaders is not None
    assert pages == re.findall(r'"(\w+)"', listed.group(1))
    assert sorted(pages) == sorted(sections)
    assert sorted(re.findall(r"(\w+):", loaders.group(1))) == sorted(pages)


def test_every_step_explains_what_happens_there() -> None:
    sections = re.findall(r'<section id="(\w+)" hidden.*?</section>', MARKUP, flags=re.DOTALL)
    bodies = re.findall(r'(<section id="\w+" hidden.*?</section>)', MARKUP, flags=re.DOTALL)

    help_start = '<p class="help"><svg class="ic" aria-hidden="true"><use href="#i-info"/></svg>'
    assert sections and all(help_start in body for body in bodies)


def test_nothing_is_loaded_from_elsewhere() -> None:
    assert not re.findall(r"https?://", MARKUP.replace("http://www.w3.org", ""))
    assert not re.findall(r"""(?:src|href)=["']https?:""", PAGE)
    assert "eval(" not in SCRIPT and "new Function" not in SCRIPT
    assert re.search(r"<style nonce=\"\{\{nonce\}\}\">", MARKUP)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to check the syntax")
def test_the_script_is_valid_javascript(tmp_path: Path) -> None:
    script = tmp_path / "page.js"
    script.write_text(SCRIPT, encoding="utf-8")

    done = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True, timeout=60, check=False
    )

    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------------- stylesheet and icons

STYLE = PAGE.split('<style nonce="{{nonce}}">', 1)[1].split("</style>", 1)[0]


def _tokens(block: str) -> dict[str, str]:
    found = {name: value.strip() for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", block)}
    return {
        name: found[value[4:-1]] if value.startswith("var(--") and value[4:-1] in found else value
        for name, value in found.items()
    }


def _theme_blocks() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    light = re.search(r"^:root \{(.*?)^\}", STYLE, flags=re.DOTALL | re.MULTILINE)
    media = re.search(
        r":root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}", STYLE, flags=re.DOTALL
    )
    dark = re.search(
        r"^:root\[data-theme=\"dark\"\] \{(.*?)^\}", STYLE, flags=re.DOTALL | re.MULTILINE
    )
    assert light and media and dark
    return _tokens(light.group(1)), _tokens(dark.group(1)), _tokens(media.group(1))


def _luminance(colour: str) -> float:
    value = colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_stylesheet_loads_nothing_from_elsewhere() -> None:
    assert "{{style}}" not in PAGE
    assert not re.findall(r"https?://", STYLE)
    assert "@import" not in STYLE and "url(" not in STYLE


def test_every_variable_in_use_is_defined_and_both_themes_define_the_same_colours() -> None:
    light, dark, media = _theme_blocks()
    used = set(re.findall(r"var\((--[\w-]+)", STYLE))

    assert sorted(used - set(light)) == []
    assert dark == media
    colours = {
        name
        for name, value in light.items()
        if value.startswith("#") and name not in {"--code", "--focus"}
    }
    assert sorted(colours - set(dark)) == []
    assert "--text" in dark and "--accent" in dark


PAIRS = [
    ("--text", "--surface"),
    ("--text", "--bg"),
    ("--text-2", "--surface"),
    ("--text-2", "--bg"),
    ("--text-3", "--surface"),
    ("--text-3", "--sunken"),
    ("--on-accent", "--accent"),
    ("--on-accent", "--accent-hover"),
    ("--accent", "--surface"),
    ("--accent", "--bg"),
    ("--accent-text", "--accent-soft"),
    ("--ok", "--ok-bg"),
    ("--warn", "--warn-bg"),
    ("--err", "--err-bg"),
    ("--text", "--sunken"),
    ("--text-2", "--sunken"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_pairs_reach_the_aa_contrast(theme: str) -> None:
    light, dark, _ = _theme_blocks()
    tokens = {**light, **dark} if theme == "dark" else light

    low = {pair: round(contrast(tokens[pair[0]], tokens[pair[1]]), 2) for pair in PAIRS}

    assert {pair: ratio for pair, ratio in low.items() if ratio < 4.5} == {}
    # Controls and focus rings need 3:1 against the surface they sit on.
    assert contrast(tokens["--line-strong"], tokens["--surface"]) >= 3
    assert contrast(tokens["--focus"], tokens["--surface"]) >= 3


def test_every_icon_in_use_exists_in_the_sprite_and_every_symbol_is_used() -> None:
    symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', MARKUP))
    state = SCRIPT.split("const STATE_ICONS = [", 1)[1].split("];", 1)[0]
    used = set(re.findall(r'\bicon\("([\w-]+)"', SCRIPT))
    used |= set(re.findall(r'\bemptyState\("([\w-]+)"', SCRIPT))
    used |= set(re.findall(r'href="#i-([\w-]+)"', MARKUP))
    used |= set(re.findall(r'", "([\w-]+)(?::\w+)?"\]', state)) - {"tick"}
    used |= set(re.findall(r'icon\(dark \? "(\w+)" : "(\w+)"\)', SCRIPT)[0])
    used |= {"check-circle"} if 'icon("check-circle")' in SCRIPT else set()

    assert sorted(used - symbols) == []
    assert sorted(symbols - used) == []
    assert "tick" in SCRIPT  # the animated check is drawn inline, not from the sprite


TOKEN = "test-token-0123456789"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        yield client


def sign_in(client: TestClient) -> None:
    client.headers.update(AUTH)


def test_the_access_token_never_travels_in_a_cookie(client: TestClient) -> None:
    # A cookie for 127.0.0.1 reaches every other port of the host, so it must not open the UI.
    opened = client.get(f"/?token={TOKEN}", follow_redirects=False)

    assert opened.status_code == 200
    assert "set-cookie" not in opened.headers
    assert (
        client.get("/api/state", headers={"Cookie": f"awa_ui_session={TOKEN}"}).status_code == 401
    )
    assert client.get("/api/state", headers=AUTH).status_code == 200


def test_without_the_token_only_the_empty_page_opens(client: TestClient) -> None:
    page = client.get("/")
    api = client.get("/api/state")

    assert page.status_code == 200
    assert "Open the link from the awa window" in page.text  # what the script shows without it
    assert api.status_code == 401
    assert api.json()["error"]["fix"]


def test_a_wrong_token_in_the_link_is_refused(client: TestClient) -> None:
    response = client.get("/?token=guess", follow_redirects=False)

    assert response.status_code == 401
    assert "Open the link from the awa window" in response.text
    assert client.get("/api/state", headers={"Authorization": "Bearer guess"}).status_code == 401


def test_other_host_names_are_refused_even_with_the_token(client: TestClient) -> None:
    sign_in(client)

    rebound = client.get("/api/state", headers={"Host": "attacker.example:8765"})
    alias = client.get("/api/state", headers={"Host": "localhost:8765"})

    assert rebound.status_code == 421
    assert alias.status_code == 200


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),
        ({"Origin": "http://attacker.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": BASE, "Authorization": "Bearer forged"}, 401),
        (SAFE, 200),
    ],
)
def test_state_changing_requests_need_same_origin_and_the_token(
    client: TestClient, headers: dict[str, str], status: int
) -> None:
    sign_in(client)

    response = client.post(
        "/api/setup", json={"values": {"AWA_DEFAULT_MODEL": "x:y"}}, headers=headers
    )

    assert response.status_code == status


def test_the_page_carries_no_secret_and_a_nonce_only_policy(client: TestClient) -> None:
    sign_in(client)

    response = client.get("/")

    policy = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in response.text
    assert "unsafe-inline" not in policy
    assert "default-src 'none'" in policy
    assert TOKEN not in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "://" not in response.text  # nothing is loaded from elsewhere


def test_invalid_bodies_are_reported_without_echoing_them(client: TestClient) -> None:
    sign_in(client)

    response = client.post("/api/setup", json={"values": {"MY_KEY": 918273645}}, headers=SAFE)

    assert response.status_code == 422
    assert "918273645" not in response.text
    assert "values.MY_KEY" in response.json()["error"]["message"]


def ui_args(*argv: str) -> argparse.Namespace:
    return build_parser(["ui"]).parse_args(["ui", *argv])


def test_the_cli_refuses_a_non_loopback_host_without_allow_remote() -> None:
    args = ui_args("--host", "0.0.0.0", "--no-browser")

    assert args.handler is _ui
    with pytest.raises(ConfigurationError, match="--allow-remote"):
        _ui(args)


def test_the_global_project_option_names_the_served_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.server._app
    import awa.ui._app

    served: list[Path] = []

    def create_ui_app(project: Path, **options: Any) -> Any:
        served.append(Path(project))
        raise KeyboardInterrupt

    monkeypatch.setattr(awa.ui._app, "create_ui_app", create_ui_app)
    monkeypatch.setattr(awa.server._app, "import_uvicorn", lambda: None)
    args = build_parser(["ui"]).parse_args(["--project", str(tmp_path), "ui", "--no-browser"])

    with pytest.raises(KeyboardInterrupt):
        _ui(args)

    assert served == [tmp_path]


def test_a_taken_port_falls_back_to_a_free_one() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        chosen = _free_port("127.0.0.1", port)

    assert chosen != port
    assert chosen > 0

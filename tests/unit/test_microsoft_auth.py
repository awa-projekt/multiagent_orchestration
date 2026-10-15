"""Signing in to Microsoft as a person: device code, refresh tokens and the token cache.

The real code runs against FakeGraph over HTTP (nothing is patched except the clocks and sleeps,
which are tied to the fake server's clock). The delegated operations of the `teams` connector are the
way a token is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import awa
from _helpers.fake_graph_proof import proves
from _helpers.fakegraph.core import decode_jwt
from _helpers.fakegraph.server import FakeGraph
from awa._internal.upstream.tools import ToolException
from awa.connectors import _engine, check_connector, connector_tools
from awa.connectors import _microsoft_auth as auth
from awa.connectors import _saas_microsoft_teams as teams_module

PUBLIC = "99999999-8888-7777-6666-555555555555"
SCOPES = ("User.Read", "ChannelMessage.Send", "ChannelMessage.Read.All")
CACHE = Path(".awa/ms_token_cache.json")
TOKEN_ENDPOINT = "/*/oauth2/v2.0/token"


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        server.add_app(PUBLIC, public_client=True, scopes=SCOPES)
        server.add_user("ada@contoso.test", name="Ada")
        server.identity.device_pending_polls = 0
        yield server


@pytest.fixture(autouse=True)
def seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, graph: FakeGraph) -> list[float]:
    """The working folder is empty, the sign-in clock is the fake server's, waits advance it."""

    monkeypatch.chdir(tmp_path)
    slept: list[float] = []

    def wait(seconds: float) -> None:
        slept.append(seconds)
        graph.clock.advance(seconds)

    monkeypatch.setattr(auth, "_now", graph.clock.time)
    monkeypatch.setattr(auth, "_sleep", wait)
    monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
    monkeypatch.setattr(teams_module._SPACE, "gap", 0.0)
    return slept


def env(graph: FakeGraph, **extra: str) -> dict[str, str]:
    return {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": PUBLIC,
        "AZURE_AUTHORITY_HOST": graph.authority,
        "MS_GRAPH_URL": graph.graph_url,
        "TEAMS_WEBHOOK_URL": f"{graph.url}/workflows/wf-1?sig=SIGNATURE-VALUE-1234",
        **extra,
    }


def sign_in(graph: FakeGraph, prompts: list[Any] | None = None, **options: Any) -> auth.SignedIn:
    found = prompts if prompts is not None else []
    return auth.sign_in(
        tenant=options.pop("tenant", graph.tenant_id),
        client_id=options.pop("client_id", PUBLIC),
        show=found.append,
        scopes=options.pop("scopes", SCOPES),
        authority=options.pop("authority", graph.authority),
        options=options.pop("http", graph.connector_options),
        **options,
    )


def world(graph: FakeGraph) -> dict[str, str]:
    teams = graph.store("teams")
    team = teams.add_team("Ops", graph.find_user("ada@contoso.test"))
    channel = teams.add_channel(team, "General")
    return {"team_id": team.id, "channel_id": channel.id}


def read(graph: FakeGraph, where: dict[str, str], **config: Any) -> str:
    """One delegated read with a freshly built connector (a new provider, as a new process)."""

    options = {**graph.connector_options, "requires_approval": False, **config}
    built = connector_tools("teams", env=env(graph, **pinned(where)), config=options)
    tool = next(t for t in built if t.name == "teams_list_channel_messages")
    return str(tool.invoke({}))


def pinned(where: dict[str, str]) -> dict[str, str]:
    """The .env lines that pin the connector to the channel of `world`."""

    return {"TEAMS_TEAM_ID": where["team_id"], "TEAMS_CHANNEL_ID": where["channel_id"]}


def refreshes(graph: FakeGraph) -> int:
    return sum(
        1
        for r in graph.calls("POST", TOKEN_ENDPOINT)
        if r.form().get("grant_type") == "refresh_token"
    )


def cache_file() -> dict[str, Any]:
    return json.loads(CACHE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- device code


@proves("entra.device_code_pending")
def test_device_code_sign_in_happy_path_saves_a_versioned_cache(graph: FakeGraph) -> None:
    prompts: list[auth.DeviceCodePrompt] = []
    done = sign_in(graph, prompts)
    assert done.username == "ada@contoso.test" and done.tenant == graph.tenant_id
    assert set(done.scopes) == set(SCOPES)
    [prompt] = prompts
    assert prompt.verification_uri == "https://microsoft.com/devicelogin"
    assert len(prompt.user_code) >= 4 and prompt.expires_in == 900
    stored = cache_file()
    assert stored["v"] == auth.CACHE_VERSION == 1
    [account] = stored["accounts"]
    assert account["username"] == "ada@contoso.test" and account["refresh_token"]
    first = graph.calls("POST", "/*/oauth2/v2.0/devicecode")[0].form()
    assert first["client_id"] == PUBLIC and "client_secret" not in first
    assert "offline_access" in first["scope"].split()  # added: a sign-in must be renewable


@proves("entra.device_code_pending")
def test_the_poll_waits_for_the_person_at_the_pace_microsoft_asks(
    graph: FakeGraph, seams: list[float]
) -> None:
    graph.identity.device_pending_polls = 2
    sign_in(graph)
    polls = graph.calls("POST", TOKEN_ENDPOINT)
    assert len(polls) == 3 and seams == [5, 5, 5]  # three polls, one interval before each


@proves("entra.device_code_slow_down")
def test_slow_down_makes_the_poll_five_seconds_slower(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch, seams: list[float]
) -> None:
    graph.identity.enforce_device_interval = True
    graph.identity.device_pending_polls = 1

    def impatient(seconds: float) -> None:  # a clock that runs five times too slow
        seams.append(seconds)
        graph.clock.advance(seconds / 5)

    monkeypatch.setattr(auth, "_sleep", impatient)
    sign_in(graph)
    # the first wait is fine (nothing to compare with); every poll after it is too soon until the
    # waits have grown to 25 s, and each slow_down made the next wait five seconds longer
    assert seams == [5, 5, 10, 15, 20, 25]


@proves("entra.device_code_slow_down")
def test_slow_down_is_not_an_error_when_the_wait_is_long_enough(
    graph: FakeGraph, seams: list[float]
) -> None:
    graph.identity.enforce_device_interval = True
    graph.identity.device_pending_polls = 2
    sign_in(graph)
    assert seams == [5, 5, 5]


@pytest.mark.slow
@proves("entra.device_code_pending")
def test_an_unfinished_sign_in_ends_with_the_code_expired(graph: FakeGraph) -> None:
    graph.identity.device_pending_polls = 10**6
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph)
    assert caught.value.code == "ms.device_code_expired" and "15 minutes" in str(caught.value)
    assert not CACHE.exists()


@proves("entra.device_code_declined")
def test_a_declined_sign_in_stops_at_once_and_writes_nothing(graph: FakeGraph) -> None:
    graph.identity.device_pending_polls = 1
    graph.store("teams").device_declined = True
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph)
    assert caught.value.code == "ms.sign_in_declined"
    assert len(graph.calls("POST", TOKEN_ENDPOINT)) == 1  # no polling after a refusal
    assert not CACHE.exists()


@proves("entra.device_code_blocked_by_conditional_access")
def test_a_tenant_that_blocks_device_code_gets_a_plain_message(graph: FakeGraph) -> None:
    graph.identity.block_device_code = True
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph)
    assert caught.value.code == "ms.device_code_blocked"
    assert "Conditional Access" in str(caught.value) and "Workflows webhook" in str(caught.value)


@proves("entra.device_code_needs_public_client")
def test_an_app_that_is_not_a_public_client_is_told_how_to_change_it(graph: FakeGraph) -> None:
    graph.apps[PUBLIC].public_client = False
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph)
    assert caught.value.code == "ms.public_client_off"
    assert "Allow public client flows" in str(caught.value)


@proves("entra.delegated_scope_needs_consent")
def test_a_permission_the_app_was_never_given_is_refused_before_any_prompt(
    graph: FakeGraph,
) -> None:
    shown: list[Any] = []
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph, shown, scopes=("Mail.Send",))
    assert caught.value.code == "ms.consent_missing" and shown == []


def test_signing_in_twice_keeps_one_entry_per_user(graph: FakeGraph) -> None:
    sign_in(graph)
    sign_in(graph)
    assert len(cache_file()["accounts"]) == 1


def test_the_prompt_is_plain_words_with_a_warning_against_code_phishing(graph: FakeGraph) -> None:
    prompts: list[auth.DeviceCodePrompt] = []
    sign_in(graph, prompts)
    text = prompts[0].text()
    assert prompts[0].user_code in text and "https://microsoft.com/devicelogin" in text
    assert "Never enter a code that someone sent you" in text
    assert "only enter it if you started this sign-in yourself" in text.lower()
    assert "awa-bot user, not your own admin account" in text
    assert "post messages in Teams channels" in text  # permissions in plain words
    assert "device_code" not in text and "refresh" not in text


@pytest.mark.parametrize(
    "address",
    [
        "https://microsoft.com/devicelogin",
        "https://login.microsoft.com/device",
        "https://login.microsoftonline.us/common/oauth2/deviceauth",
        "https://login.partner.microsoftonline.cn/common/oauth2/deviceauth",
    ],
)
def test_microsoft_sign_in_pages_are_accepted(address: str) -> None:
    assert auth._verification_uri(address, auth.DEFAULT_AUTHORITY) == address


@pytest.mark.parametrize(
    "address",
    [
        "https://microsoft.com.evil.example/devicelogin",
        "https://evil.example/microsoft.com",
        "https://user@microsoft.com/devicelogin",
        "javascript:alert(1)",
        "ftp://microsoft.com/devicelogin",
        "",
        None,
        42,
    ],
)
def test_a_sign_in_page_that_is_not_microsoft_is_never_shown(address: Any) -> None:
    with pytest.raises(auth.MicrosoftAuthError, match="not a Microsoft page"):
        auth._verification_uri(address, auth.DEFAULT_AUTHORITY)


# --------------------------------------------------------------------------- the provider


def test_a_user_operation_uses_the_cached_token_and_asks_microsoft_only_once(
    graph: FakeGraph,
) -> None:
    where = world(graph)
    sign_in(graph)
    for _ in range(3):
        assert read(graph, where).startswith("<tool_output")
    assert refreshes(graph) == 0  # the token from the sign-in was still good
    sent = graph.calls("GET", "/v1.0/teams/*")[0].header("authorization")
    claims = decode_jwt(sent.split()[1])
    assert claims["upn"] == "ada@contoso.test" and "roles" not in claims  # a user, not the app


@proves("entra.refresh_token_rotates")
def test_the_token_is_renewed_five_minutes_early_and_the_new_refresh_token_is_kept(
    graph: FakeGraph,
) -> None:
    where = world(graph)
    sign_in(graph)
    before = cache_file()["accounts"][0]["refresh_token"]
    graph.clock.advance(3000)  # 599 s left: still fine
    read(graph, where)
    assert refreshes(graph) == 0
    graph.clock.advance(400)  # 199 s left: inside the margin
    assert read(graph, where).startswith("<tool_output")
    assert refreshes(graph) == 1
    assert cache_file()["accounts"][0]["refresh_token"] != before  # the NEW one is stored
    assert graph.calls("GET", "/v1.0/teams/*")[-1].status == 200  # and the server never saw 401
    graph.clock.advance(3400)
    read(graph, where)  # second rotation works from the stored token
    assert refreshes(graph) == 2 and graph.store("teams").stale_refresh_tokens == 0


def test_a_public_client_sends_no_secret_with_a_refresh(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(3500)
    read(graph, where)
    form = graph.calls("POST", TOKEN_ENDPOINT)[-1].form()
    assert form["grant_type"] == "refresh_token" and "client_secret" not in form


@proves("entra.refresh_token_revoked")
def test_a_revoked_sign_in_says_so_and_leaves_the_file_alone(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.revoke_refresh_tokens()
    graph.clock.advance(3500)
    before = CACHE.read_bytes()
    result = read(graph, where)
    assert result.startswith("Error:") and "revoked" in result
    assert "awa connect microsoft --teams" in result
    assert CACHE.read_bytes() == before
    assert graph.calls("GET", "/v1.0/*") == []  # nothing was sent to Graph


@proves("entra.refresh_token_idle_90_days")
def test_a_sign_in_unused_for_90_days_asks_for_a_new_one(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(91 * 86_400)
    result = read(graph, where)
    assert "not used for 90 days" in result and "Sign in again" in result


def test_a_token_the_server_rejects_early_is_replaced_once_and_the_call_succeeds(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = world(graph)
    monkeypatch.setattr(auth, "_now", time.time)  # the local clock is real, the server's is not
    sign_in(graph)
    graph.clock.advance(4000)  # the server now thinks the token ended; the machine does not
    assert read(graph, where).startswith("<tool_output")
    assert [r.status for r in graph.calls("GET", "/v1.0/teams/*")] == [401, 200]
    assert refreshes(graph) == 1


def test_an_access_token_revoked_on_the_server_is_replaced(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.revoke_access_token(cache_file()["accounts"][0]["access_token"])
    assert read(graph, where).startswith("<tool_output")
    assert refreshes(graph) == 1


# --------------------------------------------------------------------------- clock skew


def test_an_expiry_far_in_the_future_is_a_wrong_clock_and_not_trusted(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    stored = cache_file()
    stored["accounts"][0]["expires_at"] = graph.clock.time() + 10 * 86_400
    CACHE.write_text(json.dumps(stored), encoding="utf-8")
    read(graph, where)
    assert refreshes(graph) == 1  # renewed instead of trusting a ten day token
    assert cache_file()["accounts"][0]["expires_at"] < graph.clock.time() + 86_400


def test_a_machine_clock_that_jumps_ahead_renews_and_one_that_is_behind_does_not_break(
    graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = world(graph)
    sign_in(graph)
    monkeypatch.setattr(auth, "_now", lambda: graph.clock.time() + 7200)  # two hours ahead
    read(graph, where)
    assert refreshes(graph) == 1
    monkeypatch.setattr(auth, "_now", lambda: graph.clock.time() - 7200)  # two hours behind
    assert read(graph, where).startswith("<tool_output")  # no loop, no crash


def test_a_missing_expires_in_falls_back_to_the_exp_claim_and_is_bounded(graph: FakeGraph) -> None:
    ada = graph.find_user("ada@contoso.test")
    token = graph.issue_token(graph.apps[PUBLIC], kind="user", user=ada)
    exp = decode_jwt(token)["exp"]
    assert auth._lifetime({}, token) == min(max(exp - auth._now(), 1.0), 86_400.0)
    assert auth._lifetime({"expires_in": "nonsense"}, token) <= 86_400
    assert auth._lifetime({"expires_in": 10**9}, token) == 86_400
    assert auth._lifetime({"expires_in": -5}, token) == 1.0


# --------------------------------------------------------------------------- the cache


def test_two_threads_never_lose_an_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the file rights have a test of their own
    monkeypatch.setattr(auth, "_restrict", lambda path: None)
    cache = auth.TokenCache(tmp_path / "c.json")
    with cache.edit() as accounts:
        accounts.append(account(counter=0))

    def bump() -> None:
        for _ in range(15):
            with cache.edit() as accounts:
                accounts[0]["scopes"] = [str(int(accounts[0]["scopes"][0]) + 1)]

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache.read()[0]["scopes"] == ["60"]


def account(counter: int = 0, **fields: Any) -> dict[str, Any]:
    return {
        "client_id": PUBLIC,
        "tenant": "t",
        "username": "u@x.test",
        "scopes": [str(counter)],
        "refresh_token": "r",
        "access_token": "a",
        "expires_at": 0,
        **fields,
    }


def test_many_providers_at_once_refresh_exactly_once(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(3500)
    answers: list[str] = []

    def go() -> None:
        answers.append(read(graph, where))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(a.startswith("<tool_output") for a in answers), answers
    assert refreshes(graph) == 1 and graph.store("teams").stale_refresh_tokens == 0


@proves("entra.refresh_token_rotates")
def test_a_worker_and_a_command_line_in_two_processes_never_refresh_at_once(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = world(graph)
    monkeypatch.setattr(auth, "_now", time.time)  # real time: the children use their own clocks
    sign_in(graph)
    stored = cache_file()
    stored["accounts"][0]["expires_at"] = 0  # due for renewal
    CACHE.write_text(json.dumps(stored), encoding="utf-8")
    script = (
        "import json, sys, time, pathlib\n"
        "from awa.connectors import connector_tools\n"
        "a = json.loads(sys.argv[1])\n"
        "pathlib.Path(sys.argv[2]).write_text('ready')\n"
        "while not pathlib.Path(a['go']).exists(): time.sleep(0.005)\n"
        "tools = {t.name: t for t in connector_tools('teams', env=a['env'], config=a['config'])}\n"
        "out = tools['teams_list_channel_messages'].invoke({})\n"
        "print('OK' if out.startswith('<tool_output') else out)\n"
    )
    go = tmp_path / "go"
    payload = json.dumps(
        {
            "go": str(go),
            "env": env(graph, **pinned(where)),
            "config": {**graph.connector_options, "requires_approval": False},
        }
    )
    source = str(Path(awa.__file__).resolve().parents[1])
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, payload, str(tmp_path / f"ready{number}")],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": source},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in range(3)
    ]
    deadline = time.monotonic() + 60
    while len(list(tmp_path.glob("ready*"))) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    go.write_text("go")
    results = [child.communicate(timeout=60) for child in children]
    assert [out.strip() for out, _ in results] == ["OK", "OK", "OK"], results
    assert refreshes(graph) == 1 and graph.store("teams").stale_refresh_tokens == 0


@pytest.mark.slow
def test_a_lock_held_by_another_process_is_waited_for_then_reported(
    graph: FakeGraph, tmp_path: Path
) -> None:
    path = tmp_path / "held.json"
    held = tmp_path / "held"
    script = (
        "import sys, time, pathlib\n"
        "from pathlib import Path\n"
        "from awa.connectors._microsoft_auth import TokenCache\n"
        "with TokenCache(Path(sys.argv[1])).edit():\n"
        "    pathlib.Path(sys.argv[2]).write_text('locked')\n"
        "    time.sleep(4)\n"
    )
    source = str(Path(awa.__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(held)],
        env={**os.environ, "PYTHONPATH": source},
    )
    try:
        deadline = time.monotonic() + 20
        while not held.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with (
            pytest.raises(auth.MicrosoftAuthError) as caught,
            auth.TokenCache(path, wait=0.3).edit(),
        ):
            pass
        assert caught.value.code == "ms.cache_busy"
    finally:
        child.wait(timeout=30)
    with auth.TokenCache(path, wait=5).edit():  # free again once the other process is done
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_cache_file_is_readable_by_its_owner_only(graph: FakeGraph) -> None:
    sign_in(graph)
    assert CACHE.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_the_cache_file_loses_its_inherited_rights_on_windows(graph: FakeGraph) -> None:
    sign_in(graph)
    shown = subprocess.run(
        [str(Path(os.environ["SYSTEMROOT"]) / "System32" / "icacls.exe"), str(CACHE.resolve())],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "(I)" not in shown  # nothing inherited from the folder
    assert os.environ["USERNAME"].lower() in shown.lower()
    assert "Everyone" not in shown and "Users:" not in shown
    assert json.loads(CACHE.read_text(encoding="utf-8"))["v"] == 1  # and we can still read it


def test_no_temporary_file_is_left_behind(graph: FakeGraph) -> None:
    sign_in(graph)
    assert sorted(p.name for p in CACHE.parent.iterdir()) == [
        "ms_token_cache.json",
        "ms_token_cache.json.lock",
    ]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a sign-in cache"),
        ('{"accounts": []}', "not a sign-in cache"),
        ('{"v": 2, "accounts": []}', "format version 2"),
        ('{"v": 1, "accounts": {}}', "no list of accounts"),
        ('{"v": 1, "accounts": [1]}', "not an object"),
        ('{"v": 1, "accounts": [{"client_id": 1}]}', "is not text"),
        (
            json.dumps({"v": 1, "accounts": [account(refresh_token="")]}),
            "no client, tenant or refresh token",
        ),
        (json.dumps({"v": 1, "accounts": [account(scopes="x")]}), "not a list of text"),
        (json.dumps({"v": 1, "accounts": [account(expires_at="soon")]}), "not a number"),
        (json.dumps({"v": 1, "accounts": [account(expires_at=True)]}), "not a number"),
        ("x" * 1_200_000, "unexpectedly large"),
    ],
    ids=lambda value: str(value)[:30],
)
def test_a_damaged_cache_is_refused_with_a_message_and_never_overwritten(
    graph: FakeGraph, content: str, reason: str
) -> None:
    where = world(graph)
    CACHE.parent.mkdir()
    CACHE.write_text(content, encoding="utf-8")
    before = CACHE.read_bytes()
    result = read(graph, where)
    assert result.startswith("Error:") and reason in result and "did not change it" in result
    with pytest.raises(auth.MicrosoftAuthError) as caught:  # the sign-in refuses before it asks
        sign_in(graph)
    assert caught.value.code == "ms.cache_unusable"
    assert graph.calls("POST", "/*/oauth2/v2.0/devicecode") == []
    assert CACHE.read_bytes() == before


def test_editing_a_cache_that_turns_out_to_be_damaged_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "late.json"
    path.write_text('{"v": 3}', encoding="utf-8")  # damaged after any earlier check
    with (
        pytest.raises(auth.MicrosoftAuthError, match="format version 3"),
        auth.TokenCache(path).edit() as accounts,
    ):
        accounts.append(account())
    assert path.read_text(encoding="utf-8") == '{"v": 3}'


def test_an_error_inside_an_edit_saves_nothing(tmp_path: Path) -> None:
    cache = auth.TokenCache(tmp_path / "c.json")
    with cache.edit() as accounts:
        accounts.append(account(counter=1))
    with pytest.raises(RuntimeError), cache.edit() as accounts:
        accounts.clear()
        raise RuntimeError("the renewal failed")
    assert len(cache.read()) == 1


def test_a_cache_that_is_not_signed_in_for_this_app_says_how_to_sign_in(graph: FakeGraph) -> None:
    where = world(graph)
    result = read(graph, where)
    assert "Not signed in to Microsoft 365" in result and "awa connect microsoft --teams" in result
    assert graph.requests == []  # not even a token request


def test_two_signed_in_users_need_the_account_option(graph: FakeGraph) -> None:
    where = world(graph)
    graph.add_user("bob@contoso.test", name="Bob")
    bob = graph.find_user("bob@contoso.test")
    assert bob is not None
    graph.store("teams").teams[where["team_id"]].members.add(bob.id)
    sign_in(graph)
    graph.identity.device_user = "bob@contoso.test"
    sign_in(graph)
    assert len(cache_file()["accounts"]) == 2
    assert "Several Microsoft accounts" in read(graph, where)
    chosen = read(graph, where, account="bob@contoso.test")
    assert chosen.startswith("<tool_output")
    sent = graph.calls("GET", "/v1.0/teams/*")[-1].header("authorization")
    assert decode_jwt(sent.split()[1])["upn"] == "bob@contoso.test"


def test_a_device_user_that_matches_no_account_is_denied(graph: FakeGraph) -> None:
    graph.identity.device_user = "nobody@contoso.test"
    with pytest.raises(auth.MicrosoftAuthError, match="access_denied|does not exist"):
        sign_in(graph)


def test_missing_settings_name_the_variables() -> None:
    with pytest.raises(auth.MicrosoftAuthError, match="AZURE_TENANT_ID and AZURE_CLIENT_ID"):
        auth._identity(None, "x")
    with pytest.raises(auth.MicrosoftAuthError, match="tenant id or domain"):
        auth._identity("a/b", "99999999-8888-7777-6666-555555555555")
    with pytest.raises(auth.MicrosoftAuthError, match="application"):
        auth._identity("contoso.onmicrosoft.com", "x y")


# --------------------------------------------------------------------------- cache path


@pytest.mark.parametrize(
    "option",
    [
        "../ms.json",
        "a/../../ms.json",
        "x/..",
        "ms.txt",
        "ms",
        "bad\x00.json",
        "a/b/../../../c.json",
    ],
)
def test_a_cache_path_cannot_climb_out_or_name_other_files(option: str) -> None:
    with pytest.raises(auth.MicrosoftAuthError):
        auth.cache_path(option)


def test_a_cache_path_may_be_nested_or_absolute_but_not_a_folder_or_a_link(tmp_path: Path) -> None:
    assert auth.cache_path("sub/dir/x.json") == (tmp_path / "sub/dir/x.json").resolve()
    outside = tmp_path / "shared" / "ms.json"
    assert auth.cache_path(str(outside)) == outside
    (tmp_path / "folder.json").mkdir()
    with pytest.raises(auth.MicrosoftAuthError, match="link or a folder"):
        auth.cache_path("folder.json")
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(auth.MicrosoftAuthError, match="link or a folder"):
        auth.cache_path("link.json")


def test_a_relative_path_through_a_linked_folder_cannot_leave_the_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (tmp_path / "out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(auth.MicrosoftAuthError, match="leads out"):
        auth.cache_path("out/ms.json")


def test_the_cache_option_moves_the_file(graph: FakeGraph) -> None:
    sign_in(graph, token_cache="elsewhere/ms.json")
    assert Path("elsewhere/ms.json").exists() and not CACHE.exists()
    where = world(graph)
    assert read(graph, where, token_cache="elsewhere/ms.json").startswith("<tool_output")
    assert read(graph, where).startswith("Error:")  # the default file has no sign-in


# --------------------------------------------------------------------------- claims


def test_claims_are_read_offline_for_display(graph: FakeGraph) -> None:
    app = graph.apps[PUBLIC]
    user = graph.find_user("ada@contoso.test")
    token = graph.issue_token(app, kind="user", scopes=["User.Read", "Chat.Read"], user=user)
    claims = auth.decode_claims(token)
    assert claims.username == "ada@contoso.test" and claims.tenant == graph.tenant_id
    assert claims.scopes == ("User.Read", "Chat.Read") and claims.roles == ()
    assert claims.expires_at == decode_jwt(token)["exp"]
    application = graph.add_app("app-1234-5678", "x", roles=("Mail.Read", "Mail.Send"))
    assert auth.decode_claims(graph.issue_token(application)).roles == ("Mail.Read", "Mail.Send")
    assert graph.requests == []  # no network


@pytest.mark.parametrize(
    "token",
    ["", "a", "a.b", "a.b.c", "a.!!!.c", "a." + "A" * 20_000 + ".c", "x.W10.c", "x.IjEi.c", "...."],
)
def test_garbage_is_never_an_error_when_reading_claims(token: str) -> None:
    claims = auth.decode_claims(token)
    assert claims == auth.TokenClaims()


def test_claims_ignore_values_of_the_wrong_kind() -> None:
    import base64

    def forge(payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"e30.{body}.sig"

    claims = auth.decode_claims(
        forge({"upn": 5, "scp": ["x"], "roles": "x", "exp": "soon", "tid": "a\x00b"})
    )
    assert claims == auth.TokenClaims()
    assert auth.decode_claims(forge({"exp": float("inf")})).expires_at is None
    assert auth.decode_claims(forge({"exp": True})).expires_at is None
    assert auth.decode_claims(forge({"exp": 10**400})).expires_at is None
    assert auth.decode_claims(forge({"exp": 1_800_000_000})).expires_at == 1_800_000_000.0


# --------------------------------------------------------------------------- hosts and settings


def test_sign_in_takes_http_options_only_so_hosts_cannot_be_widened(graph: FakeGraph) -> None:
    with pytest.raises(auth.MicrosoftAuthError, match="allowed_hosts"):
        sign_in(graph, http={**graph.connector_options, "allowed_hosts": ["evil.example"]})
    with pytest.raises(auth.MicrosoftAuthError, match="base_url"):
        sign_in(graph, http={"base_url": "https://evil.example"})
    assert graph.requests == []


@pytest.mark.parametrize(
    "authority",
    [
        "https://login.microsoftonline.com@evil.example",
        "https://login.microsoftonline.com/common",
        "https://login.microsoftonline.com?x=1",
        "https://login.microsoftonline.com#x",
        "ftp://login.microsoftonline.com",
        "file:///etc/passwd",
        "login.microsoftonline.com",
        "",
    ],
)
def test_the_authority_must_be_a_plain_host_address(authority: str, graph: FakeGraph) -> None:
    with pytest.raises(auth.MicrosoftAuthError, match="plain host address"):
        sign_in(graph, authority=authority)
    assert graph.requests == []


@pytest.mark.parametrize(
    "authority", ["https://169.254.169.254", "https://127.0.0.1:9", "http://evil.example"]
)
def test_an_authority_override_cannot_reach_internal_or_plain_http_hosts(
    authority: str, graph: FakeGraph
) -> None:
    with pytest.raises(ToolException) as caught:
        sign_in(graph, authority=authority, http={})  # production defaults: no private networks
    refusal = "plain http://" if authority.startswith("http:") else "private or reserved address"
    assert refusal in str(caught.value)
    assert graph.requests == []


def test_the_local_fake_is_refused_unless_private_networks_are_allowed(graph: FakeGraph) -> None:
    with pytest.raises(ToolException, match="private"):
        sign_in(graph, http={})
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(3500)
    result = read(graph, where)  # the same call through the connector is fine with the options
    assert result.startswith("<tool_output")
    built = connector_tools(
        "teams", env=env(graph, **pinned(where)), config={"requires_approval": False}
    )
    tool = next(t for t in built if t.name == "teams_list_channel_messages")
    assert "private" in tool.invoke({})  # and refused without them


def test_the_token_goes_only_to_the_graph_host(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    read(graph, where)
    for request in graph.requests:
        if "/oauth2/" in request.path:
            assert request.header("authorization") is None  # the token endpoint gets no bearer
    sent = [r for r in graph.requests if r.header("authorization")]
    assert sent and all(r.path.startswith("/v1.0/") for r in sent)  # only Graph calls carry it


# --------------------------------------------------------------------------- transient failures


def test_a_token_endpoint_that_throttles_leaves_the_sign_in_untouched(graph: FakeGraph) -> None:
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(3500)
    before = CACHE.read_bytes()
    graph.throttle(TOKEN_ENDPOINT, retry_after=1, times=10)
    result = read(graph, where)
    assert "limiting requests" in result and "Wait a moment" in result
    assert CACHE.read_bytes() == before
    graph.clear_faults()
    assert read(graph, where).startswith("<tool_output")  # and it works again afterwards


def test_a_server_error_on_renewal_is_retried_without_losing_the_refresh_token(
    graph: FakeGraph,
) -> None:
    where = world(graph)
    sign_in(graph)
    graph.clock.advance(3500)
    graph.fail("POST", TOKEN_ENDPOINT, status=503, times=1)
    assert read(graph, where).startswith("<tool_output")
    assert refreshes(graph) == 2 and graph.store("teams").stale_refresh_tokens == 0


# --------------------------------------------------------------------------- nothing secret leaks


def test_no_token_shows_up_in_any_output_of_any_path(
    graph: FakeGraph, caplog: pytest.LogCaptureFixture
) -> None:
    where = world(graph)
    shown: list[auth.DeviceCodePrompt] = []
    caplog.set_level("DEBUG")
    done = sign_in(graph, shown)
    stored = cache_file()["accounts"][0]
    device_code = graph.calls("POST", TOKEN_ENDPOINT)[0].form()["device_code"]
    outputs = [repr(done), repr(shown[0]), shown[0].text(), repr(auth.TokenCache(CACHE))]
    outputs.append(repr(auth.UserTokenProvider(None)))  # type: ignore[arg-type]
    outputs.append(read(graph, where))
    graph.clock.advance(3500)
    graph.throttle(TOKEN_ENDPOINT, retry_after=1, times=10)
    outputs.append(read(graph, where))  # throttled
    graph.clear_faults()
    graph.revoke_refresh_tokens()
    graph.clock.advance(3500)
    outputs.append(read(graph, where))  # revoked
    CACHE.write_text("{broken " + stored["refresh_token"], encoding="utf-8")
    outputs.append(read(graph, where))  # damaged cache (its content is not repeated)
    with pytest.raises(auth.MicrosoftAuthError) as caught:
        sign_in(graph)
    outputs.append(str(caught.value))
    outputs.append(caplog.text)
    refresh = stored["refresh_token"]
    for text in outputs:
        for secret in (refresh, stored["access_token"], device_code):
            assert secret not in text
    check = check_connector("teams", env=env(graph), config=graph.connector_options)
    assert refresh not in check.message and stored["access_token"] not in check.message

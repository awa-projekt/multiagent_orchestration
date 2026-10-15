"""`awa connect microsoft` and certificate sign-in for the Microsoft connectors."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from _helpers.fake_graph_proof import proves
from _helpers.fakegraph import FakeGraph
from _helpers.fakegraph.core import FakeClock, TokenError, decode_jwt
from _helpers.fakegraph.identity import verify_assertion
from _helpers.microsoft_world import microsoft_world
from awa.checks._engine import CheckReport
from awa.checks.connect import QUESTIONS, run_connect_microsoft
from awa.cli import main
from awa.connectors import _engine, connector_tools
from awa.connectors import _microsoft_cert as cert
from awa.connectors._catalog import connector_context
from awa.connectors._microsoft_errors import MicrosoftAuthError
from awa.connectors._saas_microsoft_teams import _SPACE
from awa.core.dotenv import read_env_file


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, _ in QUESTIONS:
        monkeypatch.delenv(name, raising=False)
    for name in ("AZURE_AUTHORITY_HOST", "MS_GRAPH_URL", "AWA_CHECK_NEGATIVE_MAILBOX"):
        monkeypatch.delenv(name, raising=False)
    _SPACE._next.clear()


def test_the_flow_asks_hidden_saves_signs_teams_in_and_checks(tmp_path: Path) -> None:
    with microsoft_world() as world:
        env = world.env
        (tmp_path / ".env").write_text(
            f"AZURE_AUTHORITY_HOST={env['AZURE_AUTHORITY_HOST']}\n"
            f"MS_GRAPH_URL={env['MS_GRAPH_URL']}\n"
            f"AWA_CHECK_NEGATIVE_MAILBOX={env['AWA_CHECK_NEGATIVE_MAILBOX']}\n",
            encoding="utf-8",
        )
        asked: list[str] = []

        def secret(prompt: str) -> str:
            name = prompt.split(":", 1)[0]
            asked.append(name)
            return env.get(name, "")

        out = io.StringIO()
        code = run_connect_microsoft(
            tmp_path, teams=True, secret=secret, out=out, connector_options=world.options
        )
    text = out.getvalue()
    assert code == 0, text
    saved = read_env_file(tmp_path / ".env")
    for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "MS_MAILBOX"):
        assert saved[name] == env[name]
    assert "AZURE_CLIENT_CERTIFICATE_PATH" not in asked  # a secret was given
    assert env["AZURE_CLIENT_SECRET"] not in text  # values are never printed
    assert "AZURE_TENANT_ID                  not set" in text
    assert "Signed in as awa-bot@" in text
    assert "OK   microsoft.teams" in text and "the pinned channel is readable" in text
    assert (tmp_path / ".awa" / "ms_token_cache.json").is_file()


def _answer_everything(project: Path) -> None:
    (project / ".env").write_text("".join(f"{name}=x\n" for name, _ in QUESTIONS), encoding="utf-8")


def test_a_second_run_asks_nothing_already_set(tmp_path: Path) -> None:
    _answer_everything(tmp_path)
    asked: list[str] = []
    out = io.StringIO()
    run_connect_microsoft(tmp_path, secret=lambda p: asked.append(p) or "", out=out)
    assert asked == []
    assert "AZURE_CLIENT_SECRET              set" in out.getvalue()


def test_connect_makes_no_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import awa.checks

    seen: list[bool] = []

    def run_groups(groups: list[str], **options: Any) -> CheckReport:
        seen.append(options["no_model"])
        return CheckReport(runs=())

    monkeypatch.setattr(awa.checks, "run_groups", run_groups)
    run_connect_microsoft(tmp_path, secret=lambda p: "", out=io.StringIO())

    assert seen == [True]


def test_cli_the_word_microsoft_names_the_system(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _answer_everything(tmp_path)
    assert main(["connect", "microsoft", "--project", str(tmp_path)]) == 1
    assert "Microsoft 365 values in .env" in capsys.readouterr().out


def test_cli_a_folder_called_microsoft_is_given_as_a_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "microsoft").mkdir()
    assert main(["connect", "./microsoft"]) == 1
    assert "has no CONNECT.md" in capsys.readouterr().err
    _answer_everything(tmp_path)
    assert main(["connect", "microsoft"]) == 1
    assert "awa connect ./microsoft" in capsys.readouterr().out


def test_cli_teams_without_microsoft_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", "--teams"]) == 1
    assert "belong to awa connect microsoft" in capsys.readouterr().err


def test_the_sign_in_asks_for_the_people_permission_when_people_are_used(tmp_path: Path) -> None:
    from awa.checks.connect import PEOPLE_SCOPE, USER_SCOPES, sign_in_scopes

    assert sign_in_scopes(tmp_path) == USER_SCOPES
    assert sign_in_scopes(tmp_path, people=True) == (*USER_SCOPES, PEOPLE_SCOPE)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "people.yaml").write_text("schema: awa.people/v1\n", encoding="utf-8")
    assert PEOPLE_SCOPE in sign_in_scopes(tmp_path)


def test_connect_people_signs_in_with_user_readbasic_all(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AZURE_TENANT_ID=6c2e3d1a-5b7f-4c9e-8a10-2f4d6e8b0c13\n"
        "AZURE_CLIENT_ID=99999999-8888-7777-6666-555555555555\n",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def sign_in(**kwargs: object) -> object:
        seen.update(kwargs)
        raise MicrosoftAuthError("stop here")

    out = io.StringIO()
    code = run_connect_microsoft(
        tmp_path, people=True, secret=lambda prompt: "", out=out, sign_in=sign_in
    )
    assert code == 1 and "User.ReadBasic.All" in seen["scopes"]  # type: ignore[operator]
    assert "TEAMS_TEAM_ID" not in out.getvalue().split("(values are never shown):")[0]


def test_cli_people_without_microsoft_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["connect", ".", "--people"]) == 1
    assert "--people" in capsys.readouterr().err


CLIENT = "11111111-2222-3333-4444-555555555555"
MAILBOX = "support@contoso.test"
PASSWORD = "Pfx-Password-Never-Printed-7"
GOLDEN = Path(__file__).parents[1] / "fixtures" / "microsoft" / "msal_client_assertion.json"
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def make_certificate(
    folder: Path,
    *,
    days: int = 365,
    password: str | None = None,
    pfx: bool = False,
    name: str = "awa.pem",
) -> tuple[Path, bytes]:
    """A self-signed RSA certificate with its key in one file; returns the file and the DER."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "awa test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=400))
        .not_valid_after(NOW + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    encryption: Any = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    path = folder / name
    if pfx:
        path.write_bytes(
            pkcs12.serialize_key_and_certificates(b"awa", key, certificate, None, encryption)
        )
    else:
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
            )
            + certificate.public_bytes(serialization.Encoding.PEM)
        )
    return path, certificate.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGraph]:
    with FakeGraph(clock=FakeClock(NOW)) as server:
        server.add_user(MAILBOX, name="Support")
        monkeypatch.setattr(cert, "_now", server.clock.time)
        monkeypatch.setattr(_engine, "_sleep", lambda seconds: None)
        yield server


def outlook_env(graph: FakeGraph, path: Path, **extra: str) -> dict[str, str]:
    return {
        "AZURE_TENANT_ID": graph.tenant_id,
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_AUTHORITY_HOST": graph.authority,
        "MS_GRAPH_URL": graph.graph_url,
        "MS_MAILBOX": MAILBOX,
        "AZURE_CLIENT_CERTIFICATE_PATH": str(path),
        **extra,
    }


def token_requests(graph: FakeGraph) -> list[dict[str, str]]:
    return [r.form() for r in graph.calls("POST", "/*/oauth2/v2.0/token")]


@proves("entra.client_assertion_certificate")
def test_certificate_round_trip_reads_the_inbox_with_one_token_request(
    graph: FakeGraph, tmp_path: Path
) -> None:
    path, der = make_certificate(tmp_path)
    app = graph.add_app(CLIENT, roles=("Mail.Read",))
    app.certificates.append(der)
    tools = connector_tools("outlook", env=outlook_env(graph, path), config=graph.connector_options)
    listing = next(t for t in tools if t.name == "outlook_list_messages")
    listing.invoke({})
    listing.invoke({})
    [form] = token_requests(graph)
    assert "client_secret" not in form
    assert form["client_assertion_type"] == cert.ASSERTION_TYPE
    header = json.loads(base64.urlsafe_b64decode(form["client_assertion"].split(".")[0] + "=="))
    assert header == {
        "alg": "PS256",
        "typ": "JWT",
        "x5t#S256": base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode(),
    }
    claims = decode_jwt(form["client_assertion"])
    assert claims["aud"] == graph.token_url()
    assert claims["iss"] == claims["sub"] == CLIENT
    assert claims["exp"] - claims["nbf"] == cert.ASSERTION_LIFETIME
    assert {"jti", "iat"} <= set(claims)


def test_the_certificate_wins_over_a_secret_and_missing_names_the_secret(
    graph: FakeGraph, tmp_path: Path
) -> None:
    path, der = make_certificate(tmp_path)
    graph.add_app(CLIENT, "a-secret-value-123", roles=("Mail.Read",)).certificates.append(der)
    env = outlook_env(graph, path, AZURE_CLIENT_SECRET="a-secret-value-123")
    ctx = connector_context("outlook", env=env, config=graph.connector_options)
    assert type(ctx.active_auth()).__name__ == "TokenAuth"  # both set: the certificate
    tools = connector_tools("outlook", env=env, config=graph.connector_options)
    next(t for t in tools if t.name == "outlook_list_messages").invoke({})
    [form] = token_requests(graph)
    assert "client_assertion" in form and "client_secret" not in form
    del env["AZURE_CLIENT_CERTIFICATE_PATH"]
    ctx = connector_context("outlook", env=env, config=graph.connector_options)
    assert type(ctx.active_auth()).__name__ == "OAuth2ClientCredentials"
    nothing = connector_context("outlook", env={"MS_MAILBOX": MAILBOX}, config={})
    assert "AZURE_CLIENT_SECRET" in nothing.missing()  # the usual path is named
    assert "AZURE_CLIENT_CERTIFICATE_PATH" not in nothing.missing()


def test_a_protected_pfx_file_signs_in(graph: FakeGraph, tmp_path: Path) -> None:
    path, der = make_certificate(tmp_path, password=PASSWORD, pfx=True, name="awa.pfx")
    graph.add_app(CLIENT, roles=("Mail.Read",)).certificates.append(der)
    env = outlook_env(graph, path, AZURE_CLIENT_CERTIFICATE_PASSWORD=PASSWORD)
    tools = connector_tools("outlook", env=env, config=graph.connector_options)
    answer = next(t for t in tools if t.name == "outlook_list_messages").invoke({})
    assert not str(answer).startswith("Error")
    [form] = token_requests(graph)
    assert "client_assertion" in form


def test_a_wrong_thumbprint_is_refused_with_advice(graph: FakeGraph, tmp_path: Path) -> None:
    path, _ = make_certificate(tmp_path)
    _, other = make_certificate(tmp_path, name="other.pem")
    graph.add_app(CLIENT, roles=("Mail.Read",)).certificates.append(other)
    tools = connector_tools("outlook", env=outlook_env(graph, path), config=graph.connector_options)
    answer = next(t for t in tools if t.name == "outlook_list_messages").invoke({})
    assert "did not accept the certificate" in str(answer)


def test_an_expired_certificate_is_refused_before_anything_is_sent(
    graph: FakeGraph, tmp_path: Path
) -> None:
    path, der = make_certificate(tmp_path, days=-1)
    graph.add_app(CLIENT, roles=("Mail.Read",)).certificates.append(der)
    ctx = connector_context("outlook", env=outlook_env(graph, path), config=graph.connector_options)
    provider = cert.CertificateTokenProvider("https://graph.microsoft.com/.default")
    with pytest.raises(MicrosoftAuthError) as caught:
        provider.token(ctx)
    assert caught.value.code == "ms.certificate_expired"
    assert token_requests(graph) == []


def test_fakegraph_refuses_an_expired_certificate_and_a_replayed_assertion(tmp_path: Path) -> None:
    path, der = make_certificate(tmp_path, days=-1)
    loaded = cert.load_certificate(path)
    url = "http://127.0.0.1:1/t/oauth2/v2.0/token"
    assertion = cert.client_assertion(loaded, client_id=CLIENT, token_url=url, now=NOW.timestamp())
    with pytest.raises(TokenError) as caught:
        verify_assertion(
            assertion, client_id=CLIENT, certificates=[der], audiences={url}, now=NOW, used=set()
        )
    assert caught.value.aadsts == 700027 and "expired" in caught.value.text
    fresh, fresh_der = make_certificate(tmp_path, name="fresh.pem")
    signed = cert.client_assertion(
        cert.load_certificate(fresh), client_id=CLIENT, token_url=url, now=NOW.timestamp()
    )
    used: set[str] = set()
    options: dict[str, Any] = {"client_id": CLIENT, "certificates": [fresh_der], "used": used}
    verify_assertion(signed, audiences={url}, now=NOW, **options)
    with pytest.raises(TokenError, match="already used"):
        verify_assertion(signed, audiences={url}, now=NOW, **options)
    late = cert.client_assertion(
        cert.load_certificate(fresh), client_id=CLIENT, token_url=url, now=NOW.timestamp() - 3600
    )
    with pytest.raises(TokenError) as caught:
        verify_assertion(late, audiences={url}, now=NOW, **options)
    assert caught.value.aadsts == 700024


def test_golden_msal_assertion_is_accepted_and_has_our_shape(tmp_path: Path) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    der = x509.load_pem_x509_certificate(golden["certificate_pem"].encode()).public_bytes(
        serialization.Encoding.DER
    )
    url = "http://127.0.0.1:1" + golden["token_url_path"]
    claims = verify_assertion(
        golden["assertion"],
        client_id=golden["client_id"],
        certificates=[der],
        audiences={url},
        now=datetime.fromtimestamp(golden["issued_at"] + 60, UTC),
        used=set(),
    )
    msal_header = json.loads(base64.urlsafe_b64decode(golden["assertion"].split(".")[0] + "=="))
    path, own_der = make_certificate(tmp_path)
    ours = cert.client_assertion(
        cert.load_certificate(path), client_id=golden["client_id"], token_url=url
    )
    our_header = json.loads(base64.urlsafe_b64decode(ours.split(".")[0] + "=="))
    assert {k: msal_header[k] for k in ("alg", "typ")} == {k: our_header[k] for k in ("alg", "typ")}
    assert set(msal_header) - {"x5c"} == set(our_header)
    assert set(claims) <= set(decode_jwt(ours))


def test_the_password_never_appears_in_messages_or_logs(
    graph: FakeGraph, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path, der = make_certificate(tmp_path, password=PASSWORD)
    graph.add_app(CLIENT, roles=("Mail.Read",)).certificates.append(der)
    env = outlook_env(graph, path, AZURE_CLIENT_CERTIFICATE_PASSWORD="wrong-" + PASSWORD)
    caplog.set_level(logging.DEBUG)
    tools = connector_tools("outlook", env=env, config=graph.connector_options)
    answer = str(next(t for t in tools if t.name == "outlook_list_messages").invoke({}))
    assert "PASSWORD must match" in answer
    assert PASSWORD not in answer
    assert PASSWORD not in caplog.text
    assert PASSWORD not in repr(cert.CertificateTokenProvider("s"))


def test_a_missing_cryptography_package_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography.x509", None)
    with pytest.raises(MicrosoftAuthError) as caught:
        cert.load_certificate(tmp_path / "missing.pem")
    assert caught.value.code == "ms.certificate_dependency"
    assert 'pip install "awa-agents[m365]"' in str(caught.value)

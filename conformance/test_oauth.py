"""OAuth: docs-mcp as a resource server for an Entra-like issuer (SPEC.md §2.2), and the refresh secret (§2.1)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator

import httpx2
import jwt
import pytest

from conftest import COMMANDS, FIXTURE_DOCS, TOKEN, Server, free_port, start_server, stop_server
from issuer import AUDIENCE, SCOPE, Issuer

pytestmark = pytest.mark.anyio

REFRESH = "webhook-secret"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def oauth_env(issuer_url: str, port: int, **extra: str) -> dict[str, str]:
    return {
        "DOCS_LOCAL_PATH": str(FIXTURE_DOCS),
        "MCP_AUTH_TOKEN": "",
        "MCP_PORT": str(port),
        "MCP_PUBLIC_URL": f"http://localhost:{port}/mcp",
        "MCP_OAUTH_ISSUER": issuer_url,
        "MCP_OAUTH_AUDIENCE": AUDIENCE,
        "MCP_OAUTH_SCOPES": SCOPE,
        **extra,
    }


def port(server: Server) -> str:
    return server.base.rsplit(":", 1)[1]


def challenge(server: Server, error: str | None = None, reason: str | None = None) -> str:
    value = (
        f'Bearer resource_metadata="http://localhost:{port(server)}/.well-known/oauth-protected-resource/mcp", '
        f'scope="{SCOPE}"'
    )
    return value + (f', error="{error}", error_description="{reason}"' if error else "")


def post(server: Server, token: str | None, path: str = "/mcp", **headers: str) -> httpx2.Response:
    sent = {**HEADERS, **headers}
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return server.http("POST", path, headers=sent, json=LIST)


def wait_for_log(server: Server, text: str) -> str:
    deadline = time.monotonic() + 10
    while text not in (logs := server.logs()) and time.monotonic() < deadline:
        time.sleep(0.1)
    return logs


@pytest.fixture(scope="module")
def issuer() -> Iterator[Issuer]:
    i = Issuer()
    i.add_key("es", alg="ES256")
    i.add_key("rogue", publish=False)
    yield i
    i.stop()


@pytest.fixture(scope="module")
def oauth(impl: str, issuer: Issuer, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-oauth")
    s = start_server(impl, oauth_env(issuer.url, free_port(), MCP_REFRESH_TOKEN=REFRESH), tmp)
    yield s
    stop_server(s)


@pytest.fixture(scope="module")
def offline(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """OAuth with an issuer nobody answers for, and no refresh secret."""
    tmp = tmp_path_factory.mktemp(f"{impl}-oauth-offline")
    s = start_server(impl, oauth_env(f"http://127.0.0.1:{free_port()}/tenant/v2.0", free_port()), tmp)
    yield s
    stop_server(s)


def test_metadata(oauth: Server, issuer: Issuer) -> None:
    expected = {
        "resource": f"http://localhost:{port(oauth)}/mcp",
        "authorization_servers": [issuer.url],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "testhandbok",
    }
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        r = oauth.http("GET", path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == expected


def test_no_token(oauth: Server) -> None:
    r = post(oauth, None)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == challenge(oauth)
    assert r.text == "Unauthorized\n"


def test_host_check_comes_first(oauth: Server, issuer: Issuer) -> None:
    r = post(oauth, issuer.token(), Host="evil.example")
    assert r.status_code == 403
    assert "MCP_ALLOWED_HOSTS" in r.text


GOOD: dict[str, Callable[[Issuer], str]] = {
    "rs256": lambda i: i.token(),
    "es256": lambda i: i.token("es"),
    "audience in a list": lambda i: i.token(aud=["api://other", AUDIENCE]),
    "full scope name in scope": lambda i: i.token(scp=None, scope=SCOPE),
    "scopes as a list": lambda i: i.token(scp=None, scope=["profile", "docs.read"]),
    "one of several scopes": lambda i: i.token(scp="profile docs.read"),
    "expired within the leeway": lambda i: i.token(exp=int(time.time()) - 30),
    "no nbf": lambda i: i.token(nbf=None),
}


@pytest.mark.parametrize("case", GOOD)
async def test_accepted(oauth: Server, issuer: Issuer, case: str) -> None:
    async with oauth.client(token=GOOD[case](issuer)) as client:
        result = await client.call_tool("docs_overview", {})
    assert not result.is_error


REFUSED: dict[str, tuple[Callable[[Issuer], str], str]] = {
    "not a jwt": (lambda i: "not-a-jwt", "malformed token"),
    "the shared token": (lambda i: TOKEN, "malformed token"),
    "padded": (lambda i: i.token().replace(".", "==.", 1), "malformed token"),
    "no kid": (lambda i: jwt.encode(i.claims(), i.keys["k1"][1], algorithm="RS256"), "malformed token"),
    "alg none": (lambda i: i.unsigned(), "unsupported algorithm"),
    "hmac with the public key": (lambda i: i.hmac_with_public_key(), "unsupported algorithm"),
    "unknown kid": (lambda i: i.token("rogue"), "unknown signing key"),
    "signed with another key": (lambda i: i.token("k1", sign_with="rogue"), "bad signature"),
    "other issuer": (lambda i: i.token(iss="https://login.example/other/v2.0"), "wrong issuer"),
    "other audience": (lambda i: i.token(aud="api://other"), "wrong audience"),
    "no audience": (lambda i: i.token(aud=None), "wrong audience"),
    "expired": (lambda i: i.token(exp=int(time.time()) - 120), "expired"),
    "no exp": (lambda i: i.token(exp=None), "expired"),
    "not yet valid": (lambda i: i.token(nbf=int(time.time()) + 120), "not yet valid"),
}


@pytest.mark.parametrize("case", REFUSED)
def test_refused(oauth: Server, issuer: Issuer, case: str) -> None:
    make, reason = REFUSED[case]
    r = post(oauth, make(issuer))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == challenge(oauth, "invalid_token", reason)
    assert r.text == "Unauthorized\n"


@pytest.mark.parametrize("scopes", [{"scp": "docs.write"}, {"scp": None}])
def test_missing_scope(oauth: Server, issuer: Issuer, scopes: dict[str, str | None]) -> None:
    r = post(oauth, issuer.token(**scopes))
    assert r.status_code == 403
    assert r.headers["www-authenticate"] == challenge(oauth, "insufficient_scope", f"missing scope {SCOPE}")
    assert r.text == "Forbidden\n"


def test_refresh_takes_only_the_secret(oauth: Server, issuer: Issuer) -> None:
    assert oauth.http("POST", "/refresh", headers={"Authorization": f"Bearer {REFRESH}"}).status_code == 200
    for token in (issuer.token(), "wrong"):
        assert oauth.http("POST", "/refresh", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unreachable_issuer(offline: Server, issuer: Issuer) -> None:
    r = post(offline, issuer.token())
    assert r.status_code == 503
    assert r.text == "Service Unavailable: cannot reach the OAuth issuer\n"


def test_refresh_off_without_secret(offline: Server) -> None:
    r = offline.http("POST", "/refresh")
    assert r.status_code == 403
    assert "MCP_REFRESH_TOKEN" in r.text


def test_oauth_warnings(offline: Server) -> None:
    logs = wait_for_log(offline, "MCP_REFRESH_TOKEN is not set: /refresh is off.")
    assert "MCP_REFRESH_TOKEN is not set: /refresh is off." in logs
    assert "MCP_AUTH_TOKEN is not set" not in logs


def test_key_rotation(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    issuer = Issuer()
    s = start_server(impl, oauth_env(issuer.url, free_port()), tmp_path_factory.mktemp(f"{impl}-rotation"))
    try:
        assert post(s, issuer.token()).status_code == 200
        assert issuer.fetches == 1
        issuer.add_key("k2")
        assert post(s, issuer.token("k2")).status_code == 200
        assert issuer.fetches == 2
        # A kid the issuer never published: refetched for at most once a minute.
        issuer.add_key("k3", publish=False)
        for _ in range(3):
            assert post(s, issuer.token("k3")).status_code == 401
        assert issuer.fetches == 2
    finally:
        stop_server(s)
        issuer.stop()


def test_shared_token_with_refresh_secret(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-refresh-secret")
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(FIXTURE_DOCS), "MCP_REFRESH_TOKEN": REFRESH}, tmp)
    try:
        assert s.http("POST", "/refresh", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
        assert s.http("POST", "/refresh", headers={"Authorization": f"Bearer {REFRESH}"}).status_code == 200
        assert post(s, TOKEN).status_code == 200
        assert post(s, REFRESH).status_code == 401
        assert s.http("GET", "/.well-known/oauth-protected-resource").status_code == 404
    finally:
        stop_server(s)


ISSUER = "https://login.example/tenant/v2.0"
PUBLIC = "https://docs.example/mcp"


@pytest.mark.parametrize(
    ("env", "names"),
    [
        ({"MCP_AUTH_TOKEN": "x", "MCP_OAUTH_ISSUER": ISSUER, "MCP_PUBLIC_URL": PUBLIC}, ["MCP_AUTH_TOKEN", "MCP_OAUTH_ISSUER"]),
        ({"MCP_OAUTH_ISSUER": ISSUER}, ["MCP_PUBLIC_URL"]),
        ({"MCP_OAUTH_ISSUER": "http://login.example/tenant/v2.0", "MCP_PUBLIC_URL": PUBLIC}, ["MCP_OAUTH_ISSUER"]),
        ({"MCP_OAUTH_ISSUER": "login.example", "MCP_PUBLIC_URL": PUBLIC}, ["MCP_OAUTH_ISSUER"]),
        ({"MCP_OAUTH_ISSUER": ISSUER, "MCP_PUBLIC_URL": "http://docs.example/mcp"}, ["MCP_PUBLIC_URL"]),
        ({"MCP_OAUTH_ISSUER": ISSUER, "MCP_PUBLIC_URL": "https:///mcp"}, ["MCP_PUBLIC_URL"]),
        ({"MCP_OAUTH_ISSUER": ISSUER, "MCP_PUBLIC_URL": f"{PUBLIC}?x=1"}, ["MCP_PUBLIC_URL"]),
        ({"MCP_OAUTH_ISSUER": ISSUER, "MCP_PUBLIC_URL": f"{PUBLIC}#x"}, ["MCP_PUBLIC_URL"]),
    ],
)
def test_config_errors(impl: str, env: dict[str, str], names: list[str]) -> None:
    full = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "DOCS_LOCAL_PATH": str(FIXTURE_DOCS),
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(free_port()),
        **env,
    }
    r = subprocess.run(COMMANDS[impl], env=full, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2, r.stdout + r.stderr
    for name in names:
        assert name in r.stderr

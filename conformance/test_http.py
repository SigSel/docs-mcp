"""The HTTP surface around /mcp: health, readiness, auth, host check (SPEC.md §2)."""

from __future__ import annotations

import json
import re
import socket
import time
from collections.abc import Iterator

import httpx2
import pytest

from conftest import FIXTURE_DOCS, TOKEN, Server, start_server, stop_server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_healthz_open(server: Server) -> None:
    r = server.http("GET", "/healthz", headers={"Host": "evil.example"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_status_object(server: Server) -> None:
    r = server.http("GET", "/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["pages"] == 7
    assert body["commit"] is None
    assert body["last_error"] is None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["synced_at"])


def test_mcp_requires_token(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_mcp_rejects_wrong_token(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth("wrong")})
    assert r.status_code == 401


def test_mcp_accepts_token(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth()})
    assert r.status_code == 200


def test_mcp_rejects_unknown_host_before_auth(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth(), "Host": "evil.example"})
    assert r.status_code == 403
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "evil.example:8080"})
    assert r.status_code == 403


def test_mcp_host_with_port_allowed(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth(), "Host": "LOCALHOST:1234"})
    assert r.status_code == 200


def test_refresh(server: Server) -> None:
    assert server.http("POST", "/refresh").status_code == 401
    assert server.http("POST", "/refresh", headers={**auth(), "Host": "evil.example"}).status_code == 403
    r = server.http("POST", "/refresh", headers=auth())
    assert r.status_code == 200
    assert r.json()["ready"] is True
    assert r.json()["pages"] == 7


def test_unknown_path_404(server: Server) -> None:
    assert server.http("GET", "/nope").status_code == 404


def test_403_names_the_setting(server: Server) -> None:
    r = server.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth(), "Host": "evil.example"})
    assert "MCP_ALLOWED_HOSTS" in r.text


def test_no_default_warnings_when_configured(server: Server) -> None:
    assert "is not set" not in server.logs()


def search(size: int) -> bytes:
    """A search_docs call of about `size` bytes."""
    params = {"name": "search_docs", "arguments": {"query": "ab " * (size // 3)}}
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()


def test_body_limit(server: Server) -> None:
    """4 MB, set by the MCP SDKs: tested here, so an SDK update cannot lift it unnoticed."""
    headers = {**MCP_HEADERS, **auth()}
    assert server.http("POST", "/mcp", content=search(3_000_000), headers=headers).status_code == 200
    assert server.http("POST", "/mcp", content=search(5_000_000), headers=headers).status_code == 413


@pytest.fixture(scope="module")
def defaults(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A server left at the defaults: no token, no allowed hosts, no listen address (empty counts as unset)."""
    tmp = tmp_path_factory.mktemp(f"{impl}-defaults")
    env = {"DOCS_LOCAL_PATH": str(FIXTURE_DOCS), "MCP_AUTH_TOKEN": "", "MCP_ALLOWED_HOSTS": "", "MCP_HOST": ""}
    s = start_server(impl, env, tmp)
    yield s
    stop_server(s)


def wait_for_log(server: Server, text: str) -> str:
    for _ in range(40):
        if text in (logs := server.logs()):
            return logs
        time.sleep(0.05)
    raise AssertionError(f"{text!r} not in log:\n{server.logs()}")


def test_defaults_are_announced(defaults: Server) -> None:
    wait_for_log(defaults, "MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are accepted.")
    wait_for_log(defaults, "MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.")


def test_defaults_serve_localhost_without_token(defaults: Server) -> None:
    r = defaults.http("POST", "/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 200


def network_address() -> str | None:
    """This machine's address on its network, the one another machine would connect to, if it has one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # a documentation address: connecting a UDP socket sends nothing
        except OSError:
            return None
        address = s.getsockname()[0]
    return None if address.startswith("127.") else address


def test_defaults_listen_on_this_machine_only(defaults: Server) -> None:
    """The host check is no access control: a request can name localhost from anywhere (SPEC.md §1)."""
    port = defaults.base.rsplit(":", 1)[1]
    wait_for_log(defaults, f"on 127.0.0.1:{port}")
    if (address := network_address()) is None:
        pytest.skip("this machine has no network address to try")
    with pytest.raises(httpx2.ConnectError):
        httpx2.post(f"http://{address}:{port}/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "localhost"}, timeout=5)


def test_refused_host_logged_once(defaults: Server) -> None:
    for _ in range(3):
        r = defaults.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "docs.example.internal:8080"})
        assert r.status_code == 403
    logs = wait_for_log(defaults, "refused a request for host 'docs.example.internal'")
    assert logs.count("refused a request for host 'docs.example.internal'") == 1


def test_wildcard_hosts_announced(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-wildcard")
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(FIXTURE_DOCS), "MCP_ALLOWED_HOSTS": "*"}, tmp)
    try:
        wait_for_log(s, "MCP_ALLOWED_HOSTS is '*': the Host header is not checked.")
        r = s.http("POST", "/mcp", json=INIT, headers={**MCP_HEADERS, **auth(), "Host": "anything.example"})
        assert r.status_code == 200
    finally:
        stop_server(s)

"""The HTTP surface around /mcp: health, readiness, auth, host check (SPEC.md §2)."""

from __future__ import annotations

import re

from conftest import TOKEN, Server

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

"""Starts a docs-mcp implementation as a subprocess and hands tests a client for it.

Pick implementations with `--impl rust,python,typescript` (default: all three).
Each must already be built; `just build` at the repo root does that.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DOCS = ROOT / "fixtures" / "docs"
TOKEN = "s3cret-test-token"
SITE_URL = "https://docs.example/handbook"

COMMANDS: dict[str, list[str]] = {
    "rust": [str(ROOT / "rust" / "target" / "release" / "docs-mcp")],
    "python": [str(ROOT / "python" / ".venv" / "bin" / "docs-mcp")],
    "typescript": ["node", str(ROOT / "typescript" / "dist" / "main.js")],
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--impl", default="rust,python,typescript", help="comma-separated implementations to test")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "impl" in metafunc.fixturenames:
        impls = [i.strip() for i in metafunc.config.getoption("impl").split(",") if i.strip()]
        metafunc.parametrize("impl", impls, scope="module")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Groups each module's tests by implementation, so each server starts once per module."""

    def impl_of(item: pytest.Item) -> str:
        callspec = getattr(item, "callspec", None)
        return callspec.params.get("impl", "") if callspec else ""

    items.sort(key=lambda item: (str(item.path), impl_of(item)))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Server:
    impl: str
    base: str
    process: subprocess.Popen[bytes]
    log: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    def client(self, mode: str = "auto", token: str | None = TOKEN) -> Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        transport = streamable_http_client(self.mcp_url, http_client=httpx2.AsyncClient(headers=headers))
        return Client(transport, mode=mode, cache=None)  # type: ignore[arg-type]

    async def call(self, tool: str, args: dict[str, Any] | None = None, mode: str = "auto") -> tuple[str, bool]:
        """Calls a tool and returns (text, is_error)."""
        async with self.client(mode) as client:
            result = await client.call_tool(tool, args or {})
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        assert len(texts) == 1, f"expected exactly one text block, got {result.content!r}"
        return texts[0], bool(result.is_error)

    async def json(self, tool: str, args: dict[str, Any] | None = None, mode: str = "auto") -> Any:
        text, is_error = await self.call(tool, args, mode)
        assert not is_error, text
        return json.loads(text)

    def http(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        return httpx2.request(method, f"{self.base}{path}", timeout=30, **kwargs)

    def logs(self) -> str:
        return self.log.read_text(errors="replace")


def start_server(
    impl: str, env: dict[str, str], tmp: Path, ready_timeout: float = 60, wait_for: str = "/readyz"
) -> Server:
    cmd = COMMANDS[impl]
    if not (Path(cmd[-1]).exists() or shutil.which(cmd[0])):
        pytest.skip(f"{impl} is not built: {cmd[-1]} missing")
    port = free_port()
    full_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_AUTH_TOKEN": TOKEN,
        "MCP_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "DOCS_NAME": "testhandbok",
        "DOCS_SITE_URL": SITE_URL,
        "DOCS_REFRESH_SECONDS": "0",
        "DOCS_CACHE_DIR": str(tmp / "cache"),
        **env,
    }
    log = tmp / f"{impl}.log"
    process = subprocess.Popen(cmd, env=full_env, stdout=log.open("wb"), stderr=subprocess.STDOUT)
    server = Server(impl=impl, base=f"http://127.0.0.1:{port}", process=process, log=log, extra_env=env)
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{impl} exited with {process.returncode}:\n{server.logs()}")
        try:
            if server.http("GET", wait_for).status_code == 200:
                return server
        except httpx2.TransportError:
            pass
        time.sleep(0.2)
    stop_server(server)
    raise RuntimeError(f"{impl} did not become ready:\n{server.logs()}")


def stop_server(server: Server) -> None:
    server.process.terminate()
    try:
        server.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.process.kill()


@pytest.fixture(scope="module")
def server(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """The implementation serving fixtures/docs from a local path."""
    tmp = tmp_path_factory.mktemp(f"{impl}-local")
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(FIXTURE_DOCS), "DOCS_EXCLUDE": "drafts/**"}, tmp)
    yield s
    stop_server(s)

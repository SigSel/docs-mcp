"""A git host that stops answering: git is killed after DOCS_GIT_TIMEOUT_SECONDS (SPEC.md §3.1, §3.3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import TOKEN, Server, start_server, stop_server
from githost import GitHost, add_page, make_repo

pytestmark = pytest.mark.anyio

TIMEOUT = 2
REFRESH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repos" / "handbook")


@pytest.fixture
def host(repo: Path) -> Iterator[GitHost]:
    host = GitHost(repo.parent)
    yield host
    host.close()


def start(impl: str, tmp: Path, host: GitHost, wait_for: str = "/readyz") -> Server:
    env = {"DOCS_REPO_URL": f"{host.url}/handbook", "DOCS_DIR": "docs", "DOCS_GIT_TIMEOUT_SECONDS": str(TIMEOUT)}
    return start_server(impl, env, tmp, wait_for=wait_for)


async def test_clone_that_hangs_is_killed(impl: str, tmp_path: Path, host: GitHost) -> None:
    host.stall()
    s = start(impl, tmp_path, host, wait_for="/healthz")
    try:
        status = s.wait_for_error()
        assert status["ready"] is False
        assert status["last_error"] == f"git clone timed out after {TIMEOUT} seconds"
        # Every process git started is gone, so nothing holds the connection open.
        assert host.wait_for_hangup()
    finally:
        stop_server(s)


async def test_fetch_that_hangs_keeps_the_index_then_recovers(
    impl: str, tmp_path: Path, host: GitHost, repo: Path
) -> None:
    s = start(impl, tmp_path, host)
    try:
        before = s.http("GET", "/readyz").json()
        host.stall()
        r = s.http("POST", "/refresh", headers=REFRESH)
        assert r.status_code == 500, r.text
        status = r.json()
        assert status["last_error"] == f"git fetch timed out after {TIMEOUT} seconds"
        assert (status["ready"], status["pages"], status["commit"]) == (True, before["pages"], before["commit"])
        assert (await s.json("search_docs", {"query": "clippy"}))["count"] > 0
        assert host.wait_for_hangup()
        # A killed git can leave lock files, so the clone is gone and the next sync starts afresh.
        assert not (tmp_path / "cache" / "repo").exists()

        host.resume()
        commit = add_page(repo, "ny.md", "# Ny side\n\nnyheitsord\n")
        r = s.http("POST", "/refresh", headers=REFRESH)
        assert r.status_code == 200, r.text
        status = r.json()
        assert (status["commit"], status["pages"], status["last_error"]) == (commit, before["pages"] + 1, None)
    finally:
        stop_server(s)

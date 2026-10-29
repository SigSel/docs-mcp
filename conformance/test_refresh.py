"""A burst of /refresh calls costs one fetch, and each call still gets a sync that starts after it (SPEC.md §3.3)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2
import pytest

from conftest import TOKEN, start_server, stop_server
from githost import GitHost, add_page, make_repo

REFRESH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repos" / "handbook")


@pytest.fixture
def host(repo: Path) -> Iterator[GitHost]:
    host = GitHost(repo.parent)
    yield host
    host.close()


def fetches(host: GitHost, since: int) -> int:
    """How many times git asked for the repo's refs since request `since`: once per clone or fetch."""
    return host.requests[since:].count("GET /handbook/info/refs")


def test_burst_shares_one_waiting_sync(impl: str, tmp_path: Path, host: GitHost, repo: Path) -> None:
    s = start_server(impl, {"DOCS_REPO_URL": f"{host.url}/handbook", "DOCS_DIR": "docs"}, tmp_path)

    def refresh() -> httpx2.Response:
        return s.http("POST", "/refresh", headers=REFRESH)

    try:
        with ThreadPoolExecutor(max_workers=11) as pool:
            host.stall()
            seen = len(host.requests)
            running = pool.submit(refresh)  # its fetch hangs, so this sync keeps running
            deadline = time.monotonic() + 10
            while len(host.requests) == seen and time.monotonic() < deadline:
                time.sleep(0.05)
            commit = add_page(repo, "ny.md", "# Ny side\n\nnyheitsord\n")
            burst = [pool.submit(refresh) for _ in range(10)]
            time.sleep(1)  # every call has reached the server and is waiting
            since = len(host.requests)
            host.resume()  # the hanging fetch fails; the one waiting sync runs
            results = [f.result() for f in burst]
            running.result()
        assert [r.status_code for r in results] == [200] * 10
        assert {r.json()["commit"] for r in results} == {commit}
        assert fetches(host, since) == 1
    finally:
        stop_server(s)

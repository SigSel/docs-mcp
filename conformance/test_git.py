"""A git source: clone, follow a ref, pick up new commits on /refresh (SPEC.md §3.1)."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import FIXTURE_DOCS, TOKEN, Server, start_server, stop_server
from githost import git

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def repo(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repo with the fixtures under docs/, a README outside it, and a `stable` branch."""
    path = tmp_path_factory.mktemp(f"{impl}-repo") / "handbook"
    shutil.copytree(FIXTURE_DOCS, path / "docs", ignore=shutil.ignore_patterns("node_modules", ".hidden"))
    (path / "README.md").write_text("# Not a doc\n\nutanforord\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "branch", "stable")
    git(path, "checkout", "-q", "stable")
    (path / "docs" / "stable-only.md").write_text("# Stabil\n\nstabilord\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "stable page")
    git(path, "checkout", "-q", "main")
    return path


@pytest.fixture(scope="module")
def git_server(impl: str, repo: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-git")
    env = {"DOCS_REPO_URL": f"file://{repo}", "DOCS_DIR": "docs", "DOCS_EXCLUDE": "drafts/**"}
    s = start_server(impl, env, tmp)
    yield s
    stop_server(s)


async def test_git_overview(git_server: Server, repo: Path) -> None:
    data = await git_server.json("docs_overview")
    source = data["source"]
    assert source["type"] == "git"
    assert source["repo"] == f"file://{repo}"
    assert source["ref"] is None
    assert source["dir"] == "docs"
    assert source["commit"] == git(repo, "rev-parse", "main")
    assert data["page_count"] == 7


async def test_git_only_docs_dir(git_server: Server) -> None:
    data = await git_server.json("search_docs", {"query": "utanforord"})
    assert data["count"] == 0


async def test_git_refresh_picks_up_commit(git_server: Server, repo: Path) -> None:
    (repo / "docs" / "ny.md").write_text("---\ntitle: Ny side\n---\n\nnyheitsord\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "new page")
    r = git_server.http("POST", "/refresh", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json()["commit"] == git(repo, "rev-parse", "main")
    assert r.json()["pages"] == 8
    data = await git_server.json("search_docs", {"query": "nyheitsord"})
    assert [h["path"] for h in data["results"]] == ["ny.md"]


async def test_git_follows_ref(impl: str, repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-git-ref")
    s = start_server(impl, {"DOCS_REPO_URL": str(repo), "DOCS_REF": "stable", "DOCS_DIR": "docs"}, tmp)
    try:
        data = await s.json("docs_overview")
        assert data["source"]["ref"] == "stable"
        assert data["source"]["commit"] == git(repo, "rev-parse", "stable")
        hits = await s.json("search_docs", {"query": "stabilord"})
        assert [h["path"] for h in hits["results"]] == ["stable-only.md"]
    finally:
        stop_server(s)


async def test_git_bad_repo_alive_but_not_ready(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-git-bad")
    s = start_server(impl, {"DOCS_REPO_URL": str(tmp / "does-not-exist")}, tmp, wait_for="/healthz")
    try:
        status = {}
        for _ in range(50):
            r = s.http("GET", "/readyz")
            assert r.status_code == 503
            status = r.json()
            if status["last_error"]:
                break
            time.sleep(0.1)
        assert status["ready"] is False
        assert status["pages"] == 0
        assert status["last_error"]
        text, is_error = await s.call("search_docs", {"query": "x"})
        assert is_error
        assert text.startswith("Index not ready yet")
    finally:
        stop_server(s)

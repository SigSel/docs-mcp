"""Links are never followed: every page is a regular file inside the docs root (SPEC.md §4.1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import Server, start_server, stop_server
from githost import git

pytestmark = pytest.mark.anyio

SECRET = "hemmelegord"  # only the files outside the repo, and the server's environment, contain it


@pytest.fixture(scope="module")
def outside(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory outside the repo, with a page and a secret in it."""
    path = tmp_path_factory.mktemp(f"{impl}-outside")
    (path / "page.md").write_text(f"# Utanfor\n\n{SECRET}\n")
    (path / "secret").write_text(f"{SECRET}\n")
    return path


def commit(repo: Path) -> Path:
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture(scope="module")
def linked(impl: str, outside: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A repo whose docs link to a file and a directory outside, to a page inside, and to the environment."""
    tmp = tmp_path_factory.mktemp(f"{impl}-links")
    docs = tmp / "handbook" / "docs"
    docs.mkdir(parents=True)
    (docs / "page.md").write_text("# Side\n\nTekst.\n")
    (docs / "same.md").symlink_to("page.md")
    (docs / "secret.md").symlink_to(outside / "secret")
    (docs / "outside").symlink_to(outside)
    (docs / "environ.md").symlink_to("/proc/self/environ")
    env = {"DOCS_REPO_URL": f"file://{commit(docs.parent)}", "DOCS_DIR": "docs", "LEAK_ME": SECRET}
    s = start_server(impl, env, tmp)
    yield s
    stop_server(s)


async def test_links_are_skipped(linked: Server) -> None:
    data = await linked.json("list_docs")
    assert [p["path"] for p in data["pages"]] == ["page.md"]


async def test_nothing_outside_is_searchable(linked: Server) -> None:
    data = await linked.json("search_docs", {"query": SECRET})
    assert data["count"] == 0


@pytest.mark.parametrize("path", ["secret.md", "environ.md", "outside/page.md"])
async def test_links_cannot_be_read(linked: Server, path: str) -> None:
    text, is_error = await linked.call("read_doc", {"path": path})
    assert is_error, text
    assert SECRET not in text


def test_docs_dir_leading_outside_fails(impl: str, outside: Path, tmp_path: Path) -> None:
    repo = tmp_path / "handbook"
    repo.mkdir()
    (repo / "docs").symlink_to(outside)
    s = start_server(impl, {"DOCS_REPO_URL": f"file://{commit(repo)}", "DOCS_DIR": "docs"}, tmp_path, wait_for="/healthz")
    try:
        status = s.wait_for_error()
        assert not status["ready"]
        assert status["last_error"] == "docs directory is outside the source: docs"
    finally:
        stop_server(s)

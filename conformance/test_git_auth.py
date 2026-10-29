"""Git authentication: a token over HTTPS, a key over SSH (SPEC.md §1, §3.1).

The token tests clone from a git host that demands HTTP Basic auth. The SSH tests put a stand-in `ssh` first on PATH:
it records how git called it, then serves the repo from the local disk.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import TOKEN, Server, start_server, stop_server
from githost import GitHost, add_page, basic, git, make_repo

pytestmark = pytest.mark.anyio

GIT_TOKEN = "git-t0ken-for-tests"
REFRESH = {"Authorization": f"Bearer {TOKEN}"}
# Keeps the developer's own git config, such as a credential helper, out of the servers' git.
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

FAKE_SSH = """#!/bin/sh
# Stands in for ssh: writes its arguments to $FAKE_SSH_LOG, one per line, then runs the remote command (the last
# argument) here instead of on a server.
printf '%s\\n' "$@" > "$FAKE_SSH_LOG"
for last; do :; done
exec sh -c "$last"
"""


@pytest.fixture(scope="module")
def repo(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_repo(tmp_path_factory.mktemp(f"{impl}-auth") / "repos" / "handbook")


def start(impl: str, tmp: Path, env: dict[str, str], wait_for: str = "/readyz") -> Server:
    return start_server(impl, {**HERMETIC, "DOCS_DIR": "docs", **env}, tmp, wait_for=wait_for)


# --- A token over HTTPS -------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def token_host(repo: Path) -> Iterator[GitHost]:
    host = GitHost(repo.parent, credentials=("git", GIT_TOKEN))
    yield host
    host.close()


@pytest.fixture(scope="module")
def token_server(impl: str, token_host: GitHost, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-token")
    s = start(impl, tmp, {"DOCS_REPO_URL": f"{token_host.url}/handbook", "DOCS_GIT_TOKEN": GIT_TOKEN})
    yield s
    stop_server(s)


async def test_token_clones(token_server: Server, token_host: GitHost, repo: Path) -> None:
    data = await token_server.json("docs_overview")
    assert data["source"]["repo"] == f"{token_host.url}/handbook"
    assert data["source"]["commit"] == git(repo, "rev-parse", "HEAD")
    assert set(token_host.auth_seen) == {basic("git", GIT_TOKEN)}


async def test_token_pulls(token_server: Server, token_host: GitHost, repo: Path) -> None:
    commit = add_page(repo, "ny.md", "# Ny side\n\nnyheitsord\n")
    r = token_server.http("POST", "/refresh", headers=REFRESH)
    assert r.status_code == 200, r.text
    assert r.json()["commit"] == commit
    assert set(token_host.auth_seen) == {basic("git", GIT_TOKEN)}


def test_token_not_on_disk_or_in_the_log(token_server: Server) -> None:
    secrets = [GIT_TOKEN.encode(), base64.b64encode(f"git:{GIT_TOKEN}".encode())]
    files = [p for p in (token_server.log.parent / "cache").rglob("*") if p.is_file()]
    assert any(p.name == "config" for p in files)
    for path in files:
        content = path.read_bytes()
        assert not any(s in content for s in secrets), f"token found in {path}"
    assert GIT_TOKEN not in token_server.logs()


async def test_token_username(impl: str, repo: Path, tmp_path: Path) -> None:
    host = GitHost(repo.parent, credentials=("x-access-token", GIT_TOKEN))
    env = {"DOCS_REPO_URL": f"{host.url}/handbook", "DOCS_GIT_TOKEN": GIT_TOKEN, "DOCS_GIT_USERNAME": "x-access-token"}
    s = start(impl, tmp_path, env)
    try:
        assert set(host.auth_seen) == {basic("x-access-token", GIT_TOKEN)}
    finally:
        stop_server(s)
        host.close()


async def test_token_file_wins(impl: str, repo: Path, tmp_path: Path) -> None:
    host = GitHost(repo.parent, credentials=("git", GIT_TOKEN))
    token_file = tmp_path / "git-token"
    token_file.write_text(f"{GIT_TOKEN}\n")
    env = {
        "DOCS_REPO_URL": f"{host.url}/handbook",
        "DOCS_GIT_TOKEN": "not-the-token",
        "DOCS_GIT_TOKEN_FILE": str(token_file),
    }
    s = start(impl, tmp_path, env)
    try:
        assert set(host.auth_seen) == {basic("git", GIT_TOKEN)}
    finally:
        stop_server(s)
        host.close()


@pytest.mark.parametrize("token", [None, "not-the-token"], ids=["no-token", "wrong-token"])
async def test_token_refused(impl: str, repo: Path, tmp_path: Path, token: str | None) -> None:
    host = GitHost(repo.parent, credentials=("git", GIT_TOKEN))
    env = {"DOCS_REPO_URL": f"{host.url}/handbook", **({"DOCS_GIT_TOKEN": token} if token else {})}
    s = start(impl, tmp_path, env, wait_for="/healthz")
    try:
        status = s.wait_for_error()
        assert status["ready"] is False
        assert status["last_error"].startswith("git clone failed: ")
        if token:
            assert token not in status["last_error"]
            assert token not in s.logs()
    finally:
        stop_server(s)
        host.close()


async def test_redirect_refused(impl: str, repo: Path, tmp_path: Path) -> None:
    """A redirect is not followed: git would send the token to wherever it points."""
    elsewhere = GitHost(repo.parent)  # asks for no token, so git carries on and sends it unasked
    host = GitHost(repo.parent)
    host.redirect_to = elsewhere.url
    env = {"DOCS_REPO_URL": f"{host.url}/handbook", "DOCS_GIT_TOKEN": GIT_TOKEN}
    s = start(impl, tmp_path, env, wait_for="/healthz")
    try:
        status = s.wait_for_error()
        assert elsewhere.auth_seen == []
        assert status["ready"] is False
        assert status["last_error"].startswith("git clone failed: ")
        assert "302" in status["last_error"]
    finally:
        stop_server(s)
        host.close()
        elsewhere.close()


# --- A key over SSH -----------------------------------------------------------------------------------------------


@pytest.fixture
def fake_ssh(tmp_path: Path) -> dict[str, str]:
    """Env that puts the stand-in ssh first on PATH, and a key whose path needs quoting in a shell."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    key = tmp_path / "it's a key" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("not a real key\n")
    key.chmod(0o600)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_SSH_LOG": str(tmp_path / "ssh-args"),
        "DOCS_GIT_SSH_KEY_FILE": str(key),
    }


def ssh_args(fake_ssh: dict[str, str]) -> list[str]:
    return Path(fake_ssh["FAKE_SSH_LOG"]).read_text().splitlines()


async def test_ssh_key_with_known_hosts(impl: str, repo: Path, tmp_path: Path, fake_ssh: dict[str, str]) -> None:
    known_hosts = tmp_path / "known hosts"
    known_hosts.write_text("")
    env = {
        **fake_ssh,
        "DOCS_REPO_URL": f"ssh://git@git.example.invalid{repo}",
        "DOCS_GIT_KNOWN_HOSTS_FILE": str(known_hosts),
    }
    expected = [
        "-i", fake_ssh["DOCS_GIT_SSH_KEY_FILE"], "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
    ]  # fmt: skip
    s = start(impl, tmp_path, env)
    try:
        data = await s.json("docs_overview")
        assert data["source"]["commit"] == git(repo, "rev-parse", "HEAD")
        assert ssh_args(fake_ssh)[: len(expected)] == expected
        assert "git@git.example.invalid" in ssh_args(fake_ssh)

        Path(fake_ssh["FAKE_SSH_LOG"]).unlink()
        commit = add_page(repo, "ssh.md", "# Over SSH\n\nskalord\n")
        r = s.http("POST", "/refresh", headers=REFRESH)
        assert r.status_code == 200, r.text
        assert r.json()["commit"] == commit
        assert ssh_args(fake_ssh)[: len(expected)] == expected
    finally:
        stop_server(s)


async def test_ssh_key_without_known_hosts(impl: str, repo: Path, tmp_path: Path, fake_ssh: dict[str, str]) -> None:
    env = {**fake_ssh, "DOCS_REPO_URL": f"git@git.example.invalid:{repo}"}
    expected = [
        "-i", fake_ssh["DOCS_GIT_SSH_KEY_FILE"], "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
    ]  # fmt: skip
    s = start(impl, tmp_path, env)
    try:
        data = await s.json("docs_overview")
        assert data["source"]["commit"] == git(repo, "rev-parse", "HEAD")
        assert ssh_args(fake_ssh)[: len(expected)] == expected
    finally:
        stop_server(s)

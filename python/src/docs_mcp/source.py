"""Keeping the index in step with the source: git clone/pull or a local directory (SPEC.md §3)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shlex
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .index import DocIndex

log = logging.getLogger("docs_mcp")
RETRY_SECONDS = 10


def redact(url: str) -> str:
    """Drops user:password@ from a URL for display."""
    parts = urlsplit(url)
    if parts.scheme and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return url


def git_env(config: Config) -> dict[str, str]:
    # git sends the token header after a redirect too, to wherever it points.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false",
    }
    url = config.repo_url or ""
    parts = urlsplit(url)
    if config.git_token and parts.scheme in ("http", "https"):
        origin = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}/"
        basic = base64.b64encode(f"{config.git_username}:{config.git_token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_1": f"http.{origin}.extraHeader",
            "GIT_CONFIG_VALUE_1": f"Authorization: Basic {basic}",
        }
    if config.git_ssh_key_file:
        ssh = f"ssh -i {shlex.quote(config.git_ssh_key_file)} -o IdentitiesOnly=yes"
        if config.git_known_hosts_file:
            ssh += f" -o UserKnownHostsFile={shlex.quote(config.git_known_hosts_file)} -o StrictHostKeyChecking=yes"
        else:
            ssh += " -o StrictHostKeyChecking=accept-new"
        env["GIT_SSH_COMMAND"] = ssh
    return env


class Syncer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.index: DocIndex | None = None
        self.commit: str | None = None
        self.synced_at: str | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._repo = config.cache_dir / "repo"
        self._env = git_env(config)
        if config.repo_url:
            shutil.rmtree(config.cache_dir, ignore_errors=True)

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.index is not None,
            "pages": len(self.index.pages) if self.index else 0,
            "commit": self.commit,
            "synced_at": self.synced_at,
            "last_error": self.last_error,
        }

    def source_info(self) -> dict[str, Any]:
        c = self.config
        if c.repo_url:
            return {
                "type": "git",
                "repo": redact(c.repo_url),
                "ref": c.ref,
                "dir": c.docs_dir,
                "commit": self.commit,
                "synced_at": self.synced_at,
            }
        return {
            "type": "local",
            "path": str(c.local_path),
            "dir": c.docs_dir,
            "commit": None,
            "synced_at": self.synced_at,
        }

    async def sync(self) -> bool:
        async with self._lock:
            try:
                if self.config.repo_url:
                    commit = await self._pull()
                    changed = commit != self.commit or self.index is None
                    root = self._repo / self.config.docs_dir
                else:
                    commit, changed = None, True
                    root = (self.config.local_path or Path()) / self.config.docs_dir
                if changed:
                    c = self.config
                    index = await asyncio.to_thread(
                        DocIndex.build, root, c.include, c.exclude, c.ignore_fields, c.name, c.site_url
                    )
                    self.index, self.commit = index, commit
                    log.info("indexed %d pages%s", len(index.pages), f" at {commit[:12]}" if commit else "")
                self.synced_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = str(e) or type(e).__name__
                log.error("sync failed: %s", self.last_error)
                return False

    async def run_forever(self) -> None:
        while not await self.sync():
            await asyncio.sleep(RETRY_SECONDS)
        if self.config.refresh_seconds == 0:
            return
        while True:
            await asyncio.sleep(self.config.refresh_seconds)
            await self.sync()

    async def _git(self, *args: str, cwd: Path | None = None) -> str:
        # In its own process group, so a timeout also kills the helpers git starts (git-remote-https, ssh).
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=self._env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            process_group=0,
        )
        timeout = self.config.git_timeout_seconds
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout or None)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            # A killed git can leave lock files behind; the next sync clones afresh.
            shutil.rmtree(self._repo, ignore_errors=True)
            raise RuntimeError(f"git {args[0]} timed out after {timeout} seconds") from None
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
        return out.decode().strip()

    async def _pull(self) -> str:
        c = self.config
        assert c.repo_url
        if not (self._repo / ".git").exists():
            shutil.rmtree(self._repo, ignore_errors=True)
            self._repo.parent.mkdir(parents=True, exist_ok=True)
            branch = ["--branch", c.ref] if c.ref else []
            await self._git(
                "clone", "--quiet", "--depth", "1", "--single-branch", *branch, "--", c.repo_url, str(self._repo)
            )
        else:
            await self._git("fetch", "--quiet", "--depth", "1", "origin", c.ref or "HEAD", cwd=self._repo)
            await self._git("reset", "--quiet", "--hard", "FETCH_HEAD", cwd=self._repo)
        return await self._git("rev-parse", "HEAD", cwd=self._repo)

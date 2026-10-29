"""Keeping the index in step with the sources: git clone/pull or a local directory (SPEC.md §3, §7.2)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shlex
import shutil
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Config, Source
from .content import Page
from .index import DocIndex, load_pages

log = logging.getLogger("docs_mcp")
RETRY_SECONDS = 10


def redact(url: str) -> str:
    """Drops user:password@ from a URL for display."""
    parts = urlsplit(url)
    if parts.scheme and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return url


def docs_root(base: Path, docs_dir: str) -> Path:
    """`base/docs_dir`, which may be reached through a link but must stay inside `base` (SPEC.md §4.1)."""
    root = base / docs_dir
    if root.exists() and not root.resolve().is_relative_to(base.resolve()):
        raise RuntimeError(f"docs directory is outside the source: {docs_dir}")
    return root


def git_env(config: Config, repo_url: str | None) -> dict[str, str]:
    # git sends the token header after a redirect too, to wherever it points.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false",
    }
    parts = urlsplit(repo_url or "")
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
    """Keeps one source's pages in step with its repo or folder."""

    def __init__(self, config: Config, source: Source, repo: Path, changed: Callable[[], Awaitable[None]]) -> None:
        self.config = config
        self.source = source
        self.pages: list[Page] | None = None
        self.loaded = False  # the index holds this source's pages
        self.commit: str | None = None
        self.synced_at: str | None = None
        self.last_error: str | None = None
        self._changed = changed
        self._lock = asyncio.Lock()
        self._repo = repo
        self._env = git_env(config, source.repo_url)

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.loaded,
            "pages": len(self.pages) if self.pages is not None else 0,
            "commit": self.commit,
            "synced_at": self.synced_at,
            "last_error": self.last_error,
        }

    def source_info(self) -> dict[str, Any]:
        s = self.source
        if s.repo_url:
            return {
                "type": "git",
                "repo": redact(s.repo_url),
                "ref": s.ref,
                "dir": s.docs_dir,
                "commit": self.commit,
                "synced_at": self.synced_at,
            }
        return {
            "type": "local",
            "path": str(s.local_path),
            "dir": s.docs_dir,
            "commit": None,
            "synced_at": self.synced_at,
        }

    async def sync(self) -> bool:
        async with self._lock:
            try:
                s = self.source
                if s.repo_url:
                    commit = await self._pull()
                    changed = commit != self.commit or self.pages is None
                    base = self._repo
                else:
                    commit, changed = None, True
                    base = s.local_path or Path()
                if changed:
                    root = docs_root(base, s.docs_dir)
                    pages = await asyncio.to_thread(
                        load_pages, root, s.include, s.exclude, s.ignore_fields, self.config.name, s.name
                    )
                    self.pages, self.commit = pages, commit
                    await self._changed()
                    self.loaded = True
                    where = f" from {s.name}" if self.config.multi else ""
                    log.info("indexed %d pages%s%s", len(pages), where, f" at {commit[:12]}" if commit else "")
                self.synced_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = str(e) or type(e).__name__
                where = f" for {self.source.name}" if self.config.multi else ""
                log.error("sync failed%s: %s", where, self.last_error)
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
        c = self.source
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


class Library:
    """Every source, and one search index over all that have loaded (SPEC.md §7.2)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.index: DocIndex | None = None
        self._rebuild_lock = asyncio.Lock()
        if any(s.repo_url for s in config.sources):
            shutil.rmtree(config.cache_dir, ignore_errors=True)
        self.syncers = [
            Syncer(config, s, config.cache_dir / s.name / "repo" if config.multi else config.cache_dir / "repo",
                   self._rebuild)
            for s in config.sources
        ]  # fmt: skip
        self.by_name = {s.source.name: s for s in self.syncers}

    async def _rebuild(self) -> None:
        """Builds the index over every source that has pages, off to the side, and swaps it in."""
        async with self._rebuild_lock:
            pages = [p for s in self.syncers if s.pages is not None for p in s.pages]
            names = [s.source.name for s in self.syncers]
            sites = {s.source.name: s.source.site_url for s in self.syncers}
            self.index = await asyncio.to_thread(DocIndex, pages, sites, names, self.config.multi)

    def status(self) -> dict[str, Any]:
        if not self.config.multi:
            return self.syncers[0].status()
        sources = [{"name": s.source.name, **s.status()} for s in self.syncers]
        return {
            "ready": all(s["ready"] for s in sources),
            "pages": sum(s["pages"] for s in sources),
            "sources": sources,
        }

    def not_ready(self) -> str:
        """The tool error for when no source has loaded yet (SPEC.md §3.3, §7.2)."""
        if not self.config.multi:
            error = self.syncers[0].last_error
            return f"Index not ready yet.{f' Last error: {error}' if error else ''}"
        errors = "".join(f" Last error in {s.source.name}: {s.last_error}" for s in self.syncers if s.last_error)
        return f"Index not ready yet.{errors}"

    def check_source(self, name: str) -> str | None:
        """The tool error for a source that is not configured or has not loaded, if any (SPEC.md §7.3)."""
        syncer = self.by_name.get(name)
        if syncer is None:
            return f"No source '{name}'. Sources: {', '.join(self.by_name)}."
        if not syncer.loaded:
            error = f" Last error: {syncer.last_error}" if syncer.last_error else ""
            return f"Source '{name}' is not ready yet.{error}"
        return None

    async def sync(self, name: str | None = None) -> bool:
        syncers = [self.by_name[name]] if name is not None else self.syncers
        return all(await asyncio.gather(*(s.sync() for s in syncers)))

    async def run_forever(self) -> None:
        await asyncio.gather(*(s.run_forever() for s in self.syncers))

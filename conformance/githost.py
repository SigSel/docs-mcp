"""A git host for the tests: serves repos over smart HTTP with `git http-backend`, as Forgejo or Azure DevOps would.

It can demand a token (HTTP Basic, as every real host accepts), record the Authorization header of every request,
redirect to another host, and stall: accept requests and never answer, like a host that has gone quiet.
"""

from __future__ import annotations

import base64
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conftest import FIXTURE_DOCS


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={**os.environ, **env}
    ).stdout.strip()


def make_repo(path: Path) -> Path:
    """A repo at `path` with the fixtures under docs/ on `main`."""
    shutil.copytree(FIXTURE_DOCS, path / "docs", ignore=shutil.ignore_patterns("node_modules", ".hidden"))
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def add_page(repo: Path, name: str, text: str) -> str:
    """Commits a new page under docs/ and returns the new commit."""
    (repo / "docs" / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


def basic(username: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()


class GitHost:
    """Serves every repo under `root` at http://127.0.0.1:<port>/<repo name>."""

    def __init__(self, root: Path, credentials: tuple[str, str] | None = None) -> None:
        self.root = root
        self.expected_auth = basic(*credentials) if credentials else None
        self.redirect_to: str | None = None
        self.auth_seen: list[str | None] = []  # the Authorization header of every request, in order
        self.hangups = 0  # stalled requests whose client went away
        self._gate: threading.Event | None = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.host = self  # type: ignore[attr-defined]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stall(self) -> None:
        """From now on, accept requests and never answer them."""
        self._gate = threading.Event()

    def resume(self) -> None:
        if self._gate is not None:
            self._gate.set()
            self._gate = None

    def wait_for_hangup(self, timeout: float = 5) -> bool:
        """True once a stalled request's client has closed the connection: nothing on its side holds it open."""
        deadline = time.monotonic() + timeout
        while self.hangups == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.hangups > 0

    def close(self) -> None:
        self.resume()
        self._httpd.shutdown()
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _handle(self) -> None:
        host: GitHost = self.server.host  # type: ignore[attr-defined]
        given = self.headers.get("Authorization")
        host.auth_seen.append(given)
        if (gate := host._gate) is not None:
            self._stall(host, gate)
            return
        if host.redirect_to:
            self.send_response(302)
            self.send_header("Location", host.redirect_to + self.path)
            self.end_headers()
            return
        if host.expected_auth and given != host.expected_auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.end_headers()
            return
        self._backend(host.root)

    def _stall(self, host: GitHost, gate: threading.Event) -> None:
        """Answers nothing until resumed, and counts a hangup when the client closes the connection first."""
        while not gate.wait(0.05):
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable and not self.connection.recv(1, socket.MSG_PEEK):
                host.hangups += 1
                return
        self.close_connection = True

    def _backend(self, root: Path) -> None:
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {
            "PATH": os.environ["PATH"],
            "GIT_PROJECT_ROOT": str(root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "REMOTE_ADDR": "127.0.0.1",
        }
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, check=True).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(": ")
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

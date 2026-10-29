"""Configuration from environment variables (SPEC.md §1)."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content import load_yaml

DEFAULT_IGNORE_FIELDS = (
    "layout,template,hero,sidebar,sidebar_position,sidebar_label,sidebar_class_name,slug,id,pagefind,editUrl,"
    "lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,outline,aside,navbar,footer,"
    "pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,toc_min_heading_level,toc_max_heading_level"
)


DEFAULT_INCLUDE = "**/*.md,**/*.mdx"
# The variables DOCS_SOURCES replaces (SPEC.md §7.1).
PER_SOURCE = (
    "DOCS_REPO_URL",
    "DOCS_LOCAL_PATH",
    "DOCS_REF",
    "DOCS_DIR",
    "DOCS_INCLUDE",
    "DOCS_EXCLUDE",
    "DOCS_IGNORE_FIELDS",
    "DOCS_LANGUAGE",
    "DOCS_SITE_URL",
)
SOURCE_KEYS = ("name", "description", "repo", "path", "ref", "dir", "include", "exclude", "ignore_fields",
               "language", "site_url")  # fmt: skip
SOURCE_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    """One repo or folder to serve (SPEC.md §7.1)."""

    name: str
    description: str | None  # None for the single source of the DOCS_* variables
    repo_url: str | None
    local_path: Path | None
    ref: str | None
    docs_dir: str
    include: list[str]
    exclude: list[str]
    ignore_fields: frozenset[str]
    language: str | None
    site_url: str | None


@dataclass(frozen=True)
class Config:
    sources: list[Source]
    multi: bool  # DOCS_SOURCES is set: outputs name their source (SPEC.md §7)
    refresh_seconds: int
    git_timeout_seconds: int  # 0 means no limit
    name: str
    git_username: str
    git_token: str | None
    git_ssh_key_file: str | None
    git_known_hosts_file: str | None
    cache_dir: Path
    host: str
    port: int
    auth_token: str | None
    oauth_issuer: str | None
    oauth_audience: str | None
    oauth_scopes: list[str]
    public_url: str | None
    refresh_token: str | None
    allowed_hosts: list[str] | None  # None means any host
    allowed_hosts_default: bool  # MCP_ALLOWED_HOSTS was not set


def _get(name: str, env: dict[str, str]) -> str | None:
    """Reads NAME, or the file named by NAME_FILE (which wins), trimmed. Empty counts as unset."""
    file = env.get(f"{name}_FILE")
    if file:
        try:
            value = Path(file).read_text().strip()
        except OSError as e:
            raise ConfigError(f"{name}_FILE: cannot read {file}: {e}") from e
    else:
        value = env.get(name, "").strip()
    return value or None


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _int(name: str, env: dict[str, str], default: int) -> int:
    raw = _get(name, env)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}")
    return value


LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def split_url(url: str) -> tuple[str, str, str]:
    """(scheme, authority, rest) of an absolute URL, as written."""
    scheme, sep, after = url.partition("://")
    if not sep:
        return "", "", url
    cut = min((i for i in (after.find(c) for c in "/?#") if i >= 0), default=len(after))
    return scheme, after[:cut], after[cut:]


def https_or_local(url: str) -> bool:
    """`https://` with a host, or `http://` to this machine (SPEC.md §1)."""
    scheme, authority, _ = split_url(url)
    hostport = authority.rsplit("@", 1)[-1]
    host = hostport[: hostport.find("]") + 1] if hostport.startswith("[") else hostport.split(":", 1)[0]
    if not host:
        return False
    return scheme.lower() == "https" or (scheme.lower() == "http" and host.lower() in LOCAL_HOSTS)


def _auth(env: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """MCP_AUTH_TOKEN, MCP_OAUTH_ISSUER and MCP_PUBLIC_URL, checked against each other (SPEC.md §1)."""
    token, issuer, public = (_get(n, env) for n in ("MCP_AUTH_TOKEN", "MCP_OAUTH_ISSUER", "MCP_PUBLIC_URL"))
    if token and issuer:
        raise ConfigError(
            "set at most one of MCP_AUTH_TOKEN and MCP_OAUTH_ISSUER: a shared token would let anyone who has it "
            "past the login"
        )
    if issuer and not public:
        raise ConfigError("MCP_OAUTH_ISSUER needs MCP_PUBLIC_URL, the /mcp URL clients use")
    for name, url in (("MCP_OAUTH_ISSUER", issuer), ("MCP_PUBLIC_URL", public)):
        if url and not https_or_local(url):
            raise ConfigError(f"{name} must be an https:// URL with a host (http:// only for localhost), got {url!r}")
    if public and ("?" in public or "#" in public):
        raise ConfigError(f"MCP_PUBLIC_URL must not have a query or a fragment, got {public!r}")
    return token, issuer, public


def _single_source(name: str, env: dict[str, str]) -> Source:
    """The one source the DOCS_* variables describe."""
    repo_url = _get("DOCS_REPO_URL", env)
    local_path = _get("DOCS_LOCAL_PATH", env)
    if bool(repo_url) == bool(local_path):
        raise ConfigError("set exactly one of DOCS_REPO_URL and DOCS_LOCAL_PATH")
    return Source(
        name=name,
        description=None,
        repo_url=repo_url,
        local_path=Path(local_path) if local_path else None,
        ref=_get("DOCS_REF", env),
        docs_dir=_get("DOCS_DIR", env) or ".",
        include=_list(_get("DOCS_INCLUDE", env) or DEFAULT_INCLUDE),
        exclude=_list(_get("DOCS_EXCLUDE", env)),
        ignore_fields=frozenset(_list(_get("DOCS_IGNORE_FIELDS", env) or DEFAULT_IGNORE_FIELDS)),
        language=_get("DOCS_LANGUAGE", env),
        site_url=_get("DOCS_SITE_URL", env),
    )


def _sources(raw: str, env: dict[str, str]) -> list[Source]:
    """DOCS_SOURCES, checked (SPEC.md §7.1)."""
    if clash := [n for n in PER_SOURCE if _get(n, env)]:
        raise ConfigError(f"DOCS_SOURCES replaces {', '.join(clash)}: set one or the other")
    try:
        items = load_yaml(raw)
    except Exception as e:  # any YAML error, or nesting the loader cannot take
        raise ConfigError(f"DOCS_SOURCES is not valid YAML: {e}") from None
    if not isinstance(items, list) or not items:
        raise ConfigError("DOCS_SOURCES must be a non-empty YAML list of sources")
    sources: list[Source] = []
    for number, item in enumerate(items, 1):
        sources.append(_source(item, number, {s.name for s in sources}))
    return sources


def _source(item: Any, number: int, taken: set[str]) -> Source:
    label = f"#{number}"
    if not isinstance(item, dict):
        raise ConfigError(f"DOCS_SOURCES: source {label} is not a mapping")
    name = item.get("name")
    if isinstance(name, str) and SOURCE_NAME.fullmatch(name):
        label = f"'{name}'"

    def fail(message: str) -> ConfigError:
        return ConfigError(f"DOCS_SOURCES: source {label}: {message}")

    for key in item:
        if key not in SOURCE_KEYS:
            raise fail(f"unknown key '{key}'")
    if not isinstance(name, str) or not SOURCE_NAME.fullmatch(name):
        raise fail("'name' must be lowercase letters and digits, words joined by single '-'")
    if name in taken:
        raise fail("the name is used twice")

    def text(key: str) -> str | None:
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            raise fail(f"'{key}' must be a string")
        return value or None

    def strings(key: str, default: str) -> list[str]:
        value = item.get(key)
        if value is None:
            return _list(default)
        if isinstance(value, str):
            return _list(value)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v.strip() for v in value if v.strip()]
        raise fail(f"'{key}' must be a string or a list of strings")

    description = text("description")
    if description is None:
        raise fail("'description' is missing: say what the source is, and when to use it")
    repo_url, local_path = text("repo"), text("path")
    if bool(repo_url) == bool(local_path):
        raise fail("set exactly one of 'repo' and 'path'")
    return Source(
        name=name,
        description=description,
        repo_url=repo_url,
        local_path=Path(local_path) if local_path else None,
        ref=text("ref"),
        docs_dir=text("dir") or ".",
        include=strings("include", DEFAULT_INCLUDE),
        exclude=strings("exclude", ""),
        ignore_fields=frozenset(strings("ignore_fields", DEFAULT_IGNORE_FIELDS)),
        language=text("language"),
        site_url=text("site_url"),
    )


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    name = _get("DOCS_NAME", env) or "docs"
    raw_sources = _get("DOCS_SOURCES", env)
    sources = _sources(raw_sources, env) if raw_sources else [_single_source(name, env)]
    auth_token, oauth_issuer, public_url = _auth(env)
    allowed_raw = _get("MCP_ALLOWED_HOSTS", env)
    allowed = _list(allowed_raw or "localhost,127.0.0.1,::1")
    return Config(
        sources=sources,
        multi=raw_sources is not None,
        refresh_seconds=_int("DOCS_REFRESH_SECONDS", env, 300),
        git_timeout_seconds=_int("DOCS_GIT_TIMEOUT_SECONDS", env, 120),
        name=name,
        git_username=_get("DOCS_GIT_USERNAME", env) or "git",
        git_token=_get("DOCS_GIT_TOKEN", env),
        git_ssh_key_file=_get("DOCS_GIT_SSH_KEY_FILE", env),
        git_known_hosts_file=_get("DOCS_GIT_KNOWN_HOSTS_FILE", env),
        cache_dir=Path(_get("DOCS_CACHE_DIR", env) or Path(tempfile.gettempdir()) / "docs-mcp"),
        host=_get("MCP_HOST", env) or "0.0.0.0",
        port=_int("MCP_PORT", env, 8080),
        auth_token=auth_token,
        oauth_issuer=oauth_issuer,
        oauth_audience=_get("MCP_OAUTH_AUDIENCE", env) or public_url,
        oauth_scopes=_list(_get("MCP_OAUTH_SCOPES", env)),
        public_url=public_url,
        refresh_token=_get("MCP_REFRESH_TOKEN", env),
        allowed_hosts=None if "*" in allowed else [h.lower().strip("[]") for h in allowed],
        allowed_hosts_default=allowed_raw is None,
    )


def load_or_exit() -> Config:
    try:
        return load()
    except ConfigError as e:
        print(f"docs-mcp: configuration error: {e}", file=sys.stderr)
        sys.exit(2)

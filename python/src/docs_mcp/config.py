"""Configuration from environment variables (SPEC.md §1)."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IGNORE_FIELDS = (
    "layout,template,hero,sidebar,sidebar_position,sidebar_label,sidebar_class_name,slug,id,pagefind,editUrl,"
    "lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,outline,aside,navbar,footer,"
    "pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,toc_min_heading_level,toc_max_heading_level"
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    repo_url: str | None
    local_path: Path | None
    ref: str | None
    docs_dir: str
    include: list[str]
    exclude: list[str]
    ignore_fields: frozenset[str]
    refresh_seconds: int
    name: str
    site_url: str | None
    git_username: str
    git_token: str | None
    git_ssh_key_file: str | None
    git_known_hosts_file: str | None
    cache_dir: Path
    host: str
    port: int
    auth_token: str | None
    allowed_hosts: list[str] | None  # None means any host


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


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    repo_url = _get("DOCS_REPO_URL", env)
    local_path = _get("DOCS_LOCAL_PATH", env)
    if bool(repo_url) == bool(local_path):
        raise ConfigError("set exactly one of DOCS_REPO_URL and DOCS_LOCAL_PATH")
    allowed = _list(_get("MCP_ALLOWED_HOSTS", env) or "localhost,127.0.0.1,::1")
    return Config(
        repo_url=repo_url,
        local_path=Path(local_path) if local_path else None,
        ref=_get("DOCS_REF", env),
        docs_dir=_get("DOCS_DIR", env) or ".",
        include=_list(_get("DOCS_INCLUDE", env) or "**/*.md,**/*.mdx"),
        exclude=_list(_get("DOCS_EXCLUDE", env)),
        ignore_fields=frozenset(_list(_get("DOCS_IGNORE_FIELDS", env) or DEFAULT_IGNORE_FIELDS)),
        refresh_seconds=_int("DOCS_REFRESH_SECONDS", env, 300),
        name=_get("DOCS_NAME", env) or "docs",
        site_url=_get("DOCS_SITE_URL", env),
        git_username=_get("DOCS_GIT_USERNAME", env) or "git",
        git_token=_get("DOCS_GIT_TOKEN", env),
        git_ssh_key_file=_get("DOCS_GIT_SSH_KEY_FILE", env),
        git_known_hosts_file=_get("DOCS_GIT_KNOWN_HOSTS_FILE", env),
        cache_dir=Path(_get("DOCS_CACHE_DIR", env) or Path(tempfile.gettempdir()) / "docs-mcp"),
        host=_get("MCP_HOST", env) or "0.0.0.0",
        port=_int("MCP_PORT", env, 8080),
        auth_token=_get("MCP_AUTH_TOKEN", env),
        allowed_hosts=None if "*" in allowed else [h.lower().strip("[]") for h in allowed],
    )


def load_or_exit() -> Config:
    try:
        return load()
    except ConfigError as e:
        print(f"docs-mcp: configuration error: {e}", file=sys.stderr)
        sys.exit(2)

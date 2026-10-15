"""Entry point: `docs-mcp` or `python -m docs_mcp`."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_or_exit
from .server import build_app
from .source import redact


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_or_exit()
    app, _ = build_app(config)
    source = redact(config.repo_url) if config.repo_url else str(config.local_path)
    logging.getLogger("docs_mcp").info(
        "serving %s from %s (dir %s) on %s:%d, allowed hosts: %s, auth: %s",
        config.name,
        source,
        config.docs_dir,
        config.host,
        config.port,
        ",".join(config.allowed_hosts) if config.allowed_hosts is not None else "*",
        "bearer token" if config.auth_token else "none",
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()

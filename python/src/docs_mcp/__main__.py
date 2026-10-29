"""Entry point: `docs-mcp` or `python -m docs_mcp`."""

from __future__ import annotations

import logging

import uvicorn

from .config import load_or_exit
from .server import build_app, startup_warnings
from .source import redact


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_or_exit()
    app, _ = build_app(config)
    if config.multi:
        served = f"{len(config.sources)} sources ({', '.join(s.name for s in config.sources)})"
    else:
        only = config.sources[0]
        served = f"{redact(only.repo_url) if only.repo_url else only.local_path} (dir {only.docs_dir})"
    logging.getLogger("docs_mcp").info(
        "serving %s from %s on %s:%d, allowed hosts: %s, auth: %s",
        config.name,
        served,
        config.host,
        config.port,
        ",".join(config.allowed_hosts) if config.allowed_hosts is not None else "*",
        f"oauth ({config.oauth_issuer})" if config.oauth_issuer else "bearer token" if config.auth_token else "none",
    )
    for warning in startup_warnings(config):
        logging.getLogger("docs_mcp").warning(warning)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()

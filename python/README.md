# docs-mcp (Python)

The Python implementation of [docs-mcp](../README.md), built on the official MCP SDK (`mcp` 2.x, `MCPServer`) with
Starlette and uvicorn. Behaviour is defined by [SPEC.md](../SPEC.md).

```sh
uv sync
DOCS_LOCAL_PATH=../fixtures/docs uv run docs-mcp
uv run ruff check src && uv run ruff format --check src
```

| Module | Does |
|---|---|
| `config.py` | Environment variables (SPEC §1) |
| `content.py` | Frontmatter, cleaning, sections and anchors (§4) |
| `index.py` | Discovery, BM25 search, filters, tool outputs (§4–6) |
| `source.py` | git clone/pull or local re-read, on a schedule (§3) |
| `server.py` | MCP tools, `/healthz`, `/readyz`, `/refresh`, host check and auth (§2, §6) |

This is the reference implementation: `just golden` regenerates the expected outputs from it.

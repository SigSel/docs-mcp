"""Golden outputs: what every implementation must return byte-for-byte on fixtures/docs.

`uv run python golden.py` regenerates golden.json from the Python implementation. Review the diff
by hand before committing it: the golden file is the spec made concrete, not whatever Python does.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from conftest import FIXTURE_DOCS, Server, start_server, stop_server

GOLDEN = Path(__file__).with_name("golden.json")
PAGES = [
    "index.mdx",
    "python/index.md",
    "python/notes.md",
    "python/tools/uv.md",
    "rust/rules.mdx",
    "rust/testing.md",
    "workflow/containers.md",
]
QUERIES = ["clippy", "RS-203", "unwrap", "testar", "rust", "uv pyproject", "eksempel", "sjiraff", "distroless root",
           "MÅ IKKJE", "køyr cargo", "handbok"]
ANCHORS = ["rs-201", "RS-203", "samandrag", "eksempel", "eksempel-1", "fixtures", "oversikt"]


async def collect(server: Server) -> dict[str, Any]:
    overview = await server.json("docs_overview")
    overview["source"].pop("synced_at")
    overview["source"].pop("path", None)
    async with server.client() as client:
        tools = {t.name: {"description": t.description,
                          "params": {k: v.get("description") for k, v in t.input_schema.get("properties", {}).items()},
                          "required": sorted(t.input_schema.get("required", []))}
                 for t in (await client.list_tools()).tools}
    return {
        "tools": dict(sorted(tools.items())),
        "overview": overview,
        "list_docs": await server.json("list_docs"),
        "read_doc": {p: (await server.call("read_doc", {"path": p}))[0] for p in PAGES},
        "search": {q: await server.json("search_docs", {"query": q, "limit": 20}) for q in QUERIES},
        "get_section": {a: (await server.call("get_section", {"anchor": a}))[0] for a in ANCHORS},
    }


async def main() -> None:
    impl = sys.argv[1] if len(sys.argv) > 1 else "python"
    with tempfile.TemporaryDirectory() as tmp:
        server = start_server(impl, {"DOCS_LOCAL_PATH": str(FIXTURE_DOCS), "DOCS_EXCLUDE": "drafts/**"}, Path(tmp))
        try:
            data = await collect(server)
        finally:
            stop_server(server)
    GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {GOLDEN}")


if __name__ == "__main__":
    asyncio.run(main())

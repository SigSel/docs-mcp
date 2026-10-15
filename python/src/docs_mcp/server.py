"""The MCP tools and the HTTP app around them (SPEC.md §2, §6)."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Config
from .index import DocIndex, ToolFailure
from .source import Syncer

INSTRUCTIONS = (
    "Documentation for {name}. Use search_docs to find what is relevant to a question, then read_doc to read a page "
    "or one of its sections. get_section looks up a heading anchor or ID, such as a rule number, across all pages. "
    "docs_overview lists the metadata fields and values you can filter on; list_docs browses pages. "
    "Cite pages by path, or by url when present."
)
# Tool and parameter descriptions: the same text in every implementation (SPEC.md §6).
DESCRIBE = {
    "docs_overview": "Overview of the documentation: source and commit, page count, folders, and every metadata "
    "field with its values and page counts. Call it to learn which filters exist.",
    "list_docs": "List pages with their path, title, url and metadata, optionally narrowed by path prefix and "
    "metadata filters.",
    "search_docs": "Full-text search over every section of every page. Use a few distinctive keywords rather than a "
    "full sentence. Returns the best-matching sections with path, heading, anchor, url and a snippet; follow up with "
    "read_doc(path, section=anchor) for the full text.",
    "read_doc": "Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading text) to "
    "read just that section and its subsections.",
    "get_section": "Look up a section by its heading anchor or ID across all pages, for example a rule number such "
    "as 'RS-203'.",
    "query": "Keywords to search for. Case-insensitive; words of 3+ letters also match longer words that start with "
    "them.",
    "filters": "Metadata filters: field name to a value or a list of values (any of them matches). docs_overview "
    "lists the fields and their values.",
    "path_prefix": "Only pages whose path starts with this, e.g. 'python/'.",
    "limit": "Maximum number of results, 1-50. Default 10.",
    "path": "Page path as returned by search_docs or list_docs, e.g. 'rust/rules.mdx'. The extension may be left out.",
    "section": "Optional anchor (e.g. 'rs-201') or heading text.",
    "anchor": "The anchor or ID, e.g. 'rs-203'. Case-insensitive.",
}
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False, idempotent_hint=True)

Filters = Annotated[dict[str, str | list[str]] | None, Field(description=DESCRIBE["filters"])]
PathPrefix = Annotated[str | None, Field(description=DESCRIBE["path_prefix"])]


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def build_mcp(config: Config, syncer: Syncer) -> MCPServer:
    mcp = MCPServer(name="docs-mcp", version=__version__, instructions=INSTRUCTIONS.format(name=config.name))

    def run(fn: Callable[[DocIndex], str]) -> CallToolResult:
        index = syncer.index
        if index is None:
            error = f" Last error: {syncer.last_error}" if syncer.last_error else ""
            return CallToolResult(
                content=[TextContent(type="text", text=f"Index not ready yet.{error}")], is_error=True
            )
        try:
            return CallToolResult(content=[TextContent(type="text", text=fn(index))])
        except ToolFailure as e:
            return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)

    def tool(name: str) -> Callable[[Callable[..., CallToolResult]], Callable[..., CallToolResult]]:
        return mcp.tool(name=name, description=DESCRIBE[name], annotations=READ_ONLY, structured_output=False)

    @tool("docs_overview")
    def docs_overview() -> CallToolResult:
        return run(lambda ix: _dumps({"name": config.name, "source": syncer.source_info(), **ix.overview()}))

    @tool("list_docs")
    def list_docs(path_prefix: PathPrefix = None, filters: Filters = None) -> CallToolResult:
        return run(lambda ix: _dumps(ix.list_docs(path_prefix, filters)))

    @tool("search_docs")
    def search_docs(
        query: Annotated[str, Field(description=DESCRIBE["query"])],
        filters: Filters = None,
        path_prefix: PathPrefix = None,
        limit: Annotated[int, Field(description=DESCRIBE["limit"])] = 10,
    ) -> CallToolResult:
        return run(lambda ix: _dumps(ix.search(query, filters, path_prefix, limit)))

    @tool("read_doc")
    def read_doc(
        path: Annotated[str, Field(description=DESCRIBE["path"])],
        section: Annotated[str | None, Field(description=DESCRIBE["section"])] = None,
    ) -> CallToolResult:
        return run(lambda ix: ix.read_doc(path, section))

    @tool("get_section")
    def get_section(anchor: Annotated[str, Field(description=DESCRIBE["anchor"])]) -> CallToolResult:
        return run(lambda ix: ix.get_section(anchor))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        status = syncer.status()
        return JSONResponse(status, status_code=200 if status["ready"] else 503)

    @mcp.custom_route("/refresh", methods=["POST"])
    async def refresh(_: Request) -> JSONResponse:
        ok = await syncer.sync()
        return JSONResponse(syncer.status(), status_code=200 if ok else 500)

    return mcp


class Guard:
    """Host check, then bearer auth, on /mcp and /refresh (SPEC.md §2)."""

    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.allowed = config.allowed_hosts
        self.token = config.auth_token.encode() if config.auth_token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope["path"].startswith("/mcp") or scope["path"] == "/refresh"):
            headers = dict(scope["headers"])
            if self.allowed is not None and _hostname(headers.get(b"host", b"").decode("latin-1")) not in self.allowed:
                await PlainTextResponse("Forbidden: host not allowed\n", status_code=403)(scope, receive, send)
                return
            if self.token is not None:
                given = headers.get(b"authorization", b"")
                if not hmac.compare_digest(given, b"Bearer " + self.token):
                    response = PlainTextResponse(
                        "Unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"}
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host[1:]
    return host.split(":", 1)[0]


def build_app(config: Config) -> tuple[ASGIApp, Syncer]:
    syncer = Syncer(config)
    mcp = build_mcp(config, syncer)
    app: Starlette = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=config.host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[Any]:
        task = asyncio.create_task(syncer.run_forever())
        async with inner(a) as state:
            yield state
        task.cancel()

    app.router.lifespan_context = lifespan
    return Guard(app, config), syncer

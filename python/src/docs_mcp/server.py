"""The MCP tools and the HTTP app around them (SPEC.md §2, §6).

No `from __future__ import annotations` here: the tool signatures use descriptions built at runtime, and the SDK
reads the annotations as objects.
"""

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Config
from .index import DocIndex, ToolFailure
from .oauth import METADATA, Refused, TokenChecker, Unreachable, metadata, metadata_path, metadata_url
from .source import Library

log = logging.getLogger("docs_mcp")

INSTRUCTIONS = (
    "Documentation for {name}{written}. Use search_docs to find what is relevant to a question, then read_doc to read "
    "a page or one of its sections. Search with keywords in {lang}, even when the user writes in another language, "
    "and answer in the user's language. get_section looks up a heading anchor or ID, such as a rule number, across all "
    "pages. docs_overview lists the metadata fields and values you can filter on; list_docs browses pages. Cite pages "
    "by path, or by url when present."
)
# Tool and parameter descriptions: the same text in every implementation (SPEC.md §6). {lang} is DOCS_LANGUAGE, or
# "the language of the documentation" when it is not set.
DESCRIBE = {
    "docs_overview": "Overview of the documentation: source and commit, language, page count, folders, and every "
    "metadata field with its values and page counts. Call it to learn which filters exist.",
    "list_docs": "List pages with their path, title, url and metadata, optionally narrowed by path prefix and "
    "metadata filters.",
    "search_docs": "Full-text search over every section of every page. Use a few distinctive keywords in {lang}, "
    "whatever language the user writes in, rather than a full sentence. Returns the best-matching sections with path, "
    "heading, anchor, url and a snippet; follow up with read_doc(path, section=anchor) for the full text.",
    "read_doc": "Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading text) to "
    "read just that section and its subsections.",
    "get_section": "Look up a section by its heading anchor or ID across all pages, for example a rule number such "
    "as 'RS-203'.",
    "query": "Keywords to search for, in {lang}. Add spelling variants, synonyms and English terms freely: keywords "
    "that do not occur are ignored. Case-insensitive; words of 3+ letters also match longer words that start with "
    "them, and words of 6+ letters also match spellings one letter off.",
    "filters": "Metadata filters: field name to a value or a list of values (any of them matches). docs_overview "
    "lists the fields and their values.",
    "path_prefix": "Only pages whose path starts with this, e.g. 'python/'.",
    "limit": "Maximum number of results, 1-50. Default 10.",
    "path": "Page path as returned by search_docs or list_docs, e.g. 'rust/rules.mdx'. The extension may be left out.",
    "section": "Optional anchor (e.g. 'rs-201') or heading text.",
    "anchor": "The anchor or ID, e.g. 'rs-203'. Case-insensitive.",
}


# With DOCS_SOURCES (SPEC.md §7.4): the instructions after the list of sources, and the `source` parameter.
INSTRUCTIONS_SOURCES = (
    "Use search_docs to find what is relevant to a question, then read_doc to read a page or file. search_docs "
    "searches every source unless you name one. Search with keywords in the language of the source you expect the "
    "answer in, even when the user writes in another language, and answer in the user's language. Files that are not "
    "Markdown come back as they are, in a code block. get_section looks up a heading anchor or ID, such as a rule "
    "number, across all sources. docs_overview lists each source's metadata fields and values you can filter on; "
    "list_docs browses a source. Cite pages by source and path, or by url when present."
)
SOURCE_PARAM = {
    "search_docs": "Only this source. Leave it out to search all of them.",
    "list_docs": "Only this source. Leave it out to list all of them.",
    "read_doc": "The page's source, as search_docs and list_docs return it. Needed only when the path is in several "
    "sources.",
}


def describe(config: Config) -> dict[str, str]:
    if config.multi:
        lang = "the language of the source you search"
    else:
        lang = config.sources[0].language or "the language of the documentation"
    return {key: text.replace("{lang}", lang) for key, text in DESCRIBE.items()}


def instructions(config: Config) -> str:
    if config.multi:
        lines = [
            f"- {s.name} ({s.language}): {s.description}" if s.language else f"- {s.name}: {s.description}"
            for s in config.sources
        ]
        head = f"Documentation and reference repositories for {config.name}, in {len(config.sources)} sources:"
        return "\n".join([head, *lines]) + "\n\n" + INSTRUCTIONS_SOURCES
    language = config.sources[0].language
    written = f", written in {language}" if language else ""
    lang = language or "the language of the documentation"
    return INSTRUCTIONS.format(name=config.name, written=written, lang=lang)


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False, destructive_hint=False, idempotent_hint=True)


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def build_mcp(config: Config, library: Library) -> MCPServer:
    text = describe(config)
    Filters = Annotated[dict[str, str | list[str]] | None, Field(description=text["filters"])]
    PathPrefix = Annotated[str | None, Field(description=text["path_prefix"])]
    ListSource = Annotated[str | None, Field(description=SOURCE_PARAM["list_docs"])]
    SearchSource = Annotated[str | None, Field(description=SOURCE_PARAM["search_docs"])]
    ReadSource = Annotated[str | None, Field(description=SOURCE_PARAM["read_doc"])]

    def failed(message: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)

    def run(fn: Callable[[DocIndex], str], source: str | None = None) -> CallToolResult:
        if source is not None and (failure := library.check_source(source)):
            return failed(failure)
        index = library.index
        if index is None:
            return failed(library.not_ready())
        try:
            return CallToolResult(content=[TextContent(type="text", text=fn(index))])
        except ToolFailure as e:
            return failed(str(e))

    def overview(ix: DocIndex) -> str:
        if not config.multi:
            only = library.syncers[0]
            source = only.source
            return _dumps(
                {
                    "name": config.name,
                    "language": source.language,
                    "source": only.source_info(),
                    **ix.overview(source.name),
                }
            )
        sources = [
            {
                "name": s.source.name,
                "description": s.source.description,
                "language": s.source.language,
                "source": s.source_info(),
                **ix.overview(s.source.name),
            }
            for s in library.syncers
        ]
        return _dumps({"name": config.name, "sources": sources})

    def docs_overview() -> CallToolResult:
        return run(overview)

    def list_docs(path_prefix: PathPrefix = None, filters: Filters = None, source: ListSource = None) -> CallToolResult:
        return run(lambda ix: _dumps(ix.list_docs(path_prefix, filters, source)), source)

    def search_docs(
        query: Annotated[str, Field(description=text["query"])],
        filters: Filters = None,
        path_prefix: PathPrefix = None,
        limit: Annotated[int, Field(description=text["limit"])] = 10,
        source: SearchSource = None,
    ) -> CallToolResult:
        return run(lambda ix: _dumps(ix.search(query, filters, path_prefix, limit, source)), source)

    def read_doc(
        path: Annotated[str, Field(description=text["path"])],
        section: Annotated[str | None, Field(description=text["section"])] = None,
        source: ReadSource = None,
    ) -> CallToolResult:
        return run(lambda ix: ix.read_doc(path, section, source), source)

    def get_section(anchor: Annotated[str, Field(description=text["anchor"])]) -> CallToolResult:
        return run(lambda ix: ix.get_section(anchor))

    tools = []
    for fn in (docs_overview, list_docs, search_docs, read_doc, get_section):
        tool = Tool.from_function(
            fn, name=fn.__name__, description=text[fn.__name__], annotations=READ_ONLY, structured_output=False
        )
        if not config.multi:  # one source: nothing to choose (SPEC.md §7.4)
            tool.parameters.get("properties", {}).pop("source", None)
        tools.append(tool)
    mcp = MCPServer(name="docs-mcp", version=__version__, instructions=instructions(config), tools=tools)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        status = library.status()
        return JSONResponse(status, status_code=200 if status["ready"] else 503)

    if config.oauth_issuer and config.public_url:
        document = metadata(config)

        async def protected_resource(_: Request) -> JSONResponse:
            return JSONResponse(document)

        for path in sorted({METADATA, metadata_path(config.public_url)}):
            mcp.custom_route(path, methods=["GET"])(protected_resource)

    @mcp.custom_route("/refresh", methods=["POST"])
    async def refresh(request: Request) -> Response:
        name = request.query_params.get("source")
        if name is not None and name not in library.by_name:
            names = ", ".join(library.by_name)
            return PlainTextResponse(f"No source '{name}'. Sources: {names}.\n", status_code=404)
        ok = await library.sync(name)
        return JSONResponse(library.status(), status_code=200 if ok else 500)

    return mcp


class Guard:
    """Host check, then auth, on /mcp, /refresh and the OAuth metadata (SPEC.md §2)."""

    def __init__(self, app: ASGIApp, config: Config, checker: TokenChecker | None) -> None:
        self.app = app
        self.allowed = config.allowed_hosts
        self.token = config.auth_token.encode() if config.auth_token else None
        refresh = config.refresh_token or config.auth_token
        self.refresh = refresh.encode() if refresh else None
        self.checker = checker
        self.challenge = ""
        if config.public_url:
            self.challenge = f'Bearer resource_metadata="{metadata_url(config.public_url)}"'
            if config.oauth_scopes:
                self.challenge += f', scope="{" ".join(config.oauth_scopes)}"'
        self.refused: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"] if scope["type"] == "http" else ""
        if path.startswith("/mcp") or path == "/refresh" or path.startswith(METADATA):
            headers = dict(scope["headers"])
            response = self._host(headers) or (
                self._refresh(headers)
                if path == "/refresh"
                else await self._mcp(headers)
                if path.startswith("/mcp")
                else None
            )
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _host(self, headers: dict[bytes, bytes]) -> Response | None:
        host = _hostname(headers.get(b"host", b"").decode("latin-1"))
        if self.allowed is None or host in self.allowed:
            return None
        if host not in self.refused and len(self.refused) < 100:
            self.refused.add(host)
            log.warning("refused a request for host '%s': it is not in MCP_ALLOWED_HOSTS", host)
        return PlainTextResponse("Forbidden: host not allowed (see MCP_ALLOWED_HOSTS)\n", status_code=403)

    def _refresh(self, headers: dict[bytes, bytes]) -> Response | None:
        if self.refresh is None:
            if self.checker is None:
                return None
            return PlainTextResponse("Forbidden: /refresh is off (see MCP_REFRESH_TOKEN)\n", status_code=403)
        return _bearer(headers, self.refresh)

    async def _mcp(self, headers: dict[bytes, bytes]) -> Response | None:
        if self.checker is None:
            return _bearer(headers, self.token) if self.token is not None else None
        scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return self._refuse(401, "Unauthorized\n", self.challenge)
        try:
            await self.checker.check(token)
        except Refused as e:
            body = "Unauthorized\n" if e.status == 401 else "Forbidden\n"
            return self._refuse(e.status, body, f'{self.challenge}, error="{e.error}", error_description="{e.reason}"')
        except Unreachable:
            return PlainTextResponse("Service Unavailable: cannot reach the OAuth issuer\n", status_code=503)
        return None

    @staticmethod
    def _refuse(status: int, body: str, challenge: str) -> Response:
        return PlainTextResponse(body, status_code=status, headers={"WWW-Authenticate": challenge})


def _bearer(headers: dict[bytes, bytes], secret: bytes) -> Response | None:
    """None when the request carries `Bearer <secret>`, compared in constant time."""
    if hmac.compare_digest(headers.get(b"authorization", b""), b"Bearer " + secret):
        return None
    return PlainTextResponse("Unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})


def startup_warnings(config: Config) -> list[str]:
    """The defaults an operator should know they are running with (SPEC.md §2)."""
    warnings = []
    if config.allowed_hosts_default:
        warnings.append(
            "MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are accepted. "
            "Set it to the host names clients use."
        )
    elif config.allowed_hosts is None:
        warnings.append("MCP_ALLOWED_HOSTS is '*': the Host header is not checked.")
    if not config.auth_token and not config.oauth_issuer:
        warnings.append("MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.")
    if config.oauth_issuer and not config.refresh_token:
        warnings.append("MCP_REFRESH_TOKEN is not set: /refresh is off.")
    return warnings


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host[1:]
    return host.split(":", 1)[0]


def build_app(config: Config) -> tuple[ASGIApp, Library]:
    library = Library(config)
    mcp = build_mcp(config, library)
    app: Starlette = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=config.host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[Any]:
        task = asyncio.create_task(library.run_forever())
        async with inner(a) as state:
            yield state
        task.cancel()

    app.router.lifespan_context = lifespan
    checker = TokenChecker(config) if config.oauth_issuer else None
    return Guard(app, config, checker), library

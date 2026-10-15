# docs-mcp (TypeScript)

The TypeScript implementation of [docs-mcp](../README.md), built on the MCP TypeScript SDK v2
(`@modelcontextprotocol/server`, `createMcpHandler`) served by `@hono/node-server`. Behaviour is defined by
[SPEC.md](../SPEC.md). Needs Node.js 22.

```sh
npm ci && npm run build
DOCS_LOCAL_PATH=../../dev-handbooks/dev-handbook DOCS_DIR=src/content/docs node dist/main.js
npm run typecheck
```

| Module | Does |
|---|---|
| `config.ts` | Environment variables (SPEC §1) |
| `content.ts` | Frontmatter (`yaml`, YAML 1.2), cleaning, sections and anchors (§4) |
| `docindex.ts` | Discovery, BM25 search, filters, tool outputs (§4–6) |
| `source.ts` | git clone/pull or local re-read, on a schedule (§3) |
| `server.ts` | MCP tools, `/healthz`, `/readyz`, `/refresh`, host check and auth (§2, §6) |

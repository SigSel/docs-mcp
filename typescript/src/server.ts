// The MCP tools and the HTTP app around them (SPEC.md §2, §6).

import { timingSafeEqual } from 'node:crypto';

import { type CallToolResult, createMcpHandler, McpServer } from '@modelcontextprotocol/server';
import * as z from 'zod/v4';

import type { Config } from './config.js';
import { type DocIndex, type Filters, ToolFailure } from './docindex.js';
import type { Syncer } from './source.js';

const VERSION = '0.1.0';

const INSTRUCTIONS =
  'Documentation for {name}. Use search_docs to find what is relevant to a question, then read_doc to read a page ' +
  'or one of its sections. get_section looks up a heading anchor or ID, such as a rule number, across all pages. ' +
  'docs_overview lists the metadata fields and values you can filter on; list_docs browses pages. ' +
  'Cite pages by path, or by url when present.';

// Tool and parameter descriptions: the same text in every implementation (SPEC.md §6).
const DESCRIBE = {
  docs_overview:
    'Overview of the documentation: source and commit, page count, folders, and every metadata field with its ' +
    'values and page counts. Call it to learn which filters exist.',
  list_docs:
    'List pages with their path, title, url and metadata, optionally narrowed by path prefix and metadata filters.',
  search_docs:
    'Full-text search over every section of every page. Use a few distinctive keywords rather than a full ' +
    'sentence. Returns the best-matching sections with path, heading, anchor, url and a snippet; follow up with ' +
    'read_doc(path, section=anchor) for the full text.',
  read_doc:
    'Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading text) to read just ' +
    'that section and its subsections.',
  get_section:
    "Look up a section by its heading anchor or ID across all pages, for example a rule number such as 'RS-203'.",
  query: 'Keywords to search for. Case-insensitive; words of 3+ letters also match longer words that start with them.',
  filters:
    'Metadata filters: field name to a value or a list of values (any of them matches). docs_overview lists the ' +
    'fields and their values.',
  path_prefix: "Only pages whose path starts with this, e.g. 'python/'.",
  limit: 'Maximum number of results, 1-50. Default 10.',
  path: "Page path as returned by search_docs or list_docs, e.g. 'rust/rules.mdx'. The extension may be left out.",
  section: "Optional anchor (e.g. 'rs-201') or heading text.",
  anchor: "The anchor or ID, e.g. 'rs-203'. Case-insensitive.",
};

const READ_ONLY = { readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: true };

const filtersSchema = z
  .record(z.string(), z.union([z.string(), z.array(z.string())]))
  .nullish()
  .describe(DESCRIBE.filters);
const pathPrefixSchema = z.string().nullish().describe(DESCRIBE.path_prefix);

const text = (t: string, isError = false): CallToolResult => ({ content: [{ type: 'text', text: t }], isError });

export function buildMcp(config: Config, syncer: Syncer): McpServer {
  const server = new McpServer(
    { name: 'docs-mcp', version: VERSION },
    { instructions: INSTRUCTIONS.replace('{name}', config.name) },
  );

  const run = (fn: (index: DocIndex) => string): CallToolResult => {
    const index = syncer.index;
    if (index === null) {
      return text(`Index not ready yet.${syncer.lastError ? ` Last error: ${syncer.lastError}` : ''}`, true);
    }
    try {
      return text(fn(index));
    } catch (e) {
      if (e instanceof ToolFailure) return text(e.message, true);
      throw e;
    }
  };

  server.registerTool(
    'docs_overview',
    { description: DESCRIBE.docs_overview, annotations: READ_ONLY },
    async () => run((ix) => JSON.stringify({ name: config.name, source: syncer.sourceInfo(), ...ix.overview() })),
  );

  server.registerTool(
    'list_docs',
    {
      description: DESCRIBE.list_docs,
      annotations: READ_ONLY,
      inputSchema: z.object({ path_prefix: pathPrefixSchema, filters: filtersSchema }),
    },
    async ({ path_prefix, filters }) => run((ix) => JSON.stringify(ix.listDocs(path_prefix, filters as Filters))),
  );

  server.registerTool(
    'search_docs',
    {
      description: DESCRIBE.search_docs,
      annotations: READ_ONLY,
      inputSchema: z.object({
        query: z.string().describe(DESCRIBE.query),
        filters: filtersSchema,
        path_prefix: pathPrefixSchema,
        limit: z.number().int().nullish().describe(DESCRIBE.limit),
      }),
    },
    async ({ query, filters, path_prefix, limit }) =>
      run((ix) => JSON.stringify(ix.search(query, filters as Filters, path_prefix, limit ?? 10))),
  );

  server.registerTool(
    'read_doc',
    {
      description: DESCRIBE.read_doc,
      annotations: READ_ONLY,
      inputSchema: z.object({
        path: z.string().describe(DESCRIBE.path),
        section: z.string().nullish().describe(DESCRIBE.section),
      }),
    },
    async ({ path, section }) => run((ix) => ix.readDoc(path, section)),
  );

  server.registerTool(
    'get_section',
    {
      description: DESCRIBE.get_section,
      annotations: READ_ONLY,
      inputSchema: z.object({ anchor: z.string().describe(DESCRIBE.anchor) }),
    },
    async ({ anchor }) => run((ix) => ix.getSection(anchor)),
  );

  return server;
}

function hostname(host: string): string {
  const h = host.trim().toLowerCase();
  if (h.startsWith('[')) return h.includes(']') ? h.slice(1, h.indexOf(']')) : h.slice(1);
  return h.split(':')[0]!;
}

function tokenMatches(given: string | null, token: string): boolean {
  const a = Buffer.from(given ?? '');
  const b = Buffer.from(`Bearer ${token}`);
  return a.length === b.length && timingSafeEqual(a, b);
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** The whole HTTP surface as a web-standard fetch handler. */
export function buildApp(config: Config, syncer: Syncer): (request: Request) => Promise<Response> {
  const mcp = createMcpHandler(() => buildMcp(config, syncer), { responseMode: 'json', legacy: 'stateless' });

  return async (request) => {
    const path = new URL(request.url).pathname;
    if (path === '/healthz' || path === '/readyz') {
      if (request.method !== 'GET') return new Response('Method Not Allowed\n', { status: 405 });
      if (path === '/healthz') return json({ status: 'ok' });
      const status = syncer.status();
      return json(status, status.ready ? 200 : 503);
    }
    if (path !== '/mcp' && path !== '/refresh') return new Response('Not Found\n', { status: 404 });

    if (config.allowedHosts !== null && !config.allowedHosts.includes(hostname(request.headers.get('host') ?? ''))) {
      return new Response('Forbidden: host not allowed\n', { status: 403 });
    }
    if (config.authToken !== null && !tokenMatches(request.headers.get('authorization'), config.authToken)) {
      return new Response('Unauthorized\n', { status: 401, headers: { 'www-authenticate': 'Bearer' } });
    }
    if (path === '/refresh') {
      if (request.method !== 'POST') return new Response('Method Not Allowed\n', { status: 405 });
      const ok = await syncer.sync();
      return json(syncer.status(), ok ? 200 : 500);
    }
    return mcp.fetch(request);
  };
}

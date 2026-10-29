# How docs-mcp works

What MCP is, who does what, and what happens between a user's question and the
answer. The README covers configuration and deployment. [SPEC.md](SPEC.md) has
the exact rules.

For a gentler start, without the protocol details, read
[What is MCP?](docs/what-is-mcp.md) and [What docs-mcp does](docs/what-docs-mcp-does.md).
The [threat model](docs/threat-model.md) covers security.

## The idea

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) is a standard
plug between an AI assistant and outside tools or data. docs-mcp uses it to give
assistants the handbook.

**docs-mcp has no AI in it.** It is a small web service that waits to be asked
things. The model inside the user's assistant decides when to ask, what to ask,
and what to do with the answer.

| Part | Role | Examples |
|---|---|---|
| **User** | Asks a question, in any language | "Can I use unwrap in our Rust service?" |
| **Assistant app**, the *MCP client* | Connects to MCP servers and passes the model's tool calls on | Claude Code, VS Code Copilot, Cursor |
| **Model** | Reads the server's instructions and tool descriptions, decides which tool to call with which arguments, and writes the answer | Claude, GPT, … |
| **docs-mcp**, the *MCP server* | Answers tool calls from an in-memory index of the handbook | this repo |
| **git repo** | Where the handbook lives; the only source of truth | Azure DevOps, Forgejo, … |

```
 User ──question──▶ Assistant app (MCP client)
                     │  the model decides it needs the handbook
                     │  and picks a tool + arguments
                     ▼
              POST /mcp  {"method":"tools/call", …}
                     │
                     ▼
              docs-mcp ──▶ looks it up in its in-memory index ──▶ returns text
                     │
                     ▼
              The model reads the result, maybe calls another tool,
              then answers the user, citing the page
```

## What the server reacts to

docs-mcp never contacts a client on its own. Four things make it act:

| Trigger | What it does |
|---|---|
| **A request on `/mcp`** from an assistant | Answers it, then forgets it. Every request stands alone: no sessions, no memory of earlier calls. That is also why any number of replicas can run side by side. |
| **The refresh timer** (`DOCS_REFRESH_SECONDS`, 300 by default), one per source | `git fetch`. If there is a new commit, it re-reads that source and swaps in a new index. |
| **`POST /refresh`**, typically a push webhook from Azure DevOps or Forgejo | The same as the timer, right away, for every source or for `?source=<name>`. |
| **`GET /healthz`, `GET /readyz`** | "I am running" and "I have an index", so Kubernetes or a container platform knows when to send traffic. |

Every `/mcp` and `/refresh` request goes through two checks before any MCP
handling:

1. **The `Host` header must be in `MCP_ALLOWED_HOSTS`**, or the request gets
   `403`. This protects against DNS rebinding.
2. **Auth**, or the request gets `401`. `/mcp` needs `Authorization: Bearer`
   with the shared token (`MCP_AUTH_TOKEN`) or, with [Entra ID
   sign-in](docs/sso-entra.md), a valid access token. `/refresh` needs the
   refresh secret (`MCP_REFRESH_TOKEN`).

## A conversation, step by step

The examples below are real traffic from docs-mcp serving the test handbook in
[`fixtures/docs`](fixtures/docs), with `DOCS_NAME=handbook` and
`DOCS_LANGUAGE=Norwegian Nynorsk`, trimmed where marked `…`. Every message is
[JSON-RPC](https://www.jsonrpc.org/specification) sent with `POST /mcp`.

### 1. Connect: the server introduces itself

The client asks who the server is:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"…","version":"…"}}}
```

The server answers with its name and its **instructions**:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-06-18",
  "capabilities":{"tools":{}},
  "serverInfo":{"name":"docs-mcp","version":"0.1.0"},
  "instructions":"Documentation for handbook, written in Norwegian Nynorsk. Use search_docs to find what is relevant to a question, then read_doc to read a page or one of its sections. Search with keywords in Norwegian Nynorsk, even when the user writes in another language, and answer in the user's language. …"}}
```

The instructions end up in the model's context. That is how the model learns that
the docs are in Nynorsk, and that it should search in Nynorsk even when the user
writes English.

Clients on the newer 2026-07-28 protocol skip this handshake. They send
`server/discover` and attach their protocol version to every request instead.
docs-mcp serves both kinds of client.

### 2. List the tools

The client sends `tools/list`, and gets the five tools, each with a description
and a JSON Schema for its arguments:

| Tool | Arguments | For |
|---|---|---|
| `search_docs` | `query`, `filters?`, `path_prefix?`, `limit?` | Find the sections that match some keywords |
| `read_doc` | `path`, `section?` | Read a page, or one section of it, as Markdown |
| `get_section` | `anchor` | Jump straight to an ID such as `RS-203`, across all pages |
| `list_docs` | `path_prefix?`, `filters?` | Browse pages and their metadata |
| `docs_overview` | | What is in the handbook: source, commit, language, folders, metadata fields |

**The descriptions are all the model knows about the tools.** It picks a tool,
and writes its arguments, from that text alone. That is why their wording is
part of the spec ([SPEC.md §6](SPEC.md#6-mcp-tools)), and why the search
description tells the model which language to write keywords in.

### 3. Wait

Nothing happens until a user asks something where the model judges that the
handbook would help.

### 4. The model calls a tool

The user asks, in English, *"Can I use unwrap in our Rust service?"*. The model
writes Nynorsk keywords and the client sends:

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"search_docs","arguments":{"query":"unwrap panikk produksjon","limit":3}}}
```

The server searches its index and answers with the best-matching sections. The
result is text (here JSON), which the model reads:

```json
{"jsonrpc":"2.0","id":3,"result":{"isError":false,"content":[{"type":"text","text":
  {"query":"unwrap panikk produksjon","count":2,"results":[
    {"path":"rust/rules.mdx","title":"Rust-reglane",
     "section":"RS-203: Ingen unwrap i produksjonskode","anchor":"rs-203","score":7.59,
     "snippet":"MÅ IKKJE Produksjonskode **MÅ IKKJE** kalle `unwrap()`. Bruk `?` og ein feiltype. …"},
    {"path":"rust/testing.md","title":"Testing i Rust",
     "section":"Sjå regelen om unwrap","anchor":"sjå-regelen-om-unwrap","score":3.446,
     "snippet":"Testkode kan bruke `unwrap()` fritt."}]}}]}}
```

(The inner JSON is shown unescaped here; on the wire it is a string.)

### 5. The model reads more

The snippet is enough to see where the answer is, so the model asks for the
whole rule:

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call",
 "params":{"name":"read_doc","arguments":{"path":"rust/rules.mdx","section":"rs-203"}}}
```

The result is the section as clean Markdown, with the page's metadata on top:

```markdown
---
path: rust/rules.mdx
title: Rust-reglane
section: rs-203
innhaldstype: krav
fagområde: backend
eigarforum: rustforum
tema: kodekvalitet, testing
teknologi: Rust, CI/CD
stikkord: reglar
---

### RS-203: Ingen unwrap i produksjonskode

MÅ IKKJE

Produksjonskode **MÅ IKKJE** kalle `unwrap()`. Bruk `?` og ein feiltype.

  **Linux**
    Køyr `cargo clippy` i terminalen.

  **Windows**
    Køyr `cargo clippy` i PowerShell.
…
```

The MDX source of this section has components in it (`<Rule>`, `<Tabs>`,
`<TabItem>`). The server has reduced them to their text, so the model reads what
a person would see on the page.

### 6. The model answers

*"No. RS-203 says production code must not call `unwrap()`; use `?` and an
error type instead. Tests are exempt."* Plus a link to the page, when
`DOCS_SITE_URL` is set.

**The model makes every decision in that chain:** whether to search at all, the
keywords, which result to read, and when it knows enough. The server only
answers each call as well as it can.

### When something is wrong

Mistakes come back as a tool result marked `isError`. The text says what to do
instead, so the model can correct itself:

- **A page that does not exist:**
  `No page 'rules'. Did you mean: rust/rules.mdx? Use list_docs or search_docs to find pages.`
- **A section that does not exist:** the error lists the anchors the page has.
- **A query with no words in it:** `The query must contain at least one word.`
- **Before the first index is built:** `Index not ready yet.`, plus the last
  error, such as a git authentication failure.

## Inside the server

![Requests arrive on one HTTP listener. Health probes go straight to the status. Everything else passes the guard: a Host check, then the shared token, an OAuth access token checked against the identity provider's keys, or the refresh secret. /mcp runs five read-only tools against an immutable search index snapshot. Separately, one syncer per source runs git against its git host, the loader turns the clone into pages, and the index builder builds a new snapshot over all sources and swaps it in.](docs/figures/server-internals.svg)

Two paths run through the process, and they meet only at the search index.

**The request path**, top half:

1. **One HTTP listener** takes everything on port 8080, as plain HTTP. TLS ends
   in front of it, at the ingress or the platform.
2. **Health probes** (`/healthz`, `/readyz`) skip the guard and read the status
   each source keeps: its commit, its last sync, its last error.
3. **The guard** checks every other request: first that the `Host` header is
   one it serves, then auth. `/mcp` needs the shared token or, with Entra ID
   sign-in, an access token. `/refresh` needs the refresh secret.
4. **The token checker** decides about access tokens on its own: allowed
   algorithm, signature by the issuer's key, issuer, audience, expiry, scope.
   It keeps the issuer's public keys in a cache, and fetches them again when
   they are an hour old, or for a key it has not seen, at most once a minute.
5. **`/mcp`** speaks MCP: JSON-RPC over Streamable HTTP, stateless. Each call
   runs one of five tools, and each tool only reads the search index. A call
   takes a few milliseconds.

**The sync path**, bottom half:

1. **One syncer per source** decides when to pull: every
   `DOCS_REFRESH_SECONDS`, every 10 seconds until the first success, or now,
   for `POST /refresh`. A source never syncs twice at once, and one slow
   source does not hold up another.
2. **git** runs as a child process: a shallow clone, then fetch and reset, into
   the source's own folder in the cache. It never follows a redirect, and is
   killed with its helpers if it hangs.
3. **The loader** walks the clone. It skips links, files that usually hold
   secrets, files over 1 MB and binary files. Markdown is parsed into
   metadata, clean text and sections. Any other file is kept as written.
4. **The index builder** turns every source's pages into one search index,
   off to the side, and swaps it in. Requests always see one complete index:
   the old one until the new one is ready. A failed pull keeps the old pages,
   and the status reports the error.

A few things that follow from this:

- **The index is rebuilt, never edited.** Nothing a request sends can change it.
- **Search finds sections, not pages.** It scores each section with BM25 over:
  - the page path, title and metadata;
  - the section's heading and text.

  Words that start with the query word also match, and so do words one letter
  off. The rules and the reasons behind them are in
  [SPEC.md §5](SPEC.md#5-search) and [MEASUREMENTS.md](MEASUREMENTS.md).
- **Nothing about the handbook is built in.** Pages, metadata fields, rule IDs
  and anchors all come from the files. Change a source and the server follows
  on the next pull.
- **Only two things go out:** git, to the git hosts, and the token checker, to
  the identity provider. Logs go to stdout, and never include tool calls.

## What it deliberately does not do

- **No AI.** Understanding the question, translating it and writing the answer
  happen in the user's assistant.
- **No writing.** All five tools are read-only. Nothing a client sends can change
  the handbook or the server.
- **No users.** It knows only whether a request carries the shared token, not who
  sent it.
- **No pushing.** It never sends a client anything that was not asked for.
- **No state between requests.** No session, no history; each call is answered
  on its own.

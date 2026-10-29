# docs-mcp

An [MCP](https://modelcontextprotocol.io) server that serves a folder of
Markdown docs, such as the dev-handbook, to AI assistants: Claude Code, VS Code
Copilot, Cursor, or anything else that speaks MCP. It clones a git repo, indexes
the `.md`/`.mdx` files in one directory, keeps pulling, and answers over HTTP.

It runs once, centrally, on the internal network. Users point their assistant
at it; nobody runs it on their own machine. Running it locally is for
developing docs-mcp and for previewing handbook changes.

New to MCP? [HOW-IT-WORKS.md](HOW-IT-WORKS.md) explains the idea and walks
through a question from user to answer, with the real requests and responses.

```
git repo (Azure DevOps, Forgejo, GitHub, …)
   │  clone, then pull every 5 min (or POST /refresh from a webhook)
   ▼
docs-mcp container ── parses Markdown + frontmatter → in-memory search index
   │  Streamable HTTP on :8080/mcp
   ▼
Claude Code · VS Code · Cursor · …   on the internal network
```

It is written three times, in **Rust**, **Python** and **TypeScript**, against
one [specification](SPEC.md). A shared [conformance suite](conformance/) checks
that all three return the same bytes. Pick whichever your team would rather
maintain (see [Which implementation](#which-implementation)).

## The handbook stays in charge

The server knows Markdown, not the handbook. All of these need **no change to
docs-mcp**:

| You change in the handbook | What happens |
|---|---|
| Add, move, rename or delete a page | Picked up on the next pull |
| Add a frontmatter field (`tema:`, `eigarforum:`, anything) | Becomes a filter, and shows up in `docs_overview` |
| Add a value to a field | Filterable straight away |
| Add a rule with `### XX-123: … { #xx-123 }` | `get_section("XX-123")` finds it |
| Use MDX components (`<Rule>`, `<Tabs>`, `<Card title=…>`) | Reduced to their text. Labels and titles become **bold** |
| Switch framework (Starlight → VitePress, Docusaurus, Zensical…) | Point `DOCS_DIR` at the new folder. Presentation-only frontmatter is ignored |
| Write it in English, or Bokmål, instead of Nynorsk | Change `DOCS_LANGUAGE` (see [Languages](#languages)) |

That last row has been tested: the cross-implementation diff below ran against
the Starlight, VitePress and Zensical variants.

## What the assistant gets

| Tool | For |
|---|---|
| `search_docs(query, filters?, path_prefix?, limit?)` | Ranked full-text search over sections. Returns path, heading, anchor, url and a snippet |
| `read_doc(path, section?)` | A page, or one section of it, as clean Markdown with its metadata in a header |
| `get_section(anchor)` | Jump straight to `rs-203`, `py-101`, or any heading anchor, across all pages |
| `list_docs(path_prefix?, filters?)` | Browse pages with their metadata |
| `docs_overview()` | Source, commit, language, folders, and every metadata field with its values |

All five are read-only. A typical exchange: *"Can I use unwrap in our Rust
service?"* The assistant calls `search_docs("unwrap")` → `rust/rules.mdx#rs-203`,
then `read_doc("rust/rules.mdx", "rs-203")`, and answers citing RS-203 with a
link to the handbook page.

## Languages

The handbook can be written in Nynorsk, Bokmål or English, and users can ask in
any of them. Two things make that work.

**The assistant searches in the handbook's language.** Users never type search
queries; their assistant writes them. Set `DOCS_LANGUAGE` to the language the
docs are written in, for example `Norwegian Nynorsk`. The server then tells the
assistant to:

- search with keywords in that language, whatever language the user writes in;
- add Bokmål spellings, synonyms and English terms as extra keywords;
- answer in the user's language.

Translating a question into keywords is something the assistant is good at.

**Search bridges the rest without knowing any language:**

- Words of 6+ letters match spellings one letter apart: `sikkerhet`/`sikkerheit`,
  `regler`/`reglar`, `avhengigheter`/`avhengigheiter`.
- A word matches longer words that start with it: `avhengigheit` →
  `avhengigheiter`.
- Page paths are searchable. They are often English even when the text is not:
  `virtual-environments.mdx`, `glossary.mdx`.

Measured on the dev-handbook with 36 questions, each asked four ways. The table
shows how often the right page came first, and how often it was in the top 3:

| The query is written in | First | Top 3 |
|---|---|---|
| Nynorsk | 83% | 92% |
| Bokmål | 81% | 97% |
| English, word for word | 67% | 81% |
| Nynorsk keywords plus Bokmål and English variants: what the assistant writes when told the language | 86% | 97% |

Before these changes, English was at 44% / 58%. Details and method are in
[MEASUREMENTS.md](MEASUREMENTS.md#search-quality-across-languages); `just eval`
reruns it.

Leave `DOCS_LANGUAGE` unset and the assistant is told to search in "the language
of the documentation", which it has to work out from the first results.

## Try it locally

For developing docs-mcp, or previewing handbook changes before they are pushed.
Users connect to the central deployment instead (see [Deploying](#deploying)).
Needs [just](https://just.systems), plus the toolchain for the implementation
you run.

```sh
just run rust ../dev-handbooks/dev-handbook src/content/docs   # or python / typescript
```

Then, in another terminal:

```sh
claude mcp add --transport http dev-handbook http://localhost:8080/mcp
```

Edit a page in the handbook and it is re-read within 5 seconds.

Or with containers, no toolchains needed:

```sh
cd deploy && podman compose --profile rust up   # → http://localhost:8081/mcp
```

## Connecting clients

**Claude Code**, with the token if the server has one:

```sh
claude mcp add --transport http dev-handbook https://docs-mcp.example.internal/mcp \
  --header "Authorization: Bearer $DOCS_MCP_TOKEN"
```

Add `--scope project` to write it to `.mcp.json` and share it with the repo.

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "dev-handbook": {
      "type": "http",
      "url": "https://docs-mcp.example.internal/mcp",
      "headers": { "Authorization": "Bearer ${input:docs-mcp-token}" }
    }
  },
  "inputs": [{ "id": "docs-mcp-token", "type": "promptString", "password": true, "description": "docs-mcp token" }]
}
```

Any other client: the endpoint is `POST /mcp` (Streamable HTTP, stateless).
Clients on the 2026-07-28 revision and on the 2025 revisions both work.

## Pointing it at git

Set the repo, the branch, and the folder that holds the docs:

```sh
DOCS_REPO_URL=https://dev.azure.com/ORG/PROJECT/_git/dev-handbook
DOCS_REF=main
DOCS_DIR=src/content/docs
```

Authentication is the same for every host: a read-only token, sent as HTTP Basic
auth. It goes only to the repo's host and is never written to disk or the URL.
Redirects are not followed, because git would send the token along. If the repo
moves, the sync fails with git's `301` or `302` error until `DOCS_REPO_URL`
points at the new address.

| Host | `DOCS_GIT_TOKEN` | `DOCS_GIT_USERNAME` |
|---|---|---|
| Azure DevOps | PAT with **Code → Read** | not needed |
| Forgejo / Gitea | token with `read:repository` | not needed (tested against Forgejo 13) |
| GitHub | fine-grained PAT, **Contents: read** | `x-access-token` |
| GitLab | project access token, `read_repository` | any non-empty name |
| Any, over SSH | `DOCS_GIT_SSH_KEY_FILE=/secrets/id_ed25519` and `DOCS_GIT_KNOWN_HOSTS_FILE` | |

Mount secrets as files and use `DOCS_GIT_TOKEN_FILE` / `MCP_AUTH_TOKEN_FILE`;
every secret variable has a `_FILE` twin.

**Instant updates.** Add a webhook on push that calls
`POST https://docs-mcp…/refresh` with `Authorization: Bearer <MCP_AUTH_TOKEN>`:

- Azure DevOps: a *Service hook → Web Hooks* with an extra HTTP header.
- Forgejo: *Webhooks → Forgejo* with the authorization header.

Without a webhook, it pulls every `DOCS_REFRESH_SECONDS` (default 300).

A git command that hangs, for example because the host stopped answering, is
killed after `DOCS_GIT_TIMEOUT_SECONDS` (default 120). The docs from the last
good pull stay in service, and `last_error` in `/readyz` says what happened.

## Deploying

### Before you deploy

docs-mcp starts locked down. Two settings decide who can reach it, and it says
at startup when either is still at its default:

| Setting | Default | Set it to |
|---|---|---|
| `MCP_ALLOWED_HOSTS` | `localhost` only: a request addressed to any other host name gets `403` | Every name clients use: the ingress host, and in Kubernetes the Service names |
| `MCP_AUTH_TOKEN` | Off: anyone who can reach the server can use it | A long random string. Clients send it as `Authorization: Bearer …` |

Also set:

- `DOCS_LANGUAGE`, see [Languages](#languages).
- A **read-only** git token.

What a deployment left at the defaults logs:

```
WARNING MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are accepted. Set it to the host names clients use.
WARNING MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.
WARNING refused a request for host 'docs-mcp.example.internal': it is not in MCP_ALLOWED_HOSTS
```

The last line appears once per host name that gets refused, so the log tells
you what to add. The `403` response points at `MCP_ALLOWED_HOSTS` too. The
`Host` check protects against DNS rebinding. `MCP_ALLOWED_HOSTS=*` turns it off
(and is logged); only do that behind something else that checks.

### Where it runs

The images listen on 8080 and run as UID 10001, with a read-only root
filesystem except `/tmp`. Endpoints:

- `/healthz`: liveness.
- `/readyz`: readiness. `200` once the first index is loaded.
- `/mcp`: the MCP endpoint.

| Where | How |
|---|---|
| Laptop, for development | `deploy/compose.yaml` (profiles `rust`, `python`, `typescript`, `git`) |
| Kubernetes | `kubectl apply -k deploy/k8s/overlays/example`. Copy the overlay, then set image, repo, `MCP_ALLOWED_HOSTS` and `secrets.env` |
| Azure Container Apps, AWS App Runner/ECS, a VM | Run the image with the env vars below. Ingress on port 8080, health check `/readyz` |

```sh
podman run -d -p 8080:8080 --read-only --tmpfs /tmp \
  -e DOCS_REPO_URL=https://dev.azure.com/ORG/PROJECT/_git/dev-handbook \
  -e DOCS_DIR=src/content/docs -e DOCS_NAME=dev-handbook -e DOCS_LANGUAGE="Norwegian Nynorsk" \
  -e DOCS_GIT_TOKEN_FILE=/run/secrets/pat -v ./pat:/run/secrets/pat:ro,z \
  -e MCP_AUTH_TOKEN=change-me -e MCP_ALLOWED_HOSTS=docs-mcp.example.internal \
  localhost/docs-mcp-rust:dev
```

The full configuration list is in [SPEC.md §1](SPEC.md#1-configuration).

## Which implementation

All three behave identically, byte for byte. They differ in what they cost to
run (full table, method and machine in
[MEASUREMENTS.md](MEASUREMENTS.md#container-footprint-and-latency)):

| | Rust | Python | TypeScript |
|---|---|---|---|
| Image size | 200 MB | 276 MB | 364 MB |
| Start to ready | 0.5 s | 3.1 s | 0.9 s |
| Memory, idle / after load | 9 / 11 MB | 63 / 64 MB | 54 / 116 MB |
| search_docs, median | 4 ms | 11 ms | 10 ms |
| Source (non-blank lines) | 1824 | 987 | 1085 |
| SDK | rmcp 3.8 + axum | mcp 2.3 (`MCPServer`) + Starlette | @modelcontextprotocol/server 2.3 |

Most of every image is `git` and its dependencies. Choose the language your
team will keep fixing. The spec and the conformance suite stay useful whichever
you keep.

## Repository layout

```
HOW-IT-WORKS.md    the idea, and a question traced from user to answer
SPEC.md            the contract: config, HTTP, parsing, search, tool outputs
rust/ python/ typescript/
                   one implementation each, with a Containerfile
conformance/       black-box pytest suite run against any implementation
  golden.json      exact expected outputs for fixtures/docs
  githost.py       a git host over HTTP for the auth and timeout tests
  compare.py       diff all implementations against a real docs tree
  search_eval.*    search quality across nn/nb/en questions (just eval)
  measure.py       image size, memory, startup, latency (just measure)
fixtures/docs/     small docs tree that exercises every rule in the spec
deploy/            compose.yaml, k8s/ (kustomize base + example overlay)
MEASUREMENTS.md    the numbers above, with method and machine
justfile           build, test, lint, run, compare, eval, image, measure
```

## Testing

```sh
just test            # 138 checks × 3 implementations
just compare         # every page, section and 180+ searches, diffed across all three
just eval            # search quality for Nynorsk, Bokmål and English questions
just lint            # clippy pedantic, ruff, tsc
just image measure   # build the images, then measure them
```

To change behaviour:

1. Change `SPEC.md`.
2. Change the tests.
3. Change the Python implementation, run `just golden`, and review the diff in
   `conformance/golden.json`.
4. Port the change to the other two until `just test` is green.

## Limits, on purpose

- **Keyword search (BM25), not embeddings.** No model to host, and results are
  explainable. It does not know synonyms or translate. The assistant does that
  when it writes the query (see [Languages](#languages)). If that turns out not
  to be enough, multilingual embeddings are the next step, at the cost of
  hosting a model.
- **One source per server.** For several handbooks, run several containers.
- **Tools only, no MCP resources or prompts.** The clients this is for use tools.
- **HTML entities** such as `&nbsp;` are passed through as written.

# docs-mcp

An [MCP](https://modelcontextprotocol.io) server that serves a folder of
Markdown docs, such as the dev-handbook, to AI assistants: Claude Code, VS Code
Copilot, Cursor, or anything else that speaks MCP. It clones a git repo, indexes
the `.md`/`.mdx` files in one directory, keeps pulling, and answers over HTTP.

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

That last row has been tested: the cross-implementation diff below ran against
the Starlight, VitePress and Zensical variants.

## What the assistant gets

| Tool | For |
|---|---|
| `search_docs(query, filters?, path_prefix?, limit?)` | Ranked full-text search over sections. Returns path, heading, anchor, url and a snippet |
| `read_doc(path, section?)` | A page, or one section of it, as clean Markdown with its metadata in a header |
| `get_section(anchor)` | Jump straight to `rs-203`, `py-101`, or any heading anchor, across all pages |
| `list_docs(path_prefix?, filters?)` | Browse pages with their metadata |
| `docs_overview()` | Source, commit, folders, and every metadata field with its values |

All five are read-only. A typical exchange: *"Can I use unwrap in our Rust
service?"* The assistant calls `search_docs("unwrap")` → `rust/rules.mdx#rs-203`,
then `read_doc("rust/rules.mdx", "rs-203")`, and answers citing RS-203 with a
link to the handbook page.

## Quick start (local)

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
auth. It is scoped to the repo's host and is never written to disk or the URL.

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

## Deploying

The images listen on 8080 and run as UID 10001, with a read-only root
filesystem except `/tmp`. Endpoints:

- `/healthz`: liveness.
- `/readyz`: readiness. `200` once the first index is loaded.
- `/mcp`: the MCP endpoint.

| Where | How |
|---|---|
| Laptop | `deploy/compose.yaml` (profiles `rust`, `python`, `typescript`, `git`) |
| Kubernetes | `kubectl apply -k deploy/k8s/overlays/example`. Copy the overlay, then set image, repo, `MCP_ALLOWED_HOSTS` and `secrets.env` |
| Azure Container Apps, AWS App Runner/ECS, a VM | Run the image with the env vars below. Ingress on port 8080, health check `/readyz` |

```sh
podman run -d -p 8080:8080 --read-only --tmpfs /tmp \
  -e DOCS_REPO_URL=https://dev.azure.com/ORG/PROJECT/_git/dev-handbook \
  -e DOCS_DIR=src/content/docs -e DOCS_NAME=dev-handbook \
  -e DOCS_GIT_TOKEN_FILE=/run/secrets/pat -v ./pat:/run/secrets/pat:ro,z \
  -e MCP_AUTH_TOKEN=change-me -e MCP_ALLOWED_HOSTS=docs-mcp.example.internal \
  localhost/docs-mcp-rust:dev
```

### Two settings people trip over

- **`MCP_ALLOWED_HOSTS`** defaults to `localhost` only. Requests whose `Host`
  header is anything else get `403`. This is DNS-rebinding protection. Set it to
  every name clients use: the ingress host, and in Kubernetes the Service names.
  `*` turns the check off; only do that behind something else that checks.
- **`MCP_AUTH_TOKEN`** is off by default. On a shared network, set it. It guards
  `/mcp` and `/refresh`.

The full configuration list is in [SPEC.md §1](SPEC.md#1-configuration).

## Which implementation

Measured on one laptop, each container serving the dev-handbook (55 pages).
Most of every image is `git` and its dependencies:

| | Rust | Python | TypeScript |
|---|---|---|---|
| Image | 199 MB | 275 MB | 363 MB |
| Memory, idle after indexing | 9 MB | 66 MB | 54 MB |
| Start to ready | 0.7 s | 3.2 s | 1.0 s |
| Source (lines) | 1638 | 889 | 966 |
| SDK | rmcp 3.5 + axum | mcp 2.3 (`MCPServer`) + Starlette | @modelcontextprotocol/server 2.3 |

They are interchangeable on the wire. Choose the language your team will keep
fixing. Keep one. The other two have done their job once the choice is made,
and the conformance suite and spec stay useful either way.

## Repository layout

```
SPEC.md            the contract: config, HTTP, parsing, search, tool outputs
rust/ python/ typescript/
                   one implementation each, with a Containerfile
conformance/       black-box pytest suite run against any implementation
  golden.json      exact expected outputs for fixtures/docs
  compare.py       diff all implementations against a real docs tree
fixtures/docs/     small docs tree that exercises every rule in the spec
deploy/            compose.yaml, k8s/ (kustomize base + example overlay)
justfile           build, test, lint, run, compare, image
```

## Testing

```sh
just test            # 111 checks × 3 implementations
just compare         # every page, section and 180+ searches, diffed across all three
just lint            # clippy pedantic, ruff, tsc
```

To change behaviour:

1. Change `SPEC.md`.
2. Change the tests.
3. Change the Python implementation, run `just golden`, and review the diff in
   `conformance/golden.json`.
4. Port the change to the other two until `just test` is green.

## Limits, on purpose

- **Keyword search (BM25), not embeddings.** No model to host, results are
  explainable, and it ranks the right page first for 18 of 23 hand-written test
  queries on the handbook. It does not understand synonyms, or English
  questions about Norwegian text.
- **One source per server.** For several handbooks, run several containers.
- **Tools only, no MCP resources or prompts.** The clients this is for use tools.
- **HTML entities** such as `&nbsp;` are passed through as written.

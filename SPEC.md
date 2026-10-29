# docs-mcp specification

The contract the Rust and Python implementations both follow. The
conformance suite in `conformance/` checks it against each one. When this file
and an implementation disagree, the implementation has the bug.

The guiding rule: **the server knows about Markdown, not about the handbook.**
Pages, sections, metadata fields, rule numbers and filter values are all
discovered from the content. Adding a page, a frontmatter field, a value or a
rule needs no change here or in any implementation.

## 1. Configuration

Everything is set with environment variables. A variable ending in `_FILE`
reads the value from that file instead, trimmed, for secrets mounted as files
(Kubernetes, Docker secrets). When both are set, `_FILE` wins.

| Variable | Default | Meaning |
|---|---|---|
| `DOCS_REPO_URL` | | Git URL to clone: `https://`, `ssh://`, `git@host:path`, `file://` or a local repo path |
| `DOCS_LOCAL_PATH` | | A local directory to serve as-is, without git. Exactly one of this and `DOCS_REPO_URL` must be set |
| `DOCS_REF` | remote default branch | Branch or tag to follow |
| `DOCS_DIR` | `.` | Directory inside the repo (or local path) that holds the docs |
| `DOCS_INCLUDE` | `**/*.md,**/*.mdx` | Comma-separated globs, relative to `DOCS_DIR` |
| `DOCS_EXCLUDE` | | Comma-separated globs, relative to `DOCS_DIR` |
| `DOCS_IGNORE_FIELDS` | see §4.2 | Comma-separated frontmatter keys that are presentation, not metadata. Setting it replaces the default list |
| `DOCS_REFRESH_SECONDS` | `300` | How often to pull (git) or re-read (local). `0` turns it off |
| `DOCS_GIT_TIMEOUT_SECONDS` | `120` | How long one git command may run before it is killed (§3.1). `0` turns the limit off |
| `DOCS_NAME` | `docs` | Display name, used in the server instructions and `docs_overview` |
| `DOCS_LANGUAGE` | | The language the docs are written in, as words a model understands: `Norwegian Nynorsk`, `English`. Tells clients which language to search in (§6) |
| `DOCS_SITE_URL` | | Base URL of the rendered site. When set, results carry a `url` (§4.6) |
| `DOCS_GIT_USERNAME` | `git` | Username for HTTPS token auth |
| `DOCS_GIT_TOKEN` | | Token or PAT for HTTPS auth (`_FILE` supported) |
| `DOCS_GIT_SSH_KEY_FILE` | | Private key for SSH auth |
| `DOCS_GIT_KNOWN_HOSTS_FILE` | | `known_hosts` for SSH. Unset means `StrictHostKeyChecking=accept-new` |
| `DOCS_CACHE_DIR` | `<system temp>/docs-mcp` | Where the clone lives. Wiped at startup |
| `MCP_HOST` | `0.0.0.0` | Listen address |
| `MCP_PORT` | `8080` | Listen port |
| `MCP_AUTH_TOKEN` | | When set, `/mcp` and `/refresh` require `Authorization: Bearer <token>` (`_FILE` supported) |
| `MCP_ALLOWED_HOSTS` | `localhost,127.0.0.1,::1` | Hostnames accepted in the `Host` header on `/mcp` and `/refresh`. `*` accepts any |

A missing or contradictory source configuration exits with status 2 and a
message naming the variable.

## 2. HTTP surface

| Endpoint | Auth | Host check | Behaviour |
|---|---|---|---|
| `/mcp` | yes | yes | MCP Streamable HTTP, stateless. Serves the 2026-07-28 revision and earlier revisions |
| `GET /healthz` | no | no | Always `200 {"status":"ok"}` once the process listens. Liveness |
| `GET /readyz` | no | no | `200` with the status object once an index is loaded, `503` with it before. Readiness |
| `POST /refresh` | yes | yes | Runs a sync now and waits for it. `200` with the status object, or `500` with it if the sync failed. For webhooks |

The status object:

```json
{"ready": true, "pages": 55, "commit": "e44bdc1…", "synced_at": "2026-10-15T17:00:00Z", "last_error": null}
```

`commit` is `null` for a local source. `synced_at` is the last successful sync,
UTC, second precision, `Z` suffix.

**Auth.** When `MCP_AUTH_TOKEN` is set, a request without a matching
`Authorization: Bearer <token>` gets `401` with `WWW-Authenticate: Bearer`. The
comparison is constant-time.

**Host check.** The hostname part of the `Host` header (port stripped,
case-insensitive, IPv6 brackets stripped) must be in `MCP_ALLOWED_HOSTS`, or the
request gets `403`. It protects against DNS rebinding. The SDKs' own host checks
are turned off so both behave the same. Health endpoints skip it, because
Kubernetes probes use the pod IP.

The host check runs before auth, so a bad host gets `403` even with a valid
token. The `403` body names `MCP_ALLOWED_HOSTS`, and the first refusal of each
host name (up to 100) is logged with that name, so an operator can see what to
add.

**Visible defaults.** At startup, each of these is logged as a warning:

- `MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are
  accepted. Set it to the host names clients use.`
- `MCP_ALLOWED_HOSTS is '*': the Host header is not checked.`
- `MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.`

## 3. Source sync

### 3.1 Git

- **Startup:** wipe `DOCS_CACHE_DIR`, then
  `git clone --depth 1 --single-branch [--branch <REF>] -- <URL> <CACHE>/repo`.
- **Refresh:** `git fetch --depth 1 origin <REF or HEAD>`, then
  `git reset --hard FETCH_HEAD`. The index is rebuilt only when `git rev-parse HEAD` changed.
- **Environment** for every git process: the inherited environment plus
  - `GIT_TERMINAL_PROMPT=0`.
  - `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.followRedirects`,
    `GIT_CONFIG_VALUE_0=false`. git never follows a redirect. When git follows
    one, it sends the token header to wherever the redirect points. A repo that
    has moved fails the sync with git's error, which names the status (`301`,
    `302`). Point `DOCS_REPO_URL` at the new address.
  - With a token: `GIT_CONFIG_COUNT=2`,
    `GIT_CONFIG_KEY_1=http.<scheme>://<host[:port]>/.extraHeader`,
    `GIT_CONFIG_VALUE_1=Authorization: Basic <base64(username:token)>`. The header
    goes only to the repo's own host, and never appears in argv, the URL or `.git/config`.
  - With an SSH key: `GIT_SSH_COMMAND=ssh -i <key> -o IdentitiesOnly=yes`
    plus either `-o UserKnownHostsFile=<file> -o StrictHostKeyChecking=yes` or
    `-o StrictHostKeyChecking=accept-new`.

  This works the same for Azure DevOps, Forgejo/Gitea, GitHub, GitLab and plain
  SSH servers, because all of them accept HTTP Basic with a token as the password.
- **Failure:** a git command that exits non-zero fails the sync with
  `git <command> failed: <its stderr, trimmed>`, where `<command>` is `clone`,
  `fetch`, `reset` or `rev-parse`.
- **Timeout:** a git command still running after `DOCS_GIT_TIMEOUT_SECONDS` is
  killed together with every process it started. git hands the network to
  helpers (`git-remote-https`, `ssh`), and a helper left running keeps the
  connection open. The sync fails with
  `git <command> timed out after <N> seconds`, and the clone is deleted so the
  next sync clones afresh: a killed git can leave lock files behind that would
  make every later fetch fail.

### 3.2 Local

Every refresh re-reads the directory and rebuilds the index. `commit` is `null`.

### 3.3 Scheduling and failure

- The HTTP server starts listening before the first sync.
- Until the first successful sync, it retries every 10 seconds. After that, it
  syncs every `DOCS_REFRESH_SECONDS`.
- A sync that fails keeps serving the previous index and records `last_error`.
- A successful sync clears `last_error`.
- Building the new index happens off to the side and is swapped in atomically.
- Syncs never overlap. `/refresh` during a sync waits for that sync to finish,
  then runs its own.
- Tools called before the first index exists return a tool error:
  `Index not ready yet` plus the last error, if any.
- Display copies of the repo URL have any `user:password@` part removed.

## 4. Content model

### 4.1 Discovery

- The root is `<repo or local path>/<DOCS_DIR>`.
- The walk skips directories named `node_modules` or starting with `.`.
- **Links are never followed.** The walk skips symbolic links, to files and to
  directories alike, so every page is a regular file inside the root. A link
  committed to the repo could otherwise point at a file outside it, such as
  `/proc/self/environ` or a mounted secret, and serve it to every client.
- The root itself may be reached through a link, but it must resolve to a
  directory inside the repo (or the local path). Otherwise the sync fails with
  `docs directory is outside the source: <DOCS_DIR>`.
- A file is a page when its root-relative path (with `/` separators) matches an
  include glob and no exclude glob.
- Glob syntax:
  - `**/` matches zero or more whole directories.
  - `**` matches anything.
  - `*` matches within one segment.
  - `?` matches one non-`/` character.
  - Everything else is literal.
- Pages are kept sorted by path in code-point order.
- Files are read as UTF-8, with invalid bytes replaced.

### 4.2 Frontmatter and metadata

- **Frontmatter detection:** the file starts with a `---` line (after an
  optional BOM), and the frontmatter ends at the next line that is exactly
  `---` (trailing `\r` and spaces ignored).
- **Parsing:** the frontmatter is parsed as YAML 1.2 (core schema: only
  `true`/`false` are booleans, dates stay strings). Invalid YAML (including a
  mapping with duplicate keys) or anything other than a mapping counts as no
  frontmatter, but the body still starts after
  the closing line.
- **`title`** (string) and **`description`** (string) are pulled out as the
  page title and description.
- **Metadata** is every other top-level key whose value is a scalar or a list of
  scalars:
  - Scalars become strings: integers in decimal, other numbers in their
    shortest form, booleans as `true`/`false`.
  - A scalar stays a string. A list stays a list of strings.
  - Nested mappings and `null` are dropped, as are list items that are not
    scalars.
  - Key order is preserved.
- **Ignored keys:** keys in `DOCS_IGNORE_FIELDS` are dropped. The default list
  holds the presentation keys of Starlight, Docusaurus, VitePress and MkDocs:

```
layout,template,hero,sidebar,sidebar_position,sidebar_label,sidebar_class_name,slug,id,pagefind,editUrl,lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,outline,aside,navbar,footer,pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,toc_min_heading_level,toc_max_heading_level
```

### 4.3 Title

The title is chosen in this order:

1. Frontmatter `title`.
2. The text of the first level-1 ATX heading in the cleaned body.
3. The file name without its extension. For `index`, the parent directory's
   name, or `DOCS_NAME` for the root index.

### 4.4 Cleaning

The body (after frontmatter) is turned into plain Markdown that reads like the
rendered page.

1. **Code fences** (a line starting with optional indent and 3+ `` ` `` or `~`)
   open a code block. The block closes at a line with the same character
   repeated at least as many times. Code is never changed. Everything below
   applies to prose only.
2. **MDX module lines:** in `.mdx` files, prose lines matching
   `^\s*(import|export)\s` are removed.
3. **Protected inline code:** inline code (`` `…` `` on one line) is protected
   from steps 4 and 5.
4. **JSX comments:** `{/* … */}` is removed, even across lines.
5. **Tags:** every tag matching `</?[A-Za-z][A-Za-z0-9._:-]*(\s[^<>]*)?/?>`, even
   across lines, is handled as follows:
   - An opening tag with a `label="…"` or `title="…"` attribute (single or double
     quotes, `label` first) becomes `**<value>**`.
   - Every other tag is removed and its inner text kept.

   Rule badges such as `<Rule level="must">MÅ</Rule>` become `MÅ`. Tabs become
   bold labels.
6. **Heading attributes:** a trailing heading attribute `{ #id … }` (spaces
   optional) is removed from the heading line and recorded as the heading's
   explicit anchor.
7. **Whitespace:** trailing whitespace is trimmed on every prose line, runs of
   blank prose lines are collapsed to one, and blank lines (code or prose,
   whitespace-only counts as blank) at the start and end are removed.

### 4.5 Sections and anchors

- **Headings** are ATX headings (`#`–`######` followed by a space) outside code.
  Heading text is the cleaned text:
  - Trailing `#`s are removed.
  - `[text](url)` becomes `text`.
  - `` ` ``, `*` and `_` around words stay in the text.
- **Section extent:**
  - A section runs from its heading line to just before the next heading of the
    same or a higher level, so it includes its subsections.
  - Text before the first heading is the **intro**: level 0, no heading, no
    anchor.
- **Anchors:** the explicit anchor if present. Otherwise the slug, which is the
  GitHub algorithm:
  1. Take the heading text with `[text](url)` reduced to `text`.
  2. Lowercase it.
  3. Remove every character that is not a Unicode letter, a Unicode number, a
     space, `-` or `_`.
  4. Trim it.
  5. Replace each space with `-`.

  A slug seen earlier in the same page gets `-1`, `-2`, … appended. Explicit
  anchors are taken as written.
- **Anchor matching** (in `read_doc` and `get_section`) is case-insensitive and
  ignores a leading `#`.

### 4.6 URLs

When `DOCS_SITE_URL` is set, a page's URL is built in four steps:

1. Strip the extension from the page path.
2. Drop a final `index` segment.
3. Join the result to the site URL with exactly one `/` between them.
4. End it with `/`.

For example, `rust/rules.mdx` → `https://host/handbook/rust/rules/`, and
`index.mdx` → `https://host/handbook/`. A section URL appends `#<anchor>`.

## 5. Search

Search runs over **sections**: every headed section, plus the intro when it
has any non-whitespace text.

**Tokens.** Lowercase the text, take maximal runs of Unicode letters and
numbers, and drop tokens shorter than 2 characters. There are no stop words and
no stemming, so nothing is tied to one language. `RS-203` becomes `rs`, `203`.

**Weighted fields.** Each field's tokens are counted with a weight, and the
weighted counts are summed into one term-frequency map per section. The sum of
all weights is the section length.

| Field | Weight |
|---|---|
| page path, without its extension | 3 |
| page title | 2 |
| section heading (none for the intro) | 3 |
| all metadata values, and the description | 1 |
| section body | 1 |

The section body is the section's own text, without its heading line and
without its subsections' text, and with every `[text](url)` reduced to `text`:
a link's URL names another page, not this one. The intro's body is the text
before the first heading.

The page path counts because file names are often the plainest description of
a page, and are often in English even when the text is not
(`virtual-environments.mdx`).

**Query terms.** The query is tokenized the same way and duplicates are
removed. Each query term `q` matches:

- the vocabulary term `q` itself, with factor 1;
- when `q` has at least 3 characters, every other vocabulary term that starts
  with `q`, with factor 0.5;
- when `q` has at least 6 characters, every vocabulary term not matched above
  that starts with the same character and is one edit away (Levenshtein
  distance 1: one character inserted, deleted or replaced), with factor 0.3.

The last rule bridges spelling variants such as Nynorsk and Bokmål
(`sikkerheit`/`sikkerhet`, `reglar`/`regler`) and typos, without knowing any
language.

The term frequency in section `d` is the sum of the factors times the term
frequencies. The document frequency is the number of sections containing any
matched term.

**Score.** BM25 with `k1 = 1.2` and `b = 0.75`. `N` is all sections and
`avgdl` is the mean section length:

```
idf(q)  = ln(1 + (N - df + 0.5) / (df + 0.5))
score   = Σ idf(q) · tf·(k1+1) / (tf + k1·(1 - b + b·len/avgdl))
```

**Results.**

- Filters and `path_prefix` are applied after scoring.
- Sections scoring 0 are dropped.
- Results are sorted by score descending, then by path, then by section order.
- At most 3 results come from any one page.
- The first `limit` results are returned.
- `score` is rounded to 3 decimals.

**Snippet.**

1. Collapse whitespace in the section body as search sees it (links reduced to
   their text), or the heading if the body is empty, to single spaces.
2. Find the first case-insensitive occurrence of any matched term.
3. Start 80 characters before it (or at 0) and take 240 characters.
4. Prefix `…` when not starting at 0. Suffix `…` when cut.

Characters are Unicode code points.

**Filters.** `filters` maps a metadata field to a string or a list of strings.
A page passes when, for every given field, its value(s) and the requested
values share at least one value, compared case-insensitively. A page without
the field fails. `path_prefix` keeps pages whose path starts with it.

## 6. MCP tools

All tools are read-only (`readOnlyHint: true`, `openWorldHint: false`) and
return a single text content block. JSON output is compact UTF-8. A failure is
a tool error (`isError: true`) whose text explains what to do instead.

**Language.** Users ask in whatever language they speak, but the client model
writes the search query. So the server tells the model which language to search
in. In the texts below:

- `{lang}` is `DOCS_LANGUAGE`, or `the language of the documentation` when it is
  not set;
- `{written}` is `, written in <DOCS_LANGUAGE>`, or nothing when it is not set;
- `{name}` is `DOCS_NAME`.

**Server instructions:**

> Documentation for {name}{written}. Use search_docs to find what is relevant to
> a question, then read_doc to read a page or one of its sections. Search with
> keywords in {lang}, even when the user writes in another language, and answer
> in the user's language. get_section looks up a heading anchor or ID, such as a
> rule number, across all pages. docs_overview lists the metadata fields and
> values you can filter on; list_docs browses pages. Cite pages by path, or by
> url when present.

**Descriptions.** Every implementation uses this exact text:

| Tool or parameter | Description |
|---|---|
| `docs_overview` | Overview of the documentation: source and commit, language, page count, folders, and every metadata field with its values and page counts. Call it to learn which filters exist. |
| `list_docs` | List pages with their path, title, url and metadata, optionally narrowed by path prefix and metadata filters. |
| `search_docs` | Full-text search over every section of every page. Use a few distinctive keywords in {lang}, whatever language the user writes in, rather than a full sentence. Returns the best-matching sections with path, heading, anchor, url and a snippet; follow up with read_doc(path, section=anchor) for the full text. |
| `read_doc` | Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading text) to read just that section and its subsections. |
| `get_section` | Look up a section by its heading anchor or ID across all pages, for example a rule number such as 'RS-203'. |
| `query` | Keywords to search for, in {lang}. Add spelling variants, synonyms and English terms freely: keywords that do not occur are ignored. Case-insensitive; words of 3+ letters also match longer words that start with them, and words of 6+ letters also match spellings one letter off. |
| `filters` | Metadata filters: field name to a value or a list of values (any of them matches). docs_overview lists the fields and their values. |
| `path_prefix` | Only pages whose path starts with this, e.g. 'python/'. |
| `limit` | Maximum number of results, 1-50. Default 10. |
| `path` | Page path as returned by search_docs or list_docs, e.g. 'rust/rules.mdx'. The extension may be left out. |
| `section` | Optional anchor (e.g. 'rs-201') or heading text. |
| `anchor` | The anchor or ID, e.g. 'rs-203'. Case-insensitive. |

### `docs_overview()`

```json
{"name":"dev-handbook",
 "language":"Norwegian Nynorsk",
 "source":{"type":"git","repo":"https://…","ref":"main","dir":"src/content/docs","commit":"…","synced_at":"…"},
 "page_count":55,
 "folders":[{"path":".","pages":2},{"path":"python","pages":30}],
 "fields":{"innhaldstype":{"krav":6,"rettleiing":38}}}
```

- **`source`:** for a local source, `source` is
  `{"type":"local","path":…,"dir":…,"commit":null,"synced_at":…}`.
- **`language`:** `DOCS_LANGUAGE`, or `null` when unset.
- **`ref`:** `null` when unset.
- **`folders`:** each page's first path segment, with `.` for pages at the root,
  sorted.
- **`fields`:** for each metadata field, each value and the number of pages that
  carry it. Both levels are sorted.

### `list_docs(path_prefix?, filters?)`

```json
{"count":2,"pages":[{"path":"rust/rules.mdx","title":"Rust-reglane","description":"…","url":"…","metadata":{"innhaldstype":"krav","tema":["testing"]}}]}
```

`description` and `url` are left out when absent.

### `search_docs(query, filters?, path_prefix?, limit?)`

`limit` defaults to 10 and is clamped to 1–50. A query without tokens is a tool
error.

```json
{"query":"clippy","count":1,"results":[{"path":"rust/rules.mdx","title":"Rust-reglane","section":"RS-201: clippy pedantic, åtvaringar avviste i CI","anchor":"rs-201","url":"…#rs-201","score":7.412,"snippet":"…"}]}
```

For an intro, `section` is the page title and `anchor` is `null`. `url` is left
out when absent.

### `read_doc(path, section?)`

**Path lookup.**

1. Strip leading and trailing `/` from the path.
2. Try an exact match, then the path with `.md`, `.mdx`, `/index.md` and
   `/index.mdx` appended.
3. Not found is a tool error that names up to 5 pages whose path contains the
   last segment of the request (case-insensitive).

**Section lookup.**

- `section` is matched against the page's anchors first, then against heading
  texts, both case-insensitively.
- Not found is a tool error that lists the page's anchors.

**Output.** Text with a header followed by the cleaned body, or by just the
section (trimmed), starting at its heading line. There is no trailing newline:

```
---
path: rust/rules.mdx
title: Rust-reglane
url: https://…/rust/rules/#rs-201
section: rs-201
innhaldstype: krav
tema: kodekvalitet, arkitektur
---

### RS-201: clippy pedantic, åtvaringar avviste i CI

…
```

Header lines, in order:

Values have whitespace runs collapsed to one space.

1. `path`
2. `title`
3. `description` (if any)
4. `url` (if any, with `#anchor` when a section was asked for)
5. `section` (the anchor, if one was asked for)
6. The metadata in frontmatter order, with lists joined by `, `.

### `get_section(anchor)`

Finds sections whose anchor matches, across all pages.

- **Explicit anchors win:** when any explicit anchor matches, slug matches are
  ignored.
- **Exactly one match:** returns exactly what `read_doc(path, anchor)` would.
- **Several matches:** returns plain text, not an error:

  ```
  Anchor 'x' matches 3 sections. Read one with read_doc(path, section):
  - a.md#x (Heading A)
  - b.md#x (Heading B)
  ```

- **No match:** a tool error that suggests `search_docs`.

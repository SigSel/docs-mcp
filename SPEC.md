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
| `DOCS_SOURCES` | | Several sources, as YAML (§7). Replaces `DOCS_REPO_URL`, `DOCS_LOCAL_PATH`, `DOCS_REF`, `DOCS_DIR`, `DOCS_INCLUDE`, `DOCS_EXCLUDE`, `DOCS_IGNORE_FIELDS`, `DOCS_LANGUAGE` and `DOCS_SITE_URL` |
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
| `MCP_AUTH_TOKEN` | | Shared-token auth: `/mcp` requires `Authorization: Bearer <token>` (§2.1, `_FILE` supported) |
| `MCP_OAUTH_ISSUER` | | OAuth auth: the issuer of the access tokens clients send, for example `https://login.microsoftonline.com/<tenant-id>/v2.0` (§2.2) |
| `MCP_OAUTH_AUDIENCE` | `MCP_PUBLIC_URL` | The audience access tokens must carry. For Entra ID, the app registration's client ID |
| `MCP_OAUTH_SCOPES` | | Comma-separated scopes access tokens must carry, and clients are told to ask for |
| `MCP_PUBLIC_URL` | | The `/mcp` URL clients use, for example `https://docs-mcp.example.internal/mcp`. Required with OAuth |
| `MCP_REFRESH_TOKEN` | | When set, `/refresh` requires `Authorization: Bearer <this>` instead of `MCP_AUTH_TOKEN` (§2.1, `_FILE` supported) |
| `MCP_ALLOWED_HOSTS` | `localhost,127.0.0.1,::1` | Hostnames accepted in the `Host` header on `/mcp`, `/refresh` and the OAuth metadata. `*` accepts any |

A missing or contradictory source configuration exits with status 2 and a
message naming the variable. So does each of these:

- `MCP_AUTH_TOKEN` and `MCP_OAUTH_ISSUER` both set. A shared token would let
  anyone who has it past the login.
- `MCP_OAUTH_ISSUER` set without `MCP_PUBLIC_URL`.
- `MCP_PUBLIC_URL` or `MCP_OAUTH_ISSUER` not an `https://` URL with a host.
  `http://` is accepted only for the hosts `localhost`, `127.0.0.1` and `[::1]`,
  for development.
- `MCP_PUBLIC_URL` with a query or a fragment.

## 2. HTTP surface

docs-mcp speaks plain HTTP. TLS belongs in front of it, in an ingress or the
platform, which is why OAuth insists on `https://` URLs.

| Endpoint | Auth | Host check | Behaviour |
|---|---|---|---|
| `/mcp` | yes | yes | MCP Streamable HTTP, stateless. Serves the 2026-07-28 revision and earlier revisions |
| `GET /healthz` | no | no | Always `200 {"status":"ok"}` once the process listens. Liveness |
| `GET /readyz` | no | no | `200` with the status object once an index is loaded, `503` with it before. Readiness |
| `POST /refresh` | refresh | yes | Runs a sync now and waits for it. `200` with the status object, or `500` with it if the sync failed. For webhooks (§2.1) |
| `GET /.well-known/oauth-protected-resource…` | no | yes | With OAuth only: the protected resource metadata (§2.2) |

The status object:

```json
{"ready": true, "pages": 55, "commit": "e44bdc1…", "synced_at": "2026-10-15T17:00:00Z", "last_error": null}
```

`commit` is `null` for a local source. `synced_at` is the last successful sync,
UTC, second precision, `Z` suffix.

**Auth.** `/mcp` has one of three modes:

- **None**, when neither `MCP_AUTH_TOKEN` nor `MCP_OAUTH_ISSUER` is set.
- **Shared token**, with `MCP_AUTH_TOKEN` (§2.1).
- **OAuth**, with `MCP_OAUTH_ISSUER` (§2.2).

**Host check.** The hostname part of the `Host` header (port stripped,
case-insensitive, IPv6 brackets stripped) must be in `MCP_ALLOWED_HOSTS`, or the
request gets `403`. It protects against DNS rebinding. It applies to `/mcp`,
`/refresh` and the OAuth metadata. The SDKs' own host checks are turned off so
both behave the same. Health endpoints skip it, because Kubernetes probes use
the pod IP.

The host check runs before auth, so a bad host gets `403` even with a valid
token. The `403` body names `MCP_ALLOWED_HOSTS`, and the first refusal of each
host name (up to 100) is logged with that name, so an operator can see what to
add.

**Visible defaults.** At startup, each of these is logged as a warning:

- `MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are
  accepted. Set it to the host names clients use.`
- `MCP_ALLOWED_HOSTS is '*': the Host header is not checked.`
- `MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.`
  Only when `MCP_OAUTH_ISSUER` is not set either.
- `MCP_REFRESH_TOKEN is not set: /refresh is off.` With OAuth only.

### 2.1 Shared token

With `MCP_AUTH_TOKEN` set, a request to `/mcp` without a matching
`Authorization: Bearer <token>` gets `401` with `WWW-Authenticate: Bearer` and
the body `Unauthorized`. Every token comparison is constant-time.

`/refresh` is for webhooks, which cannot log in. It needs
`Authorization: Bearer <MCP_REFRESH_TOKEN>`, or `MCP_AUTH_TOKEN` when
`MCP_REFRESH_TOKEN` is not set, and refuses anything else the same way. With
neither set, `/refresh` is open in auth mode none, and off with OAuth: `403`
with a body that names `MCP_REFRESH_TOKEN`.

### 2.2 OAuth

With `MCP_OAUTH_ISSUER` set, docs-mcp is an OAuth resource server, as the MCP
authorization specification describes. A client gets an access token from the
issuer, through the user's browser, and sends it with every request to `/mcp`.
docs-mcp checks the token and keeps nothing. Every valid token gets the same
read-only access: who may get one is decided at the issuer.

**Protected resource metadata** (RFC 9728) is served at
`/.well-known/oauth-protected-resource` followed by the path of
`MCP_PUBLIC_URL` without a trailing `/`, and at
`/.well-known/oauth-protected-resource` itself. For
`MCP_PUBLIC_URL=https://docs.example/mcp`, that is
`/.well-known/oauth-protected-resource/mcp`:

```json
{"resource":"https://docs.example/mcp","authorization_servers":["https://login.microsoftonline.com/<tenant-id>/v2.0"],"scopes_supported":["api://docs-mcp/docs.read"],"bearer_methods_supported":["header"],"resource_name":"dev-handbook"}
```

`resource` is `MCP_PUBLIC_URL` and `authorization_servers` holds
`MCP_OAUTH_ISSUER`, both exactly as configured. `scopes_supported` is left out
when `MCP_OAUTH_SCOPES` is empty. `resource_name` is `DOCS_NAME`.

**Refusals.** The metadata URL is the scheme and authority of `MCP_PUBLIC_URL`
followed by the metadata path above. A refused request to `/mcp` gets:

| Request | Status | Body |
|---|---|---|
| No `Authorization: Bearer` header | `401` | `Unauthorized` |
| A token that fails a check below | `401` | `Unauthorized` |
| A valid token without a required scope | `403` | `Forbidden` |

with the header
`WWW-Authenticate: Bearer resource_metadata="<url>"`, then
`, scope="<MCP_OAUTH_SCOPES, space-separated>"` when scopes are set, then
`, error="invalid_token", error_description="<reason>"` for a refused token or
`, error="insufficient_scope", error_description="missing scope <scope>"` for a
missing scope.

**Token checks**, in this order. The first that fails gives the reason:

1. The token has three parts separated by `.`, each base64url without padding.
   The first two decode to JSON objects, and the first has a string `kid` and
   a string `alg`. Otherwise: `malformed token`.
2. `alg` is one of `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`,
   `ES256` and `ES384`. Otherwise: `unsupported algorithm`. `none` and the HMAC
   algorithms are never accepted.
3. The issuer's key set has a key with that `kid` that fits `alg`: `kty` `RSA`
   for `RS*` and `PS*`, `EC` with `crv` `P-256` for `ES256` and `P-384` for
   `ES384`, its `alg` (if any) equal to the token's, and its `use` (if any)
   `sig`. Otherwise: `unknown signing key`.
4. The signature verifies with that key. Otherwise: `bad signature`.
5. `iss` equals `MCP_OAUTH_ISSUER`. Otherwise: `wrong issuer`.
6. `aud`, a string or a list of strings, holds `MCP_OAUTH_AUDIENCE`.
   Otherwise: `wrong audience`.
7. `exp` is a number, and now is before `exp` plus 60 seconds. Otherwise:
   `expired`.
8. `nbf`, if present, is a number no later than now plus 60 seconds.
   Otherwise: `not yet valid`.
9. Every scope in `MCP_OAUTH_SCOPES` is among the token's scopes. Otherwise
   `403`, naming the first scope missing. The token's scopes are its `scp`
   claim (Entra ID), or else its `scope` claim: a space-separated string or a
   list of strings. Two scopes match when the parts after their last `/` are
   equal, because Entra ID puts `docs.read` in `scp` for the scope
   `api://docs-mcp/docs.read`.

**Signing keys.** docs-mcp fetches
`<MCP_OAUTH_ISSUER>/.well-known/openid-configuration` (without doubling a
trailing `/`), checks that its `issuer` equals `MCP_OAUTH_ISSUER`, and fetches
the key set from its `jwks_uri`. It does so:

- when a token first needs a key;
- when the key set is more than an hour old, so a key the issuer withdraws
  stops being trusted;
- when a token names a `kid` the key set lacks. This reason fetches at most
  once a minute, so tokens with made-up `kid`s cannot keep docs-mcp fetching.

Fetches use the system's CA certificates, time out after 10 seconds, follow no
redirects, and refuse responses over 1 MB. Like the issuer, `jwks_uri` must be
`https://` unless its host is local. A failed fetch keeps the old key set, is
logged, and is tried again 10 seconds later at the earliest, so an issuer that
is down does not hold up every request. With no key set at all, the request
gets `503` with the body
`Service Unavailable: cannot reach the OAuth issuer`.

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

Everything in this section and in §5 reads files that anyone who can push to a
source wrote. **No page may cost much more to parse and index than it is
long**, in time or memory, whatever it holds. The rules below are written so a
linear-time implementation exists, and both are one: the Python implementation
finds JSX comments, heading attributes and closing hashes without regexes,
because Python's backtracking made them quadratic.
`conformance/test_page_cost.py` holds pages that once broke this rule.

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
- **Files that usually hold secrets are never pages**, whatever the globs say:
  names that are `.env` or start with `.env.`, and names ending in `.pem`,
  `.key`, `.p12` or `.pfx`, in any case. This is a safety net, not a guarantee: anything
  else committed to a source reaches every client.
- **A file over 1,000,000 bytes, or with a NUL byte in it, is skipped:** too
  big to be one useful result, or not text.
- Pages are kept sorted by path in code-point order.
- Files are read as UTF-8, with invalid bytes replaced.
- A file whose extension is `.md` or `.mdx`, in any case, is a **Markdown page**
  (§4.2–4.6). Any other file is a **file page** (§4.7).

### 4.2 Frontmatter and metadata

- **Frontmatter detection:** the file starts with a `---` line (after an
  optional BOM), and the frontmatter ends at the next line that is exactly
  `---` (trailing `\r` and spaces ignored).
- **Parsing:** the frontmatter is parsed as YAML 1.2 (core schema: only
  `true`/`false` are booleans, dates stay strings). Invalid YAML (including a
  mapping with duplicate keys) or anything other than a mapping counts as no
  frontmatter, but the body still starts after
  the closing line.
- **Limits:** frontmatter that uses an alias (`*name`), or nests collections
  more than 32 deep (the top-level mapping is 1), also counts as invalid. It
  is rejected before it is built: a few aliases can expand 400 bytes into
  gigabytes, and deep nesting can exhaust a parser's stack. Anchors (`&name`)
  without aliases are fine.
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
File pages have no URL: the site renders only the Markdown.

### 4.7 File pages

Any file that is not Markdown, such as a pipeline template, a `pyproject.toml`
or a `Dockerfile`, is served as it is written, so the model sees exactly what
is in the repo.

- **Text:** a leading BOM is removed and `\r\n` becomes `\n`, as for Markdown.
  Nothing else changes: no frontmatter, no cleaning.
- **Title:** the file name, such as `build.yml`.
- **No description, no metadata, no headings.** The page has one section, the
  intro, which holds the whole text. Metadata filters never match it.
- **Search** sees the text as it is (links are not reduced).
- **`read_doc`** returns the header, a blank line, and the text in a fenced
  code block:
  - The fence is backticks, one more than the longest run of backticks in the
    text, and at least three.
  - Its info string is the file's extension in lowercase, without the dot, or
    the whole file name in lowercase when there is no extension:
    `build.yml` → `yml`, `Dockerfile` → `dockerfile`.
  - The text ends with a newline before the closing fence, added if it has
    none.

  ````
  ---
  path: templates/build.yml
  title: build.yml
  ---

  ```yml
  parameters:
    - name: pythonVersion
  …
  ```
  ````

  Asking for a `section` of a file page is the usual tool error, with no
  anchors to list.

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

## 7. Several sources

One docs-mcp can serve several **sources**: the handbook, a repo of pipeline
templates, a project template, each with its own repo, folder and globs.

### 7.1 Configuration

`DOCS_SOURCES` (or `DOCS_SOURCES_FILE`) is a YAML list. Each item is a source:

```yaml
- name: handbook
  description: The dev handbook. Rules and guidance for Rust, Python and our workflow.
  repo: https://dev.azure.com/ORG/PROJECT/_git/dev-handbook
  dir: src/content/docs
  language: Norwegian Nynorsk
- name: ci-templates
  description: Azure DevOps pipeline templates. Use them when writing or reviewing azure-pipelines.yml.
  repo: https://dev.azure.com/ORG/PROJECT/_git/ci-templates
  include: ["**/*.yml", "**/*.yaml", "**/*.md"]
```

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Lowercase letters and digits, words joined by single `-`. Unique. Tools and results use it |
| `description` | yes | What the source is, and when to use it. The model reads it |
| `repo` | one of `repo` and `path` | As `DOCS_REPO_URL` |
| `path` | | As `DOCS_LOCAL_PATH` |
| `ref`, `dir`, `language`, `site_url` | no | As `DOCS_REF`, `DOCS_DIR`, `DOCS_LANGUAGE`, `DOCS_SITE_URL` |
| `include`, `exclude`, `ignore_fields` | no | As `DOCS_INCLUDE`, `DOCS_EXCLUDE`, `DOCS_IGNORE_FIELDS`: a comma-separated string, or a list of strings |

Values are strings, apart from the three lists. Defaults are those of the
variables. Everything else is shared by all sources: the git credentials and
timeout, `DOCS_REFRESH_SECONDS`, `DOCS_CACHE_DIR`, `DOCS_NAME` (the server's
name) and every `MCP_*` setting. One git account therefore reads every repo:
give it exactly those.

The source list is the operator's: it is never read from a source.

Exit with status 2 and a message naming `DOCS_SOURCES`, for:

- `DOCS_SOURCES` together with any of the variables it replaces:
  `DOCS_REPO_URL`, `DOCS_LOCAL_PATH`, `DOCS_REF`, `DOCS_DIR`, `DOCS_INCLUDE`,
  `DOCS_EXCLUDE`, `DOCS_IGNORE_FIELDS`, `DOCS_LANGUAGE`, `DOCS_SITE_URL`;
- YAML that does not parse, or is not a non-empty list of mappings;
- an unknown key, a value of the wrong type, a missing or invalid `name`, a
  repeated `name`, a missing `description`, or not exactly one of `repo` and
  `path`.

The message also names the source (by `name`, or by its position from 1) and
the key.

Without `DOCS_SOURCES`, the variables describe a single source, and nothing in
§7.3–7.4 applies: every output is exactly as in the sections before. With
`DOCS_SOURCES`, §7.3–7.4 apply even to a list of one.

### 7.2 Sync

- Each source is cloned to `<DOCS_CACHE_DIR>/<name>/repo`, and syncs on its own,
  as §3 describes: its own schedule, retries, `last_error`, and lock. One
  failing or slow source does not hold up another.
- When a source has new pages, the search index over all sources is rebuilt
  off to the side and swapped in.
- `/readyz` is `200` once every source has loaded once, so a deployment with a
  broken new source never takes over from a working one. Its status object:

  ```json
  {"ready": true, "pages": 82,
   "sources": [{"name": "handbook", "ready": true, "pages": 55, "commit": "…", "synced_at": "…", "last_error": null}, …]}
  ```

  `pages` at the top is the sum. The list follows the configured order.
- `POST /refresh` syncs every source at once and waits for all of them: `200`
  when all succeeded, `500` when any failed. `POST /refresh?source=<name>`
  syncs one. An unknown name gets `404`, with a body that names the sources.
- A tool called before any source has loaded returns `Index not ready yet.`,
  then ` Last error in <name>: <error>` for each source with an error. Until
  then, tools use the sources that have loaded.

### 7.3 Search and lookups

- Search scores the sections of all sources together: `N`, document
  frequencies and `avgdl` are over all of them, and query terms expand against
  all their words.
- Ties are broken by source, in configured order, before path.
- At most 3 results come from one page, a page being a source and a path.
- `source`, when given, keeps only that source, applied like `path_prefix`.
- `read_doc(path, section?, source?)` finds the path as §6 describes:
  - in `source`, when given;
  - otherwise in every source. Found in one, it is used. Found in several, it
    is a tool error: `'<path>' is in several sources: <a>, <b>. Pass source to
    read_doc.`
  - Not found, the error lists up to 5 similar paths from all sources as
    `<path> (<source>)`.
- `get_section(anchor)` looks across all sources. Several matches are listed as
  `- <source>: <path>#<anchor> (<heading>)`, after the line
  `Anchor '<anchor>' matches <n> sections. Read one with read_doc(path, section, source):`.
- A `source` that is not configured is a tool error:
  `No source '<x>'. Sources: <a>, <b>, <c>.` One that has not loaded yet:
  `Source '<x>' is not ready yet.`, plus ` Last error: <error>` if any.

### 7.4 Tools

**Server instructions:**

> Documentation and reference repositories for {name}, in {n} sources:
> - {source name} ({language}): {description}
> - …
>
> Use search_docs to find what is relevant to a question, then read_doc to read
> a page or file. search_docs searches every source unless you name one. Search
> with keywords in the language of the source you expect the answer in, even
> when the user writes in another language, and answer in the user's language.
> Files that are not Markdown come back as they are, in a code block.
> get_section looks up a heading anchor or ID, such as a rule number, across all
> sources. docs_overview lists each source's metadata fields and values you can
> filter on; list_docs browses a source. Cite pages by source and path, or by
> url when present.

The list has one line per source, in configured order, and ends with a blank
line; the paragraph after it is one line. A source without a `language` is
listed as `- {source name}: {description}`.

**Descriptions.** `{lang}` becomes `the language of the source you search`.
Three tools take one more parameter, `source`, which a single source does not
show:

| Tool | `source` description |
|---|---|
| `search_docs` | `Only this source. Leave it out to search all of them.` |
| `list_docs` | `Only this source. Leave it out to list all of them.` |
| `read_doc` | `The page's source, as search_docs and list_docs return it. Needed only when the path is in several sources.` |

**Outputs.** Every page, result and listing names its source first:

- `list_docs` and `search_docs`: each entry starts with `"source": "<name>"`.
- `read_doc` and `get_section`: the header starts with `source: <name>`.
- `docs_overview`:

  ```json
  {"name":"docs","sources":[{"name":"handbook","description":"…","language":"Norwegian Nynorsk","source":{"type":"git",…},"page_count":55,"folders":[…],"fields":{…}},…]}
  ```

  Each source is the single-source overview of §6 with its `description` after
  `name`. A source that has not loaded yet has `page_count` 0, no folders and
  no fields.

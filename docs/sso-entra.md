# Sign-in with Entra ID

How to let users reach docs-mcp by signing in with their work account,
instead of with a shared token. For the general idea, read
[What is MCP?](what-is-mcp.md) first. The exact rules are in
[SPEC.md §2.2](../SPEC.md#22-oauth).

**What you get:**

- **Nobody shares a secret.** Each user signs in as themselves, through Entra
  ID, with the same login and MFA as everything else.
- **Access is managed in Entra ID.** Assign the groups that may read the
  handbook. Remove someone, and they lose access when their current token runs
  out: Entra ID's access tokens last 60 to 90 minutes.
- **docs-mcp holds no user secrets.** It checks each request's access token
  against Entra ID's public signing keys, and keeps nothing.

**Before you start:** check [client support](#3-connect-the-clients). VS Code
is the client Microsoft documents for this. Copilot CLI and the JetBrains
Copilot plugin have open bugs with Entra ID sign-in, so test them before you
switch.

## How it fits together

1. The assistant app calls docs-mcp without a token, and gets `401` with a link
   to docs-mcp's metadata.
2. The metadata names Entra ID as the place to sign in, and the scope to ask for.
3. The app opens a browser. The user signs in with Entra ID, which checks that
   they are assigned to the app, and hands the app an access token for docs-mcp.
4. The app sends that token with every call. docs-mcp checks its signature,
   issuer, audience, expiry and scope, and answers.

docs-mcp never sees a password. It fetches Entra ID's public signing keys over
HTTPS, so it needs to reach `login.microsoftonline.com` on port 443.

## 1. Register docs-mcp in Entra ID

You need an Entra ID role that can create app registrations and grant admin
consent, such as Application Administrator.

**Create the registration.** *Entra admin center → App registrations → New
registration.*

- Name: `docs-mcp (dev-handbook)`, or similar.
- Supported account types: *Accounts in this organizational directory only*.
- No redirect URI: docs-mcp is an API, not an app people sign in to.

Note the **Application (client) ID** and the **Directory (tenant) ID**.

**Ask for v2 tokens.** In *Manifest*, set `requestedAccessTokenVersion` to `2`.
Without it, Entra ID issues v1 tokens, whose issuer and audience do not match
what docs-mcp expects. It also cannot take an `https://` Application ID URI
without it.

**Expose an API.**

- **Application ID URI:** the exact URL clients use for docs-mcp, such as
  `https://docs-mcp.example.internal/mcp`, with no trailing `/`. Clients send
  this URL when they ask Entra ID for a token, and Entra ID refuses unless it
  matches. Entra ID only accepts `https://` URIs on a domain verified in your
  tenant. If docs-mcp's host name is not on one, sort that out with your tenant
  administrators first.
- **Add a scope:** `docs.read`, *Admins only* consent, display name *Read the
  handbook*.
- **Authorized client applications:** add VS Code's client ID,
  `aebc6443-996d-45c2-90f0-388ff96faa56`, for the `docs.read` scope, so VS Code
  can get tokens for docs-mcp without asking each user for consent.

**Grant admin consent** for the scope (*API permissions*).

**Limit who may sign in.** *Enterprise applications → docs-mcp → Properties →
Assignment required: Yes*. Then, under *Users and groups*, add the groups that
may read the handbook. Everyone else is refused by Entra ID, before they ever
reach docs-mcp.

## 2. Configure docs-mcp

| Setting | Value |
|---|---|
| `MCP_PUBLIC_URL` | The Application ID URI: `https://docs-mcp.example.internal/mcp` |
| `MCP_OAUTH_ISSUER` | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| `MCP_OAUTH_AUDIENCE` | The Application (client) ID. A v2 token's audience is the client ID, not the URI |
| `MCP_OAUTH_SCOPES` | `https://docs-mcp.example.internal/mcp/docs.read` |
| `MCP_REFRESH_TOKEN` | A long random string, for the push webhook. Without it, `/refresh` is off |
| `MCP_ALLOWED_HOSTS` | As before: every host name clients use |

Leave `MCP_AUTH_TOKEN` unset: docs-mcp refuses to start with both, because a
shared token would let anyone who has it past the sign-in. Both URLs must be
`https://`, so TLS has to be in front of docs-mcp. The Kubernetes example's
ingress does that, and its overlay has these settings, commented out. On Azure
Container Apps, follow [Set up docs-mcp on Azure Container Apps](deploy-azure-container-apps.md).

**Check it**, from a machine on the network:

```sh
curl https://docs-mcp.example.internal/.well-known/oauth-protected-resource/mcp
curl -i -X POST https://docs-mcp.example.internal/mcp
```

The first returns JSON naming your tenant as the authorization server. The
second returns `401` with a `WWW-Authenticate` header that points at the first.

## 3. Connect the clients

Entra ID does not let clients register themselves, which most MCP clients
expect to do. Each client therefore needs a client ID registered in advance.
How well that works differs per client, as of October 2026:

| Client | Status | What it needs |
|---|---|---|
| **VS Code** (Copilot) | Documented by Microsoft | Its client ID authorized in step 1. Then add the server by URL; VS Code signs in with its own Microsoft account support |
| **Copilot CLI** | Open bugs | A client ID of your own (below). Reported problems: it can ignore a configured client ID ([#2717](https://github.com/github/copilot-cli/issues/2717)), and it can leave out the scope, which Entra ID refuses with `AADSTS900144` ([#4582](https://github.com/github/copilot-cli/issues/4582)) |
| **JetBrains** (Copilot plugin) | Reported problems | A client ID of your own (below). It has been reported to always ask for a client ID and a secret ([#1462](https://github.com/microsoft/copilot-intellij-feedback/issues/1462)) |

**VS Code**, in `.vscode/mcp.json`:

```json
{ "servers": { "dev-handbook": { "type": "http", "url": "https://docs-mcp.example.internal/mcp" } } }
```

No token and no header: VS Code finds the sign-in through docs-mcp's metadata.

**A client ID for the other clients.** Register one more app, such as
`docs-mcp clients`: a *public client* (*Authentication → Allow public client
flows: Yes*), with the redirect URI the client uses, for example
`http://localhost:<port>/callback` under *Mobile and desktop applications*. Add
its client ID to docs-mcp's *Authorized client applications*, as for VS Code.
Then give that client ID to the client, with no secret. Each client's own
documentation says where it goes, and which redirect URI it uses.

## 4. Test it

Run this once per client, with that client's current version, before you
switch users over:

| Step | Expected |
|---|---|
| Add the server, with no token | The client opens a browser at `login.microsoftonline.com` |
| Sign in as an assigned user | The client lists five tools |
| Ask *"What does the handbook say about …?"* | An answer that cites a handbook page |
| Sign in as a user who is not assigned | Entra ID refuses, with `AADSTS50105` |
| Remove an assigned user, then wait 90 minutes | Their calls start to fail with `401` |

When something fails, two places say why:

- **Entra ID's error page**, for errors before a token exists.
- **The `error_description` in docs-mcp's `401` or `403`**, for errors in the
  token. The client's log usually has it, or reproduce it with `curl`.

| Error | Usual cause |
|---|---|
| `AADSTS9010010` | The client asked for a resource that is not the Application ID URI. Check that `MCP_PUBLIC_URL` and the URI match exactly, with no trailing `/` |
| `AADSTS65001` | Consent: the client is not in *Authorized client applications*, or admin consent was not granted |
| `AADSTS900144` | The client sent no scope. A client bug; see the table above |
| `AADSTS50105` | The user is not assigned to the app. Working as intended |
| `wrong issuer` | v1 tokens: set `requestedAccessTokenVersion` to `2`. Or a different tenant in `MCP_OAUTH_ISSUER` |
| `wrong audience` | `MCP_OAUTH_AUDIENCE` must be the Application (client) ID |
| `missing scope …` | The token has no `docs.read`. Check the scope's name, and that the client asks for it |
| `503 … cannot reach the OAuth issuer` | docs-mcp cannot reach `login.microsoftonline.com`. Check egress and the proxy |

**If a client cannot sign in yet**, there is no shared-token back door: that
would undo the point of the sign-in. Keep users of that client on a separate
docs-mcp with a shared token until the client is fixed, or wait for the fix.

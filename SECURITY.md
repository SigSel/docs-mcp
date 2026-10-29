# Security

docs-mcp serves internal documentation to people's AI assistants, so a hole in
it can leak that documentation, or the tokens that read it. Reports are
welcome, and handled before anything else.

## Reporting a vulnerability

Report it privately, through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Please do not open a public issue or pull
request for it.

Say what you found, how to reproduce it, and what someone could do with it. If
you can, reproduce it against the test docs in `fixtures/docs` rather than
your own: never send content from your own documentation.

You will hear back within four weeks. Once a fix is ready, it lands on `main`, the
[threat model](docs/threat-model.md#findings) records it, and you are credited
unless you would rather not be.

## Supported versions

There are no releases yet. Fixes go to `main` only: build from the latest
commit.

## Scope

In scope:

- both implementations, in `rust/` and `python/`;
- the container images their `Containerfile`s build;
- the example deployments in `deploy/` and the deployment guides in `docs/`.

Out of scope, because docs-mcp cannot fix them:

- **Prompt injection through the content of a served repo.** Serving that text
  to the model unchanged is docs-mcp's job. The
  [threat model](docs/threat-model.md#e4-handbook-content) explains what stands
  in the way instead. A way for content to change what docs-mcp's *tools* claim
  or do is in scope.
- **Bugs in git, the MCP SDKs, the base images or the platform.** Report them
  upstream. Tell us too, if docs-mcp should work around one.
- **Deployments that skip the
  [operator checklist](docs/threat-model.md#checklist-for-operators)**, such as
  running without a token or sign-in on a network others can reach.

## What we already check

The [threat model](docs/threat-model.md) lists who might attack docs-mcp, what
stops them, and every finding of its reviews, fixed or open. Each fix comes
with a test in `conformance/`. `just audit` checks the locked dependencies
against known vulnerabilities.

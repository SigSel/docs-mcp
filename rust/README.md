# docs-mcp (Rust)

The Rust implementation of [docs-mcp](../README.md), built on `rmcp` 3.x with axum. Behaviour is defined by
[SPEC.md](../SPEC.md).

```sh
cargo build --release
DOCS_LOCAL_PATH=../../dev-handbooks/dev-handbook DOCS_DIR=src/content/docs target/release/docs-mcp
cargo clippy --release -- -D warnings   # pedantic, see Cargo.toml
```

| Module | Does |
|---|---|
| `config.rs` | Environment variables (SPEC §1) |
| `content.rs` | Frontmatter (`yaml-rust2`, YAML 1.2), cleaning, sections and anchors (§4) |
| `index.rs` | Discovery, BM25 search, filters, tool outputs (§4–6) |
| `source.rs` | git clone/pull or local re-read, on a schedule (§3) |
| `server.rs` | MCP tools, `/healthz`, `/readyz`, `/refresh`, host check and auth (§2, §6) |

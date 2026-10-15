//! The MCP tools and the HTTP app around them (SPEC.md §2, §6).

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use rmcp::handler::server::router::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{CallToolResult, ContentBlock, Implementation, ServerCapabilities, ServerConfig};
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::streamable_http_server::{StreamableHttpServerConfig, StreamableHttpService};
use rmcp::{ServerHandler, tool, tool_handler, tool_router};
use schemars::JsonSchema;
use serde::Deserialize;
use subtle::ConstantTimeEq;

use crate::config::Config;
use crate::index::{DocIndex, Filters, ToolFailure};
use crate::source::Syncer;

const INSTRUCTIONS: &str = "Documentation for {name}. Use search_docs to find what is relevant to a question, then \
read_doc to read a page or one of its sections. get_section looks up a heading anchor or ID, such as a rule number, \
across all pages. docs_overview lists the metadata fields and values you can filter on; list_docs browses pages. \
Cite pages by path, or by url when present.";

// Tool and parameter descriptions: the same text in every implementation (SPEC.md §6).
const DOCS_OVERVIEW: &str = "Overview of the documentation: source and commit, page count, folders, and every metadata \
field with its values and page counts. Call it to learn which filters exist.";
const LIST_DOCS: &str = "List pages with their path, title, url and metadata, optionally narrowed by path prefix and \
metadata filters.";
const SEARCH_DOCS: &str = "Full-text search over every section of every page. Use a few distinctive keywords rather \
than a full sentence. Returns the best-matching sections with path, heading, anchor, url and a snippet; follow up \
with read_doc(path, section=anchor) for the full text.";
const READ_DOC: &str = "Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading \
text) to read just that section and its subsections.";
const GET_SECTION: &str = "Look up a section by its heading anchor or ID across all pages, for example a rule number \
such as 'RS-203'.";
const QUERY: &str = "Keywords to search for. Case-insensitive; words of 3+ letters also match longer words that start \
with them.";
const FILTERS: &str = "Metadata filters: field name to a value or a list of values (any of them matches). \
docs_overview lists the fields and their values.";
const PATH_PREFIX: &str = "Only pages whose path starts with this, e.g. 'python/'.";
const LIMIT: &str = "Maximum number of results, 1-50. Default 10.";
const PATH: &str = "Page path as returned by search_docs or list_docs, e.g. 'rust/rules.mdx'. The extension may be \
left out.";
const SECTION: &str = "Optional anchor (e.g. 'rs-201') or heading text.";
const ANCHOR: &str = "The anchor or ID, e.g. 'rs-203'. Case-insensitive.";

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ListArgs {
    #[schemars(description = PATH_PREFIX)]
    path_prefix: Option<String>,
    #[schemars(description = FILTERS)]
    filters: Option<Filters>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SearchArgs {
    #[schemars(description = QUERY)]
    query: String,
    #[schemars(description = FILTERS)]
    filters: Option<Filters>,
    #[schemars(description = PATH_PREFIX)]
    path_prefix: Option<String>,
    #[schemars(description = LIMIT)]
    limit: Option<i64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ReadArgs {
    #[schemars(description = PATH)]
    path: String,
    #[schemars(description = SECTION)]
    section: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SectionArgs {
    #[schemars(description = ANCHOR)]
    anchor: String,
}

#[derive(Clone)]
pub struct DocsServer {
    syncer: Arc<Syncer>,
    name: String,
    #[expect(dead_code, reason = "read by the code #[tool_handler] generates")]
    tool_router: ToolRouter<Self>,
}

impl DocsServer {
    fn run(&self, f: impl FnOnce(&DocIndex) -> Result<String, ToolFailure>) -> CallToolResult {
        let Some(index) = self.syncer.index() else {
            let error = self
                .syncer
                .last_error()
                .map(|e| format!(" Last error: {e}"))
                .unwrap_or_default();
            return CallToolResult::error(vec![ContentBlock::text(format!(
                "Index not ready yet.{error}"
            ))]);
        };
        match f(&index) {
            Ok(text) => CallToolResult::success(vec![ContentBlock::text(text)]),
            Err(ToolFailure(message)) => CallToolResult::error(vec![ContentBlock::text(message)]),
        }
    }
}

#[tool_router]
impl DocsServer {
    pub fn new(syncer: Arc<Syncer>, name: String) -> Self {
        Self {
            syncer,
            name,
            tool_router: Self::tool_router(),
        }
    }

    #[tool(name = "docs_overview", description = DOCS_OVERVIEW,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn docs_overview(&self) -> CallToolResult {
        self.run(|ix| {
            let mut out = serde_json::Map::new();
            out.insert("name".into(), self.name.clone().into());
            out.insert("source".into(), self.syncer.source_info());
            out.extend(ix.overview());
            Ok(serde_json::Value::Object(out).to_string())
        })
    }

    #[tool(name = "list_docs", description = LIST_DOCS,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn list_docs(&self, Parameters(args): Parameters<ListArgs>) -> CallToolResult {
        self.run(|ix| {
            Ok(ix
                .list_docs(args.path_prefix.as_deref(), args.filters.as_ref())
                .to_string())
        })
    }

    #[tool(name = "search_docs", description = SEARCH_DOCS,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn search_docs(&self, Parameters(args): Parameters<SearchArgs>) -> CallToolResult {
        self.run(|ix| {
            ix.search(
                &args.query,
                args.filters.as_ref(),
                args.path_prefix.as_deref(),
                args.limit.unwrap_or(10),
            )
            .map(|v| v.to_string())
        })
    }

    #[tool(name = "read_doc", description = READ_DOC,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn read_doc(&self, Parameters(args): Parameters<ReadArgs>) -> CallToolResult {
        self.run(|ix| ix.read_doc(&args.path, args.section.as_deref()))
    }

    #[tool(name = "get_section", description = GET_SECTION,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn get_section(&self, Parameters(args): Parameters<SectionArgs>) -> CallToolResult {
        self.run(|ix| ix.get_section(&args.anchor))
    }
}

#[tool_handler]
impl ServerHandler for DocsServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("docs-mcp", env!("CARGO_PKG_VERSION")))
            .with_instructions(INSTRUCTIONS.replace("{name}", &self.name))
    }
}

#[derive(Clone)]
struct AppState {
    syncer: Arc<Syncer>,
    config: Arc<Config>,
}

fn hostname(host: &str) -> String {
    let h = host.trim().to_lowercase();
    if let Some(rest) = h.strip_prefix('[') {
        return rest.split(']').next().unwrap_or(rest).to_string();
    }
    h.split(':').next().unwrap_or("").to_string()
}

/// Host check, then bearer auth, on /mcp and /refresh (SPEC.md §2).
async fn guard(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if path.starts_with("/mcp") || path == "/refresh" {
        let headers = request.headers();
        if let Some(allowed) = &state.config.allowed_hosts {
            let host = headers
                .get(header::HOST)
                .and_then(|h| h.to_str().ok())
                .unwrap_or("");
            if !allowed.contains(&hostname(host)) {
                return (StatusCode::FORBIDDEN, "Forbidden: host not allowed\n").into_response();
            }
        }
        if let Some(token) = &state.config.auth_token {
            let given = headers
                .get(header::AUTHORIZATION)
                .map_or(&b""[..], HeaderValue::as_bytes);
            let expected = format!("Bearer {token}");
            if !bool::from(given.ct_eq(expected.as_bytes())) {
                return (
                    StatusCode::UNAUTHORIZED,
                    [(header::WWW_AUTHENTICATE, "Bearer")],
                    "Unauthorized\n",
                )
                    .into_response();
            }
        }
    }
    next.run(request).await
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status": "ok"}))
}

async fn readyz(State(state): State<AppState>) -> Response {
    let status = state.syncer.status();
    let code = if status.ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(status)).into_response()
}

async fn refresh(State(state): State<AppState>) -> Response {
    let ok = state.syncer.sync().await;
    let code = if ok {
        StatusCode::OK
    } else {
        StatusCode::INTERNAL_SERVER_ERROR
    };
    (code, Json(state.syncer.status())).into_response()
}

pub fn app(config: Arc<Config>, syncer: Arc<Syncer>) -> Router {
    let server = DocsServer::new(syncer.clone(), config.name.clone());
    let mcp = StreamableHttpService::new(
        move || Ok(server.clone()),
        Arc::new(LocalSessionManager::default()),
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true)
            .disable_allowed_hosts(),
    );
    let state = AppState { syncer, config };
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/refresh", post(refresh))
        .nest_service("/mcp", mcp)
        .fallback(|| async { (StatusCode::NOT_FOUND, Body::from("Not Found\n")) })
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}

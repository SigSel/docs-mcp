//! The MCP tools and the HTTP app around them (SPEC.md §2, §6).

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

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

const INSTRUCTIONS: &str = "Documentation for {name}{written}. Use search_docs to find what is relevant to a \
question, then read_doc to read a page or one of its sections. Search with keywords in {lang}, even when the user \
writes in another language, and answer in the user's language. get_section looks up a heading anchor or ID, such as \
a rule number, across all pages. docs_overview lists the metadata fields and values you can filter on; list_docs \
browses pages. Cite pages by path, or by url when present.";

// Tool and parameter descriptions: the same text in every implementation (SPEC.md §6). `{lang}` is replaced at
// startup with DOCS_LANGUAGE, or "the language of the documentation" when it is not set (see `localize`).
const DOCS_OVERVIEW: &str = "Overview of the documentation: source and commit, language, page count, folders, and \
every metadata field with its values and page counts. Call it to learn which filters exist.";
const LIST_DOCS: &str = "List pages with their path, title, url and metadata, optionally narrowed by path prefix and \
metadata filters.";
const SEARCH_DOCS: &str = "Full-text search over every section of every page. Use a few distinctive keywords in \
{lang}, whatever language the user writes in, rather than a full sentence. Returns the best-matching sections with \
path, heading, anchor, url and a snippet; follow up with read_doc(path, section=anchor) for the full text.";
const READ_DOC: &str = "Read a page as Markdown, with its metadata in a header. Pass section (an anchor or heading \
text) to read just that section and its subsections.";
const GET_SECTION: &str = "Look up a section by its heading anchor or ID across all pages, for example a rule number \
such as 'RS-203'.";
const QUERY: &str = "Keywords to search for, in {lang}. Add spelling variants, synonyms and English terms freely: \
keywords that do not occur are ignored. Case-insensitive; words of 3+ letters also match longer words that start with \
them, and words of 6+ letters also match spellings one letter off.";
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
    config: Arc<Config>,
    /// The tools with the configured language in their descriptions; `#[tool_handler]` serves this one.
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
    pub fn new(syncer: Arc<Syncer>, config: Arc<Config>) -> Self {
        let tool_router = localize(Self::tool_router(), &language(&config));
        Self {
            syncer,
            config,
            tool_router,
        }
    }

    #[tool(name = "docs_overview", description = DOCS_OVERVIEW,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn docs_overview(&self) -> CallToolResult {
        self.run(|ix| {
            let mut out = serde_json::Map::new();
            out.insert("name".into(), self.config.name.clone().into());
            out.insert("language".into(), self.config.language.clone().into());
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

#[tool_handler(router = self.tool_router)]
impl ServerHandler for DocsServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("docs-mcp", env!("CARGO_PKG_VERSION")))
            .with_instructions(instructions(&self.config))
    }
}

/// `{lang}` in the descriptions and instructions (SPEC.md §6).
fn language(config: &Config) -> String {
    config
        .language
        .clone()
        .unwrap_or_else(|| "the language of the documentation".into())
}

fn instructions(config: &Config) -> String {
    let written = config
        .language
        .as_ref()
        .map(|l| format!(", written in {l}"))
        .unwrap_or_default();
    INSTRUCTIONS
        .replace("{name}", &config.name)
        .replace("{written}", &written)
        .replace("{lang}", &language(config))
}

/// Puts the configured language into the tool and parameter descriptions.
fn localize(mut router: ToolRouter<DocsServer>, lang: &str) -> ToolRouter<DocsServer> {
    fn replace(value: &mut serde_json::Value, lang: &str) {
        match value {
            serde_json::Value::String(s) if s.contains("{lang}") => *s = s.replace("{lang}", lang),
            serde_json::Value::Array(items) => items.iter_mut().for_each(|v| replace(v, lang)),
            serde_json::Value::Object(map) => map.values_mut().for_each(|v| replace(v, lang)),
            _ => {}
        }
    }
    for route in router.map.values_mut() {
        if let Some(d) = &route.attr.description {
            route.attr.description = Some(d.replace("{lang}", lang).into());
        }
        let mut schema = serde_json::Value::Object((*route.attr.input_schema).clone());
        replace(&mut schema, lang);
        if let serde_json::Value::Object(map) = schema {
            route.attr.input_schema = Arc::new(map);
        }
    }
    router
}

/// The defaults an operator should know they are running with (SPEC.md §2).
pub fn startup_warnings(config: &Config) -> Vec<&'static str> {
    let mut warnings = Vec::new();
    if config.allowed_hosts_default {
        warnings.push(
            "MCP_ALLOWED_HOSTS is not set: only requests addressed to localhost are accepted. \
             Set it to the host names clients use.",
        );
    } else if config.allowed_hosts.is_none() {
        warnings.push("MCP_ALLOWED_HOSTS is '*': the Host header is not checked.");
    }
    if config.auth_token.is_none() {
        warnings.push("MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.");
    }
    warnings
}

#[derive(Clone)]
struct AppState {
    syncer: Arc<Syncer>,
    config: Arc<Config>,
    /// Host names already logged as refused, so a scanner cannot flood the log.
    refused: Arc<Mutex<HashSet<String>>>,
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
            let host = hostname(host);
            if !allowed.contains(&host) {
                let mut refused = state.refused.lock().unwrap();
                if refused.len() < 100 && refused.insert(host.clone()) {
                    tracing::warn!(
                        "refused a request for host '{host}': it is not in MCP_ALLOWED_HOSTS"
                    );
                }
                return (
                    StatusCode::FORBIDDEN,
                    "Forbidden: host not allowed (see MCP_ALLOWED_HOSTS)\n",
                )
                    .into_response();
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
    let server = DocsServer::new(syncer.clone(), config.clone());
    let mcp = StreamableHttpService::new(
        move || Ok(server.clone()),
        Arc::new(LocalSessionManager::default()),
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true)
            .disable_allowed_hosts(),
    );
    let state = AppState {
        syncer,
        config,
        refused: Arc::default(),
    };
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/refresh", post(refresh))
        .nest_service("/mcp", mcp)
        .fallback(|| async { (StatusCode::NOT_FOUND, Body::from("Not Found\n")) })
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}

//! The MCP tools and the HTTP app around them (SPEC.md §2, §6).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use axum::Router;
use axum::body::Body;
use axum::extract::{Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
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
use crate::oauth::{METADATA, Refusal, TokenChecker, metadata, metadata_path, metadata_url};
use crate::source::Library;

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

// With DOCS_SOURCES (SPEC.md §7.4): the instructions after the list of sources, and the `source` parameter.
const INSTRUCTIONS_SOURCES: &str = "Use search_docs to find what is relevant to a question, then read_doc to read \
a page or file. search_docs searches every source unless you name one. Search with keywords in the language of the \
source you expect the answer in, even when the user writes in another language, and answer in the user's language. \
Files that are not Markdown come back as they are, in a code block. get_section looks up a heading anchor or ID, such \
as a rule number, across all sources. docs_overview lists each source's metadata fields and values you can filter \
on; list_docs browses a source. Cite pages by source and path, or by url when present.";
const SEARCH_SOURCE: &str = "Only this source. Leave it out to search all of them.";
const LIST_SOURCE: &str = "Only this source. Leave it out to list all of them.";
const READ_SOURCE: &str = "The page's source, as search_docs and list_docs return it. Needed only when the path is \
in several sources.";

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ListArgs {
    #[schemars(description = PATH_PREFIX)]
    path_prefix: Option<String>,
    #[schemars(description = FILTERS)]
    filters: Option<Filters>,
    #[schemars(description = LIST_SOURCE)]
    source: Option<String>,
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
    #[schemars(description = SEARCH_SOURCE)]
    source: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ReadArgs {
    #[schemars(description = PATH)]
    path: String,
    #[schemars(description = SECTION)]
    section: Option<String>,
    #[schemars(description = READ_SOURCE)]
    source: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SectionArgs {
    #[schemars(description = ANCHOR)]
    anchor: String,
}

#[derive(Clone)]
pub struct DocsServer {
    library: Arc<Library>,
    config: Arc<Config>,
    /// The tools with the configured language in their descriptions; `#[tool_handler]` serves this one.
    tool_router: ToolRouter<Self>,
}

impl DocsServer {
    fn run(
        &self,
        source: Option<&str>,
        f: impl FnOnce(&DocIndex) -> Result<String, ToolFailure>,
    ) -> CallToolResult {
        if let Some(failure) = source.and_then(|s| self.library.check_source(s)) {
            return CallToolResult::error(vec![ContentBlock::text(failure)]);
        }
        let Some(index) = self.library.index() else {
            return CallToolResult::error(vec![ContentBlock::text(self.library.not_ready())]);
        };
        match f(&index) {
            Ok(text) => CallToolResult::success(vec![ContentBlock::text(text)]),
            Err(ToolFailure(message)) => CallToolResult::error(vec![ContentBlock::text(message)]),
        }
    }
}

#[tool_router]
impl DocsServer {
    pub fn new(library: Arc<Library>, config: Arc<Config>) -> Self {
        let tool_router = localize(Self::tool_router(), &language(&config), config.multi);
        Self {
            library,
            config,
            tool_router,
        }
    }

    #[tool(name = "docs_overview", description = DOCS_OVERVIEW,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn docs_overview(&self) -> CallToolResult {
        self.run(None, |ix| {
            let overview = |syncer: &crate::source::Syncer, described: bool| {
                let source = &syncer.source;
                let mut out = serde_json::Map::new();
                out.insert("name".into(), source.name.clone().into());
                if described {
                    out.insert("description".into(), source.description.clone().into());
                }
                out.insert("language".into(), source.language.clone().into());
                out.insert("source".into(), syncer.source_info());
                out.extend(ix.overview(&source.name));
                out
            };
            if !self.config.multi {
                let mut out = overview(&self.library.syncers[0], false);
                out.insert("name".into(), self.config.name.clone().into());
                return Ok(serde_json::Value::Object(out).to_string());
            }
            let sources: Vec<serde_json::Value> = self
                .library
                .syncers
                .iter()
                .map(|s| serde_json::Value::Object(overview(s, true)))
                .collect();
            Ok(serde_json::json!({"name": self.config.name, "sources": sources}).to_string())
        })
    }

    #[tool(name = "list_docs", description = LIST_DOCS,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn list_docs(&self, Parameters(args): Parameters<ListArgs>) -> CallToolResult {
        let source = args.source.as_deref();
        self.run(source, |ix| {
            Ok(ix
                .list_docs(args.path_prefix.as_deref(), args.filters.as_ref(), source)
                .to_string())
        })
    }

    #[tool(name = "search_docs", description = SEARCH_DOCS,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn search_docs(&self, Parameters(args): Parameters<SearchArgs>) -> CallToolResult {
        let source = args.source.as_deref();
        self.run(source, |ix| {
            ix.search(
                &args.query,
                args.filters.as_ref(),
                args.path_prefix.as_deref(),
                args.limit.unwrap_or(10),
                source,
            )
            .map(|v| v.to_string())
        })
    }

    #[tool(name = "read_doc", description = READ_DOC,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn read_doc(&self, Parameters(args): Parameters<ReadArgs>) -> CallToolResult {
        let source = args.source.as_deref();
        self.run(source, |ix| {
            ix.read_doc(&args.path, args.section.as_deref(), source)
        })
    }

    #[tool(name = "get_section", description = GET_SECTION,
           annotations(read_only_hint = true, open_world_hint = false, destructive_hint = false, idempotent_hint = true))]
    async fn get_section(&self, Parameters(args): Parameters<SectionArgs>) -> CallToolResult {
        self.run(None, |ix| ix.get_section(&args.anchor))
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

/// `{lang}` in the descriptions and instructions (SPEC.md §6, §7.4).
fn language(config: &Config) -> String {
    if config.multi {
        return "the language of the source you search".into();
    }
    config.sources[0]
        .language
        .clone()
        .unwrap_or_else(|| "the language of the documentation".into())
}

fn instructions(config: &Config) -> String {
    if config.multi {
        let mut lines = vec![format!(
            "Documentation and reference repositories for {}, in {} sources:",
            config.name,
            config.sources.len()
        )];
        for s in &config.sources {
            let description = s.description.as_deref().unwrap_or("");
            lines.push(match &s.language {
                Some(l) => format!("- {} ({l}): {description}", s.name),
                None => format!("- {}: {description}", s.name),
            });
        }
        return format!("{}\n\n{INSTRUCTIONS_SOURCES}", lines.join("\n"));
    }
    let written = config.sources[0]
        .language
        .as_ref()
        .map(|l| format!(", written in {l}"))
        .unwrap_or_default();
    INSTRUCTIONS
        .replace("{name}", &config.name)
        .replace("{written}", &written)
        .replace("{lang}", &language(config))
}

/// Puts the configured language into the tool and parameter descriptions. With one source, there is no `source`
/// to choose (SPEC.md §7.4).
fn localize(mut router: ToolRouter<DocsServer>, lang: &str, multi: bool) -> ToolRouter<DocsServer> {
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
        if !multi
            && let Some(properties) = schema
                .get_mut("properties")
                .and_then(serde_json::Value::as_object_mut)
        {
            properties.remove("source");
        }
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
    if config.auth_token.is_none() && config.oauth_issuer.is_none() {
        warnings.push("MCP_AUTH_TOKEN is not set: anyone who can reach this server can use it.");
    }
    if config.oauth_issuer.is_some() && config.refresh_token.is_none() {
        warnings.push("MCP_REFRESH_TOKEN is not set: /refresh is off.");
    }
    warnings
}

#[derive(Clone)]
struct AppState {
    library: Arc<Library>,
    config: Arc<Config>,
    /// Host names already logged as refused, so a scanner cannot flood the log.
    refused: Arc<Mutex<HashSet<String>>>,
    checker: Option<Arc<TokenChecker>>,
    /// `WWW-Authenticate` for OAuth refusals, before any error.
    challenge: String,
}

fn hostname(host: &str) -> String {
    let h = host.trim().to_lowercase();
    if let Some(rest) = h.strip_prefix('[') {
        return rest.split(']').next().unwrap_or(rest).to_string();
    }
    h.split(':').next().unwrap_or("").to_string()
}

/// Host check, then auth, on /mcp, /refresh and the OAuth metadata (SPEC.md §2).
async fn guard(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    if path.starts_with("/mcp") || path == "/refresh" || path.starts_with(METADATA) {
        let headers = request.headers();
        if let Some(refused) = check_host(&state, headers) {
            return refused;
        }
        let refused = if path == "/refresh" {
            check_refresh(&state, headers)
        } else if path.starts_with("/mcp") {
            check_mcp(&state, headers).await
        } else {
            None
        };
        if let Some(refused) = refused {
            return refused;
        }
    }
    next.run(request).await
}

fn check_host(state: &AppState, headers: &HeaderMap) -> Option<Response> {
    let allowed = state.config.allowed_hosts.as_ref()?;
    let host = hostname(
        headers
            .get(header::HOST)
            .and_then(|h| h.to_str().ok())
            .unwrap_or(""),
    );
    if allowed.contains(&host) {
        return None;
    }
    let mut refused = state.refused.lock().unwrap();
    if refused.len() < 100 && refused.insert(host.clone()) {
        tracing::warn!("refused a request for host '{host}': it is not in MCP_ALLOWED_HOSTS");
    }
    Some(
        (
            StatusCode::FORBIDDEN,
            "Forbidden: host not allowed (see MCP_ALLOWED_HOSTS)\n",
        )
            .into_response(),
    )
}

fn check_refresh(state: &AppState, headers: &HeaderMap) -> Option<Response> {
    let config = &state.config;
    match config.refresh_token.as_ref().or(config.auth_token.as_ref()) {
        Some(secret) => bearer(headers, secret),
        None if state.checker.is_some() => Some(
            (
                StatusCode::FORBIDDEN,
                "Forbidden: /refresh is off (see MCP_REFRESH_TOKEN)\n",
            )
                .into_response(),
        ),
        None => None,
    }
}

async fn check_mcp(state: &AppState, headers: &HeaderMap) -> Option<Response> {
    let Some(checker) = &state.checker else {
        return bearer(headers, state.config.auth_token.as_ref()?);
    };
    let given = headers
        .get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let token = match given.split_once(' ') {
        Some((scheme, token)) if scheme.eq_ignore_ascii_case("bearer") => token.trim(),
        _ => "",
    };
    if token.is_empty() {
        return Some(refuse(
            StatusCode::UNAUTHORIZED,
            "Unauthorized\n",
            &state.challenge,
        ));
    }
    match checker.check(token).await {
        Ok(()) => None,
        Err(Refusal::Invalid(reason)) => Some(refuse(
            StatusCode::UNAUTHORIZED,
            "Unauthorized\n",
            &format!(
                "{}, error=\"invalid_token\", error_description=\"{reason}\"",
                state.challenge
            ),
        )),
        Err(Refusal::MissingScope(scope)) => Some(refuse(
            StatusCode::FORBIDDEN,
            "Forbidden\n",
            &format!(
                "{}, error=\"insufficient_scope\", error_description=\"missing scope {scope}\"",
                state.challenge
            ),
        )),
        Err(Refusal::Unreachable) => Some(
            (
                StatusCode::SERVICE_UNAVAILABLE,
                "Service Unavailable: cannot reach the OAuth issuer\n",
            )
                .into_response(),
        ),
    }
}

fn refuse(status: StatusCode, body: &'static str, challenge: &str) -> Response {
    let challenge = HeaderValue::from_str(challenge).unwrap_or(HeaderValue::from_static("Bearer"));
    (status, [(header::WWW_AUTHENTICATE, challenge)], body).into_response()
}

/// `None` when the request carries `Bearer <secret>`, compared in constant time.
fn bearer(headers: &HeaderMap, secret: &str) -> Option<Response> {
    let given = headers
        .get(header::AUTHORIZATION)
        .map_or(&b""[..], HeaderValue::as_bytes);
    let expected = format!("Bearer {secret}");
    if bool::from(given.ct_eq(expected.as_bytes())) {
        return None;
    }
    Some(
        (
            StatusCode::UNAUTHORIZED,
            [(header::WWW_AUTHENTICATE, "Bearer")],
            "Unauthorized\n",
        )
            .into_response(),
    )
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status": "ok"}))
}

async fn readyz(State(state): State<AppState>) -> Response {
    let status = state.library.status();
    let code = if status["ready"] == serde_json::json!(true) {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(status)).into_response()
}

async fn refresh(
    State(state): State<AppState>,
    Query(query): Query<HashMap<String, String>>,
) -> Response {
    let name = query.get("source").map(String::as_str);
    if let Some(name) = name
        && !state.library.has(name)
    {
        let body = format!("No source '{name}'. Sources: {}.\n", state.library.names());
        return (StatusCode::NOT_FOUND, body).into_response();
    }
    let ok = state.library.sync(name).await;
    let code = if ok {
        StatusCode::OK
    } else {
        StatusCode::INTERNAL_SERVER_ERROR
    };
    (code, Json(state.library.status())).into_response()
}

pub fn app(config: Arc<Config>, library: Arc<Library>) -> Router {
    let server = DocsServer::new(library.clone(), config.clone());
    let mcp = StreamableHttpService::new(
        move || Ok(server.clone()),
        Arc::new(LocalSessionManager::default()),
        StreamableHttpServerConfig::default()
            .with_legacy_session_mode(false)
            .with_json_response(true)
            .disable_allowed_hosts(),
    );
    let checker = config
        .oauth_issuer
        .as_ref()
        .map(|_| Arc::new(TokenChecker::new(&config)));
    let challenge = config
        .public_url
        .as_ref()
        .map(|public| {
            let scope = if config.oauth_scopes.is_empty() {
                String::new()
            } else {
                format!(", scope=\"{}\"", config.oauth_scopes.join(" "))
            };
            format!(
                "Bearer resource_metadata=\"{}\"{scope}",
                metadata_url(public)
            )
        })
        .unwrap_or_default();
    let mut router = Router::new();
    if let (Some(_), Some(public)) = (&checker, &config.public_url) {
        let document = Json(metadata(&config));
        for path in BTreeSet::from([METADATA.to_string(), metadata_path(public)]) {
            let document = document.clone();
            router = router.route(&path, get(move || async move { document }));
        }
    }
    let state = AppState {
        library,
        config,
        refused: Arc::default(),
        checker,
        challenge,
    };
    router
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/refresh", post(refresh))
        .nest_service("/mcp", mcp)
        .fallback(|| async { (StatusCode::NOT_FOUND, Body::from("Not Found\n")) })
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}

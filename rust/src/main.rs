//! docs-mcp: an MCP server for a folder of Markdown docs, kept in step with git.

mod config;
mod content;
mod index;
mod oauth;
mod server;
mod source;

use std::collections::HashMap;
use std::sync::Arc;

use source::{Syncer, redact};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // RUST_LOG overrides; rmcp logs every stateless request at info, so it is quieter by default.
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,rmcp=warn"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stdout()))
        .init();
    let env: HashMap<String, String> = std::env::vars().collect();
    let config = match config::load(&env) {
        Ok(c) => Arc::new(c),
        Err(e) => {
            eprintln!("docs-mcp: configuration error: {e}");
            std::process::exit(2);
        }
    };
    let syncer = Arc::new(Syncer::new((*config).clone()));
    let listener = tokio::net::TcpListener::bind((config.host.as_str(), config.port)).await?;
    let source = match &config.repo_url {
        Some(url) => redact(url),
        None => config
            .local_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_default(),
    };
    tracing::info!(
        "serving {} from {} (dir {}) on {}:{}, allowed hosts: {}, auth: {}",
        config.name,
        source,
        config.docs_dir,
        config.host,
        config.port,
        config
            .allowed_hosts
            .as_ref()
            .map_or("*".to_string(), |h| h.join(",")),
        match (&config.oauth_issuer, &config.auth_token) {
            (Some(issuer), _) => format!("oauth ({issuer})"),
            (None, Some(_)) => "bearer token".into(),
            (None, None) => "none".into(),
        },
    );
    for warning in server::startup_warnings(&config) {
        tracing::warn!("{warning}");
    }
    tokio::spawn(syncer.clone().run_forever());
    axum::serve(listener, server::app(config, syncer))
        .with_graceful_shutdown(shutdown())
        .await?;
    Ok(())
}

async fn shutdown() {
    let ctrl_c = tokio::signal::ctrl_c();
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler");
    tokio::select! {
        _ = ctrl_c => {},
        _ = term.recv() => {},
    }
}

//! Configuration from environment variables (SPEC.md §1).

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

pub const DEFAULT_IGNORE_FIELDS: &str = "layout,template,hero,sidebar,sidebar_position,sidebar_label,\
sidebar_class_name,slug,id,pagefind,editUrl,lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,\
outline,aside,navbar,footer,pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,\
toc_min_heading_level,toc_max_heading_level";

#[derive(Debug, Clone)]
pub struct Config {
    pub repo_url: Option<String>,
    pub local_path: Option<PathBuf>,
    pub git_ref: Option<String>,
    pub docs_dir: String,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
    pub ignore_fields: HashSet<String>,
    pub refresh_seconds: u64,
    /// 0 means no limit.
    pub git_timeout_seconds: u64,
    pub name: String,
    pub language: Option<String>,
    pub site_url: Option<String>,
    pub git_username: String,
    pub git_token: Option<String>,
    pub git_ssh_key_file: Option<String>,
    pub git_known_hosts_file: Option<String>,
    pub cache_dir: PathBuf,
    pub host: String,
    pub port: u16,
    pub auth_token: Option<String>,
    /// `None` means any host.
    pub allowed_hosts: Option<Vec<String>>,
    /// `MCP_ALLOWED_HOSTS` was not set.
    pub allowed_hosts_default: bool,
}

#[derive(Debug)]
pub struct ConfigError(String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

type Env = HashMap<String, String>;

/// Reads `NAME`, or the file named by `NAME_FILE` (which wins), trimmed. Empty counts as unset.
fn get(name: &str, env: &Env) -> Result<Option<String>, ConfigError> {
    let value = match env.get(&format!("{name}_FILE")).filter(|f| !f.is_empty()) {
        Some(file) => std::fs::read_to_string(file)
            .map_err(|e| ConfigError(format!("{name}_FILE: cannot read {file}: {e}")))?
            .trim()
            .to_string(),
        None => env
            .get(name)
            .map(|v| v.trim().to_string())
            .unwrap_or_default(),
    };
    Ok(Some(value).filter(|v| !v.is_empty()))
}

fn list(value: Option<String>) -> Vec<String> {
    value
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(String::from)
        .collect()
}

fn number<T: std::str::FromStr>(name: &str, env: &Env, default: T) -> Result<T, ConfigError> {
    match get(name, env)? {
        None => Ok(default),
        Some(raw) => {
            if raw.starts_with('-') {
                return Err(ConfigError(format!(
                    "{name} must not be negative, got {raw}"
                )));
            }
            raw.parse()
                .map_err(|_| ConfigError(format!("{name} must be a whole number, got '{raw}'")))
        }
    }
}

pub fn load(env: &Env) -> Result<Config, ConfigError> {
    let repo_url = get("DOCS_REPO_URL", env)?;
    let local_path = get("DOCS_LOCAL_PATH", env)?;
    if repo_url.is_some() == local_path.is_some() {
        return Err(ConfigError(
            "set exactly one of DOCS_REPO_URL and DOCS_LOCAL_PATH".into(),
        ));
    }
    let allowed_raw = get("MCP_ALLOWED_HOSTS", env)?;
    let allowed_hosts_default = allowed_raw.is_none();
    let allowed = list(Some(
        allowed_raw.unwrap_or_else(|| "localhost,127.0.0.1,::1".into()),
    ));
    let or = |v: Option<String>, d: &str| v.unwrap_or_else(|| d.to_string());
    Ok(Config {
        repo_url,
        local_path: local_path.map(PathBuf::from),
        git_ref: get("DOCS_REF", env)?,
        docs_dir: or(get("DOCS_DIR", env)?, "."),
        include: list(Some(or(get("DOCS_INCLUDE", env)?, "**/*.md,**/*.mdx"))),
        exclude: list(get("DOCS_EXCLUDE", env)?),
        ignore_fields: list(Some(or(
            get("DOCS_IGNORE_FIELDS", env)?,
            DEFAULT_IGNORE_FIELDS,
        )))
        .into_iter()
        .collect(),
        refresh_seconds: number("DOCS_REFRESH_SECONDS", env, 300)?,
        git_timeout_seconds: number("DOCS_GIT_TIMEOUT_SECONDS", env, 120)?,
        name: or(get("DOCS_NAME", env)?, "docs"),
        language: get("DOCS_LANGUAGE", env)?,
        site_url: get("DOCS_SITE_URL", env)?,
        git_username: or(get("DOCS_GIT_USERNAME", env)?, "git"),
        git_token: get("DOCS_GIT_TOKEN", env)?,
        git_ssh_key_file: get("DOCS_GIT_SSH_KEY_FILE", env)?,
        git_known_hosts_file: get("DOCS_GIT_KNOWN_HOSTS_FILE", env)?,
        cache_dir: get("DOCS_CACHE_DIR", env)?
            .map_or_else(|| std::env::temp_dir().join("docs-mcp"), PathBuf::from),
        host: or(get("MCP_HOST", env)?, "0.0.0.0"),
        port: number("MCP_PORT", env, 8080)?,
        auth_token: get("MCP_AUTH_TOKEN", env)?,
        allowed_hosts: if allowed.iter().any(|h| h == "*") {
            None
        } else {
            Some(
                allowed
                    .iter()
                    .map(|h| h.to_lowercase().trim_matches(['[', ']']).to_string())
                    .collect(),
            )
        },
        allowed_hosts_default,
    })
}

//! Configuration from environment variables (SPEC.md §1).

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use yaml_rust2::{Yaml, YamlLoader};

pub const DEFAULT_IGNORE_FIELDS: &str = "layout,template,hero,sidebar,sidebar_position,sidebar_label,\
sidebar_class_name,slug,id,pagefind,editUrl,lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,\
outline,aside,navbar,footer,pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,\
toc_min_heading_level,toc_max_heading_level";

const DEFAULT_INCLUDE: &str = "**/*.md,**/*.mdx";
/// The variables `DOCS_SOURCES` replaces (SPEC.md §7.1).
const PER_SOURCE: [&str; 9] = [
    "DOCS_REPO_URL",
    "DOCS_LOCAL_PATH",
    "DOCS_REF",
    "DOCS_DIR",
    "DOCS_INCLUDE",
    "DOCS_EXCLUDE",
    "DOCS_IGNORE_FIELDS",
    "DOCS_LANGUAGE",
    "DOCS_SITE_URL",
];
const SOURCE_KEYS: [&str; 11] = [
    "name",
    "description",
    "repo",
    "path",
    "ref",
    "dir",
    "include",
    "exclude",
    "ignore_fields",
    "language",
    "site_url",
];

/// One repo or folder to serve (SPEC.md §7.1).
#[derive(Debug, Clone)]
pub struct Source {
    pub name: String,
    /// `None` for the single source of the `DOCS_*` variables.
    pub description: Option<String>,
    pub repo_url: Option<String>,
    pub local_path: Option<PathBuf>,
    pub git_ref: Option<String>,
    pub docs_dir: String,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
    pub ignore_fields: HashSet<String>,
    pub language: Option<String>,
    pub site_url: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub sources: Vec<Source>,
    /// `DOCS_SOURCES` is set: outputs name their source (SPEC.md §7).
    pub multi: bool,
    pub refresh_seconds: u64,
    /// 0 means no limit.
    pub git_timeout_seconds: u64,
    pub name: String,
    pub git_username: String,
    pub git_token: Option<String>,
    pub git_ssh_key_file: Option<String>,
    pub git_known_hosts_file: Option<String>,
    pub cache_dir: PathBuf,
    pub host: String,
    pub port: u16,
    pub auth_token: Option<String>,
    pub oauth_issuer: Option<String>,
    pub oauth_audience: Option<String>,
    pub oauth_scopes: Vec<String>,
    pub public_url: Option<String>,
    pub refresh_token: Option<String>,
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

const LOCAL_HOSTS: [&str; 3] = ["localhost", "127.0.0.1", "[::1]"];

/// (scheme, authority, rest) of an absolute URL, as written.
pub fn split_url(url: &str) -> (&str, &str, &str) {
    let Some((scheme, after)) = url.split_once("://") else {
        return ("", "", url);
    };
    let cut = after.find(['/', '?', '#']).unwrap_or(after.len());
    (scheme, &after[..cut], &after[cut..])
}

/// `https://` with a host, or `http://` to this machine (SPEC.md §1).
pub fn https_or_local(url: &str) -> bool {
    let (scheme, authority, _) = split_url(url);
    let hostport = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
    let host = if hostport.starts_with('[') {
        hostport.find(']').map_or("", |end| &hostport[..=end])
    } else {
        hostport.split(':').next().unwrap_or("")
    };
    let scheme = scheme.to_lowercase();
    !host.is_empty()
        && (scheme == "https"
            || (scheme == "http" && LOCAL_HOSTS.contains(&host.to_lowercase().as_str())))
}

type Auth = (Option<String>, Option<String>, Option<String>);

/// `MCP_AUTH_TOKEN`, `MCP_OAUTH_ISSUER` and `MCP_PUBLIC_URL`, checked against each other (SPEC.md §1).
fn auth(env: &Env) -> Result<Auth, ConfigError> {
    let token = get("MCP_AUTH_TOKEN", env)?;
    let issuer = get("MCP_OAUTH_ISSUER", env)?;
    let public = get("MCP_PUBLIC_URL", env)?;
    if token.is_some() && issuer.is_some() {
        return Err(ConfigError(
            "set at most one of MCP_AUTH_TOKEN and MCP_OAUTH_ISSUER: a shared token would let \
             anyone who has it past the login"
                .into(),
        ));
    }
    if issuer.is_some() && public.is_none() {
        return Err(ConfigError(
            "MCP_OAUTH_ISSUER needs MCP_PUBLIC_URL, the /mcp URL clients use".into(),
        ));
    }
    for (name, url) in [("MCP_OAUTH_ISSUER", &issuer), ("MCP_PUBLIC_URL", &public)] {
        if let Some(url) = url
            && !https_or_local(url)
        {
            return Err(ConfigError(format!(
                "{name} must be an https:// URL with a host (http:// only for localhost), got '{url}'"
            )));
        }
    }
    if let Some(url) = &public
        && url.contains(['?', '#'])
    {
        return Err(ConfigError(format!(
            "MCP_PUBLIC_URL must not have a query or a fragment, got '{url}'"
        )));
    }
    Ok((token, issuer, public))
}

/// The one source the `DOCS_*` variables describe.
fn single_source(name: &str, env: &Env) -> Result<Source, ConfigError> {
    let repo_url = get("DOCS_REPO_URL", env)?;
    let local_path = get("DOCS_LOCAL_PATH", env)?;
    if repo_url.is_some() == local_path.is_some() {
        return Err(ConfigError(
            "set exactly one of DOCS_REPO_URL and DOCS_LOCAL_PATH".into(),
        ));
    }
    let or = |v: Option<String>, d: &str| v.unwrap_or_else(|| d.to_string());
    Ok(Source {
        name: name.to_string(),
        description: None,
        repo_url,
        local_path: local_path.map(PathBuf::from),
        git_ref: get("DOCS_REF", env)?,
        docs_dir: or(get("DOCS_DIR", env)?, "."),
        include: list(Some(or(get("DOCS_INCLUDE", env)?, DEFAULT_INCLUDE))),
        exclude: list(get("DOCS_EXCLUDE", env)?),
        ignore_fields: list(Some(or(
            get("DOCS_IGNORE_FIELDS", env)?,
            DEFAULT_IGNORE_FIELDS,
        )))
        .into_iter()
        .collect(),
        language: get("DOCS_LANGUAGE", env)?,
        site_url: get("DOCS_SITE_URL", env)?,
    })
}

/// `DOCS_SOURCES`, checked (SPEC.md §7.1).
fn sources(raw: &str, env: &Env) -> Result<Vec<Source>, ConfigError> {
    let mut clash = Vec::new();
    for name in PER_SOURCE {
        if get(name, env)?.is_some() {
            clash.push(name);
        }
    }
    if !clash.is_empty() {
        return Err(ConfigError(format!(
            "DOCS_SOURCES replaces {}: set one or the other",
            clash.join(", ")
        )));
    }
    let docs = YamlLoader::load_from_str(raw)
        .map_err(|e| ConfigError(format!("DOCS_SOURCES is not valid YAML: {e}")))?;
    let items = match docs.into_iter().next() {
        Some(Yaml::Array(items)) if !items.is_empty() => items,
        _ => {
            return Err(ConfigError(
                "DOCS_SOURCES must be a non-empty YAML list of sources".into(),
            ));
        }
    };
    let mut out: Vec<Source> = Vec::new();
    for (i, item) in items.iter().enumerate() {
        let next = source(item, i + 1, &out)?;
        out.push(next);
    }
    Ok(out)
}

fn valid_name(name: &str) -> bool {
    name.split('-').all(|word| {
        !word.is_empty()
            && word
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
    })
}

fn source(item: &Yaml, number: usize, taken: &[Source]) -> Result<Source, ConfigError> {
    let Yaml::Hash(map) = item else {
        return Err(ConfigError(format!(
            "DOCS_SOURCES: source #{number} is not a mapping"
        )));
    };
    let value = |key: &str| {
        map.get(&Yaml::String(key.into()))
            .filter(|v| !matches!(v, Yaml::Null))
    };
    let name = value("name")
        .and_then(Yaml::as_str)
        .filter(|n| valid_name(n));
    let label = name.map_or_else(|| format!("#{number}"), |n| format!("'{n}'"));
    let fail = |message: String| ConfigError(format!("DOCS_SOURCES: source {label}: {message}"));
    for key in map.keys() {
        match key.as_str() {
            Some(k) if SOURCE_KEYS.contains(&k) => {}
            Some(k) => return Err(fail(format!("unknown key '{k}'"))),
            None => return Err(fail(format!("unknown key {key:?}"))),
        }
    }
    let Some(name) = name else {
        return Err(fail(
            "'name' must be lowercase letters and digits, words joined by single '-'".into(),
        ));
    };
    if taken.iter().any(|s| s.name == name) {
        return Err(fail("the name is used twice".into()));
    }
    let text = |key: &str| -> Result<Option<String>, ConfigError> {
        match value(key) {
            None => Ok(None),
            Some(Yaml::String(s)) => Ok(Some(s.clone()).filter(|s| !s.is_empty())),
            Some(_) => Err(fail(format!("'{key}' must be a string"))),
        }
    };
    let strings = |key: &str, default: &str| -> Result<Vec<String>, ConfigError> {
        match value(key) {
            None => Ok(list(Some(default.to_string()))),
            Some(Yaml::String(s)) => Ok(list(Some(s.clone()))),
            Some(Yaml::Array(items)) if items.iter().all(|v| v.as_str().is_some()) => Ok(items
                .iter()
                .filter_map(Yaml::as_str)
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .map(String::from)
                .collect()),
            Some(_) => Err(fail(format!(
                "'{key}' must be a string or a list of strings"
            ))),
        }
    };
    let Some(description) = text("description")? else {
        return Err(fail(
            "'description' is missing: say what the source is, and when to use it".into(),
        ));
    };
    let (repo_url, local_path) = (text("repo")?, text("path")?);
    if repo_url.is_some() == local_path.is_some() {
        return Err(fail("set exactly one of 'repo' and 'path'".into()));
    }
    Ok(Source {
        name: name.to_string(),
        description: Some(description),
        repo_url,
        local_path: local_path.map(PathBuf::from),
        git_ref: text("ref")?,
        docs_dir: text("dir")?.unwrap_or_else(|| ".".into()),
        include: strings("include", DEFAULT_INCLUDE)?,
        exclude: strings("exclude", "")?,
        ignore_fields: strings("ignore_fields", DEFAULT_IGNORE_FIELDS)?
            .into_iter()
            .collect(),
        language: text("language")?,
        site_url: text("site_url")?,
    })
}

pub fn load(env: &Env) -> Result<Config, ConfigError> {
    let name = get("DOCS_NAME", env)?.unwrap_or_else(|| "docs".into());
    let raw_sources = get("DOCS_SOURCES", env)?;
    let sources = match &raw_sources {
        Some(raw) => sources(raw, env)?,
        None => vec![single_source(&name, env)?],
    };
    let (auth_token, oauth_issuer, public_url) = auth(env)?;
    let allowed_raw = get("MCP_ALLOWED_HOSTS", env)?;
    let allowed_hosts_default = allowed_raw.is_none();
    let allowed = list(Some(
        allowed_raw.unwrap_or_else(|| "localhost,127.0.0.1,::1".into()),
    ));
    let or = |v: Option<String>, d: &str| v.unwrap_or_else(|| d.to_string());
    Ok(Config {
        multi: raw_sources.is_some(),
        sources,
        refresh_seconds: number("DOCS_REFRESH_SECONDS", env, 300)?,
        git_timeout_seconds: number("DOCS_GIT_TIMEOUT_SECONDS", env, 120)?,
        name,
        git_username: or(get("DOCS_GIT_USERNAME", env)?, "git"),
        git_token: get("DOCS_GIT_TOKEN", env)?,
        git_ssh_key_file: get("DOCS_GIT_SSH_KEY_FILE", env)?,
        git_known_hosts_file: get("DOCS_GIT_KNOWN_HOSTS_FILE", env)?,
        cache_dir: get("DOCS_CACHE_DIR", env)?
            .map_or_else(|| std::env::temp_dir().join("docs-mcp"), PathBuf::from),
        host: or(get("MCP_HOST", env)?, "0.0.0.0"),
        port: number("MCP_PORT", env, 8080)?,
        auth_token,
        oauth_issuer,
        oauth_audience: get("MCP_OAUTH_AUDIENCE", env)?.or_else(|| public_url.clone()),
        oauth_scopes: list(get("MCP_OAUTH_SCOPES", env)?),
        public_url,
        refresh_token: get("MCP_REFRESH_TOKEN", env)?,
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

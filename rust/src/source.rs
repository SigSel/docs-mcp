//! Keeping the index in step with the source: git clone/pull or a local directory (SPEC.md §3).

use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use base64::Engine;
use serde::Serialize;
use serde_json::{Value, json};
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::config::Config;
use crate::index::DocIndex;

const RETRY_SECONDS: u64 = 10;

/// Drops `user:password@` from a URL for display.
pub fn redact(url: &str) -> String {
    if let Some((scheme, rest)) = url.split_once("://") {
        let (authority, path) = rest.split_at(rest.find('/').unwrap_or(rest.len()));
        if let Some((_, host)) = authority.rsplit_once('@') {
            return format!("{scheme}://{host}{path}");
        }
    }
    url.to_string()
}

fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

pub fn git_env(config: &Config) -> Vec<(String, String)> {
    let mut env = vec![("GIT_TERMINAL_PROMPT".to_string(), "0".to_string())];
    let url = config.repo_url.as_deref().unwrap_or("");
    if let (Some(token), Some((scheme, rest))) = (&config.git_token, url.split_once("://")) {
        let scheme = scheme.to_lowercase();
        if scheme == "http" || scheme == "https" {
            let authority = rest.split('/').next().unwrap_or("");
            let host = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
            let basic = base64::engine::general_purpose::STANDARD
                .encode(format!("{}:{token}", config.git_username));
            env.push(("GIT_CONFIG_COUNT".into(), "1".into()));
            env.push((
                "GIT_CONFIG_KEY_0".into(),
                format!("http.{scheme}://{host}/.extraHeader"),
            ));
            env.push((
                "GIT_CONFIG_VALUE_0".into(),
                format!("Authorization: Basic {basic}"),
            ));
        }
    }
    if let Some(key) = &config.git_ssh_key_file {
        let host_check = match &config.git_known_hosts_file {
            Some(kh) => format!(
                "-o UserKnownHostsFile={} -o StrictHostKeyChecking=yes",
                shell_quote(kh)
            ),
            None => "-o StrictHostKeyChecking=accept-new".to_string(),
        };
        let ssh = format!(
            "ssh -i {} -o IdentitiesOnly=yes {host_check}",
            shell_quote(key)
        );
        env.push(("GIT_SSH_COMMAND".into(), ssh));
    }
    env
}

#[derive(Debug, Clone, Serialize)]
pub struct Status {
    pub ready: bool,
    pub pages: usize,
    pub commit: Option<String>,
    pub synced_at: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Default)]
struct State {
    index: Option<Arc<DocIndex>>,
    commit: Option<String>,
    synced_at: Option<String>,
    last_error: Option<String>,
}

pub struct Syncer {
    config: Config,
    state: RwLock<State>,
    lock: Mutex<()>,
    repo: PathBuf,
    env: Vec<(String, String)>,
}

impl Syncer {
    pub fn new(config: Config) -> Self {
        if config.repo_url.is_some() {
            let _ = std::fs::remove_dir_all(&config.cache_dir);
        }
        Self {
            repo: config.cache_dir.join("repo"),
            env: git_env(&config),
            config,
            state: RwLock::new(State::default()),
            lock: Mutex::new(()),
        }
    }

    pub fn index(&self) -> Option<Arc<DocIndex>> {
        self.state.read().unwrap().index.clone()
    }

    pub fn last_error(&self) -> Option<String> {
        self.state.read().unwrap().last_error.clone()
    }

    pub fn status(&self) -> Status {
        let s = self.state.read().unwrap();
        Status {
            ready: s.index.is_some(),
            pages: s.index.as_ref().map_or(0, |i| i.pages.len()),
            commit: s.commit.clone(),
            synced_at: s.synced_at.clone(),
            last_error: s.last_error.clone(),
        }
    }

    pub fn source_info(&self) -> Value {
        let s = self.state.read().unwrap();
        let c = &self.config;
        match &c.repo_url {
            Some(url) => {
                json!({"type": "git", "repo": redact(url), "ref": c.git_ref, "dir": c.docs_dir,
                                "commit": s.commit, "synced_at": s.synced_at})
            }
            None => {
                json!({"type": "local", "path": c.local_path, "dir": c.docs_dir, "commit": null,
                           "synced_at": s.synced_at})
            }
        }
    }

    /// Runs one sync after any sync already in progress; never overlaps.
    pub async fn sync(&self) -> bool {
        let _guard = self.lock.lock().await;
        match self.sync_once().await {
            Ok(()) => {
                let mut s = self.state.write().unwrap();
                s.synced_at = Some(
                    jiff::Timestamp::now()
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                        .to_string(),
                );
                s.last_error = None;
                true
            }
            Err(e) => {
                let message = format!("{e:#}");
                tracing::error!("sync failed: {message}");
                self.state.write().unwrap().last_error = Some(message);
                false
            }
        }
    }

    pub async fn run_forever(self: Arc<Self>) {
        while !self.sync().await {
            tokio::time::sleep(Duration::from_secs(RETRY_SECONDS)).await;
        }
        if self.config.refresh_seconds == 0 {
            return;
        }
        loop {
            tokio::time::sleep(Duration::from_secs(self.config.refresh_seconds)).await;
            self.sync().await;
        }
    }

    async fn sync_once(&self) -> anyhow::Result<()> {
        let c = &self.config;
        let (commit, root) = match &c.repo_url {
            Some(_) => (Some(self.pull().await?), self.repo.join(&c.docs_dir)),
            None => (
                None,
                c.local_path.clone().unwrap_or_default().join(&c.docs_dir),
            ),
        };
        let changed = {
            let s = self.state.read().unwrap();
            commit.is_none() || commit != s.commit || s.index.is_none()
        };
        if changed {
            let (include, exclude, ignore, name, site) = (
                c.include.clone(),
                c.exclude.clone(),
                c.ignore_fields.clone(),
                c.name.clone(),
                c.site_url.clone(),
            );
            let index = tokio::task::spawn_blocking(move || {
                DocIndex::build(&root, &include, &exclude, &ignore, &name, site)
            })
            .await??;
            let at = commit
                .as_ref()
                .map(|sha| format!(" at {}", &sha[..sha.len().min(12)]));
            tracing::info!(
                "indexed {} pages{}",
                index.pages.len(),
                at.unwrap_or_default()
            );
            let mut s = self.state.write().unwrap();
            s.index = Some(Arc::new(index));
            s.commit = commit;
        }
        Ok(())
    }

    async fn git(&self, args: &[&str]) -> anyhow::Result<String> {
        let output = Command::new("git")
            .args(args)
            .envs(self.env.iter().cloned())
            .output()
            .await?;
        if !output.status.success() {
            anyhow::bail!(
                "git {} failed: {}",
                args[0],
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    async fn pull(&self) -> anyhow::Result<String> {
        let c = &self.config;
        let repo = self.repo.to_string_lossy().into_owned();
        if self.repo.join(".git").exists() {
            let git_ref = c.git_ref.as_deref().unwrap_or("HEAD");
            self.git(&[
                "-C", &repo, "fetch", "--quiet", "--depth", "1", "origin", git_ref,
            ])
            .await?;
            self.git(&["-C", &repo, "reset", "--quiet", "--hard", "FETCH_HEAD"])
                .await?;
        } else {
            let _ = std::fs::remove_dir_all(&self.repo);
            std::fs::create_dir_all(&c.cache_dir)?;
            let mut args = vec!["clone", "--quiet", "--depth", "1", "--single-branch"];
            if let Some(r) = &c.git_ref {
                args.extend(["--branch", r]);
            }
            args.extend(["--", c.repo_url.as_deref().unwrap_or_default(), &repo]);
            self.git(&args).await?;
        }
        self.git(&["-C", &repo, "rev-parse", "HEAD"]).await
    }
}

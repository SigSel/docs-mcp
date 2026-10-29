//! Keeping the index in step with the sources: git clone/pull or a local directory (SPEC.md §3, §7.2).

use std::collections::HashMap;
use std::fmt::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use base64::Engine;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::{Map, Value, json};
use tokio::process::Command;
use tokio::sync::Mutex;
use tokio::task::JoinSet;

use crate::config::{Config, Source};
use crate::content::Page;
use crate::index::{DocIndex, load_pages};

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

/// `base/dir`, which may be reached through a link but must stay inside `base` (SPEC.md §4.1).
fn docs_root(base: &Path, dir: &str) -> anyhow::Result<PathBuf> {
    // Collecting the components drops `.` ones, so `x/.` reads `x`, as in the other implementations.
    let root: PathBuf = base.join(dir).components().collect();
    if let (Ok(resolved), Ok(base)) = (root.canonicalize(), base.canonicalize())
        && !resolved.starts_with(&base)
    {
        anyhow::bail!("docs directory is outside the source: {dir}");
    }
    Ok(root)
}

fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

pub fn git_env(config: &Config, repo_url: Option<&str>) -> Vec<(String, String)> {
    // git sends the token header after a redirect too, to wherever it points.
    let mut git_config = vec![("http.followRedirects".to_string(), "false".to_string())];
    let url = repo_url.unwrap_or("");
    if let (Some(token), Some((scheme, rest))) = (&config.git_token, url.split_once("://")) {
        let scheme = scheme.to_lowercase();
        if scheme == "http" || scheme == "https" {
            let authority = rest.split('/').next().unwrap_or("");
            let host = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
            let basic = base64::engine::general_purpose::STANDARD
                .encode(format!("{}:{token}", config.git_username));
            git_config.push((
                format!("http.{scheme}://{host}/.extraHeader"),
                format!("Authorization: Basic {basic}"),
            ));
        }
    }
    let mut env = vec![
        ("GIT_TERMINAL_PROMPT".to_string(), "0".to_string()),
        ("GIT_CONFIG_COUNT".to_string(), git_config.len().to_string()),
    ];
    for (i, (key, value)) in git_config.into_iter().enumerate() {
        env.push((format!("GIT_CONFIG_KEY_{i}"), key));
        env.push((format!("GIT_CONFIG_VALUE_{i}"), value));
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

#[derive(Default)]
struct State {
    pages: Option<Arc<Vec<Page>>>,
    /// The index holds this source's pages.
    loaded: bool,
    commit: Option<String>,
    synced_at: Option<String>,
    last_error: Option<String>,
}

/// One source's state, and the git commands that keep its clone in step.
pub struct Syncer {
    pub source: Source,
    state: RwLock<State>,
    lock: Mutex<()>,
    repo: PathBuf,
    env: Vec<(String, String)>,
    timeout: u64,
}

impl Syncer {
    fn status(&self) -> Map<String, Value> {
        let s = self.state.read().unwrap();
        let mut out = Map::new();
        out.insert("ready".into(), json!(s.loaded));
        out.insert(
            "pages".into(),
            json!(s.pages.as_ref().map_or(0, |p| p.len())),
        );
        out.insert("commit".into(), json!(s.commit));
        out.insert("synced_at".into(), json!(s.synced_at));
        out.insert("last_error".into(), json!(s.last_error));
        out
    }

    pub fn source_info(&self) -> Value {
        let s = self.state.read().unwrap();
        let c = &self.source;
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

    async fn git(&self, args: &[&str], cwd: Option<&Path>) -> anyhow::Result<String> {
        let mut command = Command::new("git");
        command
            .args(args)
            .envs(self.env.iter().cloned())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // Its own process group, so a timeout also kills the helpers git starts (git-remote-https, ssh).
            .process_group(0);
        if let Some(dir) = cwd {
            command.current_dir(dir);
        }
        let child = command.spawn()?;
        let group = child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .map(Pid::from_raw);
        let secs = self.timeout;
        // 0 means no limit; tokio treats a deadline past the end of time as never.
        let limit = if secs == 0 {
            Duration::MAX
        } else {
            Duration::from_secs(secs)
        };
        let Ok(output) = tokio::time::timeout(limit, child.wait_with_output()).await else {
            // Dropping the wait left git to tokio, which reaps it once it is killed.
            if let Some(group) = group {
                let _ = killpg(group, Signal::SIGKILL);
            }
            // A killed git can leave lock files behind; the next sync clones afresh.
            let _ = std::fs::remove_dir_all(&self.repo);
            anyhow::bail!("git {} timed out after {secs} seconds", args[0]);
        };
        let output = output?;
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
        let c = &self.source;
        let repo = self.repo.to_string_lossy().into_owned();
        let cwd = Some(self.repo.as_path());
        if self.repo.join(".git").exists() {
            let git_ref = c.git_ref.as_deref().unwrap_or("HEAD");
            self.git(
                &["fetch", "--quiet", "--depth", "1", "origin", git_ref],
                cwd,
            )
            .await?;
            self.git(&["reset", "--quiet", "--hard", "FETCH_HEAD"], cwd)
                .await?;
        } else {
            let _ = std::fs::remove_dir_all(&self.repo);
            if let Some(parent) = self.repo.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut args = vec!["clone", "--quiet", "--depth", "1", "--single-branch"];
            if let Some(r) = &c.git_ref {
                args.extend(["--branch", r]);
            }
            args.extend(["--", c.repo_url.as_deref().unwrap_or_default(), &repo]);
            self.git(&args, None).await?;
        }
        self.git(&["rev-parse", "HEAD"], cwd).await
    }
}

/// Every source, and one search index over all that have loaded (SPEC.md §7.2).
pub struct Library {
    config: Config,
    pub syncers: Vec<Syncer>,
    index: RwLock<Option<Arc<DocIndex>>>,
    rebuild_lock: Mutex<()>,
}

impl Library {
    pub fn new(config: Config) -> Self {
        if config.sources.iter().any(|s| s.repo_url.is_some()) {
            let _ = std::fs::remove_dir_all(&config.cache_dir);
        }
        let syncers = config
            .sources
            .iter()
            .map(|s| Syncer {
                repo: if config.multi {
                    config.cache_dir.join(&s.name).join("repo")
                } else {
                    config.cache_dir.join("repo")
                },
                env: git_env(&config, s.repo_url.as_deref()),
                timeout: config.git_timeout_seconds,
                source: s.clone(),
                state: RwLock::new(State::default()),
                lock: Mutex::new(()),
            })
            .collect();
        Self {
            config,
            syncers,
            index: RwLock::new(None),
            rebuild_lock: Mutex::new(()),
        }
    }

    pub fn index(&self) -> Option<Arc<DocIndex>> {
        self.index.read().unwrap().clone()
    }

    pub fn has(&self, name: &str) -> bool {
        self.syncers.iter().any(|s| s.source.name == name)
    }

    pub fn names(&self) -> String {
        let names: Vec<&str> = self
            .syncers
            .iter()
            .map(|s| s.source.name.as_str())
            .collect();
        names.join(", ")
    }

    pub fn status(&self) -> Value {
        if !self.config.multi {
            return Value::Object(self.syncers[0].status());
        }
        let sources: Vec<Map<String, Value>> = self
            .syncers
            .iter()
            .map(|s| {
                let mut entry = Map::new();
                entry.insert("name".into(), json!(s.source.name));
                entry.extend(s.status());
                entry
            })
            .collect();
        let ready = sources.iter().all(|s| s["ready"] == json!(true));
        let pages: u64 = sources.iter().filter_map(|s| s["pages"].as_u64()).sum();
        json!({"ready": ready, "pages": pages, "sources": sources})
    }

    /// The tool error for when no source has loaded yet (SPEC.md §3.3, §7.2).
    pub fn not_ready(&self) -> String {
        let errors: Vec<(String, String)> = self
            .syncers
            .iter()
            .filter_map(|s| {
                let error = s.state.read().unwrap().last_error.clone()?;
                Some((s.source.name.clone(), error))
            })
            .collect();
        if !self.config.multi {
            return match errors.first() {
                Some((_, e)) => format!("Index not ready yet. Last error: {e}"),
                None => "Index not ready yet.".into(),
            };
        }
        let mut out = String::from("Index not ready yet.");
        for (name, error) in errors {
            let _ = write!(out, " Last error in {name}: {error}");
        }
        out
    }

    /// The tool error for a source that is not configured or has not loaded, if any (SPEC.md §7.3).
    pub fn check_source(&self, name: &str) -> Option<String> {
        let Some(syncer) = self.syncers.iter().find(|s| s.source.name == name) else {
            return Some(format!("No source '{name}'. Sources: {}.", self.names()));
        };
        let s = syncer.state.read().unwrap();
        if s.loaded {
            return None;
        }
        let error = s
            .last_error
            .as_ref()
            .map(|e| format!(" Last error: {e}"))
            .unwrap_or_default();
        Some(format!("Source '{name}' is not ready yet.{error}"))
    }

    /// Syncs one source, or all of them at once; true when every sync succeeded.
    pub async fn sync(self: &Arc<Self>, name: Option<&str>) -> bool {
        let mut set = JoinSet::new();
        for (i, syncer) in self.syncers.iter().enumerate() {
            if name.is_none_or(|n| syncer.source.name == n) {
                let library = Arc::clone(self);
                set.spawn(async move { library.sync_one(i).await });
            }
        }
        let mut ok = true;
        while let Some(done) = set.join_next().await {
            ok &= done.unwrap_or(false);
        }
        ok
    }

    pub async fn run_forever(self: Arc<Self>) {
        let mut set = JoinSet::new();
        for i in 0..self.syncers.len() {
            let library = Arc::clone(&self);
            set.spawn(async move { library.run_source(i).await });
        }
        while set.join_next().await.is_some() {}
    }

    async fn run_source(&self, i: usize) {
        while !self.sync_one(i).await {
            tokio::time::sleep(Duration::from_secs(RETRY_SECONDS)).await;
        }
        if self.config.refresh_seconds == 0 {
            return;
        }
        loop {
            tokio::time::sleep(Duration::from_secs(self.config.refresh_seconds)).await;
            self.sync_one(i).await;
        }
    }

    /// Runs one sync of one source, after any sync of it already in progress; never overlaps.
    async fn sync_one(&self, i: usize) -> bool {
        let syncer = &self.syncers[i];
        let _guard = syncer.lock.lock().await;
        match self.load(syncer).await {
            Ok(()) => {
                let mut s = syncer.state.write().unwrap();
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
                if self.config.multi {
                    tracing::error!("sync failed for {}: {message}", syncer.source.name);
                } else {
                    tracing::error!("sync failed: {message}");
                }
                syncer.state.write().unwrap().last_error = Some(message);
                false
            }
        }
    }

    async fn load(&self, syncer: &Syncer) -> anyhow::Result<()> {
        let src = &syncer.source;
        let (commit, base) = match &src.repo_url {
            Some(_) => (Some(syncer.pull().await?), syncer.repo.clone()),
            None => (None, src.local_path.clone().unwrap_or_default()),
        };
        let changed = {
            let s = syncer.state.read().unwrap();
            commit.is_none() || commit != s.commit || s.pages.is_none()
        };
        if !changed {
            return Ok(());
        }
        let root = docs_root(&base, &src.docs_dir)?;
        let (include, exclude, ignore, root_name, name) = (
            src.include.clone(),
            src.exclude.clone(),
            src.ignore_fields.clone(),
            self.config.name.clone(),
            src.name.clone(),
        );
        let pages = tokio::task::spawn_blocking(move || {
            load_pages(&root, &include, &exclude, &ignore, &root_name, &name)
        })
        .await??;
        let count = pages.len();
        {
            let mut s = syncer.state.write().unwrap();
            s.pages = Some(Arc::new(pages));
            s.commit.clone_from(&commit);
        }
        self.rebuild().await?;
        syncer.state.write().unwrap().loaded = true;
        let from = if self.config.multi {
            format!(" from {}", src.name)
        } else {
            String::new()
        };
        let at = commit
            .as_ref()
            .map(|sha| format!(" at {}", &sha[..sha.len().min(12)]))
            .unwrap_or_default();
        tracing::info!("indexed {count} pages{from}{at}");
        Ok(())
    }

    /// Builds the index over every source that has pages, off to the side, and swaps it in.
    async fn rebuild(&self) -> anyhow::Result<()> {
        let _guard = self.rebuild_lock.lock().await;
        let mut pages = Vec::new();
        for syncer in &self.syncers {
            if let Some(p) = &syncer.state.read().unwrap().pages {
                pages.extend(p.iter().cloned());
            }
        }
        let names: Vec<String> = self.syncers.iter().map(|s| s.source.name.clone()).collect();
        let sites: HashMap<String, Option<String>> = self
            .syncers
            .iter()
            .map(|s| (s.source.name.clone(), s.source.site_url.clone()))
            .collect();
        let multi = self.config.multi;
        let index =
            tokio::task::spawn_blocking(move || DocIndex::new(pages, sites, names, multi)).await?;
        *self.index.write().unwrap() = Some(Arc::new(index));
        Ok(())
    }
}

// Keeping the index in step with the source: git clone/pull or a local directory (SPEC.md §3).

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';

import type { Config } from './config.js';
import { DocIndex } from './docindex.js';
import { log } from './log.js';

const RETRY_SECONDS = 10;

/** Drops user:password@ from a URL for display. */
export function redact(url: string): string {
  return url.replace(/^([a-z][a-z0-9+.-]*:\/\/)[^/@]*@/i, '$1');
}

const shellQuote = (s: string): string => `'${s.replaceAll("'", `'\\''`)}'`;

export function gitEnv(config: Config): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  const match = /^(https?):\/\/(?:[^/@]*@)?([^/]+)/i.exec(config.repoUrl ?? '');
  if (config.gitToken && match) {
    const basic = Buffer.from(`${config.gitUsername}:${config.gitToken}`).toString('base64');
    env.GIT_CONFIG_COUNT = '1';
    env.GIT_CONFIG_KEY_0 = `http.${match[1]!.toLowerCase()}://${match[2]}/.extraHeader`;
    env.GIT_CONFIG_VALUE_0 = `Authorization: Basic ${basic}`;
  }
  if (config.gitSshKeyFile) {
    let ssh = `ssh -i ${shellQuote(config.gitSshKeyFile)} -o IdentitiesOnly=yes`;
    ssh += config.gitKnownHostsFile
      ? ` -o UserKnownHostsFile=${shellQuote(config.gitKnownHostsFile)} -o StrictHostKeyChecking=yes`
      : ' -o StrictHostKeyChecking=accept-new';
    env.GIT_SSH_COMMAND = ssh;
  }
  return env;
}

export interface Status {
  ready: boolean;
  pages: number;
  commit: string | null;
  synced_at: string | null;
  last_error: string | null;
}

export class Syncer {
  index: DocIndex | null = null;
  commit: string | null = null;
  syncedAt: string | null = null;
  lastError: string | null = null;
  private running: Promise<unknown> = Promise.resolve();
  private readonly repo: string;
  private readonly env: NodeJS.ProcessEnv;

  constructor(private readonly config: Config) {
    this.repo = join(config.cacheDir, 'repo');
    this.env = gitEnv(config);
    if (config.repoUrl) rmSync(config.cacheDir, { recursive: true, force: true });
  }

  status(): Status {
    return {
      ready: this.index !== null,
      pages: this.index ? this.index.pages.length : 0,
      commit: this.commit,
      synced_at: this.syncedAt,
      last_error: this.lastError,
    };
  }

  sourceInfo(): Record<string, unknown> {
    const c = this.config;
    if (c.repoUrl) {
      return { type: 'git', repo: redact(c.repoUrl), ref: c.ref, dir: c.docsDir, commit: this.commit, synced_at: this.syncedAt };
    }
    return { type: 'local', path: c.localPath, dir: c.docsDir, commit: null, synced_at: this.syncedAt };
  }

  /** Runs one sync after any sync already in progress; never overlaps. */
  sync(): Promise<boolean> {
    const next = this.running.then(() => this.syncOnce());
    this.running = next;
    return next;
  }

  async runForever(): Promise<void> {
    while (!(await this.sync())) await sleep(RETRY_SECONDS);
    if (this.config.refreshSeconds === 0) return;
    for (;;) {
      await sleep(this.config.refreshSeconds);
      await this.sync();
    }
  }

  private async syncOnce(): Promise<boolean> {
    const c = this.config;
    try {
      let commit: string | null = null;
      let changed = true;
      let root: string;
      if (c.repoUrl) {
        commit = await this.pull();
        changed = commit !== this.commit || this.index === null;
        root = join(this.repo, c.docsDir);
      } else {
        root = join(c.localPath ?? '.', c.docsDir);
      }
      if (changed) {
        const index = DocIndex.build(root, c.include, c.exclude, c.ignoreFields, c.name, c.siteUrl);
        this.index = index;
        this.commit = commit;
        log.info(`indexed ${index.pages.length} pages${commit ? ` at ${commit.slice(0, 12)}` : ''}`);
      }
      this.syncedAt = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
      this.lastError = null;
      return true;
    } catch (e) {
      this.lastError = (e as Error).message || String(e);
      log.error(`sync failed: ${this.lastError}`);
      return false;
    }
  }

  private git(args: string[], cwd?: string): Promise<string> {
    return new Promise((resolve, reject) => {
      // Detached: its own process group, so a timeout also kills the helpers git starts (git-remote-https, ssh).
      const child = spawn('git', args, { cwd, env: this.env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
      const stdout: Buffer[] = [];
      const stderr: Buffer[] = [];
      child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk));
      child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk));
      const seconds = this.config.gitTimeoutSeconds;
      let timedOut = false;
      const timer =
        seconds > 0
          ? setTimeout(() => {
              timedOut = true;
              try {
                process.kill(-child.pid!, 'SIGKILL');
              } catch {
                // already gone
              }
            }, seconds * 1000)
          : undefined;
      child.on('error', (error) => {
        clearTimeout(timer);
        reject(new Error(`git ${args[0]} failed: ${error.message}`));
      });
      child.on('close', (code) => {
        clearTimeout(timer);
        if (timedOut) {
          // A killed git can leave lock files behind; the next sync clones afresh.
          rmSync(this.repo, { recursive: true, force: true });
          reject(new Error(`git ${args[0]} timed out after ${seconds} seconds`));
        } else if (code !== 0) {
          reject(new Error(`git ${args[0]} failed: ${Buffer.concat(stderr).toString().trim()}`));
        } else {
          resolve(Buffer.concat(stdout).toString().trim());
        }
      });
    });
  }

  private async pull(): Promise<string> {
    const c = this.config;
    if (!existsSync(join(this.repo, '.git'))) {
      rmSync(this.repo, { recursive: true, force: true });
      mkdirSync(dirname(this.repo), { recursive: true });
      const branch = c.ref ? ['--branch', c.ref] : [];
      await this.git(['clone', '--quiet', '--depth', '1', '--single-branch', ...branch, '--', c.repoUrl!, this.repo]);
    } else {
      await this.git(['fetch', '--quiet', '--depth', '1', 'origin', c.ref ?? 'HEAD'], this.repo);
      await this.git(['reset', '--quiet', '--hard', 'FETCH_HEAD'], this.repo);
    }
    return this.git(['rev-parse', 'HEAD'], this.repo);
  }
}

const sleep = (seconds: number): Promise<void> => new Promise((r) => setTimeout(r, seconds * 1000));

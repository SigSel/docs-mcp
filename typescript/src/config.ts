// Configuration from environment variables (SPEC.md §1).

import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const DEFAULT_IGNORE_FIELDS =
  'layout,template,hero,sidebar,sidebar_position,sidebar_label,sidebar_class_name,slug,id,pagefind,editUrl,' +
  'lastUpdated,prev,next,head,banner,tableOfContents,draft,hide,search,outline,aside,navbar,footer,' +
  'pagination_next,pagination_prev,custom_edit_url,displayed_sidebar,toc_min_heading_level,toc_max_heading_level';

export class ConfigError extends Error {}

export interface Config {
  repoUrl: string | null;
  localPath: string | null;
  ref: string | null;
  docsDir: string;
  include: string[];
  exclude: string[];
  ignoreFields: Set<string>;
  refreshSeconds: number;
  name: string;
  siteUrl: string | null;
  gitUsername: string;
  gitToken: string | null;
  gitSshKeyFile: string | null;
  gitKnownHostsFile: string | null;
  cacheDir: string;
  host: string;
  port: number;
  authToken: string | null;
  /** null means any host */
  allowedHosts: string[] | null;
}

type Env = Record<string, string | undefined>;

/** Reads NAME, or the file named by NAME_FILE (which wins), trimmed. Empty counts as unset. */
function get(name: string, env: Env): string | null {
  const file = env[`${name}_FILE`];
  let value: string;
  if (file) {
    try {
      value = readFileSync(file, 'utf8').trim();
    } catch (e) {
      throw new ConfigError(`${name}_FILE: cannot read ${file}: ${(e as Error).message}`);
    }
  } else {
    value = (env[name] ?? '').trim();
  }
  return value || null;
}

function list(value: string | null): string[] {
  return (value ?? '')
    .split(',')
    .map((v) => v.trim())
    .filter((v) => v);
}

function int(name: string, env: Env, fallback: number): number {
  const raw = get(name, env);
  if (raw === null) return fallback;
  if (!/^-?\d+$/.test(raw)) throw new ConfigError(`${name} must be a whole number, got '${raw}'`);
  const value = Number(raw);
  if (value < 0) throw new ConfigError(`${name} must not be negative, got ${value}`);
  return value;
}

export function load(env: Env = process.env): Config {
  const repoUrl = get('DOCS_REPO_URL', env);
  const localPath = get('DOCS_LOCAL_PATH', env);
  if (Boolean(repoUrl) === Boolean(localPath)) {
    throw new ConfigError('set exactly one of DOCS_REPO_URL and DOCS_LOCAL_PATH');
  }
  const allowed = list(get('MCP_ALLOWED_HOSTS', env) ?? 'localhost,127.0.0.1,::1');
  return {
    repoUrl,
    localPath,
    ref: get('DOCS_REF', env),
    docsDir: get('DOCS_DIR', env) ?? '.',
    include: list(get('DOCS_INCLUDE', env) ?? '**/*.md,**/*.mdx'),
    exclude: list(get('DOCS_EXCLUDE', env)),
    ignoreFields: new Set(list(get('DOCS_IGNORE_FIELDS', env) ?? DEFAULT_IGNORE_FIELDS)),
    refreshSeconds: int('DOCS_REFRESH_SECONDS', env, 300),
    name: get('DOCS_NAME', env) ?? 'docs',
    siteUrl: get('DOCS_SITE_URL', env),
    gitUsername: get('DOCS_GIT_USERNAME', env) ?? 'git',
    gitToken: get('DOCS_GIT_TOKEN', env),
    gitSshKeyFile: get('DOCS_GIT_SSH_KEY_FILE', env),
    gitKnownHostsFile: get('DOCS_GIT_KNOWN_HOSTS_FILE', env),
    cacheDir: get('DOCS_CACHE_DIR', env) ?? join(tmpdir(), 'docs-mcp'),
    host: get('MCP_HOST', env) ?? '0.0.0.0',
    port: int('MCP_PORT', env, 8080),
    authToken: get('MCP_AUTH_TOKEN', env),
    allowedHosts: allowed.includes('*') ? null : allowed.map((h) => h.toLowerCase().replace(/^\[|\]$/g, '')),
  };
}

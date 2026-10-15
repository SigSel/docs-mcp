#!/usr/bin/env node
// Entry point: `docs-mcp` or `node dist/main.js`.

import { serve } from '@hono/node-server';

import { ConfigError, load } from './config.js';
import { log } from './log.js';
import { buildApp, startupWarnings } from './server.js';
import { redact, Syncer } from './source.js';

function main(): void {
  let config;
  try {
    config = load();
  } catch (e) {
    if (e instanceof ConfigError) {
      console.error(`docs-mcp: configuration error: ${e.message}`);
      process.exit(2);
    }
    throw e;
  }
  const syncer = new Syncer(config);
  const server = serve({ fetch: buildApp(config, syncer), hostname: config.host, port: config.port });
  const source = config.repoUrl ? redact(config.repoUrl) : config.localPath;
  log.info(
    `serving ${config.name} from ${source} (dir ${config.docsDir}) on ${config.host}:${config.port}, ` +
      `allowed hosts: ${config.allowedHosts?.join(',') ?? '*'}, auth: ${config.authToken ? 'bearer token' : 'none'}`,
  );
  for (const warning of startupWarnings(config)) log.warning(warning);
  void syncer.runForever();
  const stop = (): void => {
    server.close();
    process.exit(0);
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}

main();

// Plain line logging to stdout/stderr, same shape as the other implementations.

const line = (level: string, message: string): string => `${new Date().toISOString()} ${level} docs_mcp: ${message}`;

export const log = {
  info: (message: string): void => console.log(line('INFO', message)),
  warning: (message: string): void => console.error(line('WARNING', message)),
  error: (message: string): void => console.error(line('ERROR', message)),
};

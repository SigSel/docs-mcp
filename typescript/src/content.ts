// Turning a Markdown/MDX file into a page: frontmatter, cleaning, sections (SPEC.md §4).

import { parseDocument } from 'yaml';

export type MetaValue = string | string[];
export type Metadata = Map<string, MetaValue>;

const FENCE_OPEN = /^\s*(`{3,}|~{3,})/;
const MDX_MODULE_LINE = /^\s*(import|export)\s/;
const INLINE_CODE = /`[^`\n]+`/g;
const JSX_COMMENT = /\{\/\*[\s\S]*?\*\/\}/g;
const TAG = /<\/?[A-Za-z][A-Za-z0-9._:-]*(\s[^<>]*)?\/?>/g;
const LABEL_ATTR = /\blabel\s*=\s*(?:"([^"]*)"|'([^']*)')/;
const TITLE_ATTR = /\btitle\s*=\s*(?:"([^"]*)"|'([^']*)')/;
export const HEADING = /^(#{1,6})[ \t]+(.*)$/;
const HEADING_ATTR = /\s*\{\s*#([A-Za-z0-9_-]+)[^}]*\}\s*$/;
const CLOSING_HASHES = /(^|\s+)#+\s*$/;
export const LINK = /\[([^\]]*)\]\([^)]*\)/g;
const PLACEHOLDER = /\u0000(\d+)\u0000/g;

export interface Section {
  /** 0 for the intro */
  level: number;
  heading: string | null;
  anchor: string | null;
  explicit: boolean;
  /** line of the heading (0 for the intro) */
  start: number;
  /** next heading of any level */
  ownEnd: number;
  /** next heading of the same or a higher level */
  end: number;
}

export class Page {
  constructor(
    readonly path: string,
    readonly title: string,
    readonly description: string | null,
    readonly metadata: Metadata,
    readonly lines: string[],
    readonly sections: Section[],
  ) {}

  get body(): string {
    return this.lines.join('\n');
  }

  sectionText(section: Section): string {
    return this.lines.slice(section.start, section.end).join('\n').trim();
  }

  ownText(section: Section): string {
    const start = section.level === 0 ? section.start : section.start + 1;
    return this.lines.slice(start, section.ownEnd).join('\n');
  }
}

interface Line {
  text: string;
  code: boolean;
  anchor: string | null;
}

const escapeRegex = (s: string): string => s.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

export function globToRegex(glob: string): RegExp {
  let out = '';
  let i = 0;
  while (i < glob.length) {
    if (glob.startsWith('**/', i)) {
      out += '(?:.*/)?';
      i += 3;
    } else if (glob.startsWith('**', i)) {
      out += '.*';
      i += 2;
    } else if (glob[i] === '*') {
      out += '[^/]*';
      i += 1;
    } else if (glob[i] === '?') {
      out += '[^/]';
      i += 1;
    } else {
      out += escapeRegex(glob[i]!);
      i += 1;
    }
  }
  return new RegExp(`^${out}$`, 'u');
}

/** Returns [mapping or null, body]. Broken YAML still has its block skipped. */
export function splitFrontmatter(text: string): [Map<unknown, unknown> | null, string] {
  const lines = text.split('\n');
  if (lines.length === 0 || lines[0]!.trimEnd() !== '---') return [null, text];
  for (let i = 1; i < lines.length; i++) {
    if (lines[i]!.trimEnd() === '---') {
      const body = lines.slice(i + 1).join('\n');
      const doc = parseDocument(lines.slice(1, i).join('\n'), { version: '1.2', uniqueKeys: true });
      if (doc.errors.length > 0) return [null, body];
      const data: unknown = doc.toJS({ mapAsMap: true });
      return [data instanceof Map ? data : null, body];
    }
  }
  return [null, text];
}

function scalar(value: unknown): string | null {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'string') return value;
  return null;
}

export function extractMetadata(front: Map<unknown, unknown>, ignore: Set<string>): Metadata {
  const meta: Metadata = new Map();
  for (const [rawKey, value] of front) {
    const key = String(rawKey);
    if (key === 'title' || key === 'description' || ignore.has(key)) continue;
    if (Array.isArray(value)) {
      meta.set(
        key,
        value.map(scalar).filter((s): s is string => s !== null),
      );
    } else {
      const s = scalar(value);
      if (s !== null) meta.set(key, s);
    }
  }
  return meta;
}

function cleanProse(input: string, mdx: boolean): string {
  let text = mdx
    ? input
        .split('\n')
        .filter((line) => !MDX_MODULE_LINE.test(line))
        .join('\n')
    : input;
  const protectedSpans: string[] = [];
  text = text.replace(INLINE_CODE, (m) => {
    protectedSpans.push(m);
    return `\u0000${protectedSpans.length - 1}\u0000`;
  });
  text = text.replace(JSX_COMMENT, '');
  text = text.replace(TAG, (m: string, attrs: string | undefined) => {
    if (m.startsWith('</')) return '';
    const attr = LABEL_ATTR.exec(attrs ?? '') ?? TITLE_ATTR.exec(attrs ?? '');
    return attr ? `**${attr[1] ?? attr[2]}**` : '';
  });
  return text.replace(PLACEHOLDER, (_m, n: string) => protectedSpans[Number(n)]!);
}

/** Cleans prose (SPEC.md §4.4) and returns lines tagged code/prose with heading anchors. */
export function clean(body: string, mdx: boolean): Line[] {
  const raw = body.split('\n');
  const segments: Array<[boolean, string[]]> = [];
  let i = 0;
  while (i < raw.length) {
    const m = FENCE_OPEN.exec(raw[i]!);
    if (m) {
      const fence = m[1]!;
      const close = new RegExp(`^\\s*${escapeRegex(fence[0]!)}{${fence.length},}\\s*$`);
      let j = i + 1;
      while (j < raw.length && !close.test(raw[j]!)) j++;
      segments.push([true, raw.slice(i, j + 1)]);
      i = j + 1;
    } else {
      let j = i;
      while (j < raw.length && !FENCE_OPEN.test(raw[j]!)) j++;
      segments.push([false, raw.slice(i, j)]);
      i = j;
    }
  }

  const lines: Line[] = [];
  for (const [isCode, seg] of segments) {
    if (isCode) {
      for (const text of seg) lines.push({ text, code: true, anchor: null });
      continue;
    }
    for (let text of cleanProse(seg.join('\n'), mdx).split('\n')) {
      text = text.trimEnd();
      let anchor: string | null = null;
      if (HEADING.test(text)) {
        const m = HEADING_ATTR.exec(text);
        if (m) {
          anchor = m[1]!;
          text = text.slice(0, m.index).trimEnd();
        }
      }
      const last = lines[lines.length - 1];
      if (text === '' && last && !last.code && last.text === '') continue;
      lines.push({ text, code: false, anchor });
    }
  }

  while (lines.length && lines[0]!.text.trim() === '') lines.shift();
  while (lines.length && lines[lines.length - 1]!.text.trim() === '') lines.pop();
  return lines;
}

export function headingText(raw: string): string {
  return raw.replace(CLOSING_HASHES, '').replace(LINK, '$1').trim();
}

export function slugify(text: string): string {
  return text
    .replace(LINK, '$1')
    .toLowerCase()
    .replace(/[^\p{L}\p{N} _-]/gu, '')
    .trim()
    .replaceAll(' ', '-');
}

export function buildSections(lines: Line[]): Section[] {
  const headings: Array<{ line: number; level: number; text: string; anchor: string; explicit: boolean }> = [];
  const occurrences = new Map<string, number>();
  lines.forEach((line, n) => {
    if (line.code) return;
    const m = HEADING.exec(line.text);
    if (!m) return;
    const text = headingText(m[2]!);
    if (line.anchor !== null) {
      headings.push({ line: n, level: m[1]!.length, text, anchor: line.anchor, explicit: true });
      return;
    }
    const base = slugify(text);
    let slug = base;
    while (occurrences.has(slug)) {
      occurrences.set(base, occurrences.get(base)! + 1);
      slug = `${base}-${occurrences.get(base)}`;
    }
    occurrences.set(slug, 0);
    headings.push({ line: n, level: m[1]!.length, text, anchor: slug, explicit: false });
  });

  const first = headings.length ? headings[0]!.line : lines.length;
  const sections: Section[] = [
    { level: 0, heading: null, anchor: null, explicit: false, start: 0, ownEnd: first, end: first },
  ];
  headings.forEach((h, idx) => {
    const ownEnd = idx + 1 < headings.length ? headings[idx + 1]!.line : lines.length;
    const next = headings.slice(idx + 1).find((o) => o.level <= h.level);
    sections.push({
      level: h.level,
      heading: h.text,
      anchor: h.anchor,
      explicit: h.explicit,
      start: h.line,
      ownEnd,
      end: next ? next.line : lines.length,
    });
  });
  return sections;
}

export function parsePage(path: string, input: string, ignoreFields: Set<string>, rootName: string): Page {
  const text = input.replace(/^﻿/, '').replaceAll('\r\n', '\n');
  const [parsed, body] = splitFrontmatter(text);
  const front = parsed ?? new Map<unknown, unknown>();
  const lines = clean(body, path.endsWith('.mdx'));
  const sections = buildSections(lines);

  const rawTitle = front.get('title');
  let title = typeof rawTitle === 'string' ? rawTitle : null;
  if (title === null) title = sections.find((s) => s.level === 1)?.heading ?? null;
  if (title === null) {
    const slash = path.lastIndexOf('/');
    const file = path.slice(slash + 1);
    const dot = file.lastIndexOf('.');
    let stem = dot >= 0 ? file.slice(0, dot) : file;
    if (stem === 'index') {
      const dir = slash >= 0 ? path.slice(0, slash) : null;
      stem = dir !== null ? dir.slice(dir.lastIndexOf('/') + 1) : rootName;
    }
    title = stem;
  }
  const rawDescription = front.get('description');

  return new Page(
    path,
    title,
    typeof rawDescription === 'string' ? rawDescription : null,
    extractMetadata(front, ignoreFields),
    lines.map((l) => l.text),
    sections,
  );
}

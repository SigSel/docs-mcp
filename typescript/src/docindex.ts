// The in-memory index: discovery, search, filters and the tool outputs (SPEC.md §4-6).

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { globToRegex, type Metadata, type MetaValue, type Page, parsePage, type Section } from './content.js';

const TOKEN = /[\p{L}\p{N}]+/gu;
const K1 = 1.2;
const B = 0.75;
const MAX_PER_PAGE = 3;

export type Filters = Record<string, string | string[]> | undefined | null;

/** A tool error whose message is shown to the model as-is. */
export class ToolFailure extends Error {}

export function tokenize(text: string): string[] {
  return (text.toLowerCase().match(TOKEN) ?? []).filter((t) => [...t].length >= 2);
}

/** Code-point order, like Python's and Rust's string comparison. */
export function compare(a: string, b: string): number {
  if (a === b) return 0;
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const ca = ia.next();
    const cb = ib.next();
    if (ca.done) return cb.done ? 0 : -1;
    if (cb.done) return 1;
    const d = ca.value.codePointAt(0)! - cb.value.codePointAt(0)!;
    if (d !== 0) return d;
  }
}

const values = (v: MetaValue): string[] => (typeof v === 'string' ? [v] : v);
const collapse = (text: string): string => text.split(/\s+/).filter(Boolean).join(' ');

export function discover(root: string, include: string[], exclude: string[]): string[] {
  const inc = include.map(globToRegex);
  const exc = exclude.map(globToRegex);
  const found: string[] = [];
  const walk = (dir: string, rel: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      const relPath = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (!entry.name.startsWith('.') && entry.name !== 'node_modules') walk(path, relPath);
      } else if (entry.isFile() || (entry.isSymbolicLink() && statSync(path, { throwIfNoEntry: false })?.isFile())) {
        if (inc.some((r) => r.test(relPath)) && !exc.some((r) => r.test(relPath))) found.push(relPath);
      }
    }
  };
  walk(root, '');
  return found.sort(compare);
}

export function pageUrl(siteUrl: string | null, path: string, anchor: string | null = null): string | null {
  if (!siteUrl) return null;
  const file = path.slice(path.lastIndexOf('/') + 1);
  let route = file.includes('.') ? path.slice(0, path.lastIndexOf('.')) : path;
  if (route === 'index') route = '';
  else if (route.endsWith('/index')) route = route.slice(0, -'/index'.length);
  const url = `${siteUrl.replace(/\/+$/, '')}/${route ? `${route}/` : ''}`;
  return anchor ? `${url}#${anchor}` : url;
}

function matches(page: Page, filters: Filters, pathPrefix: string | null | undefined): boolean {
  if (pathPrefix && !page.path.startsWith(pathPrefix)) return false;
  for (const [key, wanted] of Object.entries(filters ?? {})) {
    const have = page.metadata.get(key);
    if (have === undefined) return false;
    const haveSet = new Set(values(have).map((v) => v.toLowerCase()));
    const wantedList = Array.isArray(wanted) ? wanted : [wanted];
    if (!wantedList.some((w) => haveSet.has(String(w).toLowerCase()))) return false;
  }
  return true;
}

interface Doc {
  page: Page;
  section: Section;
  order: number;
  tf: Map<string, number>;
  length: number;
}

const metadataObject = (meta: Metadata): Record<string, MetaValue> => Object.fromEntries(meta);

function snippet(text: string, terms: Set<string>): string {
  const flat = collapse(text);
  const lower = flat.toLowerCase();
  let first = -1;
  for (const term of terms) {
    const at = lower.indexOf(term);
    if (at >= 0 && (first < 0 || at < first)) first = at;
  }
  const chars = [...flat];
  const start = first >= 0 ? Math.max(0, [...lower.slice(0, first)].length - 80) : 0;
  const out = chars.slice(start, start + 240).join('');
  return (start > 0 ? '…' : '') + out + (start + 240 < chars.length ? '…' : '');
}

export class DocIndex {
  readonly byPath: Map<string, Page>;
  private readonly docs: Doc[] = [];
  private readonly postings = new Map<string, Set<number>>();
  private readonly avgdl: number;

  constructor(
    readonly pages: Page[],
    private readonly siteUrl: string | null,
  ) {
    this.byPath = new Map(pages.map((p) => [p.path, p]));
    for (const page of pages) {
      const tags = [...[...page.metadata.values()].flatMap(values), page.description ?? ''].join(' ');
      const titleTokens = tokenize(page.title);
      const tagTokens = tokenize(tags);
      page.sections.forEach((section, order) => {
        if (section.level === 0 && !page.ownText(section).trim()) return;
        const tf = new Map<string, number>();
        const add = (term: string, n: number): void => {
          tf.set(term, (tf.get(term) ?? 0) + n);
        };
        for (const t of titleTokens) add(t, 2);
        if (section.heading) for (const t of tokenize(section.heading)) add(t, 3);
        for (const t of tagTokens) add(t, 1);
        for (const t of tokenize(page.ownText(section))) add(t, 1);
        let length = 0;
        for (const n of tf.values()) length += n;
        const id = this.docs.length;
        this.docs.push({ page, section, order, tf, length });
        for (const term of tf.keys()) {
          let set = this.postings.get(term);
          if (!set) this.postings.set(term, (set = new Set()));
          set.add(id);
        }
      });
    }
    this.avgdl = this.docs.length ? this.docs.reduce((s, d) => s + d.length, 0) / this.docs.length : 0;
  }

  static build(
    root: string,
    include: string[],
    exclude: string[],
    ignoreFields: Set<string>,
    rootName: string,
    siteUrl: string | null,
  ): DocIndex {
    if (!statSync(root, { throwIfNoEntry: false })?.isDirectory()) {
      throw new Error(`docs directory not found: ${root}`);
    }
    const pages = discover(root, include, exclude).map((rel) =>
      parsePage(rel, new TextDecoder('utf-8').decode(readFileSync(join(root, rel))), ignoreFields, rootName),
    );
    return new DocIndex(pages, siteUrl);
  }

  // --- tools -----------------------------------------------------------------

  overview(): Record<string, unknown> {
    const folders = new Map<string, number>();
    const fields = new Map<string, Map<string, number>>();
    for (const page of this.pages) {
      const folder = page.path.includes('/') ? page.path.slice(0, page.path.indexOf('/')) : '.';
      folders.set(folder, (folders.get(folder) ?? 0) + 1);
      for (const [key, value] of page.metadata) {
        let counter = fields.get(key);
        if (!counter) fields.set(key, (counter = new Map()));
        for (const v of new Set(values(value))) counter.set(v, (counter.get(v) ?? 0) + 1);
      }
    }
    const sorted = <T>(m: Map<string, T>): Array<[string, T]> => [...m].sort((a, b) => compare(a[0], b[0]));
    return {
      page_count: this.pages.length,
      folders: sorted(folders).map(([path, pages]) => ({ path, pages })),
      fields: Object.fromEntries(sorted(fields).map(([k, c]) => [k, Object.fromEntries(sorted(c))])),
    };
  }

  listDocs(pathPrefix: string | null | undefined, filters: Filters): Record<string, unknown> {
    const pages = this.pages
      .filter((p) => matches(p, filters, pathPrefix))
      .map((page) => {
        const entry: Record<string, unknown> = { path: page.path, title: page.title };
        if (page.description !== null) entry.description = page.description;
        const url = pageUrl(this.siteUrl, page.path);
        if (url !== null) entry.url = url;
        entry.metadata = metadataObject(page.metadata);
        return entry;
      });
    return { count: pages.length, pages };
  }

  search(query: string, filters: Filters, pathPrefix: string | null | undefined, limit: number): Record<string, unknown> {
    const terms = [...new Set(tokenize(query))];
    if (terms.length === 0) throw new ToolFailure('The query must contain at least one word.');
    const max = Math.max(1, Math.min(50, Math.trunc(limit)));
    const n = this.docs.length;
    const scores = new Array<number>(n).fill(0);
    const matchedTerms = new Set<string>();
    for (const q of terms) {
      const factors = new Map<string, number>();
      if (this.postings.has(q)) factors.set(q, 1);
      if ([...q].length >= 3) {
        for (const term of this.postings.keys()) if (term !== q && term.startsWith(q)) factors.set(term, 0.5);
      }
      if (factors.size === 0) continue;
      for (const t of factors.keys()) matchedTerms.add(t);
      const ids = new Set<number>();
      for (const t of factors.keys()) for (const id of this.postings.get(t)!) ids.add(id);
      const idf = Math.log(1 + (n - ids.size + 0.5) / (ids.size + 0.5));
      for (const id of ids) {
        const doc = this.docs[id]!;
        let tf = 0;
        for (const [t, f] of factors) tf += f * (doc.tf.get(t) ?? 0);
        if (tf <= 0) continue;
        const norm = K1 * (1 - B + (B * doc.length) / this.avgdl);
        scores[id]! += (idf * tf * (K1 + 1)) / (tf + norm);
      }
    }

    const ranked = [...scores.keys()]
      .filter((i) => scores[i]! > 0 && matches(this.docs[i]!.page, filters, pathPrefix))
      .sort((a, b) => {
        const da = this.docs[a]!;
        const db = this.docs[b]!;
        return scores[b]! - scores[a]! || compare(da.page.path, db.page.path) || da.order - db.order;
      });
    const results: Array<Record<string, unknown>> = [];
    const perPage = new Map<string, number>();
    for (const i of ranked) {
      const doc = this.docs[i]!;
      const seen = perPage.get(doc.page.path) ?? 0;
      if (seen >= MAX_PER_PAGE) continue;
      perPage.set(doc.page.path, seen + 1);
      const { section } = doc;
      const hit: Record<string, unknown> = {
        path: doc.page.path,
        title: doc.page.title,
        section: section.level > 0 ? section.heading : doc.page.title,
        anchor: section.anchor,
      };
      const url = pageUrl(this.siteUrl, doc.page.path, section.anchor);
      if (url !== null) hit.url = url;
      hit.score = Math.round(scores[i]! * 1000) / 1000;
      hit.snippet = snippet(doc.page.ownText(section) || (section.heading ?? ''), matchedTerms);
      results.push(hit);
      if (results.length >= max) break;
    }
    return { query, count: results.length, results };
  }

  resolve(path: string): Page {
    const p = path.replace(/^\/+|\/+$/g, '');
    const candidates = p ? [p, `${p}.md`, `${p}.mdx`, `${p}/index.md`, `${p}/index.mdx`] : ['index.md', 'index.mdx'];
    for (const c of candidates) {
      const page = this.byPath.get(c);
      if (page) return page;
    }
    const last = p.slice(p.lastIndexOf('/') + 1).toLowerCase();
    const similar = this.pages
      .filter((pg) => last && pg.path.toLowerCase().includes(last))
      .slice(0, 5)
      .map((pg) => pg.path);
    const hint = similar.length ? ` Did you mean: ${similar.join(', ')}?` : '';
    throw new ToolFailure(`No page '${path}'.${hint} Use list_docs or search_docs to find pages.`);
  }

  readDoc(path: string, section: string | null | undefined): string {
    const page = this.resolve(path);
    if (!section) return this.render(page, null);
    const wanted = section.toLowerCase().replace(/^#+/, '');
    const found =
      page.sections.find((s) => s.anchor !== null && s.anchor.toLowerCase() === wanted) ??
      page.sections.find((s) => s.heading !== null && s.heading.toLowerCase() === section.toLowerCase());
    if (!found) {
      const anchors = page.sections.flatMap((s) => (s.anchor !== null ? [s.anchor] : [])).join(', ');
      throw new ToolFailure(`No section '${section}' in ${page.path}. Anchors: ${anchors}`);
    }
    return this.render(page, found);
  }

  getSection(anchor: string): string {
    const wanted = anchor.toLowerCase().replace(/^#+/, '');
    let found = this.pages.flatMap((p) =>
      p.sections.filter((s) => s.anchor !== null && s.anchor.toLowerCase() === wanted).map((s) => [p, s] as const),
    );
    const explicit = found.filter(([, s]) => s.explicit);
    if (explicit.length) found = explicit;
    if (found.length === 0) {
      throw new ToolFailure(`No section with anchor '${anchor}'. Use search_docs to find it by text.`);
    }
    if (found.length === 1) return this.render(found[0]![0], found[0]![1]);
    return [
      `Anchor '${anchor}' matches ${found.length} sections. Read one with read_doc(path, section):`,
      ...found.map(([p, s]) => `- ${p.path}#${s.anchor} (${s.heading})`),
    ].join('\n');
  }

  private render(page: Page, section: Section | null): string {
    const header = [`path: ${page.path}`, `title: ${collapse(page.title)}`];
    if (page.description !== null) header.push(`description: ${collapse(page.description)}`);
    const url = pageUrl(this.siteUrl, page.path, section ? section.anchor : null);
    if (url !== null) header.push(`url: ${url}`);
    if (section) header.push(`section: ${section.anchor}`);
    for (const [key, value] of page.metadata) header.push(`${key}: ${collapse(values(value).join(', '))}`);
    const body = section ? page.sectionText(section) : page.body;
    return `---\n${header.join('\n')}\n---\n\n${body}`;
  }
}

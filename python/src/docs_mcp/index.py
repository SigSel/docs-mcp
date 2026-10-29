"""The in-memory index: discovery, search, filters and the tool outputs (SPEC.md §4-6)."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .content import LINK, Metadata, Page, Section, glob_to_regex, parse_file, parse_page

TOKEN = re.compile(r"[^\W_]+")
K1, B = 1.2, 0.75
MAX_PER_PAGE = 3
PREFIX_FACTOR, PREFIX_MIN_LEN = 0.5, 3
FUZZY_FACTOR, FUZZY_MIN_LEN = 0.3, 6
MAX_FILE_BYTES = 1_000_000
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class ToolFailure(Exception):
    """A tool error whose message is shown to the model as-is."""


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if len(t) >= 2]


def discover(root: Path, include: list[str], exclude: list[str]) -> list[str]:
    inc = [glob_to_regex(g) for g in include]
    exc = [glob_to_regex(g) for g in exclude]
    found: list[str] = []
    # os.walk does not descend into linked directories. Linked files are skipped below: a link committed to the repo
    # could point at a secret outside it (SPEC.md §4.1).
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or _secret(name):
                continue
            rel = path.relative_to(root).as_posix()
            if any(r.fullmatch(rel) for r in inc) and not any(r.fullmatch(rel) for r in exc):
                found.append(rel)
    return sorted(found)


def _secret(name: str) -> bool:
    """Files that usually hold secrets are never pages, whatever the globs say (SPEC.md §4.1)."""
    lower = name.lower()
    return lower == ".env" or lower.startswith(".env.") or lower.endswith(SECRET_SUFFIXES)


def _markdown(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower().endswith((".md", ".mdx"))


def load_pages(
    root: Path, include: list[str], exclude: list[str], ignore_fields: frozenset[str], root_name: str, source: str
) -> list[Page]:
    """The pages under `root`: Markdown pages and file pages, without files that are too big or not text."""
    if not root.is_dir():
        raise FileNotFoundError(f"docs directory not found: {root}")
    pages = []
    for rel in discover(root, include, exclude):
        path = root / rel
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES or b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        page = parse_page(rel, text, ignore_fields, root_name) if _markdown(rel) else parse_file(rel, text)
        page.source = source
        pages.append(page)
    return pages


def page_url(site_url: str | None, path: str, anchor: str | None = None) -> str | None:
    if not site_url:
        return None
    route = _strip_extension(path)
    if route == "index":
        route = ""
    elif route.endswith("/index"):
        route = route[: -len("/index")]
    url = site_url.rstrip("/") + "/" + (route + "/" if route else "")
    return f"{url}#{anchor}" if anchor else url


def _matches(page: Page, filters: dict[str, Any] | None, path_prefix: str | None, source: str | None = None) -> bool:
    if source is not None and page.source != source:
        return False
    if path_prefix and not page.path.startswith(path_prefix):
        return False
    for key, wanted in (filters or {}).items():
        have = page.metadata.get(key)
        if have is None:
            return False
        have_set = {v.lower() for v in ([have] if isinstance(have, str) else have)}
        wanted_set = {str(v).lower() for v in ([wanted] if isinstance(wanted, (str, int, float, bool)) else wanted)}
        if not have_set & wanted_set:
            return False
    return True


@dataclass
class _Doc:
    page: Page
    section: Section
    order: int
    tf: Counter[str]  # the section's own terms: its heading and text
    # Its page's title, metadata and path terms, one Counter shared by every section of the page. Copied into each
    # section, a page with many of both cost memory for their product (SPEC.md §5).
    page_tf: Counter[str]
    length: float


@dataclass
class _Postings:
    """Where a term occurs: in sections' own terms, and in whole pages, as the doc ids of their sections."""

    docs: set[int] = field(default_factory=set)
    pages: list[range] = field(default_factory=list)


def _collapse(text: str) -> str:
    return " ".join(text.split())


class DocIndex:
    """One search index over the pages of every source that has loaded (SPEC.md §5, §7.3)."""

    def __init__(self, pages: list[Page], site_urls: dict[str, str | None], names: list[str], multi: bool) -> None:
        self.pages = pages  # in configured source order, each source's sorted by path
        self.by_path = {(p.source, p.path): p for p in pages}
        self.site_urls = site_urls
        self.names = names
        self.order = {name: i for i, name in enumerate(names)}
        self.multi = multi
        self.docs: list[_Doc] = []
        self.postings: dict[str, _Postings] = {}
        for page in pages:
            tags = " ".join([*metadata_values(page.metadata), page.description or ""])
            title_tf, tags_tf = Counter(tokenize(page.title)), Counter(tokenize(tags))
            path_tf = Counter(tokenize(_strip_extension(page.path)))
            shared: Counter[str] = Counter()
            for counts, weight in ((title_tf, 2), (tags_tf, 1), (path_tf, 3)):
                for term, n in counts.items():
                    shared[term] += weight * n
            shared_length = sum(shared.values())
            first = len(self.docs)
            for order, section in enumerate(page.sections):
                if section.level == 0 and not page.own_text(section).strip():
                    continue
                heading = tokenize(section.heading) if section.heading else []
                tf: Counter[str] = Counter()
                for term in heading:
                    tf[term] += 3
                for term in tokenize(_search_text(page, section)):
                    tf[term] += 1
                if len(self.docs) == first:
                    # The page's terms join the vocabulary in the order the page reads, as they did when each section
                    # held them: title, heading, metadata, path, then the text.
                    for term in (*title_tf, *heading, *tags_tf, *path_tf, *tf):
                        self.postings.setdefault(term, _Postings())
                doc_id = len(self.docs)
                self.docs.append(_Doc(page, section, order, tf, shared, float(shared_length + sum(tf.values()))))
                for term in tf:
                    self.postings.setdefault(term, _Postings()).docs.add(doc_id)
            if len(self.docs) > first:
                for term in shared:
                    self.postings[term].pages.append(range(first, len(self.docs)))
        self.avgdl = (sum(d.length for d in self.docs) / len(self.docs)) if self.docs else 0.0

    # --- tools ---------------------------------------------------------------

    def url(self, page: Page, anchor: str | None = None) -> str | None:
        return None if page.file else page_url(self.site_urls.get(page.source), page.path, anchor)

    def _named(self, page: Page, entry: dict[str, Any]) -> dict[str, Any]:
        return {"source": page.source, **entry} if self.multi else entry

    def overview(self, source: str) -> dict[str, Any]:
        pages = [p for p in self.pages if p.source == source]
        folders = Counter(p.path.split("/", 1)[0] if "/" in p.path else "." for p in pages)
        fields: dict[str, Counter[str]] = {}
        for page in pages:
            for key, value in page.metadata.items():
                values = {value} if isinstance(value, str) else set(value)
                counter = fields.setdefault(key, Counter())
                for v in values:
                    counter[v] += 1
        return {
            "page_count": len(pages),
            "folders": [{"path": f, "pages": n} for f, n in sorted(folders.items())],
            "fields": {k: dict(sorted(c.items())) for k, c in sorted(fields.items())},
        }

    def list_docs(self, path_prefix: str | None, filters: dict[str, Any] | None, source: str | None) -> dict[str, Any]:
        pages = []
        for page in self.pages:
            if not _matches(page, filters, path_prefix, source):
                continue
            entry: dict[str, Any] = {"path": page.path, "title": page.title}
            if page.description is not None:
                entry["description"] = page.description
            if (url := self.url(page)) is not None:
                entry["url"] = url
            entry["metadata"] = page.metadata
            pages.append(self._named(page, entry))
        return {"count": len(pages), "pages": pages}

    def search(
        self, query: str, filters: dict[str, Any] | None, path_prefix: str | None, limit: int, source: str | None
    ) -> dict[str, Any]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            raise ToolFailure("The query must contain at least one word.")
        limit = max(1, min(50, limit))
        n = len(self.docs)
        scores = [0.0] * n
        matched_terms: set[str] = set()
        for q in terms:
            factors = {q: 1.0} if q in self.postings else {}
            if len(q) >= PREFIX_MIN_LEN:
                for term in self.postings:
                    if term != q and term.startswith(q):
                        factors[term] = PREFIX_FACTOR
            if len(q) >= FUZZY_MIN_LEN:
                for term in self.postings:
                    if term not in factors and term[0] == q[0] and _within_one_edit(q, term):
                        factors[term] = FUZZY_FACTOR
            if not factors:
                continue
            matched_terms.update(factors)
            found = [self.postings[t] for t in factors]
            doc_ids: set[int] = set().union(*(p.docs for p in found), *(ids for p in found for ids in p.pages))
            idf = math.log(1 + (n - len(doc_ids) + 0.5) / (len(doc_ids) + 0.5))
            for doc_id in doc_ids:
                doc = self.docs[doc_id]
                tf = sum(f * (doc.page_tf.get(t, 0) + doc.tf.get(t, 0)) for t, f in factors.items())
                if tf <= 0:
                    continue
                norm = K1 * (1 - B + B * doc.length / self.avgdl)
                scores[doc_id] += idf * tf * (K1 + 1) / (tf + norm)

        ranked = sorted(
            (i for i in range(n) if scores[i] > 0 and _matches(self.docs[i].page, filters, path_prefix, source)),
            key=lambda i: (
                -scores[i],
                self.order[self.docs[i].page.source],
                self.docs[i].page.path,
                self.docs[i].order,
            ),
        )
        results, per_page = [], Counter[tuple[str, str]]()
        for i in ranked:
            doc = self.docs[i]
            key = (doc.page.source, doc.page.path)
            if per_page[key] >= MAX_PER_PAGE:
                continue
            per_page[key] += 1
            section = doc.section
            hit: dict[str, Any] = {
                "path": doc.page.path,
                "title": doc.page.title,
                "section": section.heading if section.level > 0 else doc.page.title,
                "anchor": section.anchor,
            }
            if (url := self.url(doc.page, section.anchor)) is not None:
                hit["url"] = url
            hit["score"] = round(scores[i], 3)
            hit["snippet"] = _snippet(_search_text(doc.page, section) or (section.heading or ""), matched_terms)
            results.append(self._named(doc.page, hit))
            if len(results) >= limit:
                break
        return {"query": query, "count": len(results), "results": results}

    def resolve(self, path: str, source: str | None) -> Page:
        p = path.strip("/")
        candidates = [p, f"{p}.md", f"{p}.mdx", f"{p}/index.md", f"{p}/index.mdx"] if p else ["index.md", "index.mdx"]
        found = []
        for name in [source] if source is not None else self.names:
            page = next((self.by_path[(name, c)] for c in candidates if (name, c) in self.by_path), None)
            if page is not None:
                found.append(page)
        if len(found) == 1:
            return found[0]
        if found:
            names = ", ".join(pg.source for pg in found)
            raise ToolFailure(f"'{path}' is in several sources: {names}. Pass source to read_doc.")
        last = p.rsplit("/", 1)[-1].lower()
        similar = [
            pg for pg in self.pages if (source is None or pg.source == source) and last and last in pg.path.lower()
        ]
        shown = [f"{pg.path} ({pg.source})" if self.multi else pg.path for pg in similar[:5]]
        hint = f" Did you mean: {', '.join(shown)}?" if shown else ""
        raise ToolFailure(f"No page '{path}'.{hint} Use list_docs or search_docs to find pages.")

    def read_doc(self, path: str, section: str | None, source: str | None) -> str:
        page = self.resolve(path, source)
        if not section:
            return self._render(page, None)
        wanted = section.lower().lstrip("#")
        found = next((s for s in page.sections if s.anchor is not None and s.anchor.lower() == wanted), None)
        if found is None:
            found = next(
                (s for s in page.sections if s.heading is not None and s.heading.lower() == section.lower()), None
            )
        if found is None:
            anchors = ", ".join(s.anchor for s in page.sections if s.anchor is not None)
            raise ToolFailure(f"No section '{section}' in {page.path}. Anchors: {anchors}")
        return self._render(page, found)

    def get_section(self, anchor: str) -> str:
        wanted = anchor.lower().lstrip("#")
        matches = [
            (p, s) for p in self.pages for s in p.sections if s.anchor is not None and s.anchor.lower() == wanted
        ]
        explicit = [(p, s) for p, s in matches if s.explicit]
        if explicit:
            matches = explicit
        if not matches:
            raise ToolFailure(f"No section with anchor '{anchor}'. Use search_docs to find it by text.")
        if len(matches) == 1:
            return self._render(*matches[0])
        if self.multi:
            lines = [
                f"Anchor '{anchor}' matches {len(matches)} sections. Read one with read_doc(path, section, source):"
            ]
            lines += [f"- {p.source}: {p.path}#{s.anchor} ({s.heading})" for p, s in matches]
        else:
            lines = [f"Anchor '{anchor}' matches {len(matches)} sections. Read one with read_doc(path, section):"]
            lines += [f"- {p.path}#{s.anchor} ({s.heading})" for p, s in matches]
        return "\n".join(lines)

    def _render(self, page: Page, section: Section | None) -> str:
        header = [f"source: {page.source}"] if self.multi else []
        header += [f"path: {page.path}", f"title: {_collapse(page.title)}"]
        if page.description is not None:
            header.append(f"description: {_collapse(page.description)}")
        if (url := self.url(page, section.anchor if section else None)) is not None:
            header.append(f"url: {url}")
        if section is not None:
            header.append(f"section: {section.anchor}")
        for key, value in page.metadata.items():
            header.append(f"{key}: {_collapse(value if isinstance(value, str) else ', '.join(value))}")
        if page.file:
            body = _fenced(page)
        elif section is not None:
            body = page.section_text(section)
        else:
            body = page.body
        return "---\n" + "\n".join(header) + "\n---\n\n" + body


def _within_one_edit(a: str, b: str) -> bool:
    """Levenshtein distance at most 1: one insertion, deletion or substitution (code points)."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1 :] == b[i + 1 :]
    return a[i:] == b[i + 1 :]


def _strip_extension(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path


def _search_text(page: Page, section: Section) -> str:
    """A section's own text as search sees it: in Markdown, links count as their text, not their URL."""
    text = page.own_text(section)
    return text if page.file else LINK.sub(r"\1", text)


def _fenced(page: Page) -> str:
    """A file page's text in a code block that its own backticks cannot close (SPEC.md §4.7)."""
    text = page.body if page.body.endswith("\n") else page.body + "\n"
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    name = page.path.rsplit("/", 1)[-1]
    info = (name.rsplit(".", 1)[1] if "." in name else name).lower()
    return f"{fence}{info}\n{text}{fence}"


def _snippet(text: str, terms: set[str]) -> str:
    flat = _collapse(text)
    lower = flat.lower()
    positions = [p for p in (lower.find(t) for t in terms) if p >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    out = flat[start : start + 240]
    return ("…" if start > 0 else "") + out + ("…" if start + 240 < len(flat) else "")


def metadata_values(meta: Metadata) -> list[str]:
    return [v for vals in meta.values() for v in ([vals] if isinstance(vals, str) else vals)]

"""The in-memory index: discovery, search, filters and the tool outputs (SPEC.md §4-6)."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content import LINK, Metadata, Page, Section, glob_to_regex, parse_page

TOKEN = re.compile(r"[^\W_]+")
K1, B = 1.2, 0.75
MAX_PER_PAGE = 3
PREFIX_FACTOR, PREFIX_MIN_LEN = 0.5, 3
FUZZY_FACTOR, FUZZY_MIN_LEN = 0.3, 6


class ToolFailure(Exception):
    """A tool error whose message is shown to the model as-is."""


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if len(t) >= 2]


def discover(root: Path, include: list[str], exclude: list[str]) -> list[str]:
    inc = [glob_to_regex(g) for g in include]
    exc = [glob_to_regex(g) for g in exclude]
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if any(r.fullmatch(rel) for r in inc) and not any(r.fullmatch(rel) for r in exc):
                found.append(rel)
    return sorted(found)


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


def _matches(page: Page, filters: dict[str, Any] | None, path_prefix: str | None) -> bool:
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
    tf: Counter[str]
    length: float


def _collapse(text: str) -> str:
    return " ".join(text.split())


class DocIndex:
    def __init__(self, pages: list[Page], site_url: str | None) -> None:
        self.pages = pages
        self.by_path = {p.path: p for p in pages}
        self.site_url = site_url
        self.docs: list[_Doc] = []
        self.postings: dict[str, set[int]] = {}
        for page in pages:
            tags = " ".join([*metadata_values(page.metadata), page.description or ""])
            title_tf, tags_tf = Counter(tokenize(page.title)), Counter(tokenize(tags))
            path_tf = Counter(tokenize(_strip_extension(page.path)))
            for order, section in enumerate(page.sections):
                if section.level == 0 and not page.own_text(section).strip():
                    continue
                tf: Counter[str] = Counter()
                for term, n in title_tf.items():
                    tf[term] += 2 * n
                if section.heading:
                    for term in tokenize(section.heading):
                        tf[term] += 3
                for term, n in tags_tf.items():
                    tf[term] += n
                for term, n in path_tf.items():
                    tf[term] += 3 * n
                for term in tokenize(_search_text(page, section)):
                    tf[term] += 1
                doc_id = len(self.docs)
                self.docs.append(_Doc(page, section, order, tf, float(sum(tf.values()))))
                for term in tf:
                    self.postings.setdefault(term, set()).add(doc_id)
        self.avgdl = (sum(d.length for d in self.docs) / len(self.docs)) if self.docs else 0.0

    @classmethod
    def build(
        cls,
        root: Path,
        include: list[str],
        exclude: list[str],
        ignore_fields: frozenset[str],
        root_name: str,
        site_url: str | None,
    ) -> DocIndex:
        if not root.is_dir():
            raise FileNotFoundError(f"docs directory not found: {root}")
        pages = [
            parse_page(rel, (root / rel).read_bytes().decode("utf-8", errors="replace"), ignore_fields, root_name)
            for rel in discover(root, include, exclude)
        ]
        return cls(pages, site_url)

    # --- tools ---------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        folders = Counter(p.path.split("/", 1)[0] if "/" in p.path else "." for p in self.pages)
        fields: dict[str, Counter[str]] = {}
        for page in self.pages:
            for key, value in page.metadata.items():
                values = {value} if isinstance(value, str) else set(value)
                counter = fields.setdefault(key, Counter())
                for v in values:
                    counter[v] += 1
        return {
            "page_count": len(self.pages),
            "folders": [{"path": f, "pages": n} for f, n in sorted(folders.items())],
            "fields": {k: dict(sorted(c.items())) for k, c in sorted(fields.items())},
        }

    def list_docs(self, path_prefix: str | None, filters: dict[str, Any] | None) -> dict[str, Any]:
        pages = []
        for page in self.pages:
            if not _matches(page, filters, path_prefix):
                continue
            entry: dict[str, Any] = {"path": page.path, "title": page.title}
            if page.description is not None:
                entry["description"] = page.description
            if (url := page_url(self.site_url, page.path)) is not None:
                entry["url"] = url
            entry["metadata"] = page.metadata
            pages.append(entry)
        return {"count": len(pages), "pages": pages}

    def search(self, query: str, filters: dict[str, Any] | None, path_prefix: str | None, limit: int) -> dict[str, Any]:
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
            doc_ids: set[int] = set().union(*(self.postings[t] for t in factors))
            idf = math.log(1 + (n - len(doc_ids) + 0.5) / (len(doc_ids) + 0.5))
            for doc_id in doc_ids:
                doc = self.docs[doc_id]
                tf = sum(f * doc.tf.get(t, 0) for t, f in factors.items())
                if tf <= 0:
                    continue
                norm = K1 * (1 - B + B * doc.length / self.avgdl)
                scores[doc_id] += idf * tf * (K1 + 1) / (tf + norm)

        ranked = sorted(
            (i for i in range(n) if scores[i] > 0 and _matches(self.docs[i].page, filters, path_prefix)),
            key=lambda i: (-scores[i], self.docs[i].page.path, self.docs[i].order),
        )
        results, per_page = [], Counter[str]()
        for i in ranked:
            doc = self.docs[i]
            if per_page[doc.page.path] >= MAX_PER_PAGE:
                continue
            per_page[doc.page.path] += 1
            section = doc.section
            hit: dict[str, Any] = {
                "path": doc.page.path,
                "title": doc.page.title,
                "section": section.heading if section.level > 0 else doc.page.title,
                "anchor": section.anchor,
            }
            if (url := page_url(self.site_url, doc.page.path, section.anchor)) is not None:
                hit["url"] = url
            hit["score"] = round(scores[i], 3)
            hit["snippet"] = _snippet(_search_text(doc.page, section) or (section.heading or ""), matched_terms)
            results.append(hit)
            if len(results) >= limit:
                break
        return {"query": query, "count": len(results), "results": results}

    def resolve(self, path: str) -> Page:
        p = path.strip("/")
        candidates = [p, f"{p}.md", f"{p}.mdx", f"{p}/index.md", f"{p}/index.mdx"] if p else ["index.md", "index.mdx"]
        for c in candidates:
            if c in self.by_path:
                return self.by_path[c]
        last = p.rsplit("/", 1)[-1].lower()
        similar = [pg.path for pg in self.pages if last and last in pg.path.lower()][:5]
        hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
        raise ToolFailure(f"No page '{path}'.{hint} Use list_docs or search_docs to find pages.")

    def read_doc(self, path: str, section: str | None) -> str:
        page = self.resolve(path)
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
        lines = [f"Anchor '{anchor}' matches {len(matches)} sections. Read one with read_doc(path, section):"]
        lines += [f"- {p.path}#{s.anchor} ({s.heading})" for p, s in matches]
        return "\n".join(lines)

    def _render(self, page: Page, section: Section | None) -> str:
        header = [f"path: {page.path}", f"title: {_collapse(page.title)}"]
        if page.description is not None:
            header.append(f"description: {_collapse(page.description)}")
        if (url := page_url(self.site_url, page.path, section.anchor if section else None)) is not None:
            header.append(f"url: {url}")
        if section is not None:
            header.append(f"section: {section.anchor}")
        for key, value in page.metadata.items():
            header.append(f"{key}: {_collapse(value if isinstance(value, str) else ', '.join(value))}")
        body = page.section_text(section) if section is not None else page.body
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
    """A section's own text as search sees it: links count as their text, not their URL."""
    return LINK.sub(r"\1", page.own_text(section))


def _snippet(text: str, terms: set[str]) -> str:
    flat = _collapse(text)
    lower = flat.lower()
    positions = [p for p in (lower.find(t) for t in terms) if p >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    out = flat[start : start + 240]
    return ("…" if start > 0 else "") + out + ("…" if start + 240 < len(flat) else "")


def metadata_values(meta: Metadata) -> list[str]:
    return [v for vals in meta.values() for v in ([vals] if isinstance(vals, str) else vals)]

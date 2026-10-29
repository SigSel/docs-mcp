"""Turning a Markdown/MDX file into a page: frontmatter, cleaning, sections (SPEC.md §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

Metadata = dict[str, str | list[str]]

FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
MDX_MODULE_LINE = re.compile(r"^\s*(import|export)\s")
INLINE_CODE = re.compile(r"`[^`\n]+`")
TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9._:-]*(\s[^<>]*)?/?>")
LABEL_ATTR = re.compile(r"""\blabel\s*=\s*(?:"([^"]*)"|'([^']*)')""")
TITLE_ATTR = re.compile(r"""\btitle\s*=\s*(?:"([^"]*)"|'([^']*)')""")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
HEADING_ATTR_OPEN = re.compile(r"\{\s*#([A-Za-z0-9_-]+)")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


class _Yaml12Loader(yaml.SafeLoader):
    """PyYAML speaks YAML 1.1; this drops the 1.1-only booleans (yes/no/on/off) and timestamps, and rejects
    duplicate keys as YAML 1.2 does."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        return super().construct_mapping(node, deep)


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_yaml(text: str) -> Any:
    """YAML 1.2, building only plain values. Raises yaml.YAMLError on bad input."""
    return yaml.load(text, Loader=_Yaml12Loader)


MAX_YAML_DEPTH = 32


def _bounded(front: str) -> bool:
    """No aliases and no deep nesting, checked on the events before anything is built: aliases can expand a few bytes
    into gigabytes, and the composer recurses once per level (SPEC.md §4.2)."""
    depth = 0
    for event in yaml.parse(front, Loader=_Yaml12Loader):
        if isinstance(event, yaml.AliasEvent):
            return False
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                return False
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
    return True


@dataclass
class Section:
    level: int  # 0 for the intro
    heading: str | None
    anchor: str | None
    explicit: bool
    start: int  # line of the heading (0 for the intro)
    own_end: int  # next heading of any level
    end: int  # next heading of the same or a higher level


@dataclass
class Page:
    path: str
    title: str
    description: str | None
    metadata: Metadata
    lines: list[str]
    sections: list[Section] = field(default_factory=list)
    file: bool = False  # not Markdown: served as written (SPEC.md §4.7)
    source: str = ""

    @property
    def body(self) -> str:
        return "\n".join(self.lines)

    def section_text(self, section: Section) -> str:
        return "\n".join(self.lines[section.start : section.end]).strip()

    def own_text(self, section: Section) -> str:
        start = section.start if section.level == 0 else section.start + 1
        return "\n".join(self.lines[start : section.own_end])


@dataclass
class _Line:
    text: str
    code: bool
    anchor: str | None = None


def glob_to_regex(glob: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Returns (mapping or None, body). Broken YAML still has its block skipped."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            body = "\n".join(lines[i + 1 :])
            front = "\n".join(lines[1:i])
            try:
                data = yaml.load(front, Loader=_Yaml12Loader) if _bounded(front) else None
            except yaml.YAMLError:
                return None, body
            return (data if isinstance(data, dict) else None), body
    return None, text


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if not value.is_integer() else str(int(value))
    if isinstance(value, str):
        return value
    return None


def extract_metadata(front: dict[str, Any], ignore: frozenset[str]) -> Metadata:
    meta: Metadata = {}
    for key, value in front.items():
        key = str(key)
        if key in ("title", "description") or key in ignore:
            continue
        if isinstance(value, list):
            meta[key] = [s for s in (_scalar(v) for v in value) if s is not None]
        elif (s := _scalar(value)) is not None:
            meta[key] = s
    return meta


# JSX comments, heading attributes and closing hashes are found by hand, not with the regexes the other
# implementations use: Python's backtracking made those cost the square of a line's length, and it holds the GIL
# meanwhile, so one crafted page froze the whole server for hours (SPEC.md §4.4). Each function matches what its
# regex does in a linear-time engine.


def _strip_jsx_comments(text: str) -> str:
    r"""Drops every `{/* … */}`, like `\{/\*.*?\*/\}` with DOTALL."""
    out, i = [], 0
    while (start := text.find("{/*", i)) != -1 and (end := text.find("*/}", start + 3)) != -1:
        out.append(text[i:start])
        i = end + 3
    out.append(text[i:])
    return "".join(out)


def _heading_attr(text: str) -> tuple[str, int] | None:
    r"""The `{#id}` that ends a heading, as (id, where it starts), like `\s*\{\s*#([A-Za-z0-9_-]+)[^}]*\}\s*$`: the
    first `{#id …` after the line's second-to-last `}`, when the line ends with `}`."""
    text = text.rstrip()
    if not text.endswith("}"):
        return None
    m = HEADING_ATTR_OPEN.search(text, text.rfind("}", 0, len(text) - 1) + 1)
    return (m.group(1), m.start()) if m else None


def _strip_closing_hashes(text: str) -> str:
    r"""Drops a closing run of `#` and the whitespace before it, like `(^|\s+)#+\s*$`."""
    tail = text.rstrip()
    start = len(tail.rstrip("#"))
    if start == len(tail) or (start and not tail[start - 1].isspace()):
        return text
    return tail[:start].rstrip()


def _clean_prose(text: str, mdx: bool) -> str:
    if mdx:
        text = "\n".join(line for line in text.split("\n") if not MDX_MODULE_LINE.match(line))
    protected: list[str] = []

    def protect(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    def tag(m: re.Match[str]) -> str:
        if m.group(0).startswith("</"):
            return ""
        attrs = m.group(1) or ""
        attr = LABEL_ATTR.search(attrs) or TITLE_ATTR.search(attrs)
        if attr:
            return f"**{attr.group(1) if attr.group(1) is not None else attr.group(2)}**"
        return ""

    text = INLINE_CODE.sub(protect, text)
    text = _strip_jsx_comments(text)
    text = TAG.sub(tag, text)
    return PLACEHOLDER.sub(lambda m: protected[int(m.group(1))], text)


def clean(body: str, mdx: bool) -> list[_Line]:
    """Cleans prose (SPEC.md §4.4) and returns lines tagged code/prose with heading anchors."""
    raw = body.split("\n")
    segments: list[tuple[bool, list[str]]] = []  # (is_code, lines)
    i = 0
    while i < len(raw):
        m = FENCE_OPEN.match(raw[i])
        if m:
            char, length = m.group(1)[0], len(m.group(1))
            close = re.compile(rf"^\s*{re.escape(char)}{{{length},}}\s*$")
            j = i + 1
            while j < len(raw) and not close.match(raw[j]):
                j += 1
            segments.append((True, raw[i : j + 1]))
            i = j + 1
        else:
            j = i
            while j < len(raw) and not FENCE_OPEN.match(raw[j]):
                j += 1
            segments.append((False, raw[i:j]))
            i = j

    lines: list[_Line] = []
    for is_code, seg in segments:
        if is_code:
            lines.extend(_Line(t, True) for t in seg)
            continue
        for text in _clean_prose("\n".join(seg), mdx).split("\n"):
            text = text.rstrip()
            anchor = None
            if HEADING.match(text) and (attr := _heading_attr(text)):
                anchor, start = attr
                text = text[:start].rstrip()
            if text == "" and lines and not lines[-1].code and lines[-1].text == "":
                continue
            lines.append(_Line(text, False, anchor))

    while lines and not lines[0].text.strip():
        lines.pop(0)
    while lines and not lines[-1].text.strip():
        lines.pop()
    return lines


def heading_text(raw: str) -> str:
    text = _strip_closing_hashes(raw)
    return LINK.sub(r"\1", text).strip()


def slugify(text: str) -> str:
    text = LINK.sub(r"\1", text).lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return text.strip().replace(" ", "-")


def build_sections(lines: list[_Line]) -> list[Section]:
    headings: list[tuple[int, int, str, str, bool]] = []  # (line, level, text, anchor, explicit)
    occurrences: dict[str, int] = {}
    for n, line in enumerate(lines):
        if line.code:
            continue
        m = HEADING.match(line.text)
        if not m:
            continue
        text = heading_text(m.group(2))
        if line.anchor is not None:
            anchor, explicit = line.anchor, True
        else:
            base = slug = slugify(text)
            while slug in occurrences:
                occurrences[base] += 1
                slug = f"{base}-{occurrences[base]}"
            occurrences[slug] = 0
            anchor, explicit = slug, False
        headings.append((n, len(m.group(1)), text, anchor, explicit))

    first = headings[0][0] if headings else len(lines)
    sections = [Section(0, None, None, False, 0, first, first)]
    for idx, (n, level, text, anchor, explicit) in enumerate(headings):
        own_end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        # By index, not over `headings[idx + 1 :]`: copying the rest of the list for every heading made a page of
        # many headings cost the square of their number.
        later = range(idx + 1, len(headings))
        end = next((headings[k][0] for k in later if headings[k][1] <= level), len(lines))
        sections.append(Section(level, text, anchor, explicit, n, own_end, end))
    return sections


def parse_page(path: str, text: str, ignore_fields: frozenset[str], root_name: str) -> Page:
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    front, body = split_frontmatter(text)
    front = front or {}
    lines = clean(body, mdx=path.endswith(".mdx"))
    sections = build_sections(lines)

    title = front.get("title") if isinstance(front.get("title"), str) else None
    if title is None:
        title = next((s.heading for s in sections if s.level == 1), None)
    if title is None:
        parts = path.rsplit("/", 1)
        stem = parts[-1].rsplit(".", 1)[0]
        if stem == "index":
            stem = parts[0].rsplit("/", 1)[-1] if len(parts) == 2 else root_name
        title = stem
    description = front.get("description") if isinstance(front.get("description"), str) else None

    return Page(
        path=path,
        title=title,
        description=description,
        metadata=extract_metadata(front, ignore_fields),
        lines=[line.text for line in lines],
        sections=sections,
    )


def parse_file(path: str, text: str) -> Page:
    """A file that is not Markdown: one intro section holding the text as written (SPEC.md §4.7)."""
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    return Page(
        path=path,
        title=path.rsplit("/", 1)[-1],
        description=None,
        metadata={},
        lines=lines,
        sections=[Section(0, None, None, False, 0, len(lines), len(lines))],
        file=True,
    )

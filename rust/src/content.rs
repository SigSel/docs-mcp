//! Turning a Markdown/MDX file into a page: frontmatter, cleaning, sections (SPEC.md §4).

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use indexmap::IndexMap;
use regex::{Captures, Regex};
use serde::Serialize;
use yaml_rust2::{Yaml, YamlLoader};

macro_rules! re {
    ($name:ident, $pattern:expr) => {
        static $name: LazyLock<Regex> =
            LazyLock::new(|| Regex::new($pattern).expect("valid regex"));
    };
}

re!(FENCE_OPEN, r"^\s*(`{3,}|~{3,})");
re!(MDX_MODULE_LINE, r"^\s*(import|export)\s");
re!(INLINE_CODE, r"`[^`\n]+`");
re!(JSX_COMMENT, r"(?s)\{/\*.*?\*/\}");
re!(TAG, r"</?[A-Za-z][A-Za-z0-9._:-]*(\s[^<>]*)?/?>");
re!(LABEL_ATTR, r#"\blabel\s*=\s*(?:"([^"]*)"|'([^']*)')"#);
re!(TITLE_ATTR, r#"\btitle\s*=\s*(?:"([^"]*)"|'([^']*)')"#);
re!(HEADING, r"^(#{1,6})[ \t]+(.*)$");
re!(HEADING_ATTR, r"\s*\{\s*#([A-Za-z0-9_-]+)[^}]*\}\s*$");
re!(CLOSING_HASHES, r"(^|\s+)#+\s*$");
re!(LINK, r"\[([^\]]*)\]\([^)]*\)");
re!(PLACEHOLDER, r"\x00(\d+)\x00");
re!(NOT_SLUG_CHAR, r"[^\p{L}\p{N} _-]");

/// A metadata value: a single string, or a list of strings.
#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum MetaValue {
    One(String),
    Many(Vec<String>),
}

impl MetaValue {
    pub fn values(&self) -> &[String] {
        match self {
            MetaValue::One(v) => std::slice::from_ref(v),
            MetaValue::Many(v) => v,
        }
    }
}

pub type Metadata = IndexMap<String, MetaValue>;

#[derive(Debug, Clone)]
pub struct Section {
    /// 0 for the intro.
    pub level: usize,
    pub heading: Option<String>,
    pub anchor: Option<String>,
    pub explicit: bool,
    /// Line of the heading (0 for the intro).
    pub start: usize,
    /// Next heading of any level.
    pub own_end: usize,
    /// Next heading of the same or a higher level.
    pub end: usize,
}

#[derive(Debug)]
pub struct Page {
    pub path: String,
    pub title: String,
    pub description: Option<String>,
    pub metadata: Metadata,
    pub lines: Vec<String>,
    pub sections: Vec<Section>,
}

impl Page {
    pub fn body(&self) -> String {
        self.lines.join("\n")
    }

    pub fn section_text(&self, section: &Section) -> String {
        self.lines[section.start..section.end]
            .join("\n")
            .trim()
            .to_string()
    }

    pub fn own_text(&self, section: &Section) -> String {
        let start = if section.level == 0 {
            section.start
        } else {
            section.start + 1
        };
        self.lines[start..section.own_end].join("\n")
    }
}

struct Line {
    text: String,
    code: bool,
    anchor: Option<String>,
}

pub fn glob_to_regex(glob: &str) -> Regex {
    let mut out = String::from("^");
    let mut rest = glob;
    while let Some(c) = rest.chars().next() {
        if let Some(r) = rest.strip_prefix("**/") {
            out.push_str("(?:.*/)?");
            rest = r;
        } else if let Some(r) = rest.strip_prefix("**") {
            out.push_str(".*");
            rest = r;
        } else {
            match c {
                '*' => out.push_str("[^/]*"),
                '?' => out.push_str("[^/]"),
                _ => out.push_str(&regex::escape(&c.to_string())),
            }
            rest = &rest[c.len_utf8()..];
        }
    }
    out.push('$');
    Regex::new(&out).expect("glob regex")
}

/// Returns (mapping or None, body). Broken YAML still has its block skipped.
pub fn split_frontmatter(text: &str) -> (Option<yaml_rust2::yaml::Hash>, String) {
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.first().map(|l| l.trim_end()) != Some("---") {
        return (None, text.to_string());
    }
    for i in 1..lines.len() {
        if lines[i].trim_end() == "---" {
            let body = lines[i + 1..].join("\n");
            let mapping = YamlLoader::load_from_str(&lines[1..i].join("\n"))
                .ok()
                .and_then(|docs| docs.into_iter().next())
                .and_then(|doc| match doc {
                    Yaml::Hash(h) => Some(h),
                    _ => None,
                });
            return (mapping, body);
        }
    }
    (None, text.to_string())
}

fn scalar(value: &Yaml) -> Option<String> {
    match value {
        Yaml::String(s) => Some(s.clone()),
        Yaml::Integer(i) => Some(i.to_string()),
        Yaml::Boolean(b) => Some(b.to_string()),
        Yaml::Real(raw) => raw
            .parse::<f64>()
            .ok()
            .map(|f| f.to_string())
            .or_else(|| Some(raw.clone())),
        _ => None,
    }
}

fn key_string(key: &Yaml) -> Option<String> {
    match key {
        Yaml::Null => Some("null".into()),
        other => scalar(other),
    }
}

pub fn extract_metadata(front: &yaml_rust2::yaml::Hash, ignore: &HashSet<String>) -> Metadata {
    let mut meta = Metadata::new();
    for (key, value) in front {
        let Some(key) = key_string(key) else { continue };
        if key == "title" || key == "description" || ignore.contains(&key) {
            continue;
        }
        match value {
            Yaml::Array(items) => {
                meta.insert(
                    key,
                    MetaValue::Many(items.iter().filter_map(scalar).collect()),
                );
            }
            other => {
                if let Some(s) = scalar(other) {
                    meta.insert(key, MetaValue::One(s));
                }
            }
        }
    }
    meta
}

fn clean_prose(input: &str, mdx: bool) -> String {
    let filtered;
    let text = if mdx {
        filtered = input
            .split('\n')
            .filter(|line| !MDX_MODULE_LINE.is_match(line))
            .collect::<Vec<_>>()
            .join("\n");
        filtered.as_str()
    } else {
        input
    };
    let mut protected: Vec<String> = Vec::new();
    let text = INLINE_CODE.replace_all(text, |c: &Captures| {
        protected.push(c[0].to_string());
        format!("\x00{}\x00", protected.len() - 1)
    });
    let text = JSX_COMMENT.replace_all(&text, "");
    let text = TAG.replace_all(&text, |c: &Captures| {
        if c[0].starts_with("</") {
            return String::new();
        }
        let attrs = c.get(1).map_or("", |m| m.as_str());
        let attr = LABEL_ATTR
            .captures(attrs)
            .or_else(|| TITLE_ATTR.captures(attrs));
        match attr {
            Some(a) => format!(
                "**{}**",
                a.get(1).or_else(|| a.get(2)).map_or("", |m| m.as_str())
            ),
            None => String::new(),
        }
    });
    PLACEHOLDER
        .replace_all(&text, |c: &Captures| {
            protected[c[1].parse::<usize>().unwrap()].clone()
        })
        .into_owned()
}

/// Cleans prose (SPEC.md §4.4) and returns lines tagged code/prose with heading anchors.
fn clean(body: &str, mdx: bool) -> Vec<Line> {
    let raw: Vec<&str> = body.split('\n').collect();
    let mut segments: Vec<(bool, &[&str])> = Vec::new();
    let mut i = 0;
    while i < raw.len() {
        if let Some(m) = FENCE_OPEN.captures(raw[i]) {
            let fence = &m[1];
            let ch = fence.chars().next().unwrap().to_string();
            let close = Regex::new(&format!(
                r"^\s*{}{{{},}}\s*$",
                regex::escape(&ch),
                fence.len()
            ))
            .expect("fence regex");
            let mut j = i + 1;
            while j < raw.len() && !close.is_match(raw[j]) {
                j += 1;
            }
            let end = (j + 1).min(raw.len());
            segments.push((true, &raw[i..end]));
            i = j + 1;
        } else {
            let mut j = i;
            while j < raw.len() && !FENCE_OPEN.is_match(raw[j]) {
                j += 1;
            }
            segments.push((false, &raw[i..j]));
            i = j;
        }
    }

    let mut lines: Vec<Line> = Vec::new();
    for (is_code, seg) in segments {
        if is_code {
            lines.extend(seg.iter().map(|t| Line {
                text: t.to_string(),
                code: true,
                anchor: None,
            }));
            continue;
        }
        for text in clean_prose(&seg.join("\n"), mdx).split('\n') {
            let mut text = text.trim_end().to_string();
            let mut anchor = None;
            if HEADING.is_match(&text)
                && let Some(m) = HEADING_ATTR.captures(&text)
            {
                anchor = Some(m[1].to_string());
                let start = m.get(0).unwrap().start();
                text = text[..start].trim_end().to_string();
            }
            if text.is_empty() && lines.last().is_some_and(|l| !l.code && l.text.is_empty()) {
                continue;
            }
            lines.push(Line {
                text,
                code: false,
                anchor,
            });
        }
    }

    let first = lines
        .iter()
        .position(|l| !l.text.trim().is_empty())
        .unwrap_or(lines.len());
    let last = lines
        .iter()
        .rposition(|l| !l.text.trim().is_empty())
        .map_or(first, |p| p + 1);
    lines.drain(last..);
    lines.drain(..first);
    lines
}

pub fn heading_text(raw: &str) -> String {
    let text = CLOSING_HASHES.replace(raw, "");
    LINK.replace_all(&text, "$1").trim().to_string()
}

pub fn slugify(text: &str) -> String {
    let text = LINK.replace_all(text, "$1").to_lowercase();
    NOT_SLUG_CHAR
        .replace_all(&text, "")
        .trim()
        .replace(' ', "-")
}

fn build_sections(lines: &[Line]) -> Vec<Section> {
    struct Heading {
        line: usize,
        level: usize,
        text: String,
        anchor: String,
        explicit: bool,
    }
    let mut headings: Vec<Heading> = Vec::new();
    let mut occurrences: HashMap<String, usize> = HashMap::new();
    for (n, line) in lines.iter().enumerate() {
        if line.code {
            continue;
        }
        let Some(m) = HEADING.captures(&line.text) else {
            continue;
        };
        let text = heading_text(&m[2]);
        let level = m[1].len();
        if let Some(anchor) = &line.anchor {
            headings.push(Heading {
                line: n,
                level,
                text,
                anchor: anchor.clone(),
                explicit: true,
            });
            continue;
        }
        let base = slugify(&text);
        let mut slug = base.clone();
        while occurrences.contains_key(&slug) {
            let count = occurrences.get_mut(&base).expect("base slug seen");
            *count += 1;
            slug = format!("{base}-{count}");
        }
        occurrences.insert(slug.clone(), 0);
        headings.push(Heading {
            line: n,
            level,
            text,
            anchor: slug,
            explicit: false,
        });
    }

    let first = headings.first().map_or(lines.len(), |h| h.line);
    let mut sections = vec![Section {
        level: 0,
        heading: None,
        anchor: None,
        explicit: false,
        start: 0,
        own_end: first,
        end: first,
    }];
    for (idx, h) in headings.iter().enumerate() {
        let own_end = headings.get(idx + 1).map_or(lines.len(), |n| n.line);
        let end = headings[idx + 1..]
            .iter()
            .find(|o| o.level <= h.level)
            .map_or(lines.len(), |o| o.line);
        sections.push(Section {
            level: h.level,
            heading: Some(h.text.clone()),
            anchor: Some(h.anchor.clone()),
            explicit: h.explicit,
            start: h.line,
            own_end,
            end,
        });
    }
    sections
}

pub fn parse_page(
    path: &str,
    input: &str,
    ignore_fields: &HashSet<String>,
    root_name: &str,
) -> Page {
    let text = input
        .strip_prefix('\u{feff}')
        .unwrap_or(input)
        .replace("\r\n", "\n");
    let (front, body) = split_frontmatter(&text);
    let front = front.unwrap_or_default();
    #[expect(
        clippy::case_sensitive_file_extension_comparisons,
        reason = "globs are case-sensitive too (SPEC.md §4.1)"
    )]
    let lines = clean(&body, path.ends_with(".mdx"));
    let sections = build_sections(&lines);

    let string_field = |key: &str| match front.get(&Yaml::String(key.into())) {
        Some(Yaml::String(s)) => Some(s.clone()),
        _ => None,
    };
    let title = string_field("title")
        .or_else(|| {
            sections
                .iter()
                .find(|s| s.level == 1)
                .and_then(|s| s.heading.clone())
        })
        .unwrap_or_else(|| {
            let (dir, file) = match path.rsplit_once('/') {
                Some((d, f)) => (Some(d), f),
                None => (None, path),
            };
            let stem = file.rsplit_once('.').map_or(file, |(s, _)| s);
            if stem == "index" {
                dir.map_or(root_name, |d| d.rsplit('/').next().unwrap_or(d))
                    .to_string()
            } else {
                stem.to_string()
            }
        });

    Page {
        path: path.to_string(),
        title,
        description: string_field("description"),
        metadata: extract_metadata(&front, ignore_fields),
        lines: lines.into_iter().map(|l| l.text).collect(),
        sections,
    }
}

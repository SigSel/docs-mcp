//! The in-memory index: discovery, search, filters and the tool outputs (SPEC.md §4-6).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::LazyLock;

use indexmap::IndexMap;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::content::{LINK, MetaValue, Metadata, Page, Section, glob_to_regex, parse_page};

static TOKEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\p{L}\p{N}]+").expect("token regex"));
const K1: f64 = 1.2;
const B: f64 = 0.75;
const MAX_PER_PAGE: usize = 3;
const PREFIX_FACTOR: f64 = 0.5;
const PREFIX_MIN_LEN: usize = 3;
const FUZZY_FACTOR: f64 = 0.3;
const FUZZY_MIN_LEN: usize = 6;

/// Metadata filters: field name to one value or a list of values.
pub type Filters = HashMap<String, FilterValue>;

#[derive(Debug, Clone, serde::Deserialize, schemars::JsonSchema)]
#[serde(untagged)]
pub enum FilterValue {
    One(String),
    Many(Vec<String>),
}

impl FilterValue {
    fn values(&self) -> &[String] {
        match self {
            FilterValue::One(v) => std::slice::from_ref(v),
            FilterValue::Many(v) => v,
        }
    }
}

/// A tool error whose message is shown to the model as-is.
#[derive(Debug)]
pub struct ToolFailure(pub String);

pub fn tokenize(text: &str) -> Vec<String> {
    TOKEN
        .find_iter(&text.to_lowercase())
        .map(|m| m.as_str().to_string())
        .filter(|t| t.chars().count() >= 2)
        .collect()
}

pub fn discover(
    root: &Path,
    include: &[String],
    exclude: &[String],
) -> std::io::Result<Vec<String>> {
    let inc: Vec<Regex> = include.iter().map(|g| glob_to_regex(g)).collect();
    let exc: Vec<Regex> = exclude.iter().map(|g| glob_to_regex(g)).collect();
    let mut found = Vec::new();
    let mut stack = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, rel)) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel_path = if rel.is_empty() {
                name.clone()
            } else {
                format!("{rel}/{name}")
            };
            // Does not follow links, so links to files and to directories are both skipped: one committed to the
            // repo could point at a secret outside it (SPEC.md §4.1).
            let file_type = entry.file_type()?;
            if file_type.is_dir() {
                if !name.starts_with('.') && name != "node_modules" {
                    stack.push((entry.path(), rel_path));
                }
            } else if file_type.is_file()
                && inc.iter().any(|r| r.is_match(&rel_path))
                && !exc.iter().any(|r| r.is_match(&rel_path))
            {
                found.push(rel_path);
            }
        }
    }
    found.sort();
    Ok(found)
}

fn strip_extension(path: &str) -> &str {
    let file = path.rsplit('/').next().unwrap_or(path);
    if file.contains('.') {
        &path[..path.rfind('.').unwrap_or(path.len())]
    } else {
        path
    }
}

/// A section's own text as search sees it: links count as their text, not their URL.
fn search_text(page: &Page, section: &Section) -> String {
    LINK.replace_all(&page.own_text(section), "$1").into_owned()
}

/// Levenshtein distance at most 1: one insertion, deletion or substitution (code points).
fn within_one_edit(first: &[char], second: &[char]) -> bool {
    let (short, long) = if first.len() <= second.len() {
        (first, second)
    } else {
        (second, first)
    };
    if long.len() - short.len() > 1 {
        return false;
    }
    let same = short.iter().zip(long).take_while(|(x, y)| x == y).count();
    if short.len() == long.len() {
        short.get(same + 1..) == long.get(same + 1..)
    } else {
        short[same..] == long[same + 1..]
    }
}

pub fn page_url(site_url: Option<&str>, path: &str, anchor: Option<&str>) -> Option<String> {
    let site = site_url.filter(|s| !s.is_empty())?;
    let mut route = strip_extension(path);
    if route == "index" {
        route = "";
    } else if let Some(r) = route.strip_suffix("/index") {
        route = r;
    }
    let base = site.trim_end_matches('/');
    let url = if route.is_empty() {
        format!("{base}/")
    } else {
        format!("{base}/{route}/")
    };
    Some(match anchor {
        Some(a) if !a.is_empty() => format!("{url}#{a}"),
        _ => url,
    })
}

fn matches(page: &Page, filters: Option<&Filters>, path_prefix: Option<&str>) -> bool {
    if let Some(prefix) = path_prefix.filter(|p| !p.is_empty())
        && !page.path.starts_with(prefix)
    {
        return false;
    }
    for (key, wanted) in filters.into_iter().flatten() {
        let Some(have) = page.metadata.get(key) else {
            return false;
        };
        let have: HashSet<String> = have.values().iter().map(|v| v.to_lowercase()).collect();
        if !wanted
            .values()
            .iter()
            .any(|w| have.contains(&w.to_lowercase()))
        {
            return false;
        }
    }
    true
}

fn collapse(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn metadata_values(meta: &Metadata) -> impl Iterator<Item = &String> {
    meta.values().flat_map(MetaValue::values)
}

fn snippet(text: &str, terms: &HashSet<String>) -> String {
    let flat = collapse(text);
    let lower = flat.to_lowercase();
    let first = terms.iter().filter_map(|t| lower.find(t.as_str())).min();
    let start = first.map_or(0, |byte| lower[..byte].chars().count().saturating_sub(80));
    let chars: Vec<char> = flat.chars().collect();
    let end = (start + 240).min(chars.len());
    let out: String = chars[start.min(chars.len())..end].iter().collect();
    format!(
        "{}{}{}",
        if start > 0 { "…" } else { "" },
        out,
        if start + 240 < chars.len() { "…" } else { "" }
    )
}

struct Doc {
    page: usize,
    section: usize,
    tf: IndexMap<String, f64>,
    length: f64,
}

pub struct DocIndex {
    pub pages: Vec<Page>,
    by_path: HashMap<String, usize>,
    site_url: Option<String>,
    docs: Vec<Doc>,
    /// Insertion-ordered, like the other implementations, so scores sum in the same order.
    postings: IndexMap<String, Vec<usize>>,
    avgdl: f64,
}

impl DocIndex {
    #[expect(
        clippy::cast_precision_loss,
        reason = "section counts are far below 2^52"
    )]
    pub fn new(pages: Vec<Page>, site_url: Option<String>) -> Self {
        let by_path = pages
            .iter()
            .enumerate()
            .map(|(i, p)| (p.path.clone(), i))
            .collect();
        let mut docs = Vec::new();
        let mut postings: IndexMap<String, Vec<usize>> = IndexMap::new();
        for (pi, page) in pages.iter().enumerate() {
            let tags: Vec<&str> = metadata_values(&page.metadata)
                .map(String::as_str)
                .chain(std::iter::once(page.description.as_deref().unwrap_or("")))
                .collect();
            let title_tokens = tokenize(&page.title);
            let tag_tokens = tokenize(&tags.join(" "));
            let path_tokens = tokenize(strip_extension(&page.path));
            for (si, section) in page.sections.iter().enumerate() {
                let own = search_text(page, section);
                if section.level == 0 && own.trim().is_empty() {
                    continue;
                }
                let mut tf: IndexMap<String, f64> = IndexMap::new();
                let mut add = |term: &str, n: f64| *tf.entry(term.to_string()).or_default() += n;
                for t in &title_tokens {
                    add(t, 2.0);
                }
                if let Some(h) = &section.heading {
                    for t in &tokenize(h) {
                        add(t, 3.0);
                    }
                }
                for t in &tag_tokens {
                    add(t, 1.0);
                }
                for t in &path_tokens {
                    add(t, 3.0);
                }
                for t in &tokenize(&own) {
                    add(t, 1.0);
                }
                let length = tf.values().sum();
                let id = docs.len();
                for term in tf.keys() {
                    postings.entry(term.clone()).or_default().push(id);
                }
                docs.push(Doc {
                    page: pi,
                    section: si,
                    tf,
                    length,
                });
            }
        }
        let avgdl = if docs.is_empty() {
            0.0
        } else {
            docs.iter().map(|d| d.length).sum::<f64>() / docs.len() as f64
        };
        Self {
            pages,
            by_path,
            site_url,
            docs,
            postings,
            avgdl,
        }
    }

    pub fn build(
        root: &Path,
        include: &[String],
        exclude: &[String],
        ignore_fields: &HashSet<String>,
        root_name: &str,
        site_url: Option<String>,
    ) -> anyhow::Result<Self> {
        if !root.is_dir() {
            anyhow::bail!("docs directory not found: {}", root.display());
        }
        let mut pages = Vec::new();
        for rel in discover(root, include, exclude)? {
            let bytes = std::fs::read(root.join(&rel))?;
            pages.push(parse_page(
                &rel,
                &String::from_utf8_lossy(&bytes),
                ignore_fields,
                root_name,
            ));
        }
        Ok(Self::new(pages, site_url))
    }

    fn url(&self, path: &str, anchor: Option<&str>) -> Option<String> {
        page_url(self.site_url.as_deref(), path, anchor)
    }

    // --- tools ---------------------------------------------------------------

    pub fn overview(&self) -> Map<String, Value> {
        let mut folders: BTreeMap<&str, usize> = BTreeMap::new();
        let mut fields: BTreeMap<&str, BTreeMap<&str, usize>> = BTreeMap::new();
        for page in &self.pages {
            let folder = page.path.split_once('/').map_or(".", |(f, _)| f);
            *folders.entry(folder).or_default() += 1;
            for (key, value) in &page.metadata {
                let counter = fields.entry(key).or_default();
                let distinct: HashSet<&str> = value.values().iter().map(String::as_str).collect();
                for v in distinct {
                    *counter.entry(v).or_default() += 1;
                }
            }
        }
        let mut out = Map::new();
        out.insert("page_count".into(), json!(self.pages.len()));
        out.insert(
            "folders".into(),
            folders
                .iter()
                .map(|(path, pages)| json!({"path": path, "pages": pages}))
                .collect(),
        );
        out.insert("fields".into(), json!(fields));
        out
    }

    pub fn list_docs(&self, path_prefix: Option<&str>, filters: Option<&Filters>) -> Value {
        let pages: Vec<Value> = self
            .pages
            .iter()
            .filter(|p| matches(p, filters, path_prefix))
            .map(|page| {
                let mut entry = Map::new();
                entry.insert("path".into(), json!(page.path));
                entry.insert("title".into(), json!(page.title));
                if let Some(d) = &page.description {
                    entry.insert("description".into(), json!(d));
                }
                if let Some(url) = self.url(&page.path, None) {
                    entry.insert("url".into(), json!(url));
                }
                entry.insert("metadata".into(), json!(page.metadata));
                Value::Object(entry)
            })
            .collect();
        json!({"count": pages.len(), "pages": pages})
    }

    /// The vocabulary terms a query term matches, with their factors, in vocabulary order (SPEC.md §5).
    fn matching_terms<'a>(&'a self, q: &'a str) -> Vec<(&'a str, f64)> {
        let mut factors: Vec<(&str, f64)> = Vec::new();
        if self.postings.contains_key(q) {
            factors.push((q, 1.0));
        }
        let q_chars: Vec<char> = q.chars().collect();
        if q_chars.len() >= PREFIX_MIN_LEN {
            for term in self.postings.keys() {
                if term != q && term.starts_with(q) {
                    factors.push((term, PREFIX_FACTOR));
                }
            }
        }
        if q_chars.len() >= FUZZY_MIN_LEN {
            let matched: HashSet<&str> = factors.iter().map(|(t, _)| *t).collect();
            for term in self.postings.keys() {
                if matched.contains(term.as_str()) || !term.starts_with(q_chars[0]) {
                    continue;
                }
                let t_chars: Vec<char> = term.chars().collect();
                if within_one_edit(&q_chars, &t_chars) {
                    factors.push((term, FUZZY_FACTOR));
                }
            }
        }
        factors
    }

    #[expect(
        clippy::cast_precision_loss,
        reason = "section counts are far below 2^52"
    )]
    pub fn search(
        &self,
        query: &str,
        filters: Option<&Filters>,
        path_prefix: Option<&str>,
        limit: i64,
    ) -> Result<Value, ToolFailure> {
        let mut seen = HashSet::new();
        let terms: Vec<String> = tokenize(query)
            .into_iter()
            .filter(|t| seen.insert(t.clone()))
            .collect();
        if terms.is_empty() {
            return Err(ToolFailure(
                "The query must contain at least one word.".into(),
            ));
        }
        let limit = usize::try_from(limit.clamp(1, 50)).unwrap_or(10);
        let n = self.docs.len();
        let mut scores = vec![0.0_f64; n];
        let mut matched_terms: HashSet<String> = HashSet::new();
        for q in &terms {
            let factors = self.matching_terms(q);
            if factors.is_empty() {
                continue;
            }
            matched_terms.extend(factors.iter().map(|(t, _)| t.to_string()));
            let ids: HashSet<usize> = factors
                .iter()
                .flat_map(|(t, _)| self.postings[*t].iter().copied())
                .collect();
            let df = ids.len() as f64;
            let idf = (1.0 + (n as f64 - df + 0.5) / (df + 0.5)).ln();
            for id in ids {
                let doc = &self.docs[id];
                let tf: f64 = factors
                    .iter()
                    .map(|(t, f)| f * doc.tf.get(*t).copied().unwrap_or(0.0))
                    .sum();
                if tf <= 0.0 {
                    continue;
                }
                let norm = K1 * (1.0 - B + B * doc.length / self.avgdl);
                scores[id] += idf * tf * (K1 + 1.0) / (tf + norm);
            }
        }

        let mut ranked: Vec<usize> = (0..n)
            .filter(|&i| {
                scores[i] > 0.0 && matches(&self.pages[self.docs[i].page], filters, path_prefix)
            })
            .collect();
        ranked.sort_by(|&a, &b| {
            let (da, db) = (&self.docs[a], &self.docs[b]);
            scores[b]
                .total_cmp(&scores[a])
                .then_with(|| self.pages[da.page].path.cmp(&self.pages[db.page].path))
                .then_with(|| da.section.cmp(&db.section))
        });
        let mut results = Vec::new();
        let mut per_page: HashMap<usize, usize> = HashMap::new();
        for i in ranked {
            let doc = &self.docs[i];
            let count = per_page.entry(doc.page).or_default();
            if *count >= MAX_PER_PAGE {
                continue;
            }
            *count += 1;
            let page = &self.pages[doc.page];
            let section = &page.sections[doc.section];
            let mut hit = Map::new();
            hit.insert("path".into(), json!(page.path));
            hit.insert("title".into(), json!(page.title));
            let heading = if section.level > 0 {
                section.heading.clone()
            } else {
                Some(page.title.clone())
            };
            hit.insert("section".into(), json!(heading));
            hit.insert("anchor".into(), json!(section.anchor));
            if let Some(url) = self.url(&page.path, section.anchor.as_deref()) {
                hit.insert("url".into(), json!(url));
            }
            hit.insert("score".into(), json!((scores[i] * 1000.0).round() / 1000.0));
            let own = search_text(page, section);
            let text = if own.is_empty() {
                section.heading.clone().unwrap_or_default()
            } else {
                own
            };
            hit.insert("snippet".into(), json!(snippet(&text, &matched_terms)));
            results.push(Value::Object(hit));
            if results.len() >= limit {
                break;
            }
        }
        Ok(json!({"query": query, "count": results.len(), "results": results}))
    }

    fn resolve(&self, path: &str) -> Result<&Page, ToolFailure> {
        let p = path.trim_matches('/');
        let candidates: Vec<String> = if p.is_empty() {
            vec!["index.md".into(), "index.mdx".into()]
        } else {
            vec![
                p.into(),
                format!("{p}.md"),
                format!("{p}.mdx"),
                format!("{p}/index.md"),
                format!("{p}/index.mdx"),
            ]
        };
        if let Some(&i) = candidates.iter().find_map(|c| self.by_path.get(c)) {
            return Ok(&self.pages[i]);
        }
        let last = p.rsplit('/').next().unwrap_or(p).to_lowercase();
        let similar: Vec<&str> = self
            .pages
            .iter()
            .filter(|pg| !last.is_empty() && pg.path.to_lowercase().contains(&last))
            .take(5)
            .map(|pg| pg.path.as_str())
            .collect();
        let hint = if similar.is_empty() {
            String::new()
        } else {
            format!(" Did you mean: {}?", similar.join(", "))
        };
        Err(ToolFailure(format!(
            "No page '{path}'.{hint} Use list_docs or search_docs to find pages."
        )))
    }

    pub fn read_doc(&self, path: &str, section: Option<&str>) -> Result<String, ToolFailure> {
        let page = self.resolve(path)?;
        let Some(section) = section.filter(|s| !s.is_empty()) else {
            return Ok(self.render(page, None));
        };
        let wanted = section.to_lowercase();
        let wanted = wanted.trim_start_matches('#');
        let found = page
            .sections
            .iter()
            .find(|s| {
                s.anchor
                    .as_ref()
                    .is_some_and(|a| a.to_lowercase() == wanted)
            })
            .or_else(|| {
                page.sections.iter().find(|s| {
                    s.heading
                        .as_ref()
                        .is_some_and(|h| h.to_lowercase() == section.to_lowercase())
                })
            });
        if let Some(s) = found {
            return Ok(self.render(page, Some(s)));
        }
        let anchors: Vec<&str> = page
            .sections
            .iter()
            .filter_map(|s| s.anchor.as_deref())
            .collect();
        Err(ToolFailure(format!(
            "No section '{section}' in {}. Anchors: {}",
            page.path,
            anchors.join(", ")
        )))
    }

    pub fn get_section(&self, anchor: &str) -> Result<String, ToolFailure> {
        let wanted = anchor.to_lowercase();
        let wanted = wanted.trim_start_matches('#');
        let mut found: Vec<(&Page, &Section)> = self
            .pages
            .iter()
            .flat_map(|p| {
                p.sections
                    .iter()
                    .filter(|s| {
                        s.anchor
                            .as_ref()
                            .is_some_and(|a| a.to_lowercase() == wanted)
                    })
                    .map(move |s| (p, s))
            })
            .collect();
        if found.iter().any(|(_, s)| s.explicit) {
            found.retain(|(_, s)| s.explicit);
        }
        match found.as_slice() {
            [] => Err(ToolFailure(format!(
                "No section with anchor '{anchor}'. Use search_docs to find it by text."
            ))),
            [(page, section)] => Ok(self.render(page, Some(section))),
            many => {
                let mut lines = vec![format!(
                    "Anchor '{anchor}' matches {} sections. Read one with read_doc(path, section):",
                    many.len()
                )];
                for (p, s) in many {
                    lines.push(format!(
                        "- {}#{} ({})",
                        p.path,
                        s.anchor.as_deref().unwrap_or(""),
                        s.heading.as_deref().unwrap_or("")
                    ));
                }
                Ok(lines.join("\n"))
            }
        }
    }

    fn render(&self, page: &Page, section: Option<&Section>) -> String {
        let mut header = vec![
            format!("path: {}", page.path),
            format!("title: {}", collapse(&page.title)),
        ];
        if let Some(d) = &page.description {
            header.push(format!("description: {}", collapse(d)));
        }
        if let Some(url) = self.url(&page.path, section.and_then(|s| s.anchor.as_deref())) {
            header.push(format!("url: {url}"));
        }
        if let Some(s) = section {
            header.push(format!("section: {}", s.anchor.as_deref().unwrap_or("")));
        }
        for (key, value) in &page.metadata {
            header.push(format!("{key}: {}", collapse(&value.values().join(", "))));
        }
        let body = match section {
            Some(s) => page.section_text(s),
            None => page.body(),
        };
        format!("---\n{}\n---\n\n{body}", header.join("\n"))
    }
}

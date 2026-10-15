"""The five MCP tools, against fixtures/docs. Section numbers refer to SPEC.md."""

from __future__ import annotations

import pytest

from conftest import SITE_URL, Server

pytestmark = pytest.mark.anyio

ALL_PATHS = [
    "index.mdx",
    "python/index.md",
    "python/notes.md",
    "python/tools/uv.md",
    "rust/rules.mdx",
    "rust/testing.md",
    "workflow/containers.md",
]
TOOLS = {"docs_overview", "list_docs", "search_docs", "read_doc", "get_section"}


# --- protocol ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_tools_listed_read_only(server: Server, mode: str) -> None:
    async with server.client(mode) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == TOOLS
        for tool in tools:
            assert tool.annotations is not None and tool.annotations.read_only_hint is True, tool.name
            assert tool.description, tool.name


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_instructions_name_the_docs(server: Server, mode: str) -> None:
    async with server.client(mode) as client:
        assert client.instructions is not None
        assert client.instructions.startswith("Documentation for testhandbok. Use search_docs")
        assert "Search with keywords in the language of the documentation, even when" in client.instructions


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_both_protocol_eras_get_the_same_answer(server: Server, mode: str) -> None:
    data = await server.json("search_docs", {"query": "clippy"}, mode=mode)
    assert data["results"][0]["anchor"] == "rs-201"


# --- docs_overview (§6) ------------------------------------------------------


async def test_overview_counts_only_included_pages(server: Server) -> None:
    data = await server.json("docs_overview")
    assert data["name"] == "testhandbok"
    assert data["language"] is None
    assert data["page_count"] == len(ALL_PATHS)
    assert data["folders"] == [
        {"path": ".", "pages": 1},
        {"path": "python", "pages": 3},
        {"path": "rust", "pages": 2},
        {"path": "workflow", "pages": 1},
    ]


async def test_overview_source_local(server: Server) -> None:
    source = (await server.json("docs_overview"))["source"]
    assert source["type"] == "local"
    assert source["commit"] is None
    assert source["dir"] == "."
    assert source["synced_at"].endswith("Z")


async def test_overview_fields_discovered_from_frontmatter(server: Server) -> None:
    fields = (await server.json("docs_overview"))["fields"]
    # presentation keys, title/description, null and nested values are not metadata
    assert list(fields) == sorted(fields)
    assert list(fields) == [
        "aktiv",
        "eigarforum",
        "fagområde",
        "innhaldstype",
        "prioritet",
        "stikkord",
        "teknologi",
        "tema",
    ]
    # notes.md has broken YAML, so its `innhaldstype: krav` does not count
    assert fields["innhaldstype"] == {"krav": 1, "rettleiing": 2, "tilråding": 1}
    assert fields["fagområde"] == {"backend": 3, "generelt": 2}
    assert fields["prioritet"] == {"2": 1}
    assert fields["aktiv"] == {"true": 1}
    assert list(fields["tema"]) == sorted(fields["tema"])


# --- list_docs (§6) ----------------------------------------------------------


async def test_list_all_sorted_with_titles_and_urls(server: Server) -> None:
    data = await server.json("list_docs")
    assert data["count"] == len(ALL_PATHS)
    pages = {p["path"]: p for p in data["pages"]}
    assert [p["path"] for p in data["pages"]] == ALL_PATHS
    assert pages["index.mdx"]["title"] == "Testhandboka"
    assert pages["python/tools/uv.md"]["title"] == "uv – pakkehandsamar"  # first H1
    assert pages["python/notes.md"]["title"] == "notes"  # file name
    assert pages["index.mdx"]["url"] == f"{SITE_URL}/"
    assert pages["python/index.md"]["url"] == f"{SITE_URL}/python/"
    assert pages["rust/rules.mdx"]["url"] == f"{SITE_URL}/rust/rules/"
    assert pages["rust/testing.md"]["description"] == "Korleis vi skriv og organiserer testar."
    assert "description" not in pages["python/index.md"]


async def test_list_metadata_shapes(server: Server) -> None:
    pages = {p["path"]: p for p in (await server.json("list_docs"))["pages"]}
    assert pages["workflow/containers.md"]["metadata"] == {
        "innhaldstype": "tilråding",
        "fagområde": "generelt",
        "eigarforum": "utviklarforum",
        "tema": "konteinerteknologi",
        "teknologi": ["Docker/Podman"],
        "prioritet": "2",
        "aktiv": "true",
    }
    assert pages["python/notes.md"]["metadata"] == {}
    assert pages["index.mdx"]["metadata"] == {}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"filters": {"teknologi": "rust"}}, ["rust/rules.mdx", "rust/testing.md"]),
        ({"filters": {"fagområde": ["generelt"]}}, ["python/index.md", "workflow/containers.md"]),
        ({"filters": {"tema": "testing", "innhaldstype": "KRAV"}}, ["rust/rules.mdx"]),
        ({"filters": {"innhaldstype": ["krav", "tilråding"]}}, ["rust/rules.mdx", "workflow/containers.md"]),
        ({"filters": {"finst-ikkje": "x"}}, []),
        ({"path_prefix": "python/"}, ["python/index.md", "python/notes.md", "python/tools/uv.md"]),
        ({"path_prefix": "rust/", "filters": {"innhaldstype": "rettleiing"}}, ["rust/testing.md"]),
    ],
)
async def test_list_filters(server: Server, args: dict, expected: list[str]) -> None:
    data = await server.json("list_docs", args)
    assert [p["path"] for p in data["pages"]] == expected
    assert data["count"] == len(expected)


# --- search_docs (§5) --------------------------------------------------------


async def test_search_finds_rule_section(server: Server) -> None:
    data = await server.json("search_docs", {"query": "clippy pedantic"})
    top = data["results"][0]
    assert data["query"] == "clippy pedantic"
    assert top["path"] == "rust/rules.mdx"
    assert top["anchor"] == "rs-201"
    assert top["section"] == "RS-201: clippy pedantic, åtvaringar avviste i CI"
    assert top["title"] == "Rust-reglane"
    assert top["url"] == f"{SITE_URL}/rust/rules/#rs-201"
    assert "clippy" in top["snippet"].lower()
    assert isinstance(top["score"], (int, float)) and top["score"] > 0


async def test_search_rule_number(server: Server) -> None:
    # In this tiny corpus the short overview table competes; get_section is the exact lookup.
    data = await server.json("search_docs", {"query": "RS-203"})
    assert "rs-203" in [r["anchor"] for r in data["results"][:2]]


async def test_search_intro_has_no_anchor(server: Server) -> None:
    data = await server.json("search_docs", {"query": "sjiraffkomponent"})
    assert data["count"] == 1
    hit = data["results"][0]
    assert hit["path"] == "python/notes.md"
    assert hit["anchor"] is None
    assert hit["section"] == "notes"
    assert hit["url"] == f"{SITE_URL}/python/notes/"


async def test_search_prefix_match(server: Server) -> None:
    data = await server.json("search_docs", {"query": "sjiraff"})
    assert [r["path"] for r in data["results"]] == ["python/notes.md"]


async def test_search_exact_beats_prefix(server: Server) -> None:
    # rust/testing.md has "testing" in its title (weight 2) and metadata (weight 1);
    # rust/rules.mdx only in metadata.
    data = await server.json("search_docs", {"query": "testing"})
    assert data["results"][0]["path"] == "rust/testing.md"


@pytest.mark.parametrize("word", ["kladdeord", "modulord", "loyndord", "kommentaren"])
async def test_search_never_sees_excluded_or_removed_text(server: Server, word: str) -> None:
    data = await server.json("search_docs", {"query": word})
    assert data["count"] == 0
    assert data["results"] == []


async def test_search_path_prefix_and_filters(server: Server) -> None:
    data = await server.json("search_docs", {"query": "unwrap", "path_prefix": "rust/testing"})
    assert data["count"] > 0
    assert {r["path"] for r in data["results"]} == {"rust/testing.md"}
    data = await server.json("search_docs", {"query": "unwrap", "filters": {"innhaldstype": "krav"}})
    assert {r["path"] for r in data["results"]} == {"rust/rules.mdx"}


async def test_search_limit_and_per_page_cap(server: Server) -> None:
    data = await server.json("search_docs", {"query": "clippy unwrap rust cargo testar", "limit": 50})
    per_page: dict[str, int] = {}
    for r in data["results"]:
        per_page[r["path"]] = per_page.get(r["path"], 0) + 1
    assert per_page and max(per_page.values()) <= 3
    scores = [r["score"] for r in data["results"]]
    assert scores == sorted(scores, reverse=True)
    one = await server.json("search_docs", {"query": "clippy unwrap rust cargo testar", "limit": 1})
    assert one["count"] == 1
    assert one["results"][0] == data["results"][0]


async def test_search_heading_text_reduces_links(server: Server) -> None:
    data = await server.json("search_docs", {"query": "regelen om unwrap", "path_prefix": "rust/testing"})
    assert data["results"][0]["section"] == "Sjå regelen om unwrap"
    assert data["results"][0]["anchor"] == "sjå-regelen-om-unwrap"


async def test_search_empty_query_is_tool_error(server: Server) -> None:
    text, is_error = await server.call("search_docs", {"query": "?! -"})
    assert is_error
    assert "query" in text.lower()


# --- read_doc (§4.4, §6) -----------------------------------------------------


RULES_HEADER = f"""---
path: rust/rules.mdx
title: Rust-reglane
url: {SITE_URL}/rust/rules/
innhaldstype: krav
fagområde: backend
eigarforum: rustforum
tema: kodekvalitet, testing
teknologi: Rust, CI/CD
stikkord: reglar
---

"""


async def test_read_doc_header(server: Server) -> None:
    text, is_error = await server.call("read_doc", {"path": "rust/rules.mdx"})
    assert not is_error, text
    assert text.startswith(RULES_HEADER)
    assert text[len(RULES_HEADER) :].startswith("Dette er heile standarden for Rust.")


async def test_read_doc_cleans_mdx(server: Server) -> None:
    text, _ = await server.call("read_doc", {"path": "rust/rules.mdx"})
    for gone in ["import Badges", "export const", "<Rule", "<Badges><Rule", "<Profile", "</Tabs>", "Denne kommentaren", "{ #rs-201 }", "{#rs-203}"]:
        assert gone not in text, gone
    for kept in [
        "MÅ IKKJE",
        "**Linux**",
        "**Windows**",  # tag spanning two lines
        "`Vec<T>`",  # inline code is protected
        "import_this_is_not_an_import();",  # code blocks are untouched
        "export fn also_not_removed() {}",
        "// <Badges> inni kode står att",
        "### RS-201: clippy pedantic, åtvaringar avviste i CI\n",
        ":::kvifor[Kvifor ikkje unwrap?]",
    ]:
        assert kept in text, kept
    assert "\n\n\n" not in text
    assert "Applikasjon" in text  # inner text of removed tags survives


async def test_read_doc_card_titles(server: Server) -> None:
    text, _ = await server.call("read_doc", {"path": "index.mdx"})
    assert "**Python**" in text and "**Rust**" in text
    assert "<Card" not in text and "import {" not in text
    assert "Alt om Rust-prosjekt." in text


async def test_read_doc_header_order_and_scalars(server: Server) -> None:
    text, _ = await server.call("read_doc", {"path": "workflow/containers.md"})
    header = text.split("\n---\n", 1)[0] + "\n---\n"
    assert header == (
        "---\n"
        "path: workflow/containers.md\n"
        "title: Konteinarar\n"
        "description: Bilete, Containerfile og drift.\n"
        f"url: {SITE_URL}/workflow/containers/\n"
        "innhaldstype: tilråding\n"
        "fagområde: generelt\n"
        "eigarforum: utviklarforum\n"
        "tema: konteinerteknologi\n"
        "teknologi: Docker/Podman\n"
        "prioritet: 2\n"
        "aktiv: true\n"
        "---\n"
    )


async def test_read_doc_broken_frontmatter_still_skipped(server: Server) -> None:
    text, _ = await server.call("read_doc", {"path": "python/notes.md"})
    assert text == (
        "---\npath: python/notes.md\ntitle: notes\n"
        f"url: {SITE_URL}/python/notes/\n---\n\n"
        "Notat utan gyldig frontmatter. Ordet sjiraffkomponent finst berre her."
    )


@pytest.mark.parametrize("path", ["rust/rules", "/rust/rules.mdx/", "rust/rules.mdx"])
async def test_read_doc_path_forms(server: Server, path: str) -> None:
    text, is_error = await server.call("read_doc", {"path": path})
    assert not is_error
    assert text.startswith(RULES_HEADER)


@pytest.mark.parametrize("path", ["python", "python/"])
async def test_read_doc_index_resolution(server: Server, path: str) -> None:
    text, is_error = await server.call("read_doc", {"path": path})
    assert not is_error
    assert text.startswith("---\npath: python/index.md\n")


async def test_read_doc_not_found_suggests(server: Server) -> None:
    text, is_error = await server.call("read_doc", {"path": "rules"})
    assert is_error
    assert "rust/rules.mdx" in text


@pytest.mark.parametrize("section", ["rs-201", "RS-201", "#rs-201"])
async def test_read_doc_section_by_anchor(server: Server, section: str) -> None:
    text, is_error = await server.call("read_doc", {"path": "rust/rules.mdx", "section": section})
    assert not is_error, text
    assert f"url: {SITE_URL}/rust/rules/#rs-201\nsection: rs-201\n" in text
    body = text.split("\n---\n\n", 1)[1]
    assert body.startswith("### RS-201: clippy pedantic, åtvaringar avviste i CI\n")
    assert "RS-203" not in body
    assert "pedantic = { level" in body


async def test_read_doc_section_includes_subsections(server: Server) -> None:
    text, _ = await server.call("read_doc", {"path": "rust/rules.mdx", "section": "oversikt"})
    body = text.split("\n---\n\n", 1)[1]
    assert body.startswith("## Oversikt\n")
    assert "### RS-201" in body and "### RS-203" in body


async def test_read_doc_section_by_heading_text(server: Server) -> None:
    text, is_error = await server.call("read_doc", {"path": "rust/testing.md", "section": "integrasjonstestar"})
    assert not is_error, text
    body = text.split("\n---\n\n", 1)[1]
    assert body.startswith("## Integrasjonstestar\n")
    assert "### Eksempel" in body and "tests/api.rs" in body
    assert "## Sjå" not in body


@pytest.mark.parametrize(
    ("anchor", "first_line"),
    [
        ("eksempel", "### Eksempel"),
        ("eksempel-1", "### Eksempel"),
        ("sjå-regelen-om-unwrap", "## Sjå [regelen](rules.mdx#rs-203) om unwrap"),
        ("fixtures", "## Fixtures ##"),
    ],
)
async def test_read_doc_slugs(server: Server, anchor: str, first_line: str) -> None:
    text, is_error = await server.call("read_doc", {"path": "rust/testing.md", "section": anchor})
    assert not is_error, text
    body = text.split("\n---\n\n", 1)[1]
    assert body.split("\n", 1)[0] == first_line
    if anchor == "eksempel-1":
        assert "tests/api.rs" in body


async def test_read_doc_section_not_found_lists_anchors(server: Server) -> None:
    text, is_error = await server.call("read_doc", {"path": "rust/testing.md", "section": "finst-ikkje"})
    assert is_error
    assert "einingstestar" in text and "eksempel-1" in text


# --- get_section (§6) --------------------------------------------------------


async def test_get_section_equals_read_doc(server: Server) -> None:
    via_get, is_error = await server.call("get_section", {"anchor": "RS-203"})
    assert not is_error, via_get
    via_read, _ = await server.call("read_doc", {"path": "rust/rules.mdx", "section": "rs-203"})
    assert via_get == via_read


async def test_get_section_explicit_anchor_wins(server: Server) -> None:
    # workflow/containers.md has `{ #samandrag }`; python/index.md has the slug `samandrag`
    via_get, is_error = await server.call("get_section", {"anchor": "samandrag"})
    assert not is_error
    via_read, _ = await server.call("read_doc", {"path": "workflow/containers.md", "section": "samandrag"})
    assert via_get == via_read


async def test_get_section_ambiguous_lists_matches(server: Server) -> None:
    text, is_error = await server.call("get_section", {"anchor": "eksempel"})
    assert not is_error
    assert text == (
        "Anchor 'eksempel' matches 2 sections. Read one with read_doc(path, section):\n"
        "- rust/testing.md#eksempel (Eksempel)\n"
        "- workflow/containers.md#eksempel (Eksempel)"
    )


async def test_get_section_missing_is_error(server: Server) -> None:
    text, is_error = await server.call("get_section", {"anchor": "finst-ikkje"})
    assert is_error
    assert "search_docs" in text

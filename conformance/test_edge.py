"""Edge cases in parsing, built in a temporary docs tree (SPEC.md §4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import SITE_URL, Server, start_server, stop_server

pytestmark = pytest.mark.anyio

FILES: dict[str, str | bytes] = {
    "index.md": "Rotside utan tittel.\n",
    "guide/index.md": "Innhald.\n",
    "dup.md": "---\ntitle: Dup\ntema: a\ntema: b\n---\nTekst.\n",
    "yaml11.md": (
        "---\ntitle: Y\naktiv: yes\nav: off\ndato: 2024-01-31\ntal: 2.50\nheil: 1.0\n"
        "liste: [1, true, null, {a: b}, x]\nnull_verdi: ~\nnesta:\n  a: b\n---\nTekst.\n"
    ),
    "bom-crlf.md": "﻿---\r\ntitle: BOM\r\ntema: x\r\n---\r\n\r\n# Overskrift\r\n\r\nTekst med CRLF.\r\n".encode(),
    "unclosed-fence.md": "# Gjerde\n\n```python\n# not a heading\nprint(1)\n",
    "unclosed-front.md": "---\ntitle: Aldri lukka\n\nTekst.\n",
    "empty.md": "",
    "only-front.md": "---\ntitle: Berre frontmatter\n---\n",
    "Ærlig/øl.md": "# Øl\n\nTekst om øl.\n",
    "UPPER.MD": "# Stor\n\nstorord\n",
    "virtual-environments.md": "# Virtuelle miljø\n\nKvart prosjekt har sitt eige miljø.\n",
    "lenkjer.md": "# Lenkjer\n\nSjå [denne sida](lenkjemaal.md) for meir.\n",
    "stavemaate.md": "# Sikkerheit\n\nOm sikkerheit i avhengigheiter.\n",
    "headings.md": (
        "# Topp\n\n#Ikkje ein overskrift\n\n## Tom anker {#}\n\n## Dobbel  mellomrom\n\n### C# og F#\n\n"
        "## Emoji 🚀 test\n\n## Same\n\n## Same\n\n## Same-1\n\n###### Djup\n\n####### For djup\n"
    ),
}
PATHS = [
    "bom-crlf.md",
    "dup.md",
    "empty.md",
    "guide/index.md",
    "headings.md",
    "index.md",
    "lenkjer.md",
    "only-front.md",
    "stavemaate.md",
    "unclosed-fence.md",
    "unclosed-front.md",
    "virtual-environments.md",
    "yaml11.md",
    "Ærlig/øl.md",
]


@pytest.fixture(scope="module")
def edge(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-edge")
    docs = tmp / "docs"
    for rel, content in FILES.items():
        path = docs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(docs), "DOCS_LANGUAGE": "Norwegian Nynorsk"}, tmp)
    yield s
    stop_server(s)


async def pages(server: Server) -> dict[str, dict]:
    data = await server.json("list_docs", {"path_prefix": None, "filters": None})
    assert [p["path"] for p in data["pages"]] == PATHS
    return {p["path"]: p for p in data["pages"]}


async def body(server: Server, path: str, section: str | None = None) -> str:
    text, is_error = await server.call("read_doc", {"path": path, **({"section": section} if section else {})})
    assert not is_error, text
    return text.split("\n---\n\n", 1)[1] if "\n---\n\n" in text else text.split("\n---\n", 1)[1]


async def test_edge_titles(edge: Server) -> None:
    titles = {path: p["title"] for path, p in (await pages(edge)).items()}
    assert titles == {
        "bom-crlf.md": "BOM",
        "dup.md": "dup",
        "empty.md": "empty",
        "guide/index.md": "guide",
        "headings.md": "Topp",
        "index.md": "testhandbok",
        "lenkjer.md": "Lenkjer",
        "only-front.md": "Berre frontmatter",
        "stavemaate.md": "Sikkerheit",
        "unclosed-fence.md": "Gjerde",
        "unclosed-front.md": "unclosed-front",
        "virtual-environments.md": "Virtuelle miljø",
        "yaml11.md": "Y",
        "Ærlig/øl.md": "Øl",
    }


async def test_edge_yaml_12_scalars(edge: Server) -> None:
    meta = (await pages(edge))["yaml11.md"]["metadata"]
    assert meta == {
        "aktiv": "yes",
        "av": "off",
        "dato": "2024-01-31",
        "tal": "2.5",
        "heil": "1",
        "liste": ["1", "true", "x"],
    }


async def test_edge_duplicate_keys_invalidate_frontmatter(edge: Server) -> None:
    page = (await pages(edge))["dup.md"]
    assert page["metadata"] == {}
    assert await body(edge, "dup.md") == "Tekst."


async def test_edge_bom_and_crlf(edge: Server) -> None:
    page = (await pages(edge))["bom-crlf.md"]
    assert page["metadata"] == {"tema": "x"}
    assert await body(edge, "bom-crlf.md") == "# Overskrift\n\nTekst med CRLF."


async def test_edge_unclosed_fence_runs_to_end(edge: Server) -> None:
    assert await body(edge, "unclosed-fence.md") == "# Gjerde\n\n```python\n# not a heading\nprint(1)"
    text, is_error = await edge.call("read_doc", {"path": "unclosed-fence.md", "section": "not-a-heading"})
    assert is_error
    assert text.endswith("Anchors: gjerde")


async def test_edge_unclosed_frontmatter_is_body(edge: Server) -> None:
    assert await body(edge, "unclosed-front.md") == "---\ntitle: Aldri lukka\n\nTekst."


async def test_edge_empty_bodies(edge: Server) -> None:
    text, _ = await edge.call("read_doc", {"path": "empty.md"})
    assert text == f"---\npath: empty.md\ntitle: empty\nurl: {SITE_URL}/empty/\n---\n\n"
    text, _ = await edge.call("read_doc", {"path": "only-front.md"})
    assert text.endswith("title: Berre frontmatter\nurl: " + SITE_URL + "/only-front/\n---\n\n")


async def test_edge_heading_anchors(edge: Server) -> None:
    text, is_error = await edge.call("read_doc", {"path": "headings.md", "section": "x"})
    assert is_error
    assert text == (
        "No section 'x' in headings.md. Anchors: topp, tom-anker, dobbel--mellomrom, c-og-f, emoji--test, "
        "same, same-1, same-1-1, djup"
    )
    assert (await body(edge, "headings.md", "c-og-f")).startswith("### C# og F#")
    assert (await body(edge, "headings.md", "djup")) == "###### Djup\n\n####### For djup"


async def test_edge_unicode_path_and_root_index_url(edge: Server) -> None:
    p = await pages(edge)
    assert p["Ærlig/øl.md"]["url"] == f"{SITE_URL}/Ærlig/øl/"
    assert p["index.md"]["url"] == f"{SITE_URL}/"
    assert p["guide/index.md"]["url"] == f"{SITE_URL}/guide/"
    overview = await edge.json("docs_overview")
    assert overview["folders"] == [{"path": ".", "pages": 12}, {"path": "guide", "pages": 1}, {"path": "Ærlig", "pages": 1}]


async def test_edge_globs_are_case_sensitive(edge: Server) -> None:
    assert (await edge.json("search_docs", {"query": "storord"}))["count"] == 0


async def test_edge_empty_intro_not_searchable(edge: Server) -> None:
    hits = (await edge.json("search_docs", {"query": "øl"}))["results"]
    assert [(h["path"], h["anchor"]) for h in hits] == [("Ærlig/øl.md", "øl")]


async def test_edge_limit_clamped(edge: Server) -> None:
    data = await edge.json("search_docs", {"query": "tekst", "limit": 0})
    assert data["count"] == 1
    data = await edge.json("search_docs", {"query": "tekst", "limit": 999})
    assert data["count"] >= 2


# --- language and matching (SPEC.md §5, §6) ------------------------------------


async def test_edge_path_is_searchable(edge: Server) -> None:
    hits = (await edge.json("search_docs", {"query": "virtual environments"}))["results"]
    assert hits[0]["path"] == "virtual-environments.md"


async def test_edge_link_urls_not_indexed(edge: Server) -> None:
    assert (await edge.json("search_docs", {"query": "lenkjemaal"}))["count"] == 0
    hits = (await edge.json("search_docs", {"query": "denne sida"}))["results"]
    assert hits[0]["path"] == "lenkjer.md"
    assert hits[0]["snippet"] == "Sjå denne sida for meir."
    assert "[denne sida](lenkjemaal.md)" in await body(edge, "lenkjer.md")


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("sikkerhet", True),  # Bokmål for "sikkerheit": one letter missing
        ("avhengigheter", True),  # one letter missing
        ("sikkerheet", True),  # one letter replaced
        ("ikkerheit", False),  # first letter differs
        ("sikkerhaaet", False),  # two edits away
        ("miljo", False),  # under 6 letters: no fuzzy match
    ],
)
async def test_edge_one_edit_matches(edge: Server, query: str, found: bool) -> None:
    hits = (await edge.json("search_docs", {"query": query}))["results"]
    if found:
        assert hits and hits[0]["path"] == "stavemaate.md"
    else:
        assert hits == []


async def test_edge_language_in_instructions_and_descriptions(edge: Server) -> None:
    async with edge.client() as client:
        assert client.instructions is not None
        assert client.instructions.startswith("Documentation for testhandbok, written in Norwegian Nynorsk.")
        assert (
            "Search with keywords in Norwegian Nynorsk, even when the user writes in another language, "
            "and answer in the user's language." in client.instructions
        )
        tools = {t.name: t for t in (await client.list_tools()).tools}
    search = tools["search_docs"]
    assert "keywords in Norwegian Nynorsk, whatever language the user writes in" in (search.description or "")
    assert search.input_schema["properties"]["query"]["description"].startswith(
        "Keywords to search for, in Norwegian Nynorsk. Add spelling variants"
    )
    assert (await edge.json("docs_overview"))["language"] == "Norwegian Nynorsk"

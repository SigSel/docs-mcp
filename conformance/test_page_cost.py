"""No page costs much more to index than it is long (SPEC.md §4.4, §5).

Each page here once cost the Python implementation the square of its length: minutes of CPU, with the whole server
frozen. The server must be ready within the fixture's timeout.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from conftest import Server, start_server, stop_server

pytestmark = pytest.mark.anyio

FILES = {
    # A long run of spaces in a heading, before its {#id}: Python's regexes for the id and for closing hashes
    # backtracked over it from every position.
    "spaces.md": "# a" + " " * 100_000 + "b {#anker}\n\nmellomromsord\n",
    # Comments that never close: the comment regex scanned to the end of the page from each one.
    "comments.mdx": "kommentarord\n\n" + "{/*" * 66_000 + "\n",
    # Many headings: Python copied the rest of the list for each one.
    "headings.md": "# a\n" * 100_000,
}


@pytest.fixture(scope="module")
def costly(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-page-cost")
    docs = tmp / "docs"
    docs.mkdir()
    for name, content in FILES.items():
        (docs / name).write_text(content)
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(docs)}, tmp, ready_timeout=30)
    yield s
    stop_server(s)


async def test_heading_with_spaces(costly: Server) -> None:
    text, is_error = await costly.call("get_section", {"anchor": "anker"})
    assert not is_error, text[:200]
    assert (await costly.json("search_docs", {"query": "mellomromsord"}))["results"][0]["path"] == "spaces.md"


async def test_comments_that_never_close(costly: Server) -> None:
    assert (await costly.json("search_docs", {"query": "kommentarord"}))["results"][0]["path"] == "comments.mdx"


async def test_many_headings(costly: Server) -> None:
    text, is_error = await costly.call("get_section", {"anchor": "a-99999"})
    assert not is_error, text[:200]
    assert "headings.md" in text

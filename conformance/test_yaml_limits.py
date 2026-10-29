"""Frontmatter that would cost too much to build counts as invalid (SPEC.md §4.2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from conftest import Server, start_server, stop_server

pytestmark = pytest.mark.anyio


def bomb() -> str:
    """Nine levels of nine aliases each: 400 bytes that expand to 9^9 strings."""
    lines = ['a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for prev, name in zip("abcdefgh", "bcdefghi", strict=True):
        lines.append(f"{name}: &{name} [" + ",".join([f"*{prev}"] * 9) + "]")
    return "\n".join(lines)


def flow(depth: int) -> str:
    """A mapping holding sequences nested to `depth` levels in all."""
    return "tema: " + "[" * (depth - 1) + "x" + "]" * (depth - 1)


def block(depth: int) -> str:
    """A mapping holding mappings nested to `depth` levels in all, written with indentation."""
    return "\n".join("  " * i + f"k{i}:" for i in range(depth)) + " x"


FILES = {
    "bomb.md": f"---\ntitle: Bombe\n{bomb()}\n---\nTekst.\n",
    "alias.md": "---\ntitle: Alias\nbase: &b x\ntema: *b\n---\nTekst.\n",
    "anchor.md": "---\ntitle: Anker\ntema: &t x\n---\nTekst.\n",
    "flow-32.md": f"---\ntitle: Flyt\n{flow(32)}\n---\nTekst.\n",
    "flow-33.md": f"---\ntitle: Flyt\n{flow(33)}\n---\nTekst.\n",
    "flow-deep.md": f"---\ntitle: Flyt\n{flow(100_000)}\n---\nTekst.\n",
    "block-32.md": f"---\ntitle: Blokk\n{block(32)}\n---\nTekst.\n",
    "block-33.md": f"---\ntitle: Blokk\n{block(33)}\n---\nTekst.\n",
    "block-deep.md": f"---\ntitle: Blokk\n{block(500)}\n---\nTekst.\n",
}


@pytest.fixture(scope="module")
def limits(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-yaml-limits")
    docs = tmp / "docs"
    docs.mkdir()
    for name, content in FILES.items():
        (docs / name).write_text(content)
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(docs)}, tmp)
    yield s
    stop_server(s)


async def pages(server: Server) -> dict[str, dict[str, Any]]:
    data = await server.json("list_docs")
    assert sorted(p["path"] for p in data["pages"]) == sorted(FILES)
    return {p["path"]: p for p in data["pages"]}


@pytest.mark.parametrize("path", ["bomb.md", "alias.md", "flow-33.md", "flow-deep.md", "block-33.md", "block-deep.md"])
async def test_rejected(limits: Server, path: str) -> None:
    page = (await pages(limits))[path]
    assert page["title"] == path.removesuffix(".md")
    assert page["metadata"] == {}


async def test_anchor_without_alias_is_fine(limits: Server) -> None:
    page = (await pages(limits))["anchor.md"]
    assert page["title"] == "Anker"
    assert page["metadata"] == {"tema": "x"}


@pytest.mark.parametrize(("path", "title"), [("flow-32.md", "Flyt"), ("block-32.md", "Blokk")])
async def test_32_deep_is_fine(limits: Server, path: str, title: str) -> None:
    page = (await pages(limits))[path]
    assert page["title"] == title


async def test_body_still_follows(limits: Server) -> None:
    text, is_error = await limits.call("read_doc", {"path": "bomb.md"})
    assert not is_error, text
    assert text.endswith("---\n\nTekst.")

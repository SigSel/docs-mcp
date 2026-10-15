"""Every implementation returns exactly the golden outputs (see golden.py)."""

from __future__ import annotations

import json

import pytest

from conftest import Server
from golden import GOLDEN, collect

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
async def actual(server: Server) -> dict:
    return await collect(server)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


EXPECTED = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


@pytest.mark.parametrize("part", ["tools", "overview", "list_docs"])
async def test_golden_part(actual: dict, part: str) -> None:
    assert actual[part] == EXPECTED[part]


@pytest.mark.parametrize("page", list(EXPECTED.get("read_doc", {})))
async def test_golden_read_doc(actual: dict, page: str) -> None:
    assert actual["read_doc"][page] == EXPECTED["read_doc"][page]


@pytest.mark.parametrize("query", list(EXPECTED.get("search", {})))
async def test_golden_search(actual: dict, query: str) -> None:
    got, want = actual["search"][query], EXPECTED["search"][query]
    assert [(r["path"], r["anchor"]) for r in got["results"]] == [(r["path"], r["anchor"]) for r in want["results"]]
    for g, w in zip(got["results"], want["results"], strict=True):
        assert g["score"] == pytest.approx(w["score"], abs=0.002)
        assert {k: v for k, v in g.items() if k != "score"} == {k: v for k, v in w.items() if k != "score"}


@pytest.mark.parametrize("anchor", list(EXPECTED.get("get_section", {})))
async def test_golden_get_section(actual: dict, anchor: str) -> None:
    assert actual["get_section"][anchor] == EXPECTED["get_section"][anchor]

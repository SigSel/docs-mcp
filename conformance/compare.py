"""Runs every implementation against a real docs tree and diffs all their outputs.

    uv run python compare.py /path/to/docs [--impl rust,python,typescript] [--query word ...]

Reads every page, every section by anchor, runs searches for a set of queries and for
words taken from the pages themselves, and reports any output that differs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import tempfile
from pathlib import Path
from typing import Any

from conftest import Server, start_server, stop_server

DEFAULT_QUERIES = ["RS-203", "PY-101", "clippy", "unwrap", "virtuelt miljø", "pre-commit", "conda", "podman",
                   "ny regel", "pytest fixtures", "ADR", "uv", "Windows", "MÅ", "kvifor", "sikkerheit"]


async def collect(server: Server, queries: list[str]) -> dict[str, Any]:
    overview = await server.json("docs_overview")
    overview["source"].pop("synced_at")
    pages = (await server.json("list_docs"))["pages"]
    out: dict[str, Any] = {"overview": overview, "list_docs": pages, "read_doc": {}, "section": {}, "search": {}}
    for page in pages:
        text, _ = await server.call("read_doc", {"path": page["path"]})
        out["read_doc"][page["path"]] = text
    return out


async def sections(server: Server, pages: list[str]) -> dict[str, str]:
    """Reads every section of every page, using the anchors the server reports on a bad lookup."""
    result = {}
    for path in pages:
        text, _ = await server.call("read_doc", {"path": path, "section": "\x00no-such-section"})
        anchors = text.split("Anchors: ", 1)[1].split(", ") if "Anchors: " in text else []
        for anchor in filter(None, anchors):
            result[f"{path}#{anchor}"] = (await server.call("read_doc", {"path": path, "section": anchor}))[0]
            result[f"get_section:{anchor}"] = (await server.call("get_section", {"anchor": anchor}))[0]
    return result


def summarise(a: Any, b: Any) -> str:
    sa, sb = json.dumps(a, ensure_ascii=False, indent=1), json.dumps(b, ensure_ascii=False, indent=1)
    for i, (x, y) in enumerate(zip(sa, sb, strict=False)):
        if x != y:
            return f"…{sa[max(0, i - 80):i + 80]!r}\n   vs\n…{sb[max(0, i - 80):i + 80]!r}"
    return f"lengths differ: {len(sa)} vs {len(sb)}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", type=Path)
    parser.add_argument("--impl", default="python,rust,typescript")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--sample", type=int, default=150, help="extra single-word queries sampled from the pages")
    args = parser.parse_args()
    impls = args.impl.split(",")
    results: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for impl in impls:
            (Path(tmp) / impl).mkdir()
            server = start_server(impl, {"DOCS_LOCAL_PATH": str(args.docs.resolve())}, Path(tmp) / impl)
            try:
                data = await collect(server, [])
                if impl == impls[0]:
                    words = sorted({w for t in data["read_doc"].values() for w in re.findall(r"[^\W_]{3,}", t)})
                    random.Random(42).shuffle(words)
                    queries = DEFAULT_QUERIES + args.query + words[: args.sample]
                    queries += [" ".join(words[i : i + 3]) for i in range(args.sample, args.sample + 60, 3)]
                for q in queries:
                    text, is_error = await server.call("search_docs", {"query": q, "limit": 50})
                    data["search"][q] = text if is_error else json.loads(text)
                data["section"] = await sections(server, [p["path"] for p in data["list_docs"]])
                results[impl] = data
            finally:
                stop_server(server)
    base = impls[0]
    total = diffs = 0
    for impl in impls[1:]:
        for part in ["overview", "list_docs", "read_doc", "section", "search"]:
            want, got = results[base][part], results[impl][part]
            items = want.items() if isinstance(want, dict) else [("*", want)]
            for key, value in items:
                total += 1
                other = got.get(key) if isinstance(got, dict) else got
                if part == "search" and isinstance(value, dict) and isinstance(other, dict):
                    strip = lambda r: [{k: v for k, v in h.items() if k != "score"} for h in r["results"]]  # noqa: E731
                    close = all(abs(x["score"] - y["score"]) <= 0.002 for x, y in zip(value["results"], other["results"], strict=False))
                    if strip(value) == strip(other) and close:
                        continue
                elif value == other:
                    continue
                diffs += 1
                if diffs <= 15:
                    print(f"[{base} vs {impl}] {part} {key!r}:\n   {summarise(value, other)}\n")
    pages = len(results[base]["list_docs"])
    print(f"{pages} pages, {len(results[base]['section'])} section reads, {len(results[base]['search'])} searches: "
          f"{total} comparisons, {diffs} differences")


if __name__ == "__main__":
    asyncio.run(main())

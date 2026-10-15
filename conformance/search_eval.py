"""Search quality across languages: how often does the right page come first?

    uv run python search_eval.py /path/to/docs [--impl python] [--show-misses]

search_eval.json holds questions about the dev-handbook (written in Nynorsk), each phrased in
Nynorsk (nn), Bokmål (nb) and English (en), plus `assistant`: the query a model writes when the
server tells it the docs are in Nynorsk (Nynorsk keywords with Bokmål and English variants).
A hit is the first result whose path, or path#anchor, matches one of the targets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from conftest import Server, start_server, stop_server

EVAL = json.loads(Path(__file__).with_name("search_eval.json").read_text())


def rank(results: list[dict], targets: list[str]) -> int | None:
    for i, r in enumerate(results, 1):
        if any(t == r["path"] or t == f"{r['path']}#{r['anchor']}" for t in targets):
            return i
    return None


async def evaluate(server: Server, show_misses: bool) -> dict[str, dict[str, float]]:
    summary = {}
    for lang in EVAL["languages"]:
        ranks, misses = [], []
        for q in EVAL["queries"]:
            data = await server.json("search_docs", {"query": q[lang], "limit": 10})
            r = rank(data["results"], q["targets"])
            ranks.append(r)
            if r != 1:
                top = data["results"][0] if data["results"] else None
                misses.append(f"{q['id']} ({q[lang]!r}) → rank {r}, top: {top['path'] + '#' + str(top['anchor']) if top else '-'}")
        n = len(ranks)
        summary[lang] = {
            "hit@1": sum(r == 1 for r in ranks) / n,
            "hit@3": sum(r is not None and r <= 3 for r in ranks) / n,
            "MRR": sum(1 / r for r in ranks if r) / n,
        }
        if show_misses:
            print(f"\n{lang} misses:\n  " + "\n  ".join(misses))
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", type=Path)
    parser.add_argument("--impl", default="python")
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        server = start_server(args.impl, {"DOCS_LOCAL_PATH": str(args.docs.resolve())}, Path(tmp))
        try:
            summary = await evaluate(server, args.show_misses)
        finally:
            stop_server(server)
    n = len(EVAL["queries"])
    print(f"\n{n} questions, implementation: {args.impl}\n")
    print(f"| {'query language':<14} | hit@1 | hit@3 | MRR  |\n|{'-' * 16}|-------|-------|------|")
    for lang, s in summary.items():
        print(f"| {lang:<14} | {s['hit@1']:5.0%} | {s['hit@3']:5.0%} | {s['MRR']:.2f} |")


if __name__ == "__main__":
    asyncio.run(main())

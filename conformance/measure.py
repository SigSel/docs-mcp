"""Container footprint and latency for each implementation's image.

    uv run python measure.py /path/to/handbook-checkout [--dir src/content/docs] [--impl rust,python]

Needs the images built (`just image`). For each image: its size, the time from `podman run` to
/readyz answering 200, memory once idle, then latency over MCP for search_docs (the questions in
search_eval.json, all languages) and read_doc (the largest page), and memory after that load.
Prints a Markdown table plus the machine and versions it ran on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from mcp import Client

from conftest import free_port
from search_eval import EVAL

UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


def podman(*args: str) -> str:
    return subprocess.run(["podman", *args], check=True, capture_output=True, text=True).stdout.strip()


def memory_mb(name: str) -> float:
    usage = podman("stats", "--no-stream", "--format", "{{.MemUsage}}", name).split("/")[0].strip()
    m = re.fullmatch(r"([\d.]+)\s*([a-zA-Z]+)", usage)
    assert m, usage
    return float(m.group(1)) * UNITS[m.group(2).lower()] / 1e6


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


async def latencies(url: str, largest: str, rounds: int) -> tuple[list[float], list[float]]:
    queries = [q[lang] for q in EVAL["queries"] for lang in EVAL["languages"]]
    search, read = [], []
    async with Client(url, cache=None) as client:
        await client.call_tool("search_docs", {"query": "warmup"})
        for _ in range(rounds):
            for q in queries:
                start = time.perf_counter()
                await client.call_tool("search_docs", {"query": q})
                search.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            await client.call_tool("read_doc", {"path": largest})
            read.append((time.perf_counter() - start) * 1000)
    return search, read


async def measure(impl: str, docs: Path, docs_dir: str, tag: str, rounds: int) -> dict[str, float]:
    image = f"localhost/docs-mcp-{impl}:{tag}"
    name = f"docs-mcp-measure-{impl}"
    port = free_port()
    size_mb = int(podman("image", "inspect", "--format", "{{.Size}}", image)) / 1e6
    subprocess.run(["podman", "rm", "-f", name], capture_output=True)
    start = time.perf_counter()
    podman(
        "run", "-d", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp", "-p", f"127.0.0.1:{port}:8080",
        "-v", f"{docs}:/docs:ro,z", "-e", "DOCS_LOCAL_PATH=/docs", "-e", f"DOCS_DIR={docs_dir}",
        "-e", "DOCS_REFRESH_SECONDS=0", "-e", "MCP_ALLOWED_HOSTS=localhost,127.0.0.1", image,
    )  # fmt: skip
    try:
        while True:
            try:
                if httpx2.get(f"http://127.0.0.1:{port}/readyz", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            if time.perf_counter() - start > 60:
                raise RuntimeError(f"{impl} not ready: {podman('logs', name)}")
            await asyncio.sleep(0.02)
        ready_s = time.perf_counter() - start
        await asyncio.sleep(3)
        idle_mb = memory_mb(name)
        url = f"http://localhost:{port}/mcp"
        async with Client(url, cache=None) as client:
            pages = json.loads((await client.call_tool("list_docs", {})).content[0].text)["pages"]
            sizes = {p["path"]: len((await client.call_tool("read_doc", {"path": p["path"]})).content[0].text)
                     for p in pages}
        largest = max(sizes, key=lambda p: sizes[p])
        search, read = await latencies(url, largest, rounds)
        return {
            "image_mb": size_mb,
            "ready_s": ready_s,
            "idle_mb": idle_mb,
            "loaded_mb": memory_mb(name),
            "search_p50": statistics.median(search),
            "search_p95": percentile(search, 95),
            "read_p50": statistics.median(read),
            "pages": len(pages),
            "searches": len(search),
            "largest": largest,
            "largest_kb": sizes[largest] / 1000,
        }
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)


def machine() -> str:
    cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")), platform.processor())
    mem_gb = int(Path("/proc/meminfo").read_text().split()[1]) / 1024**2
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    if subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
        commit += " + uncommitted changes"
    return (f"{datetime.now(UTC):%Y-%m-%d}, commit {commit}, {cpu}, {mem_gb:.0f} GB RAM, "
            f"Linux {platform.release()}, podman {podman('version', '--format', '{{.Client.Version}}')}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docs", type=Path, help="the handbook checkout")
    parser.add_argument("--dir", default="src/content/docs", help="docs directory inside the checkout")
    parser.add_argument("--impl", default="rust,python")
    parser.add_argument("--tag", default="dev")
    parser.add_argument("--rounds", type=int, default=3, help="passes over the eval questions")
    args = parser.parse_args()
    impls = args.impl.split(",")
    results = {i: await measure(i, args.docs.resolve(), args.dir, args.tag, args.rounds) for i in impls}
    first = results[impls[0]]
    print(f"{machine()}\n{first['pages']} pages; {first['searches']} searches and {args.rounds} reads of "
          f"{first['largest']} ({first['largest_kb']:.0f} kB) per implementation, one client, sequential.\n")
    rows = [
        ("Image size", "image_mb", "{:.0f} MB"),
        ("`podman run` to ready", "ready_s", "{:.2f} s"),
        ("Memory, idle", "idle_mb", "{:.0f} MB"),
        ("Memory, after the load", "loaded_mb", "{:.0f} MB"),
        ("search_docs, median", "search_p50", "{:.1f} ms"),
        ("search_docs, 95th percentile", "search_p95", "{:.1f} ms"),
        ("read_doc (largest page), median", "read_p50", "{:.1f} ms"),
    ]
    print("| | " + " | ".join(i.capitalize() for i in impls) + " |")
    print("|---|" + "---|" * len(impls))
    for label, key, fmt in rows:
        print(f"| {label} | " + " | ".join(fmt.format(results[i][key]) for i in impls) + " |")


if __name__ == "__main__":
    asyncio.run(main())

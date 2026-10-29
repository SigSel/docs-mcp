"""Several sources in one server, and file pages (SPEC.md §4.1, §4.7, §7)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import COMMANDS, FIXTURE_DOCS, TOKEN, Server, free_port, start_server, stop_server
from githost import git

pytestmark = pytest.mark.anyio

BUILD = (
    "parameters:\n"
    "  - name: pythonVersion\n"
    "    default: '3.12'\n"
    "# A comment with ```` four backticks.\n"
    "steps:\n"
    "  - script: uv sync --locked\n"
    "    displayName: rorleggjarord\n"
)
PIPELINES: dict[str, str | bytes] = {
    "README.md": "# Pipeline templates\n\n## Usage\n\nReference a template with `template:`.\n",
    "templates/build-python.yml": BUILD,
    "templates/deploy.yaml": "steps:\n  - script: echo deploy\n",
    ".env": "SECRET=hemmelegord\n",
    ".env.local": "SECRET=hemmelegord\n",
    "certs/server.pem": "-----BEGIN PRIVATE KEY-----\nhemmelegord\n",
    "big.yml": "x: " + "a" * 1_000_000 + "\n",
    "blob.yml": b"x: 1\x00hemmelegord\n",
}
PYTHON: dict[str, str | bytes] = {
    "README.md": "# Python template\n\n## Usage\n\nCopy it.\n",
    "pyproject.toml": '[project]\nname = "app"\nrequires-python = ">=3.12"\n',
    "Dockerfile": "FROM python:3.12-slim",
    "Makefile": b"test:\r\n\tuv run pytest\r\n",
    "src/app/__init__.py": '"""The app."""\n',
}
HANDBOOK_DESC = "The test handbook."
PIPELINES_DESC = "Pipeline templates. Use them when writing azure-pipelines.yml."
PYTHON_DESC = "The template every new Python project starts from."


def sources_env(sources: list[dict[str, Any]]) -> dict[str, str]:
    """DOCS_SOURCES, written as JSON (which is YAML), and none of the variables it replaces."""
    return {"DOCS_SOURCES": json.dumps(sources), "DOCS_SITE_URL": ""}


def wait_for_sources(server: Server, ready: str, failed: str, timeout: float = 10) -> dict[str, Any]:
    """Polls /readyz until source `ready` has loaded and source `failed` has an error."""
    deadline = time.monotonic() + timeout
    while True:
        status = server.http("GET", "/readyz").json()
        by_name = {s["name"]: s for s in status["sources"]}
        if (by_name[ready]["ready"] and by_name[failed]["last_error"]) or time.monotonic() > deadline:
            return status
        time.sleep(0.1)


def write_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


@pytest.fixture(scope="module")
def multi(impl: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp(f"{impl}-sources")
    sources = [
        {"name": "handbook", "description": HANDBOOK_DESC, "path": str(FIXTURE_DOCS), "exclude": "drafts/**",
         "language": "Norwegian Nynorsk"},
        {"name": "ci-templates", "description": PIPELINES_DESC, "path": str(write_tree(tmp / "p", PIPELINES)),
         "include": ["**/*"], "language": "English"},
        {"name": "python-project-template", "description": PYTHON_DESC, "path": str(write_tree(tmp / "py", PYTHON)),
         "include": "**/*"},
    ]
    s = start_server(impl, sources_env(sources), tmp)
    yield s
    stop_server(s)


async def pages(server: Server, source: str | None = None) -> list[dict[str, Any]]:
    return (await server.json("list_docs", {"source": source} if source else {}))["pages"]


async def test_instructions_list_the_sources(multi: Server) -> None:
    async with multi.client() as client:
        instructions = client.instructions
    assert instructions is not None
    head = instructions.split("\n\n", 1)[0]
    assert head == (
        "Documentation and reference repositories for testhandbok, in 3 sources:\n"
        f"- handbook (Norwegian Nynorsk): {HANDBOOK_DESC}\n"
        f"- ci-templates (English): {PIPELINES_DESC}\n"
        f"- python-project-template: {PYTHON_DESC}"
    )
    assert "search_docs searches every source unless you name one." in instructions


async def test_tools_take_a_source(multi: Server) -> None:
    async with multi.client() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    described = {name: tools[name].input_schema["properties"].get("source", {}).get("description") for name in tools}
    assert described == {
        "search_docs": "Only this source. Leave it out to search all of them.",
        "list_docs": "Only this source. Leave it out to list all of them.",
        "read_doc": "The page's source, as search_docs and list_docs return it. Needed only when the path is in "
        "several sources.",
        "get_section": None,
        "docs_overview": None,
    }
    assert "keywords in the language of the source you search, whatever" in (tools["search_docs"].description or "")


async def test_overview(multi: Server) -> None:
    data = await multi.json("docs_overview")
    assert list(data) == ["name", "sources"]
    assert data["name"] == "testhandbok"
    names = [(s["name"], s["description"], s["language"]) for s in data["sources"]]
    assert names == [
        ("handbook", HANDBOOK_DESC, "Norwegian Nynorsk"),
        ("ci-templates", PIPELINES_DESC, "English"),
        ("python-project-template", PYTHON_DESC, None),
    ]
    pipelines = data["sources"][1]
    assert list(pipelines) == ["name", "description", "language", "source", "page_count", "folders", "fields"]
    assert pipelines["source"]["type"] == "local"
    assert pipelines["page_count"] == 3
    assert pipelines["folders"] == [{"path": ".", "pages": 1}, {"path": "templates", "pages": 2}]
    assert pipelines["fields"] == {}


async def test_secrets_binaries_and_big_files_are_skipped(multi: Server) -> None:
    assert [p["path"] for p in await pages(multi, "ci-templates")] == [
        "README.md",
        "templates/build-python.yml",
        "templates/deploy.yaml",
    ]
    assert (await multi.json("search_docs", {"query": "hemmelegord"}))["count"] == 0


async def test_file_pages_listed(multi: Server) -> None:
    listed = await pages(multi, "python-project-template")
    assert [p["path"] for p in listed] == ["Dockerfile", "Makefile", "README.md", "pyproject.toml", "src/app/__init__.py"]
    assert listed[3] == {"source": "python-project-template", "path": "pyproject.toml", "title": "pyproject.toml", "metadata": {}}


async def test_list_all_sources(multi: Server) -> None:
    listed = await pages(multi)
    handbook = [p for p in listed if p["source"] == "handbook"]
    assert listed[: len(handbook)] == handbook
    assert len(listed) == len(handbook) + 3 + 5
    assert all(next(iter(p)) == "source" for p in listed)


async def test_search_finds_files_in_any_source(multi: Server) -> None:
    data = await multi.json("search_docs", {"query": "rorleggjarord"})
    assert data["count"] == 1
    hit = data["results"][0]
    assert list(hit) == ["source", "path", "title", "section", "anchor", "score", "snippet"]
    assert (hit["source"], hit["path"], hit["section"], hit["anchor"]) == (
        "ci-templates",
        "templates/build-python.yml",
        "build-python.yml",
        None,
    )


async def test_search_one_source(multi: Server) -> None:
    data = await multi.json("search_docs", {"query": "usage", "source": "python-project-template"})
    assert data["count"] >= 1
    assert {r["source"] for r in data["results"]} == {"python-project-template"}


async def test_read_file_page(multi: Server) -> None:
    text, is_error = await multi.call("read_doc", {"path": "templates/build-python.yml", "source": "ci-templates"})
    assert not is_error, text
    fence = "`````"
    assert text == (
        "---\nsource: ci-templates\npath: templates/build-python.yml\ntitle: build-python.yml\n---\n\n"
        f"{fence}yml\n{BUILD}{fence}"
    )


async def test_read_file_page_without_extension_or_newline(multi: Server) -> None:
    text, _ = await multi.call("read_doc", {"path": "Dockerfile"})
    assert text.endswith("---\n\n```dockerfile\nFROM python:3.12-slim\n```")
    text, _ = await multi.call("read_doc", {"path": "Makefile"})
    assert text.endswith("---\n\n```makefile\ntest:\n\tuv run pytest\n```")


async def test_read_markdown_page_names_its_source(multi: Server) -> None:
    text, is_error = await multi.call("read_doc", {"path": "rust/rules", "section": "rs-203"})
    assert not is_error, text
    assert text.startswith("---\nsource: handbook\npath: rust/rules.mdx\n")


async def test_read_path_in_several_sources(multi: Server) -> None:
    text, is_error = await multi.call("read_doc", {"path": "README.md"})
    assert is_error
    assert text == "'README.md' is in several sources: ci-templates, python-project-template. Pass source to read_doc."
    text, is_error = await multi.call("read_doc", {"path": "README.md", "source": "python-project-template"})
    assert not is_error
    assert text.startswith("---\nsource: python-project-template\npath: README.md\ntitle: Python template\n")


async def test_read_missing_suggests_across_sources(multi: Server) -> None:
    text, is_error = await multi.call("read_doc", {"path": "deploy"})
    assert is_error
    assert text == (
        "No page 'deploy'. Did you mean: templates/deploy.yaml (ci-templates)? "
        "Use list_docs or search_docs to find pages."
    )


async def test_file_page_has_no_sections(multi: Server) -> None:
    text, is_error = await multi.call("read_doc", {"path": "pyproject.toml", "section": "project"})
    assert is_error
    assert text == "No section 'project' in pyproject.toml. Anchors: "


async def test_get_section_across_sources(multi: Server) -> None:
    text, is_error = await multi.call("get_section", {"anchor": "usage"})
    assert not is_error
    assert text == (
        "Anchor 'usage' matches 2 sections. Read one with read_doc(path, section, source):\n"
        "- ci-templates: README.md#usage (Usage)\n"
        "- python-project-template: README.md#usage (Usage)"
    )
    text, _ = await multi.call("get_section", {"anchor": "rs-203"})
    assert text.startswith("---\nsource: handbook\npath: rust/rules.mdx\n")


@pytest.mark.parametrize("tool", ["search_docs", "list_docs", "read_doc"])
async def test_unknown_source(multi: Server, tool: str) -> None:
    args = {"search_docs": {"query": "x"}, "list_docs": {}, "read_doc": {"path": "README.md"}}[tool]
    text, is_error = await multi.call(tool, {**args, "source": "nope"})
    assert is_error
    assert text == "No source 'nope'. Sources: handbook, ci-templates, python-project-template."


def test_readyz_per_source(multi: Server) -> None:
    status = multi.http("GET", "/readyz").json()
    assert list(status) == ["ready", "pages", "sources"]
    assert status["ready"] is True
    assert [s["name"] for s in status["sources"]] == ["handbook", "ci-templates", "python-project-template"]
    assert all(list(s) == ["name", "ready", "pages", "commit", "synced_at", "last_error"] for s in status["sources"])
    assert status["pages"] == sum(s["pages"] for s in status["sources"])


def test_refresh_one_source(multi: Server) -> None:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert multi.http("POST", "/refresh?source=python-project-template", headers=auth).status_code == 200
    assert multi.http("POST", "/refresh", headers=auth).status_code == 200
    r = multi.http("POST", "/refresh?source=nope", headers=auth)
    assert r.status_code == 404
    assert "handbook, ci-templates, python-project-template" in r.text


async def test_a_failing_source_leaves_the_others(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-sources-failing")
    missing = tmp / "missing"
    sources = [
        {"name": "good", "description": "Works.", "path": str(FIXTURE_DOCS)},
        {"name": "bad", "description": "Does not.", "path": str(missing)},
    ]
    s = start_server(impl, sources_env(sources), tmp, wait_for="/healthz")
    try:
        status = wait_for_sources(s, ready="good", failed="bad")
        assert s.http("GET", "/readyz").status_code == 503
        assert status["ready"] is False
        good, bad = status["sources"]
        assert good["ready"] is True and bad["ready"] is False
        assert bad["last_error"] == f"docs directory not found: {missing}"
        assert (await s.json("search_docs", {"query": "clippy"}))["count"] >= 1
        text, is_error = await s.call("list_docs", {"source": "bad"})
        assert is_error
        assert text == f"Source 'bad' is not ready yet. Last error: docs directory not found: {missing}"
    finally:
        stop_server(s)


async def test_sources_from_git(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp(f"{impl}-sources-git")
    repos = {}
    for name, files in (("pipelines", PIPELINES), ("python", PYTHON)):
        repo = write_tree(tmp / name, {k: v for k, v in files.items() if k.endswith((".md", ".yml", ".yaml", ".toml"))})
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        repos[name] = repo
    sources = [{"name": n, "description": n, "repo": f"file://{r}", "include": "**/*"} for n, r in repos.items()]
    s = start_server(impl, sources_env(sources), tmp)
    try:
        cache = tmp / "cache"
        assert (cache / "pipelines" / "repo" / ".git").is_dir()
        assert (cache / "python" / "repo" / ".git").is_dir()
        (repos["python"] / "new.toml").write_text("nyttord = 1\n")
        git(repos["python"], "add", "-A")
        git(repos["python"], "commit", "-q", "-m", "new")
        auth = {"Authorization": f"Bearer {TOKEN}"}
        before = s.http("GET", "/readyz").json()["sources"]
        assert s.http("POST", "/refresh?source=python", headers=auth).status_code == 200
        after = s.http("GET", "/readyz").json()["sources"]
        assert after[0]["commit"] == before[0]["commit"]
        assert after[1]["commit"] == git(repos["python"], "rev-parse", "HEAD")
        hits = (await s.json("search_docs", {"query": "nyttord"}))["results"]
        assert [(h["source"], h["path"]) for h in hits] == [("python", "new.toml")]
    finally:
        stop_server(s)


async def test_file_pages_with_one_source(impl: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Without DOCS_SOURCES, nothing names a source, but other files are still file pages."""
    tmp = tmp_path_factory.mktemp(f"{impl}-file-pages")
    docs = write_tree(tmp / "docs", {"a.md": "# A\n\nTekst.\n", "build.yml": "steps: []\n"})
    s = start_server(impl, {"DOCS_LOCAL_PATH": str(docs), "DOCS_INCLUDE": "**/*", "DOCS_SITE_URL": ""}, tmp)
    try:
        listed = await pages(s)
        assert listed == [
            {"path": "a.md", "title": "A", "metadata": {}},
            {"path": "build.yml", "title": "build.yml", "metadata": {}},
        ]
        text, _ = await s.call("read_doc", {"path": "build.yml"})
        assert text == "---\npath: build.yml\ntitle: build.yml\n---\n\n```yml\nsteps: []\n```"
        async with s.client() as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        assert all("source" not in t.input_schema.get("properties", {}) for t in tools.values())
    finally:
        stop_server(s)


def run_config(impl: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(free_port()),
        **env,
    }
    return subprocess.run(COMMANDS[impl], env=full, capture_output=True, text=True, timeout=30)


GOOD = {"name": "a", "description": "A.", "path": "/tmp"}


@pytest.mark.parametrize(
    ("sources", "extra"),
    [
        pytest.param(json.dumps([GOOD]), {"DOCS_LOCAL_PATH": "/tmp"}, id="with DOCS_LOCAL_PATH"),
        pytest.param(json.dumps([GOOD]), {"DOCS_LANGUAGE": "English"}, id="with DOCS_LANGUAGE"),
        pytest.param("[: x", {}, id="not YAML"),
        pytest.param("{}", {}, id="not a list"),
        pytest.param("[]", {}, id="empty"),
        pytest.param("[1]", {}, id="not a mapping"),
        pytest.param(json.dumps([{**GOOD, "colour": "red"}]), {}, id="unknown key"),
        pytest.param(json.dumps([{**GOOD, "name": None}]), {}, id="no name"),
        pytest.param(json.dumps([{**GOOD, "name": "Bad_Name"}]), {}, id="bad name"),
        pytest.param(json.dumps([{**GOOD, "name": "a--b"}]), {}, id="double dash"),
        pytest.param(json.dumps([GOOD, GOOD]), {}, id="repeated name"),
        pytest.param(json.dumps([{"name": "a", "path": "/tmp"}]), {}, id="no description"),
        pytest.param(json.dumps([{**GOOD, "repo": "file:///tmp"}]), {}, id="repo and path"),
        pytest.param(json.dumps([{"name": "a", "description": "A."}]), {}, id="neither repo nor path"),
        pytest.param(json.dumps([{**GOOD, "include": 3}]), {}, id="include not a string"),
        pytest.param(json.dumps([{**GOOD, "dir": ["x"]}]), {}, id="dir not a string"),
    ],
)
def test_config_errors(impl: str, sources: str, extra: dict[str, str]) -> None:
    r = run_config(impl, {"DOCS_SOURCES": sources, **extra})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "DOCS_SOURCES" in r.stderr

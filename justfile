# docs-mcp: one MCP server, two implementations. `just` lists the recipes.

impls := "rust python"
handbook := env("DOCS_PATH", "../dev-handbooks/dev-handbook")

default:
    @just --list

# Build every implementation (or one: `just build rust`)
build impl="all":
    #!/usr/bin/env bash
    set -euo pipefail
    for i in {{ if impl == "all" { impls } else { impl } }}; do
      case $i in
        rust) (cd rust && cargo build --release --locked) ;;
        python) (cd python && uv sync --locked) ;;
      esac
    done

# Run the conformance suite against every implementation (or one)
test impl="all": (build impl)
    cd conformance && uv run pytest --impl {{ if impl == "all" { replace(impls, " ", ",") } else { impl } }}

# Lint and format-check both
lint:
    cd rust && cargo fmt --check && cargo clippy --release -- -D warnings
    cd python && uv run ruff check src && uv run ruff format --check src

# Diff all implementations against a real docs tree
compare docs=(handbook / "src/content/docs"): build
    cd conformance && uv run python compare.py {{ absolute_path(docs) }}

# Search quality for Nynorsk, Bokmål and English questions (conformance/search_eval.json)
eval impl="python" docs=(handbook / "src/content/docs"): (build impl)
    cd conformance && uv run python search_eval.py {{ absolute_path(docs) }} --impl {{ impl }}

# Image size, memory, startup and latency of each image; needs `just image` first
measure path=handbook dir="src/content/docs":
    cd conformance && uv run python measure.py {{ absolute_path(path) }} --dir {{ dir }}

# Regenerate conformance/golden.json from the Python implementation; review the diff!
golden: (build "python")
    cd conformance && uv run python golden.py python

# Serve a local handbook checkout on http://localhost:8080/mcp
run impl="rust" path=handbook dir="src/content/docs": (build impl)
    #!/usr/bin/env bash
    set -euo pipefail
    export DOCS_LOCAL_PATH="{{ absolute_path(path) }}" DOCS_DIR="{{ dir }}" DOCS_REFRESH_SECONDS=5
    export DOCS_NAME="${DOCS_NAME:-$(basename "{{ absolute_path(path) }}")}" DOCS_LANGUAGE="${DOCS_LANGUAGE:-Norwegian Nynorsk}"
    case {{ impl }} in
      rust) exec rust/target/release/docs-mcp ;;
      python) exec python/.venv/bin/docs-mcp ;;
    esac

# Serve a local handbook checkout from a container on http://localhost:8080/mcp, as playground/ expects
try impl="rust" path=handbook dir="src/content/docs": (image impl)
    #!/usr/bin/env bash
    set -euo pipefail
    exec podman run --rm --replace --name docs-mcp-try --read-only --tmpfs /tmp \
      -p 127.0.0.1:8080:8080 -v "{{ absolute_path(path) }}:/docs:ro,z" \
      -e DOCS_LOCAL_PATH=/docs -e DOCS_DIR="{{ dir }}" -e DOCS_REFRESH_SECONDS=5 \
      -e DOCS_NAME="${DOCS_NAME:-$(basename "{{ absolute_path(path) }}")}" \
      -e DOCS_LANGUAGE="${DOCS_LANGUAGE:-Norwegian Nynorsk}" \
      -e MCP_ALLOWED_HOSTS=localhost,127.0.0.1 \
      "localhost/docs-mcp-{{ impl }}:dev"

# Build the container image for one implementation (or all)
image impl="all" tag="dev":
    #!/usr/bin/env bash
    set -euo pipefail
    for i in {{ if impl == "all" { impls } else { impl } }}; do
      podman build -t "localhost/docs-mcp-$i:{{ tag }}" -f "$i/Containerfile" "$i"
    done

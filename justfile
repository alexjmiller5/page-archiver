set shell := ["bash", "-cu"]
export UV_PROJECT_ENVIRONMENT := env_var("HOME") + "/.cache/uv-venvs/page-archiver"

run *args:
    uv run page-archiver {{args}}

alias dev := run

test *args:
    uv run pytest {{args}}

check:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff format .
    uv run ruff check --fix .
    uv run ruff format .

build:
    bun install --frozen-lockfile
    bun run scripts/build-browser.ts
    nix build

# Repository Guidelines

## Project Structure & Module Organization

`src/trawl/` contains the core Python library. The main entry point is `pipeline.py`, with focused modules for extraction, chunking, retrieval, reranking, telemetry, cache handling, profiles, and host stats. Site-specific adapters live in `src/trawl/fetchers/`; VLM profile support lives in `src/trawl/profiles/`. `src/trawl_mcp/` contains the MCP server and HTTP entry points. Tests are in `tests/`, with reusable fixtures under `tests/fixtures/`. Benchmarks and diagnostic scripts are in `benchmarks/`; client configuration examples are in `examples/`; design notes and plans are in `docs/` and `notes/`.

## Build, Test, and Development Commands

Create the reference environment with:

```bash
mamba env create -f environment.yml
mamba run -n trawl playwright install chromium
```

For a venv workflow:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
```

Run the full unit suite with `pytest`. Run targeted tests with `pytest tests/test_youtube_fetcher.py` or `pytest tests/test_pipeline.py`. Start the MCP server locally with `trawl-mcp` after installing the package.

## Coding Style & Naming Conventions

Use Python 3.10+ and keep modules small and purpose-specific. Follow existing naming: modules and functions use `snake_case`, classes use `PascalCase`, and environment variables use `TRAWL_*`. Format and lint with:

```bash
ruff format src tests
ruff check src tests
```

Ruff is configured in `pyproject.toml` with 100-character lines, py310 target, import sorting, bugbear checks, and no `E501` enforcement. Add docstrings for public functions and comments only when the reason is not obvious.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio` in auto mode. Name new tests `tests/test_<feature>.py` and keep fixture data under `tests/fixtures/`. Prefer offline unit tests for fetchers, chunking, extraction, and routing. End-to-end pipeline tests may require local embedding or reranker services; document any required `TRAWL_*` variables in the test or PR.

## Commit & Pull Request Guidelines

Recent history uses conventional prefixes such as `feat(pipeline):`, `fix(fetchers):`, `docs:`, `test:`, `refactor:`, `chore(release):`, and `spike(reranker):`. Keep commits scoped and imperative. Pull requests should include a short problem statement, the change summary, linked issue if available, and verification output such as `pytest` and `ruff check src tests`. Include before/after benchmark or parity results when changing retrieval, ranking, chunking, or tuned constants.

## Security & Configuration Tips

Do not commit `.env`, cache files, downloaded browser data, or benchmark outputs. Keep defaults in `.env.example`, and use `TRAWL_EMBED_URL`, reranker, HyDE, and profile settings only through environment variables.

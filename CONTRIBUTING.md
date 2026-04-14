# Contributing to trawl

Thanks for your interest in trawl. This is a small project with a
deliberately narrow scope (fetch + selectively extract one page per
call). Contributions that align with that scope are very welcome;
see [CLAUDE.md's "In / out of scope"](CLAUDE.md#in--out-of-scope)
section before proposing larger changes.

## Dev setup

The reference dev workflow uses a dedicated mamba (or conda)
environment named `trawl`:

```bash
mamba env create -f environment.yml     # installs trawl + dev extras
mamba run -n trawl playwright install chromium
mamba activate trawl
cp .env.example .env                     # only if overriding defaults
```

If you prefer pip/venv:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
```

All commands below assume you're inside the env.

## Running tests

trawl has two tiers of tests:

1. **Unit tests** (offline, fast). These run in CI and require no
   external services:

   ```bash
   pytest tests/test_profiles.py tests/test_profile_transfer.py
   ```

2. **Integration / parity matrix** (requires a live bge-m3 embedding
   server at `TRAWL_EMBED_URL`, default `http://localhost:8081/v1`).
   This is the 12-case end-to-end matrix that guards against
   regressions in the pipeline:

   ```bash
   python tests/test_pipeline.py                          # all 12 cases
   python tests/test_pipeline.py --only kbo_schedule -v   # one case, verbose
   python tests/test_pipeline.py --hyde                   # with HyDE enabled
   ```

   The MCP stdio smoke test is separate:

   ```bash
   python tests/test_mcp_server.py
   ```

Both integration scripts exit non-zero on any failure, so they're safe
to wire into CI once you have a reachable embedding server.

## Adding a fetcher

Each fetcher lives in `src/trawl/fetchers/<name>.py` and exports a
single `fetch(url: str) -> FetcherResult` function. Look at
`fetchers/wikipedia.py` for the simplest example of an API-first
fetcher with a Playwright fallback.

When adding a fetcher:

1. Add a URL-pattern check to `pipeline.pick_fetcher()`.
2. Add a test case to `tests/test_cases.yaml` covering the new
   domain — a representative URL plus ground-truth facts that should
   appear in top-k for a given query.
3. Run `python tests/test_pipeline.py --only <your_case>` until it
   passes.
4. Run the full `python tests/test_pipeline.py` to confirm you didn't
   regress anything else.

## Code style

- Python 3.10+, typed where it helps readability.
- `ruff check src tests` and `ruff format src tests` (config in
  `pyproject.toml`). CI enforces both.
- No emoji in source or test files.
- Docstrings on public functions; a one-line comment only when the
  *why* is non-obvious — the code is expected to speak for itself.
- Commits: conventional-commit prefixes (`feat`, `fix`, `docs`,
  `test`, `refactor`, `chore`).

## Things NOT to change casually

A handful of tuning constants were dialled in empirically and changing
any of them can regress 1-3 cases of the parity matrix silently. See
the "Things NOT to change" table in [CLAUDE.md](CLAUDE.md#things-not-to-change-without-re-running-the-full-test-matrix).
If a change to one of those is justified, include the before/after
matrix result in the PR description.

## Filing issues

Useful issue reports include:

- A specific URL trawl gets wrong, plus the query and the expected
  content
- The output of `python tests/test_pipeline.py --only <case> -v` if
  it's a regression against an existing test case
- Environment details (Python version, OS, llama-server endpoint if
  non-default)

Bug reports for sites behind active anti-bot (Cloudflare Turnstile with
proof-of-work, DataDome) will be closed — trawl's passive stealth path
can't defeat those and we've decided not to take a dependency on a
paid anti-bot service. See the "Known limitations" section of
[ARCHITECTURE.md](ARCHITECTURE.md).

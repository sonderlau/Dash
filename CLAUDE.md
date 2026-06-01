# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Dash is a personal arXiv daily reader. A pipeline of Python scripts fetches papers from configured arXiv categories, generates Chinese summaries from arXiv metadata and abstracts via DeepSeek, optionally scores papers against personal keywords, and writes a lightweight static site under `docs/` that GitHub Pages serves.

## Non-negotiable preferences (treat as ADRs)

These come from `HANDOFF.md` and `REFACTOR_PLAN.md`. Do not relitigate them without asking.

- **No timezone logic in application code.** Scheduling is controlled by GitHub Actions cron, not Python.
- **DeepSeek-only.** Do not introduce a generic LLM-provider abstraction. The `OPENAI_*` env names are leftover OpenAI-compatible naming, not a contract.
- **Summaries are metadata/abstract-only.** The production path must not download PDFs or claim to read full text.
- **`docs/data/*.json` must stay lightweight.** Do not add heavy fields to the public snapshot.
- **The online workflow (`.github/workflows/daily.yml`) calls stage scripts directly** (`run_daily.py`, `enrich.py`, `build_site_data.py`, `validate_data.py`). It does not call `pipeline.py`. `pipeline.py` is a local convenience wrapper only.
- **Pipeline is explicitly staged; stages may parallelize internally.** Per-paper concurrency lives inside `enrich.py`/`summarize.py`, not across stages.
- **`.env.local` is local-only; never commit it.** `.env.local.example` is the template.

## Snapshot date semantics

The daily JSON filename is the *local run date*, not the arXiv publication date. Reruns on the same date merge into the same `tmp/state/YYYY-MM-DD.json`; cross-day deduplication is by arXiv id against the previous day's snapshot.

## Common commands

All scripts live in `scripts/` and are invoked from the repo root via the venv Python. They sibling-import (`from common import ...`), so run them from `scripts/`'s parent or with the working directory at the repo root.

Local end-to-end run (wrapper):

```bash
.venv/bin/python scripts/pipeline.py --date 2026-05-16
.venv/bin/python scripts/pipeline.py --date 2026-05-16 --skip-summarize
.venv/bin/python scripts/pipeline.py --date 2026-05-16 --summarize-limit 10
.venv/bin/python scripts/pipeline.py --date 2026-05-16 --refresh-ok
```

Stage-by-stage (matches what GitHub Actions runs):

```bash
.venv/bin/python scripts/run_daily.py        --date 2026-05-16
.venv/bin/python scripts/enrich.py           --date 2026-05-16        # metadata/abstract summaries
.venv/bin/python scripts/build_site_data.py  --latest-date 2026-05-16
.venv/bin/python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json
```

Standalone summarizer:

```bash
.venv/bin/python scripts/summarize.py        --date 2026-05-16
```

Cleanup:

```bash
.venv/bin/python scripts/cleanup_artifacts.py --all
```

There is no test suite, no linter config, and no build step beyond running these scripts.

## Architecture

### Data flow and file responsibilities

```
arXiv /list + /api  →  run_daily.py     →  tmp/state/YYYY-MM-DD.json
                       enrich.py        ↻  (DeepSeek metadata/abstract summary)
                       build_site_data  →  docs/data/YYYY-MM-DD.json   (lightweight public)
                                        →  docs/data/index.json
                       validate_data    →  fail loud if anything is empty/missing
```

- `tmp/state/YYYY-MM-DD.json` — pipeline working copy.
- `docs/data/YYYY-MM-DD.json` — public copy served by GitHub Pages.
- `docs/data/index.json` — frontend index of available dates and metadata.

### `enrich.py` — the per-paper pipeline

`enrich.py` is the production stage. It runs a summary worker pool over papers that still need summaries. It uses arXiv metadata and abstracts only, so a paper costs one short DeepSeek request instead of PDF download, JVM extraction, and chunk/reduce calls.

Default: 4 summary workers. Override with `--summary-workers` or `SUMMARY_MAX_WORKERS`.

`enrich.py` reuses helpers from `summarize.py` (`should_skip`, `summarize_one_paper`, etc.) — keep those importable.

### `snapshot_writer.SnapshotWriter`

Debounced, thread-safe writer for `tmp/state/YYYY-MM-DD.json`. Concurrent summary workers call `mark_dirty()`; the writer flushes at most every `min_interval_seconds` or every `every_n` marks, with a forced flush at close. Never write the daily state JSON directly from a worker — go through this writer or you will fight the disk on every paper.

### Configuration layering

`scripts/common.py:load_config` reads `config.yaml` then applies env overrides. Notable: setting `CATEGORIES` in env (comma/space/semicolon separated) overrides `arxiv.categories` from `config.yaml` at runtime. Local dev auto-loads `.env.local` via `load_local_env()`; `os.environ.setdefault` is used so already-set env vars win.

DeepSeek settings come from `load_deepseek_settings()` reading `LLM_ENABLED`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, `LANGUAGE`, `LLM_TIMEOUT_SECONDS`, `LLM_RETRY_TIMES`. Requests use `httpx.Client(..., trust_env=False)` to bypass any local HTTP proxy and `response_format={"type": "json_object"}`. Retry covers rate-limit, timeout, transport, and malformed/truncated JSON.

`scripts/common.py:load_keywords` reads `keywords.yaml`. Empty or missing keywords disable relevance scoring; non-empty keywords ask the model for a string integer `relevance_score` from 0 to 100.

### Prompts

Live in `src/prompts/` as plain text files: `summary_system.txt`, `summary_user.txt`. Prompts must preserve the six-field JSON contract: `tldr`, `motivation`, `method`, `result`, `conclusion`, `relevance_score`.

## Frontend

Vanilla HTML/CSS/JS in `docs/index.html`, `docs/style.css`, `docs/app.js`. It fetches `docs/data/index.json` then per-day JSONs. Keep the public payload small; any field added to a paper that the UI does not use should also be added to `HEAVY_PAPER_FIELDS` in `build_site_data.py`.

## CodeGraph is initialized

`.codegraph/` exists in this repo, so prefer codegraph tools over grep-and-read for symbol lookups, call graphs, and impact analysis. Use `codegraph_search` for symbols, `codegraph_callers` / `codegraph_callees` to trace flow, and `codegraph_impact` before touching shared helpers like `common.py` or `snapshot_writer.py`.

## Style notes specific to this repo

- Scripts use `from __future__ import annotations` and PEP 604 unions.
- Logging is `print({...})` of small dicts to stdout, not the `logging` module. Match that style in pipeline scripts.
- Heavy field names are centralized as `HEAVY_PAPER_FIELDS` in `build_site_data.py` — update there if the schema grows.
- Cross-script imports are sibling-style (`from common import ...`); a few scripts have a `try/except ModuleNotFoundError` fallback to `from scripts.common import ...` for package-style invocation. Preserve that pattern when adding cross-script imports.

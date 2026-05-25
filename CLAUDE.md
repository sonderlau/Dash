# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Dash is a personal arXiv daily reader. A pipeline of Python scripts fetches papers from configured arXiv categories, downloads and extracts PDFs, generates Chinese summaries via DeepSeek, and writes a lightweight static site under `docs/` that GitHub Pages serves.

## Non-negotiable preferences (treat as ADRs)

These come from `HANDOFF.md` and `REFACTOR_PLAN.md`. Do not relitigate them without asking.

- **No timezone logic in application code.** Scheduling is controlled by GitHub Actions cron, not Python.
- **DeepSeek-only.** Do not introduce a generic LLM-provider abstraction. The `OPENAI_*` env names are leftover OpenAI-compatible naming, not a contract.
- **`docs/data/*.json` must stay lightweight.** `build_site_data.py` strips `fulltext_markdown`, `fulltext_source`, `fulltext_status` from the public payload; `tmp/state/*.json` is the heavy working copy. Do not add heavy fields to the public snapshot.
- **The online workflow (`.github/workflows/daily.yml`) calls stage scripts directly** (`run_daily.py`, `enrich.py`, `build_site_data.py`, `validate_data.py`). It does not call `pipeline.py`. `pipeline.py` is a local convenience wrapper only.
- **Pipeline is explicitly staged; stages may parallelize internally.** Per-paper concurrency lives inside `enrich.py`/`extract_fulltext.py`/`summarize.py`, not across stages.
- **Fulltext-first summarization.** Abstract-only summary is fallback when PDF extract fails or times out.
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
.venv/bin/python scripts/enrich.py           --date 2026-05-16        # extract + summarize, per-paper pipelined
.venv/bin/python scripts/build_site_data.py  --latest-date 2026-05-16
.venv/bin/python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json
```

Split form (still supported, useful when refreshing only one side):

```bash
.venv/bin/python scripts/extract_fulltext.py --date 2026-05-16
.venv/bin/python scripts/summarize.py        --date 2026-05-16
```

Cleanup:

```bash
.venv/bin/python scripts/cleanup_artifacts.py --all
.venv/bin/python scripts/cleanup_artifacts.py --pdf-cache --pdf-extract
```

There is no test suite, no linter config, and no build step beyond running these scripts.

## Architecture

### Data flow and file responsibilities

```
arXiv /list + /api  →  run_daily.py     →  tmp/state/YYYY-MM-DD.json   (heavy working state)
                       enrich.py        ↻  (downloads PDFs to tmp/paper_cache/,
                                            extracts to tmp/pdf_extract/,
                                            then DeepSeek summary)
                       build_site_data  →  docs/data/YYYY-MM-DD.json   (lightweight public)
                                        →  docs/data/index.json
                       validate_data    →  fail loud if anything is empty/missing
```

- `tmp/state/YYYY-MM-DD.json` — pipeline working copy with `fulltext_*` fields.
- `docs/data/YYYY-MM-DD.json` — public, fulltext-stripped copy served by GitHub Pages.
- `docs/data/index.json` — frontend index of available dates and metadata.
- `tmp/paper_cache/` — cached PDF downloads.
- `tmp/pdf_extract/<paper_id>/` — `opendataloader-pdf` output (markdown/json) plus extraction metadata.

### `enrich.py` — the per-paper pipeline

`enrich.py` is the production stage. It runs three thread pools — download, extract, summary — and pipelines per-paper handoffs: as soon as one paper's PDF is downloaded it is queued for extraction, and as soon as extraction finishes it is queued for summary. Total wall-clock approximates `max(download_total, extract_total, summary_total)` rather than their sum.

Defaults: 8 download workers (pure network I/O), 2 extract workers (JVM/CPU bound), 4 summary workers. Override with `--download-workers` / `--extract-workers` / `--summary-workers` or `PDF_DOWNLOAD_MAX_WORKERS` / `PDF_EXTRACT_MAX_WORKERS` / `SUMMARY_MAX_WORKERS`.

Splitting download out of extract is what unlocks the speedup: the legacy `ensure_fulltext_for_paper` did download+extract in one function, so download concurrency was capped at `extract_workers`. The new shape uses `download_pdf_for_paper` and `extract_pdf_to_markdown` from `pdf_fulltext.py`. `enrich.py` reuses helpers from `extract_fulltext.py` and `summarize.py` (`should_skip`, `summarize_one_paper`, etc.) — keep those importable.

### `snapshot_writer.SnapshotWriter`

Debounced, thread-safe writer for `tmp/state/YYYY-MM-DD.json`. All concurrent workers (extract + summary) call `mark_dirty()`; the writer flushes at most every `min_interval_seconds` or every `every_n` marks, with a forced flush at close. Never write the daily state JSON directly from a worker — go through this writer or you will fight the disk on every paper.

### Configuration layering

`scripts/common.py:load_config` reads `config.yaml` then applies env overrides. Notable: setting `CATEGORIES` in env (comma/space/semicolon separated) overrides `arxiv.categories` from `config.yaml` at runtime. Local dev auto-loads `.env.local` via `load_local_env()`; `os.environ.setdefault` is used so already-set env vars win.

DeepSeek settings come from `load_deepseek_settings()` reading `LLM_ENABLED`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, `LANGUAGE`, `LLM_TIMEOUT_SECONDS`, `LLM_RETRY_TIMES`. Requests use `httpx.Client(..., trust_env=False)` to bypass any local HTTP proxy and `response_format={"type": "json_object"}`. Retry covers rate-limit, timeout, transport, and malformed/truncated JSON.

### Prompts

Live in `src/prompts/` as plain text files: `summary_system.txt`, `summary_user.txt`, `summary_chunk_user.txt`, `summary_reduce_user.txt`. The chunk/reduce pair is used when fulltext exceeds `summary_chunk_trigger_chars` in `config.yaml`.

### PDF extraction layering caveat

`opendataloader_pdf.convert()` already runs `java -jar` as a subprocess. `pdf_fulltext.py` historically wrapped that in another `subprocess.run([sys.executable, "-c", ...])` — the "Python → Python → java" stack noted in `REFACTOR_PLAN.md`. JVM startup per paper is unavoidable without switching to the hybrid server variant; check the current state of `pdf_fulltext.py` before claiming an optimization here.

`pdf_fulltext.py` now exposes three functions:
- `download_pdf_for_paper(paper, force_refresh, config)` — pure network I/O, safe to call with high concurrency.
- `extract_pdf_to_markdown(paper, force_refresh, config)` — runs the JVM extractor on an already-downloaded PDF; CPU/JVM bound, keep concurrency low.
- `ensure_fulltext_for_paper(paper, force_refresh)` — backwards-compatible composite for `extract_fulltext.py` and any caller that wants both in one call.

## Frontend

Vanilla HTML/CSS/JS in `docs/index.html`, `docs/style.css`, `docs/app.js`. It fetches `docs/data/index.json` then per-day JSONs. Keep the public payload small; any field added to a paper that the UI does not use should also be added to `HEAVY_PAPER_FIELDS` in `build_site_data.py`.

## CodeGraph is initialized

`.codegraph/` exists in this repo, so prefer codegraph tools over grep-and-read for symbol lookups, call graphs, and impact analysis. Use `codegraph_search` for symbols, `codegraph_callers` / `codegraph_callees` to trace flow, and `codegraph_impact` before touching shared helpers like `common.py` or `snapshot_writer.py`.

## Style notes specific to this repo

- Scripts use `from __future__ import annotations` and PEP 604 unions.
- Logging is `print({...})` of small dicts to stdout, not the `logging` module. Match that style in pipeline scripts.
- Heavy field names are centralized as `HEAVY_PAPER_FIELDS` in `build_site_data.py` — update there if the schema grows.
- Cross-script imports are sibling-style (`from common import ...`); a few scripts have a `try/except ModuleNotFoundError` fallback to `from scripts.common import ...` for package-style invocation. Preserve that pattern when adding cross-script imports.

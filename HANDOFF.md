# Dash Handoff

This document describes the current repo state for the next engineer or agent.

## Project goal

Dash is a personal arXiv `/new` reader for daily use:

- fetch new papers from configured arXiv categories,
- deduplicate by arXiv id across days,
- summarize papers in Chinese from arXiv metadata and abstracts via DeepSeek,
- optionally score papers against personal keywords,
- publish a lightweight static site from `docs/`.

Snapshot filenames use the local run date, not the arXiv publication date. Reruns on the same date merge into the same snapshot file.

## Non-negotiable user preferences

- Do not add timezone logic into application code; scheduling is controlled by GitHub Actions.
- `.env.local` is local-only and must never be committed.
- The project is DeepSeek-only; do not add a generic LLM provider abstraction.
- Production summaries are metadata/abstract-only. Do not download PDFs or claim full-text reading.
- `docs/data/*.json` must stay lightweight.
- Online workflow runs separate stage scripts, not `pipeline.py`.
- Pipeline stages are explicit; each stage may parallelize internally.

## Current data flow

1. `scripts/run_daily.py`
   - fetches ids from arXiv `/list/<category>/new`,
   - backfills metadata through arXiv API,
   - drops ids already present in the previous day snapshot,
   - merges same-day reruns into `tmp/state/YYYY-MM-DD.json`,
   - strips legacy `fulltext_*` fields if old state files contain them.
2. `scripts/enrich.py`
   - runs a summary worker pool over papers that still need summaries,
   - sends title, authors, categories, dates, comment, journal ref, DOI, abstract URL, abstract, and optional personal keywords to DeepSeek,
   - writes six summary fields: `tldr`, `motivation`, `method`, `result`, `conclusion`, `relevance_score`.
3. `scripts/build_site_data.py`
   - writes lightweight public files into `docs/data/`,
   - rebuilds `docs/data/index.json`.
4. `scripts/validate_data.py`
   - fails if generated state or public data is malformed.

`scripts/pipeline.py` is only a local convenience wrapper.

## Important files

- `config.yaml` — arXiv categories, site metadata, output policy.
- `keywords.yaml` — optional personal relevance keywords; `keywords: []` disables relevance scoring.
- `scripts/common.py` — config/env/keyword loading and path helpers.
- `scripts/fetch_arxiv.py` — category page + arXiv API fetcher.
- `scripts/run_daily.py` — fetch/dedup/merge stage.
- `scripts/enrich.py` — production summary stage.
- `scripts/summarize.py` — DeepSeek request/parse/retry helpers and standalone summary entrypoint.
- `scripts/snapshot_writer.py` — debounced state writer used by concurrent summary workers.
- `scripts/build_site_data.py` — public data builder.
- `scripts/validate_data.py` — generated data validator.
- `docs/index.html`, `docs/style.css`, `docs/app.js` — static frontend.
- `src/prompts/summary_system.txt`, `src/prompts/summary_user.txt` — only live prompts.

## Operational notes

Environment variables:

- `CATEGORIES` optionally overrides `config.yaml` categories.
- `LLM_ENABLED=false` disables paid LLM calls and writes fallback summaries.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`, `LANGUAGE`, `LLM_TIMEOUT_SECONDS`, `LLM_RETRY_TIMES` configure DeepSeek.
- `SUMMARY_MAX_WORKERS` controls concurrent summary requests; GitHub Actions uses 4.

Default GitHub Actions flow:

```bash
python scripts/run_daily.py --date YYYY-MM-DD
python scripts/enrich.py --date YYYY-MM-DD
python scripts/build_site_data.py --latest-date YYYY-MM-DD
python scripts/validate_data.py tmp/state/YYYY-MM-DD.json docs/data/index.json docs/data/YYYY-MM-DD.json
```

Local wrapper:

```bash
.venv/bin/python scripts/pipeline.py --date YYYY-MM-DD
```

Validation before committing:

```bash
.venv/bin/python -m compileall scripts
node --check docs/app.js
```

## What not to break

- Same-day merge behavior for snapshots.
- Previous-day dedup by arXiv id.
- Stage-separated online workflow.
- Metadata/abstract-only summary contract.
- Optional keyword relevance scoring.
- Lightweight public data.
- DeepSeek JSON mode request path with `trust_env=False`.

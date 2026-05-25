# Dash Handoff

This document is for the next engineer or agent taking over the repo. It focuses on current reality, not intended architecture.

## Project goal

Dash is a personal arXiv `/new` reader for daily use:

- fetch new papers from configured arXiv categories
- deduplicate by arXiv id across days
- extract PDF full text
- send paper content to DeepSeek for Chinese summaries
- publish a lightweight static site from `docs/`

The user does not want the snapshot date to be bound to arXiv publication date. One local day should produce one snapshot JSON, and reruns on the same day should merge into that same file.

## Non-negotiable user preferences

- Do not add timezone logic into application code. Scheduling time is controlled outside the app, especially by GitHub Actions.
- `.env.local` is for local development only and must never be committed.
- The project is effectively DeepSeek-only. General provider abstraction is not a priority.
- Frontend should stay lightweight and fast. `docs/data/*.json` should not carry heavy fulltext payloads.
- Online workflow should run separate stage scripts, not `pipeline.py --from-stage ...`.
- Pipeline should be explicitly split into stages, but each stage may use internal parallelism.
- Fulltext-based summary is preferred. Abstract-only summary is fallback only when extraction fails or times out.

## Current repo status

The worktree is still very early and largely uncommitted.

- `README.md` is heavily updated from the original one-line stub.
- Most project files are still untracked according to `git status`.
- Current local sample data exists:
  - `tmp/state/2026-05-16.json`
  - `docs/data/2026-05-16.json`
  - `docs/data/index.json`

Current sample snapshot state:

- date: `2026-05-16`
- categories in generated data: `["physics.ao-ph"]`
- paper count: `7`
- summary status counts: `{"ok": 7}`

Note: current `config.yaml` still contains `cs.CV`, `cs.CL`, `cs.LG`, but runtime category selection is overridden by `CATEGORIES` from `.env.local` through `scripts/common.py`.

## Architecture overview

### Data flow

1. `scripts/run_daily.py`
   - fetches papers from arXiv `/list/<category>/new`
   - backfills metadata via arXiv API by id
   - drops ids already present in the previous day snapshot
   - merges same-day reruns into `tmp/state/YYYY-MM-DD.json`

2. `scripts/extract_fulltext.py`
   - loads `tmp/state/YYYY-MM-DD.json`
   - downloads PDFs into `tmp/paper_cache/`
   - runs `opendataloader-pdf`
   - stores extracted artifacts in `tmp/pdf_extract/<paper_id>/`
   - writes structured fulltext back into the state JSON

3. `scripts/summarize.py`
   - loads state JSON
   - prefers `fulltext_markdown`
   - falls back to abstract-only input if fulltext is missing
   - calls DeepSeek `/chat/completions`
   - writes Chinese structured summary fields back into the state JSON

4. `scripts/build_site_data.py`
   - strips heavy fields from state JSON
   - writes lightweight public files into `docs/data/`
   - rebuilds `docs/data/index.json`

5. `scripts/validate_data.py`
   - validates generated files

### Local convenience wrapper

`scripts/pipeline.py` exists only as a local convenience wrapper. It is not the preferred production entrypoint.

### Online workflow

`.github/workflows/daily.yml` already follows the user's preferred pattern and runs stage scripts directly:

1. `run_daily.py`
2. `enrich.py` — combined extract + summarize, pipelined per paper. Replaces the previous `extract_fulltext.py` → `summarize.py` two-step. The two split scripts are still supported for ad-hoc use; `enrich.py` reuses their core functions (`ensure_fulltext_for_paper`, `summarize_one_paper`, `apply_fallback`, `flatten_sections`, `refresh_summary_counts`) so semantics stay aligned.
3. `build_site_data.py`
4. `validate_data.py`

Snapshot writes go through `scripts/snapshot_writer.py` (`SnapshotWriter`) — a thread-safe, debounced writer that flushes every N marks or every T seconds, with a guaranteed final flush at close. `extract_fulltext.py`, `summarize.py`, and `enrich.py` all funnel writes through it so concurrent workers don't contend on disk I/O and the on-disk file lags the in-memory payload by at most one debounce window.

## Important files

### Pipeline and backend

- [config.yaml](/Users/sonderlau/Dev/Dash/config.yaml)
- [scripts/common.py](/Users/sonderlau/Dev/Dash/scripts/common.py)
- [scripts/fetch_arxiv.py](/Users/sonderlau/Dev/Dash/scripts/fetch_arxiv.py)
- [scripts/run_daily.py](/Users/sonderlau/Dev/Dash/scripts/run_daily.py)
- [scripts/pdf_fulltext.py](/Users/sonderlau/Dev/Dash/scripts/pdf_fulltext.py)
- [scripts/extract_fulltext.py](/Users/sonderlau/Dev/Dash/scripts/extract_fulltext.py)
- [scripts/summarize.py](/Users/sonderlau/Dev/Dash/scripts/summarize.py)
- [scripts/enrich.py](/Users/sonderlau/Dev/Dash/scripts/enrich.py)
- [scripts/snapshot_writer.py](/Users/sonderlau/Dev/Dash/scripts/snapshot_writer.py)
- [scripts/build_site_data.py](/Users/sonderlau/Dev/Dash/scripts/build_site_data.py)
- [scripts/validate_data.py](/Users/sonderlau/Dev/Dash/scripts/validate_data.py)
- [scripts/cleanup_artifacts.py](/Users/sonderlau/Dev/Dash/scripts/cleanup_artifacts.py)

### Frontend

- [docs/index.html](/Users/sonderlau/Dev/Dash/docs/index.html)
- [docs/style.css](/Users/sonderlau/Dev/Dash/docs/style.css)
- [docs/app.js](/Users/sonderlau/Dev/Dash/docs/app.js)

### Prompt templates

- [src/prompts/summary_system.txt](/Users/sonderlau/Dev/Dash/src/prompts/summary_system.txt)
- [src/prompts/summary_user.txt](/Users/sonderlau/Dev/Dash/src/prompts/summary_user.txt)
- [src/prompts/summary_chunk_user.txt](/Users/sonderlau/Dev/Dash/src/prompts/summary_chunk_user.txt)
- [src/prompts/summary_reduce_user.txt](/Users/sonderlau/Dev/Dash/src/prompts/summary_reduce_user.txt)

### Reference / planning documents

- [DESIGN.md](/Users/sonderlau/Dev/Dash/DESIGN.md)
- [arxiv-personal-tool-plan.md](/Users/sonderlau/Dev/Dash/arxiv-personal-tool-plan.md)

## Current implementation details worth knowing

### Fetching strategy

The project no longer treats "today" as arXiv published date. Instead:

- fetch from arXiv category `/new` pages
- use current local run date as snapshot filename
- deduplicate against the previous day snapshot by arXiv id
- merge multiple same-day runs into the same state file

This behavior lives mainly in:

- `scripts/fetch_arxiv.py`
- `scripts/run_daily.py`

### Category source of truth

Categories used at runtime can come from environment:

- `CATEGORIES=...` is parsed in `scripts/common.py`
- it overrides `config.yaml`

This is important because generated public data may not match `config.yaml` if `.env.local` is active.

### Fulltext extraction

`opendataloader-pdf` is integrated through `scripts/pdf_fulltext.py`.

Important behaviors:

- extraction runs in a subprocess
- hard timeout exists to avoid indefinite hangs
- failed extraction is recorded as `extract_failed:<ExceptionName>`
- summary can still proceed using abstract fallback

Artifacts:

- PDF cache: `tmp/paper_cache/`
- extraction cache: `tmp/pdf_extract/<paper_id>/`

### Summarization

DeepSeek is called through `httpx` in `scripts/summarize.py`.

Important details:

- `trust_env=False` is used to avoid local proxy interference
- JSON mode is used through `response_format: {"type":"json_object"}`
- retries exist for timeout, rate limit, transport, malformed JSON, and length truncation
- summary writes structured Chinese fields:
  - `tldr`
  - `motivation`
  - `method`
  - `result`
  - `conclusion`

### Chunking and long-context handling

There is already infrastructure for chunk prompts and reduce prompts in `src/prompts/`, and config has:

- `summary_chunk_char_budget`
- `summary_chunk_trigger_chars`
- `summary_chunk_overlap_chars`

This area should be reviewed carefully before expanding paper volume, because fulltext summarization latency is currently the main cost center.

### Frontend behavior

The frontend is static vanilla JS and consumes only `docs/data/index.json` plus a selected day JSON.

Current UI behaviors:

- theme toggle with dark/light mode
- date picker for available snapshot dates
- search box
- filter box with persisted keywords in browser cookies
- category chips with multi-select and an `All` button
- card grid for papers
- modal detail view with TLDR, 2x2 notes, and original abstract

## Known issues

### 1. Frontend horizontal overflow is not fully resolved

This is the biggest known open issue right now.

Observed behavior:

- the user reports horizontal scrolling when viewport width goes above roughly `1000px`
- there is extra blank space on the right
- it still reproduces after several CSS fixes

What has already been tried:

- removed outer layout width based on `100vw`
- added `overflow-x: clip` on `html, body`
- added `min-width: 0` to several flex/grid children
- added `overflow-wrap: anywhere` to long text blocks
- tightened modal width constraints

Most likely remaining causes:

- some grid or flex child still participates in min-content sizing at the 3-column breakpoint
- a specific paper card with long content may still be stretching a grid column
- there may be an interaction between dialog styling and page width

Recommended next step:

- add a temporary debug snippet in `docs/app.js` that logs the widest element:
  - `document.documentElement.clientWidth`
  - `document.documentElement.scrollWidth`
  - all elements where `getBoundingClientRect().right` exceeds viewport width
- inspect at widths around `1024px`, `1100px`, `1200px`
- do not keep guessing visually

### 2. Frontend polish remains sensitive

The user is very unhappy with repeated UI regressions, especially around:

- top control bar alignment
- filter control aesthetics
- spacing and perceived polish

Any further frontend change should be made carefully and verified directly in browser.

### 3. README and repo state are still immature

- many files are untracked
- README is improved but still not the complete handoff story
- project structure is functional, but not yet cleaned for first public commit

## Operational notes

### Local environment

Scripts auto-load `.env.local` if present. Important env vars:

- `CATEGORIES`
- `LLM_ENABLED`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `MODEL_NAME`
- `LANGUAGE`
- `LLM_TIMEOUT_SECONDS`
- `LLM_RETRY_TIMES`
- `PDF_EXTRACT_MAX_WORKERS`
- `SUMMARY_MAX_WORKERS`

Do not commit `.env.local`.

### Dependencies

Current runtime dependencies in [requirements.txt](/Users/sonderlau/Dev/Dash/requirements.txt):

- `feedparser==6.0.11`
- `httpx==0.28.1`
- `PyYAML==6.0.2`
- `opendataloader-pdf==2.4.3`
- `tqdm==4.67.1`

### Quick local commands

Full local convenience run:

```bash
python scripts/pipeline.py --date 2026-05-16
```

Preferred stage-by-stage local run:

```bash
python scripts/run_daily.py --date 2026-05-16
python scripts/extract_fulltext.py --date 2026-05-16
python scripts/summarize.py --date 2026-05-16
python scripts/build_site_data.py --latest-date 2026-05-16
python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json
```

Lightweight validation:

```bash
node --check docs/app.js
python3 -m py_compile scripts/*.py
```

Cleanup:

```bash
python scripts/cleanup_artifacts.py --all
```

## Suggested next priorities

1. Resolve horizontal overflow with real DOM-level diagnosis, not more blind CSS edits.
2. Verify the top control bar visually on desktop and mobile after overflow is fixed.
3. Review `scripts/summarize.py` long-context logic against real daily load and refine chunking/reduction if needed.
4. Clean the repo for a first coherent commit:
   - review `.gitignore`
   - exclude local-only debug artifacts
   - confirm generated data policy
5. Only after UI stabilization, continue visual polish and card/modal refinement.

## What not to break

- same-day merge behavior for snapshots
- previous-day dedup by arXiv id
- stage-separated online workflow
- heavy fulltext stripped from `docs/data/*.json`
- `.env.local` local-only behavior
- DeepSeek request path that bypasses local proxy issues

## Short status summary

The backend pipeline is largely functional for a personal MVP.

The frontend is usable but not stable yet. The main blocker is unresolved horizontal overflow around desktop widths above ~1000px, plus the user's strong dissatisfaction with UI regressions.

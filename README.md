# Dash

Dash is a personal arXiv daily reader that:

- fetches papers from selected arXiv categories,
- generates Chinese summaries from arXiv metadata and abstracts with DeepSeek,
- optionally scores each paper against personal reading keywords,
- writes stable daily JSON snapshots,
- publishes a static reading site from `docs/`.

## Snapshot model

The generated JSON filename is the local run date, not the arXiv publication date.

- `docs/data/2026-05-16.json` means "the snapshot collected on 2026-05-16"
- papers inside that file may have `published_date` values from earlier arXiv update cycles
- rerunning the pipeline on the same day merges new fetched papers into the same daily snapshot file

This keeps one snapshot file per day while decoupling local collection time from arXiv release timing.

The initial architecture is intentionally small:

- Python scripts for the data pipeline
- arXiv API as the paper source
- static HTML/CSS/JS for the UI
- GitHub Actions for scheduling
- GitHub Pages for deployment

## Structure

```text
config.yaml
keywords.yaml
requirements.txt
scripts/
src/prompts/
docs/
.github/workflows/
```

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.local.example .env.local
python scripts/pipeline.py --date 2026-05-16
```

For local development, scripts auto-load `.env.local` if it exists.

Required env vars for summarization:

- `LLM_ENABLED`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `MODEL_NAME`
- `LANGUAGE`

Recommended DeepSeek local setup:

- `OPENAI_BASE_URL=https://api.deepseek.com`
- `MODEL_NAME=deepseek-v4-flash`
- `LLM_TIMEOUT_SECONDS=600`
- `LLM_RETRY_TIMES=3`

## Metadata summaries and relevance scoring

Summaries are abstract/metadata-only by default. The model receives title,
authors, categories, arXiv dates, optional comment / journal reference / DOI,
abstract URL, and the English abstract. It does not download PDFs or claim to
have read the full paper.

Personal relevance scoring is configured in `keywords.yaml`:

```yaml
keywords:
  - diffusion planning
  - robot manipulation
  - long-context code generation
```

Leave `keywords: []` empty to disable `relevance_score` generation. When
enabled, the summary prompt asks DeepSeek to return an integer string from 0 to
100 for personal reading priority, not paper quality.

## Pipeline stages

For local development, `scripts/pipeline.py` remains a convenience wrapper:

```bash
python scripts/pipeline.py --date 2026-05-16
```

Useful variants:

- `python scripts/pipeline.py --date 2026-05-16 --skip-summarize`
- `python scripts/pipeline.py --date 2026-05-16 --summarize-limit 10`
- `python scripts/pipeline.py --date 2026-05-16 --refresh-ok`

For scheduled / online execution, each stage should run as its own script:

1. `python scripts/run_daily.py --date 2026-05-16`
2. `python scripts/enrich.py --date 2026-05-16` — summarizes papers concurrently from metadata and abstracts.
3. `python scripts/build_site_data.py --latest-date 2026-05-16`
4. `python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json`

The standalone summarizer is still available if you only want to refresh summaries:

- `python scripts/summarize.py --date 2026-05-16`

Stage-level parallelism:

- `fetch_arxiv` 抓取 category `/list/<cat>/new` 是并发的（默认上限 8 路，由 `ARXIV_LIST_WORKERS` 控制）；arXiv `/api/query` 的 50-id chunked 调用默认串行（`ARXIV_API_WORKERS=1`），chunk 间隔默认 10 秒（`ARXIV_API_REQUEST_DELAY_SECONDS`）以避开 GitHub runner 共享 IP 的 429
- `enrich` 只有 summary worker pool；默认 4 个 summary worker，可用 `--summary-workers` 或 `SUMMARY_MAX_WORKERS` 覆盖
- `scripts/summarize.py` 也保留 `--max-workers`，用于单独刷新 metadata/abstract summaries
- 每日 snapshot 文件由 debounced 线程安全 writer 落盘，并发 worker 只 mark dirty，不竞争磁盘
- 进度通过 `tqdm`（拆分 stage）或 `enrich` 的逐篇日志可见
- pipeline stage 之间仍显式分开
- 本地与线上入口仍分开：本地用 wrapper，线上直接调 stage 脚本

File responsibilities:

- `tmp/state/YYYY-MM-DD.json`: pipeline working state
- `docs/data/YYYY-MM-DD.json`: frontend-facing lightweight snapshot
- `docs/data/index.json`: frontend index metadata

Cleanup:

- `python scripts/cleanup_artifacts.py --all`

## Current stack decision

- Backend/runtime: Python 3.12
- Fetching: arXiv API via Atom feed
- Storage: versioned JSON files in `docs/data/`
- Frontend: vanilla HTML/CSS/JS
- Hosting: GitHub Pages from `/docs`
- Automation: GitHub Actions workflow; current cron schedule is commented out, manual dispatch remains available

DeepSeek requests currently use:

- `/chat/completions`
- `response_format: {"type":"json_object"}`
- `httpx.Client(..., trust_env=False)` to avoid local proxy interference
- retry on rate-limit, timeout, transport, and malformed/truncated JSON cases

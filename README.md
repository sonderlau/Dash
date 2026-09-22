# Dash

Dash is a personal arXiv daily reader that:

- fetches papers from selected arXiv categories,
- generates Chinese summaries from arXiv metadata and abstracts with Xiaomi MiMo streaming chat,
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

Recommended MiMo local setup:

- `OPENAI_BASE_URL=https://api.xiaomimimo.com/v1`
- `MODEL_NAME=mimo-v2.6-flash`
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
enabled, the summary prompt asks MiMo to return an integer string from 0 to
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
2. `python scripts/enrich.py --date 2026-05-16` — streams a JSON summary for each paper that still needs one.
3. `python scripts/build_site_data.py --latest-date 2026-05-16`
4. `python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json`

`python scripts/pipeline.py` runs those stages and then builds the site.

The standalone submitter is still available if you only want to refresh summaries:

- `python scripts/summarize.py --date 2026-05-16`

Stage notes:

- `fetch_arxiv` 以 category `/list/<cat>/new` 为主数据源（默认上限 8 路，由 `ARXIV_LIST_WORKERS` 控制），因此只要 list 页可用就能确定今日新增 paper；arXiv `/api/query` 只做可选 metadata 补充，成功时只补 abstract、DOI、comment 等字段，429/503 会标记 `fetch_status.api_backfill_status = "degraded"` 并继续产出当天 snapshot
- 如果 list 页成功但去重后没有新 paper，`run_daily.py` 会标记 `run_status = "no_new_papers"`；GitHub Actions 会跳过摘要、validate 当前空日期、commit 和 deploy，不发布空 snapshot
- `enrich` 按论文流式请求。并发由 `--summary-workers` 或 `SUMMARY_MAX_WORKERS` 控制，默认 4
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
- Fetching: arXiv `/list/<cat>/new` pages, with optional arXiv API field enrichment
- Storage: versioned JSON files in `docs/data/`
- Frontend: vanilla HTML/CSS/JS
- Hosting: GitHub Pages from `/docs`
- Automation: GitHub Actions workflow with scheduled and manual dispatch

`daily.yml` runs at UTC 10:10. It summarizes and publishes in the same job.

MiMo summary requests currently use:

- streaming `POST /v1/chat/completions` on `https://api.xiaomimimo.com/v1`
- `response_format: {"type":"json_object"}` and `thinking.type=disabled`
- the client joins `delta.content` and parses that string as the six-field summary
- `httpx.Client(..., trust_env=False)` to avoid local proxy interference
- malformed JSON is retried; a paper that still fails is stored as a fallback summary

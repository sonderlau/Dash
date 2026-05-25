# Dash

Dash is a personal arXiv daily reader that:

- fetches papers from selected arXiv categories,
- extracts PDF full text with `opendataloader-pdf`,
- generates Chinese summaries with DeepSeek,
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

## PDF full-text extraction

Summaries now prefer full paper text over abstract-only input.

- PDF files are cached in `tmp/paper_cache/`
- extracted markdown/json are cached in `tmp/pdf_extract/`
- DeepSeek receives `abstract + extracted full text`
- `docs/data/*.json` keeps frontend payloads lighter by stripping raw full text during site rebuild

Current tradeoff:

- summary quality is better because the model sees the paper body
- latency and token usage are much higher on long papers
- local cache becomes important to avoid repeated PDF downloads and repeated extraction

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
2. `python scripts/enrich.py --date 2026-05-16` — pipelines extract and summarize per paper (each paper hands off to summary as soon as its fulltext is ready, so total wall-clock is closer to `max(extract_total, summary_total)` than the sum). The split form below is still supported.
3. `python scripts/build_site_data.py --latest-date 2026-05-16`
4. `python scripts/validate_data.py tmp/state/2026-05-16.json docs/data/index.json docs/data/2026-05-16.json`

The split form (run extract and summarize as separate stages) is still available — useful when you only want to refresh one side:

- `python scripts/extract_fulltext.py --date 2026-05-16`
- `python scripts/summarize.py --date 2026-05-16`

Stage-level parallelism:

- `fetch_arxiv`抓取 5 个 category 的 `/list/<cat>/new` 是并发的（默认上限 8 路，由 `ARXIV_LIST_WORKERS` 控制），arXiv `/api/query` 的 50-id chunked 调用也并发（默认 4 路，由 `ARXIV_API_WORKERS` 控制）
- `enrich` 是 download → extract → summary 的三段流水：每篇论文下载完立刻进入 extract，extract 完立刻进入 summary，wall-clock 接近 `max(download_total, extract_total, summary_total)`
- 默认 8 个 download worker（纯网络 IO，可以高并发）、2 个 extract worker（JVM/CPU 受限）、4 个 summary worker；可用 `--download-workers` / `--extract-workers` / `--summary-workers`，或对应 env `PDF_DOWNLOAD_MAX_WORKERS` / `PDF_EXTRACT_MAX_WORKERS` / `SUMMARY_MAX_WORKERS` 覆盖
- 独立运行的 `extract_fulltext` 和 `summarize` 也保留同名 env 与 worker 默认值
- 每日 snapshot 文件由 debounced 线程安全 writer 落盘，并发 worker 只 mark dirty，不竞争磁盘
- 进度通过 `tqdm`（拆分 stage）或 `enrich` 的逐篇日志可见
- pipeline stage 之间仍显式分开
- 本地与线上入口仍分开：本地用 wrapper，线上直接调 stage 脚本

File responsibilities:

- `tmp/state/YYYY-MM-DD.json`: pipeline working state, including heavy fields
- `docs/data/YYYY-MM-DD.json`: frontend-facing lightweight snapshot
- `docs/data/index.json`: frontend index metadata
- `tmp/paper_cache/`: cached PDFs
- `tmp/pdf_extract/`: extracted markdown/json and extraction metadata

Cleanup:

- `python scripts/cleanup_artifacts.py --all`
- `python scripts/cleanup_artifacts.py --pdf-cache --pdf-extract`

## Current stack decision

- Backend/runtime: Python 3.12
- Fetching: arXiv API via Atom feed
- Storage: versioned JSON files in `docs/data/`
- Frontend: vanilla HTML/CSS/JS
- Hosting: GitHub Pages from `/docs`
- Automation: GitHub Actions scheduled workflow

DeepSeek requests currently use:

- `/chat/completions`
- `response_format: {"type":"json_object"}`
- `httpx.Client(..., trust_env=False)` to avoid local proxy interference
- retry on rate-limit, timeout, transport, and malformed/truncated JSON cases

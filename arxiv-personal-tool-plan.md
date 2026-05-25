# arXiv Personal Daily Reader: Planning Document

## Purpose

This document defines a minimal, stable, personal-use arXiv daily reader project.

The goal is not to build a public product. The goal is to build a tool that:

- runs automatically every day,
- fetches papers from selected arXiv categories,
- generates Chinese summaries with an LLM,
- publishes a static page for reading,
- stays easy to understand and maintain.

The design below is intentionally conservative. It prioritizes long-term stability over feature richness.

## Project Positioning

This should be treated as a personal infrastructure script with a static UI, not as a full-stack app.

That means:

- no always-on backend,
- no database,
- no user system,
- no complicated crawler logic,
- no dependency on third-party frontend services,
- no hidden data flow split across multiple branches unless absolutely necessary.

If the system can be explained in one page and debugged in one hour, it is probably at the right size.

## Core Goals

The first version should do only the following:

1. Read project configuration from one file.
2. Fetch new papers from specified arXiv categories.
3. Deduplicate papers by arXiv ID.
4. Generate Chinese summaries with a configurable LLM.
5. Keep the original English abstract when summarization fails.
6. Save daily data as JSON files.
7. Publish a static website through GitHub Pages.
8. Let the user browse by date, category, and simple search.

## Explicit Non-Goals

Do not build these in the MVP:

- authentication,
- password protection,
- branch-based data separation like `main` + `data`,
- markdown generation,
- external sensitivity filtering services,
- browser-side calls to AI APIs,
- heavy UI customization,
- personalized recommendation models,
- multi-user support,
- comments, likes, or social features,
- server-side search.

These are not forbidden forever. They are simply not worth the first maintenance burden.

## Success Criteria

The project is successful if:

- it can run unattended on GitHub Actions for weeks,
- failures are obvious from logs,
- a bad AI response does not break the whole site,
- the generated page always has readable fallback content,
- categories shown on the page always match the configured logic,
- the codebase remains small enough for one person to fully understand.

## Recommended Architecture

### High-level structure

Use a simple static-site architecture:

- Python for data fetching and generation
- GitHub Actions for scheduling
- JSON files for data storage
- static HTML/CSS/JS for the UI
- GitHub Pages for deployment

### Suggested repository structure

```text
repo/
  config.yaml
  requirements.txt
  scripts/
    fetch_arxiv.py
    summarize.py
    build_site_data.py
    run_daily.py
  src/
    prompts/
      summary_system.txt
      summary_user.txt
  docs/
    index.html
    app.js
    style.css
    data/
      index.json
      2026-05-16.json
  .github/
    workflows/
      daily.yml
  README.md
```

### Why this structure

- `config.yaml`: one place to manage categories, model, language, output behavior
- `scripts/`: all automation logic stays in one place
- `docs/`: GitHub Pages can publish directly from here
- `docs/data/`: generated static data is colocated with the frontend
- no split deployment branch: easier to reason about and debug

## Configuration Design

All important project behavior should come from `config.yaml`.

Example:

```yaml
site:
  title: "My arXiv Daily Reader"
  base_url: "/my-arxiv-reader/"

arxiv:
  categories:
    - cs.CV
    - cs.CL
    - cs.LG
  max_results_per_category: 100

llm:
  provider: openai_compatible
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
  language: "zh-CN"
  timeout_seconds: 60
  retry_times: 2

output:
  keep_days: 90
  write_pretty_json: false
  fallback_to_english_abstract: true
```

Configuration rules:

- one file only,
- no duplicated config across workflow and code,
- category order in config should matter,
- display logic should derive from config, not from raw API ordering.

## Data Model

The data model should be explicit and stable.

Each paper should have fields similar to:

```json
{
  "id": "2605.12345",
  "title": "Paper title",
  "authors": ["Author A", "Author B"],
  "categories": ["stat.ML", "cs.LG"],
  "matched_categories": ["cs.LG"],
  "display_category": "cs.LG",
  "abs_url": "https://arxiv.org/abs/2605.12345",
  "pdf_url": "https://arxiv.org/pdf/2605.12345",
  "abstract_en": "Original abstract",
  "summary_zh": "Chinese summary",
  "summary_status": "ok",
  "published_date": "2026-05-16",
  "source": "arxiv"
}
```

### Important fields

- `categories`: full category list from arXiv
- `matched_categories`: categories from your config that this paper matched
- `display_category`: the category shown in the UI

This avoids the exact bug pattern you saw in the forked project.

Do not derive display grouping from `categories[0]`.

### Daily index format

`docs/data/index.json` should contain lightweight metadata only:

```json
{
  "available_dates": ["2026-05-16", "2026-05-15"],
  "categories": ["cs.CV", "cs.CL", "cs.LG"],
  "latest_date": "2026-05-16"
}
```

Each daily file like `docs/data/2026-05-16.json` should contain the real paper list.

This keeps frontend loading simple.

## Data Source Strategy

### Strong recommendation

Use the official arXiv API first, not HTML scraping.

Reason:

- simpler implementation,
- fewer selector breakages,
- easier to test,
- easier to explain,
- lower maintenance cost.

### Tradeoff

The API can be less convenient for some category-specific daily listing behavior than HTML pages.
That is acceptable for a personal tool.

The project should prefer stability over perfectly matching the website's daily page formatting.

## Categorization Logic

This needs to be defined clearly up front.

Recommended rule:

1. Fetch papers that belong to any configured target category.
2. Keep all raw categories returned by arXiv.
3. Compute `matched_categories = intersection(raw_categories, configured_categories)`.
4. Set `display_category` as:
   - the first configured category that appears in `matched_categories`,
   - or `other` if none match and you intentionally keep it.

For the personal tool, the cleanest option is:

- only keep papers where `matched_categories` is non-empty,
- display each paper under the first matched configured category.

That guarantees the page categories always align with user intention.

## Summarization Strategy

### Rule

AI enhancement is optional enrichment, not a hard dependency for site integrity.

If summarization fails:

- the paper should remain in output,
- `summary_zh` can be empty or fallback text,
- `abstract_en` must still exist,
- the page should still render normally.

### Recommended prompt behavior

The prompt should ask for a short, structured Chinese output:

- one-sentence TL;DR
- motivation
- method
- result
- optional takeaway

But the stored data should tolerate partial completion.

### Failure handling

Do not let one bad paper crash the whole daily run.

Use per-paper handling:

- success -> store summary
- parse failure -> store fallback fields
- timeout -> store fallback fields
- rate limit -> retry a small number of times, then fallback

### Hard rule

Never publish an empty daily file just because AI failed.

## Frontend Scope

The frontend should stay small and boring.

### MVP UI features

- date selector
- category filter
- paper list
- title search
- detail panel or modal
- links to abstract and PDF

### Recommended UX behavior

- default to latest date
- show counts per category
- show whether the Chinese summary is available
- fall back to English abstract if no Chinese summary exists

### Avoid in MVP

- masonry layouts
- advanced animation
- multiple view modes
- local authentication
- export tools
- deeply nested filters

The page should feel fast and reliable, not complicated.

## GitHub Actions Design

The workflow should remain extremely simple.

### Daily job steps

1. Checkout repository
2. Install Python dependencies
3. Run daily script
4. Verify generated files are non-empty and valid JSON
5. Commit `docs/data/*` and index updates
6. Push to main

### Important validation gates

The workflow should fail if:

- no daily JSON file was generated,
- generated JSON is invalid,
- generated daily JSON contains zero papers when fetch logs indicate papers were found,
- index file is not updated consistently.

The workflow should not fail if:

- some papers failed AI summarization,
- some optional metadata is missing.

### Minimal deployment model

Use GitHub Pages from:

- branch: `main`
- folder: `/docs`

This is much easier than a multi-branch content model.

## Reliability Principles

These are mandatory if the tool is supposed to last.

### Principle 1: Single source of truth

- categories from `config.yaml`
- page reads only `docs/data/*`
- no hidden external file list service

### Principle 2: Fail soft

If AI, metadata enrichment, or secondary parsing fails, keep the paper.

### Principle 3: Validate before publish

Do not deploy if the generated daily file is structurally broken.

### Principle 4: Keep logs readable

Every daily run should answer:

- how many papers fetched,
- how many matched,
- how many summarized successfully,
- how many fell back,
- what file was written.

### Principle 5: Minimize moving parts

Every new external dependency increases maintenance cost.

## Likely Problems and Mitigations

### 1. arXiv API returns unexpected or incomplete metadata

Risk:

- category list shape changes,
- missing author fields,
- missing publication dates.

Mitigation:

- treat missing fields defensively,
- validate required fields,
- log and skip only truly broken records.

### 2. LLM output is malformed

Risk:

- invalid JSON,
- partial fields,
- language drift,
- timeout.

Mitigation:

- use structured parsing where possible,
- fallback to plain text summary field,
- never let malformed output remove the paper.

### 3. API rate limits or provider instability

Risk:

- daily job fails unpredictably.

Mitigation:

- retry a small number of times,
- summarize sequentially or with low concurrency,
- keep original abstract as fallback,
- make the run succeed with degraded output.

### 4. Category display drifts from configured categories

Risk:

- papers show under unrelated primary categories.

Mitigation:

- explicitly compute `matched_categories`,
- explicitly store `display_category`,
- never use raw first category for UI grouping.

### 5. Site grows too large over time

Risk:

- too many daily files,
- slower frontend loading,
- noisy repository history.

Mitigation:

- limit retention window, for example 90 or 180 days,
- keep `index.json` lightweight,
- lazy load one day at a time.

### 6. Workflow succeeds with useless output

Risk:

- empty files,
- stale index,
- broken JSON.

Mitigation:

- validate file size,
- validate JSON parse,
- validate paper count,
- stop before commit if checks fail.

## Roadmap

### Phase 0: Planning

Goal:

- freeze scope,
- confirm architecture,
- confirm data model,
- confirm deployment model.

Deliverables:

- this planning document,
- repository skeleton,
- config schema.

### Phase 1: Data pipeline MVP

Goal:

- fetch papers,
- filter categories,
- write daily JSON.

Deliverables:

- `fetch_arxiv.py`
- `build_site_data.py`
- sample `docs/data/YYYY-MM-DD.json`
- `index.json`

Success check:

- one local run generates valid JSON with expected categories.

### Phase 2: AI summarization

Goal:

- add Chinese summaries without breaking base output.

Deliverables:

- `summarize.py`
- fallback logic
- structured summary schema

Success check:

- partial AI failure still produces usable daily data.

### Phase 3: Static frontend

Goal:

- view papers by date and category.

Deliverables:

- `docs/index.html`
- `docs/app.js`
- `docs/style.css`

Success check:

- latest date loads,
- category filter works,
- missing summary falls back to English abstract.

### Phase 4: Automation

Goal:

- run daily without manual work.

Deliverables:

- `.github/workflows/daily.yml`
- validation and commit logic

Success check:

- manual trigger succeeds,
- scheduled run is understandable from logs.

### Phase 5: Hardening

Goal:

- reduce future maintenance risk.

Deliverables:

- retention policy
- better logging
- simple local smoke test
- basic JSON schema validation

Success check:

- one failure mode does not corrupt the site.

## Local Development Plan

The first local dev loop should be very small:

1. Write config
2. Fetch one day of papers
3. Build JSON
4. Open static page locally
5. Add AI summarization
6. Add workflow

Do not build the frontend before the data format is stable.
The data contract should come first.

## Testing Strategy

This project does not need a large test suite at first, but it does need a few targeted checks.

### Minimum useful tests

- category matching test
- `display_category` selection test
- fallback behavior test when summary fails
- JSON output structure test
- one smoke test for `run_daily.py`

### Manual checks

For every major change:

- inspect one generated daily JSON,
- verify paper count,
- verify categories shown are expected,
- verify one failed summary does not remove the paper.

## Maintenance Cost Estimate

### Initial build

Reasonable estimate for one person:

- planning and scaffold: half day
- data pipeline MVP: half day to one day
- AI integration: half day to one day
- frontend MVP: half day to one day
- workflow and hardening: half day to one day

Total realistic MVP range:

- 2 to 4 focused days

### Ongoing maintenance

If scope stays disciplined, expected maintenance is low:

- occasional model config change,
- rare arXiv API adjustments,
- occasional prompt tuning,
- workflow troubleshooting.

Expected steady-state maintenance:

- roughly 0 to 2 hours per month

This is only true if the system stays small.

## Decision Guidance

This project is worth starting if:

- you want full control over category logic,
- you want reliable personal use rather than public adoption,
- you accept 2 to 4 days of initial build for long-term clarity.

This project is not worth starting if:

- you only need a temporary paper viewer,
- the upstream project will probably satisfy you after a fix,
- you are likely to keep expanding features during implementation.

The main risk is not technical difficulty. The main risk is scope growth.

## Recommended First Build Order

If this project is approved, build in this order:

1. repository scaffold
2. `config.yaml`
3. `fetch_arxiv.py`
4. daily JSON writer
5. `index.json`
6. minimal `index.html` + `app.js`
7. summarization step
8. workflow automation
9. validation and retention cleanup

That order keeps the project debuggable at every step.

## Final Recommendation

If the objective is a long-term stable personal tool, this project is justified.

It should not be built as a clone of the existing forked project.
It should be built as a much smaller system with:

- one config file,
- one data pipeline,
- one static frontend,
- one deployment path,
- graceful degradation when AI fails.

That is the version most likely to remain usable months later without becoming a maintenance burden.

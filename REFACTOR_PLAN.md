# Dash maintenance plan

The previous PDF/full-text optimization plan is obsolete. The production pipeline now summarizes arXiv metadata and abstracts only.

## Current priorities

1. Keep GitHub Actions reliable for high-volume days by avoiding PDF downloads, JVM extraction, and chunk/reduce LLM calls.
2. Keep summary prompts stable and short so DeepSeek prompt cache remains useful.
3. Keep `keywords.yaml` optional: empty keywords must disable `relevance_score` generation.
4. Keep public `docs/data/*.json` small and schema-compatible with the vanilla frontend.
5. Add tests only where they protect pipeline/data contracts; avoid broad abstractions for this personal-use project.

## Future improvements

- Add a tiny fixture-based validation test for `run_daily.merge_papers`, `load_keywords`, and summary payload construction.
- Add a dry-run mode for `scripts/enrich.py` that renders prompt payloads without calling DeepSeek.
- Add optional top-N full-paper reading as a separate future feature only if explicitly requested; it must not re-enter the default daily workflow.
- Continue frontend polish only with browser verification.

## Out of scope

- Generic LLM provider abstraction.
- Production PDF extraction.
- Chunk/reduce full-text summarization.
- Cross-stage concurrency outside the explicit stage scripts.

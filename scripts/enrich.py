from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date
from typing import Any

from common import daily_path, load_config, load_deepseek_settings, load_keywords, read_json, write_json
from snapshot_writer import SnapshotWriter
from summarize import (
    apply_fallback,
    build_summary_http_client,
    flatten_sections,
    refresh_summary_counts,
    reset_fallback_summary,
    should_skip as summary_should_skip,
    summarize_one_paper,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize arXiv papers from metadata and abstracts concurrently.",
    )
    parser.add_argument("--date", required=True, help="Target daily file date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N papers.")
    parser.add_argument(
        "--summary-workers",
        type=int,
        default=int(os.getenv("SUMMARY_MAX_WORKERS", "4")),
        help="Concurrent DeepSeek summary workers.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1300,
        help="Initial max_tokens for one summary request.",
    )
    parser.add_argument(
        "--refresh-ok",
        action="store_true",
        help="Re-summarize papers already marked ok within the current scope.",
    )
    parser.add_argument(
        "--skip-summarize",
        action="store_true",
        help="Skip the LLM summary stage.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    llm_settings = load_deepseek_settings()
    target = daily_path(date.fromisoformat(args.date))
    payload = read_json(target)
    pretty = config["output"].get("write_pretty_json", True)
    limit = args.limit if args.limit and args.limit > 0 else None
    summary_workers = max(1, args.summary_workers)
    keywords = load_keywords()
    retries = int(llm_settings["retry_times"])
    summarize_enabled = (not args.skip_summarize) and llm_settings["enabled"]
    if (not args.skip_summarize) and llm_settings["enabled"] and not llm_settings["api_key"]:
        raise RuntimeError("Missing OPENAI_API_KEY")

    if (not args.skip_summarize) and not llm_settings["enabled"]:
        for index, paper in enumerate(payload.get("papers", [])):
            if limit is not None and index >= limit:
                break
            if summary_should_skip(paper):
                continue
            apply_fallback(config, paper, "llm_disabled")
        refresh_summary_counts(payload)
        write_json(target, payload, pretty=pretty)
        print({"status": "skipped", "reason": "llm_disabled", "papers": len(payload.get("papers", []))})
        return

    work_items: list[tuple[int, dict[str, Any]]] = []
    for index, paper in enumerate(payload.get("papers", [])):
        if limit is not None and index >= limit:
            break
        if summarize_enabled and args.refresh_ok and paper.get("summary_status") == "ok":
            reset_fallback_summary(paper)
        if summarize_enabled and limit is not None and paper.get("summary_status", "").startswith("fallback"):
            reset_fallback_summary(paper)
        summary_done = summary_should_skip(paper, keywords) if summarize_enabled else True
        if summary_done:
            continue
        work_items.append((index, deepcopy(paper)))

    if not work_items:
        write_json(target, payload, pretty=pretty)
        print({"status": "ok", "summary_processed": 0})
        return

    stats = {
        "summary_ok": 0,
        "summary_fallback": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }

    with build_summary_http_client(llm_settings, summary_workers) as client, ThreadPoolExecutor(
        max_workers=summary_workers
    ) as summary_pool, SnapshotWriter(
        target,
        payload,
        pretty=pretty,
        on_flush=refresh_summary_counts,
    ) as writer:

        future_to_index = {}
        for index, paper_copy in work_items:
            print(
                {
                    "stage": "metadata",
                    "paper_index": index + 1,
                    "paper_id": paper_copy["id"],
                    "status": "queued",
                }
            )
            paper = payload["papers"][index]
            print(
                {
                    "stage": "summary",
                    "paper_index": index + 1,
                    "paper_id": paper["id"],
                    "status": "started",
                    "model": llm_settings["model"],
                }
            )
            future = summary_pool.submit(
                summarize_one_paper,
                deepcopy(paper),
                llm_settings,
                retries,
                args.max_tokens,
                client,
                keywords,
            )
            future_to_index[future] = index

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            paper = payload["papers"][index]
            try:
                sections, telemetry = future.result()
                paper["summary_sections"] = sections
                paper["summary_zh"] = flatten_sections(sections)
                paper["summary_status"] = "ok"
                paper["summary_input_source"] = "metadata"
                stats["summary_ok"] += 1
                stats["prompt_tokens"] += telemetry.get("prompt_tokens") or 0
                stats["completion_tokens"] += telemetry.get("completion_tokens") or 0
                stats["prompt_cache_hit_tokens"] += telemetry.get("prompt_cache_hit_tokens") or 0
                stats["prompt_cache_miss_tokens"] += telemetry.get("prompt_cache_miss_tokens") or 0
                print(
                    {
                        "stage": "summary",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "ok",
                        "summary_input_source": paper["summary_input_source"],
                        "relevance_enabled": bool(keywords),
                        **telemetry,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                error_name = exc.__class__.__name__
                error_detail = str(exc).strip() or error_name
                apply_fallback(config, paper, error_name)
                stats["summary_fallback"] += 1
                print(
                    {
                        "stage": "summary",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "fallback",
                        "error": error_name,
                        "detail": error_detail[:200],
                    }
                )
            writer.mark_dirty()

    cache_hit = stats["prompt_cache_hit_tokens"]
    cache_miss = stats["prompt_cache_miss_tokens"]
    cache_total = cache_hit + cache_miss
    hit_rate = round(cache_hit / cache_total, 3) if cache_total > 0 else None
    print({"status": "ok", **stats, "prompt_cache_hit_rate": hit_rate})


if __name__ == "__main__":
    main()

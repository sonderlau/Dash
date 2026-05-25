from __future__ import annotations

import argparse
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from typing import Any

from common import daily_path, load_config, load_deepseek_settings, read_json, write_json
from extract_fulltext import should_skip as extract_should_skip
from pdf_fulltext import download_pdf_for_paper, extract_pdf_to_markdown
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
        description=(
            "Per-paper pipeline: download PDFs, extract fulltext, and summarize concurrently. "
            "Each paper hands off to the next stage as soon as the prior stage finishes, "
            "so total wall-clock approximates max(download_total, extract_total, summary_total) "
            "rather than the sum."
        ),
    )
    parser.add_argument("--date", required=True, help="Target daily file date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N papers.")
    parser.add_argument(
        "--download-workers",
        type=int,
        default=int(os.getenv("PDF_DOWNLOAD_MAX_WORKERS", "8")),
        help="Concurrent PDF download workers (pure network I/O, can be high).",
    )
    parser.add_argument(
        "--extract-workers",
        type=int,
        default=int(os.getenv("PDF_EXTRACT_MAX_WORKERS", "2")),
        help="Concurrent PDF extraction workers (CPU + JVM bound).",
    )
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
        "--refresh-extract",
        action="store_true",
        help="Re-extract fulltext even if cached markdown exists.",
    )
    parser.add_argument(
        "--refresh-ok",
        action="store_true",
        help="Re-summarize papers already marked ok within the current scope.",
    )
    parser.add_argument(
        "--skip-summarize",
        action="store_true",
        help="Run extract only; useful for debugging the extraction stage.",
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
    download_workers = max(1, args.download_workers)
    extract_workers = max(1, args.extract_workers)
    summary_workers = max(1, args.summary_workers)
    arxiv_config = config.get("arxiv", {})
    chunk_trigger = int(arxiv_config.get("summary_chunk_trigger_chars", 18000))
    chunk_size = int(arxiv_config.get("summary_chunk_char_budget", 12000))
    chunk_overlap = int(arxiv_config.get("summary_chunk_overlap_chars", 1200))
    retries = int(llm_settings["retry_times"])
    summarize_enabled = (not args.skip_summarize) and llm_settings["enabled"]
    if (not args.skip_summarize) and llm_settings["enabled"] and not llm_settings["api_key"]:
        raise RuntimeError("Missing OPENAI_API_KEY")

    work_items: list[tuple[int, dict[str, Any]]] = []
    for index, paper in enumerate(payload.get("papers", [])):
        if limit is not None and index >= limit:
            break
        if args.refresh_ok and paper.get("summary_status") == "ok":
            reset_fallback_summary(paper)
        if limit is not None and paper.get("summary_status", "").startswith("fallback"):
            reset_fallback_summary(paper)
        extract_done = extract_should_skip(paper, refresh=args.refresh_extract)
        summary_done = summary_should_skip(paper) if summarize_enabled else True
        if extract_done and summary_done:
            continue
        work_items.append((index, deepcopy(paper)))

    if not work_items:
        write_json(target, payload, pretty=pretty)
        print({"status": "ok", "download_processed": 0, "extract_processed": 0, "summary_processed": 0})
        return

    stats = {
        "download_ok": 0,
        "download_fail": 0,
        "extract_ok": 0,
        "extract_fail": 0,
        "summary_ok": 0,
        "summary_fallback": 0,
    }
    stats_lock = threading.Lock()

    with build_summary_http_client(llm_settings, summary_workers) as client, SnapshotWriter(
        target,
        payload,
        pretty=pretty,
        on_flush=refresh_summary_counts,
    ) as writer, ThreadPoolExecutor(
        max_workers=summary_workers
    ) as summary_pool, ThreadPoolExecutor(
        max_workers=extract_workers
    ) as extract_pool, ThreadPoolExecutor(
        max_workers=download_workers
    ) as download_pool:

        def on_summary_done(index: int, future: Future) -> None:
            paper = payload["papers"][index]
            try:
                sections, telemetry = future.result()
                paper["summary_sections"] = sections
                paper["summary_zh"] = flatten_sections(sections)
                paper["summary_status"] = "ok"
                paper["summary_input_source"] = (
                    "pdf_fulltext" if paper.get("fulltext_markdown") else "abstract_only"
                )
                with stats_lock:
                    stats["summary_ok"] += 1
                print(
                    {
                        "stage": "summary",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "ok",
                        "summary_input_source": paper["summary_input_source"],
                        "fulltext_chars": len(paper.get("fulltext_markdown", "")),
                        **telemetry,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                error_name = exc.__class__.__name__
                error_detail = str(exc).strip() or error_name
                apply_fallback(config, paper, error_name)
                with stats_lock:
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

        def submit_summary(index: int) -> None:
            paper = payload["papers"][index]
            if not (summarize_enabled and not summary_should_skip(paper)):
                return
            paper_for_summary = deepcopy(paper)
            print(
                {
                    "stage": "summary",
                    "paper_index": index + 1,
                    "paper_id": paper["id"],
                    "status": "started",
                    "model": llm_settings["model"],
                }
            )
            summary_future = summary_pool.submit(
                summarize_one_paper,
                paper_for_summary,
                llm_settings,
                retries,
                args.max_tokens,
                chunk_trigger,
                chunk_size,
                chunk_overlap,
                client,
            )
            summary_future.add_done_callback(
                lambda fut, idx=index: on_summary_done(idx, fut)
            )

        def on_extract_done(index: int, future: Future) -> None:
            paper = payload["papers"][index]
            try:
                result = future.result()
                paper["fulltext_markdown"] = result.get("fulltext_markdown", "")
                paper["fulltext_source"] = result.get("source", "")
                paper["fulltext_status"] = result.get("status", "unknown")
                with stats_lock:
                    stats["extract_ok"] += 1
                print(
                    {
                        "stage": "extract",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": paper["fulltext_status"],
                        "fulltext_chars": len(paper.get("fulltext_markdown", "")),
                        "source": paper.get("fulltext_source", ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                paper["fulltext_markdown"] = ""
                paper["fulltext_source"] = "extract_failed"
                paper["fulltext_status"] = f"extract_failed:{exc.__class__.__name__}"
                with stats_lock:
                    stats["extract_fail"] += 1
                print(
                    {
                        "stage": "extract",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": paper["fulltext_status"],
                        "detail": str(exc).strip()[:200],
                    }
                )
            writer.mark_dirty()
            submit_summary(index)

        def submit_extract(index: int) -> None:
            paper = payload["papers"][index]
            paper_for_extract = deepcopy(paper)
            print(
                {
                    "stage": "extract",
                    "paper_index": index + 1,
                    "paper_id": paper["id"],
                    "status": "started",
                }
            )
            extract_future = extract_pool.submit(
                extract_pdf_to_markdown, paper_for_extract, args.refresh_extract
            )
            extract_future.add_done_callback(
                lambda fut, idx=index: on_extract_done(idx, fut)
            )

        def on_download_done(index: int, future: Future) -> None:
            paper = payload["papers"][index]
            try:
                result = future.result()
                status = result.get("status", "unknown")
                if status == "ok":
                    with stats_lock:
                        stats["download_ok"] += 1
                    print(
                        {
                            "stage": "download",
                            "paper_index": index + 1,
                            "paper_id": paper["id"],
                            "status": "ok",
                        }
                    )
                    submit_extract(index)
                    return
                # Download failed → mark fulltext failure, skip extract, still try abstract-only summary.
                paper["fulltext_markdown"] = ""
                paper["fulltext_source"] = "abstract_only"
                paper["fulltext_status"] = status
                with stats_lock:
                    stats["download_fail"] += 1
                print(
                    {
                        "stage": "download",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": status,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                paper["fulltext_markdown"] = ""
                paper["fulltext_source"] = "abstract_only"
                paper["fulltext_status"] = f"download_failed:{exc.__class__.__name__}"
                with stats_lock:
                    stats["download_fail"] += 1
                print(
                    {
                        "stage": "download",
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": paper["fulltext_status"],
                        "detail": str(exc).strip()[:200],
                    }
                )
            writer.mark_dirty()
            submit_summary(index)

        for index, paper_copy in work_items:
            print(
                {
                    "stage": "download",
                    "paper_index": index + 1,
                    "paper_id": paper_copy["id"],
                    "status": "started",
                }
            )
            download_future = download_pool.submit(
                download_pdf_for_paper, paper_copy, args.refresh_extract
            )
            download_future.add_done_callback(
                lambda fut, idx=index: on_download_done(idx, fut)
            )

        # Pool teardown order matters:
        #   1. download_pool: all downloads finished (and have submitted extracts)
        #   2. extract_pool:  all extractions finished (and have submitted summaries)
        #   3. summary_pool:  all summaries finished
        #   4. SnapshotWriter close: final flush.

    if (not args.skip_summarize) and not llm_settings["enabled"]:
        for paper in payload["papers"]:
            if summary_should_skip(paper):
                continue
            apply_fallback(config, paper, "llm_disabled")
            with stats_lock:
                stats["summary_fallback"] += 1
        refresh_summary_counts(payload)
        write_json(target, payload, pretty=pretty)

    print({"status": "ok", **stats})


if __name__ == "__main__":
    main()

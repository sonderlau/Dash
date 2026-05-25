from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date
from typing import Any

from common import daily_path, load_config, read_json, write_json
from pdf_fulltext import ensure_fulltext_for_paper
from snapshot_writer import SnapshotWriter
from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract PDF fulltext for papers in a daily snapshot.")
    parser.add_argument("--date", required=True, help="Target daily file date, format YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N papers.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.getenv("PDF_EXTRACT_MAX_WORKERS", "2")),
        help="Maximum number of concurrent PDF extraction workers.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-extract even if fulltext already exists.",
    )
    return parser.parse_args()


def should_skip(paper: dict[str, Any], refresh: bool) -> bool:
    if refresh:
        return False
    return paper.get("fulltext_status") == "ok" and bool(paper.get("fulltext_markdown"))


def main() -> None:
    args = parse_args()
    config = load_config()
    target = daily_path(date.fromisoformat(args.date))
    payload = read_json(target)
    limit = args.limit if args.limit and args.limit > 0 else None
    max_workers = max(1, args.max_workers)
    pretty = config["output"].get("write_pretty_json", True)

    work_items: list[tuple[int, dict[str, Any]]] = []
    skipped = 0

    for index, paper in enumerate(payload.get("papers", [])):
        if limit is not None and index >= limit:
            break
        if should_skip(paper, refresh=args.refresh):
            skipped += 1
            continue
        work_items.append((index, deepcopy(paper)))

    if not work_items:
        write_json(target, payload, pretty=pretty)
        print({"status": "ok", "processed": 0, "skipped": skipped})
        return

    with SnapshotWriter(target, payload, pretty=pretty) as writer, ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:
        future_to_index = {
            executor.submit(ensure_fulltext_for_paper, paper, args.refresh): index
            for index, paper in work_items
        }

        progress = tqdm(total=len(work_items), desc="Extract fulltext", unit="paper")
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            paper = payload["papers"][index]
            try:
                result = future.result()
                paper["fulltext_markdown"] = result.get("fulltext_markdown", "")
                paper["fulltext_source"] = result.get("source", "")
                paper["fulltext_status"] = result.get("status", "unknown")
                print(
                    {
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
                print(
                    {
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": paper["fulltext_status"],
                        "detail": str(exc).strip()[:200],
                    }
                )
            writer.mark_dirty()
            progress.update(1)
        progress.close()

    print({"status": "ok", "processed": len(work_items), "skipped": skipped})


if __name__ == "__main__":
    main()

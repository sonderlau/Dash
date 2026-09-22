from __future__ import annotations

import argparse
import os
from datetime import date

from common import daily_path
from summarize import (
    MAX_SUMMARY_TOKENS,
    run_summaries,
    write_github_output,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize a daily snapshot with streaming MiMo JSON.",
    )
    parser.add_argument("--date", default=None, help="Target daily file date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only submit the first N papers.")
    parser.add_argument(
        "--summary-workers",
        type=int,
        default=int(os.getenv("SUMMARY_MAX_WORKERS", "4")),
        help="How many papers to summarize at once.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_SUMMARY_TOKENS,
        help="max_completion_tokens for each summary.",
    )
    parser.add_argument(
        "--refresh-ok",
        action="store_true",
        help="Re-summarize papers already marked ok within the current scope.",
    )
    parser.add_argument(
        "--skip-summarize",
        action="store_true",
        help="Skip summary generation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.date:
        raise SystemExit("--date is required")
    target = daily_path(date.fromisoformat(args.date))
    if not target.exists():
        raise SystemExit(f"Missing state file: {target}")
    run_summaries(
        target,
        limit=args.limit if args.limit and args.limit > 0 else None,
        refresh_ok=args.refresh_ok,
        skip=args.skip_summarize,
        max_tokens=args.max_tokens,
        workers=args.summary_workers,
    )
    write_github_output(
        {
            "batch_pending": "false",
            "state_changed": "true",
            "publish": "true",
        }
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
from json import load as load_json
from datetime import date, datetime, timezone

from common import (
    daily_path,
    ensure_docs_data_dir,
    ensure_state_data_dir,
    load_config,
    previous_daily_path,
    prune_old_files,
    read_json,
    write_json,
)
from fetch_arxiv import fetch_papers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full daily pipeline.")
    parser.add_argument("--date", default=None, help="Override output date as YYYY-MM-DD.")
    return parser.parse_args()


def build_daily_payload(config: dict, target_date: date, papers: list[dict]) -> dict:
    category_counts: dict[str, int] = {}
    summary_status_counts: dict[str, int] = {}
    for paper in papers:
        category_counts[paper["display_category"]] = category_counts.get(paper["display_category"], 0) + 1
        summary_status_counts[paper["summary_status"]] = summary_status_counts.get(paper["summary_status"], 0) + 1

    paper_dates = sorted({paper["published_date"] for paper in papers}, reverse=True)

    return {
        "date": target_date.isoformat(),
        "snapshot_date": target_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "categories": config["arxiv"]["categories"],
        "paper_count": len(papers),
        "category_counts": category_counts,
        "summary_status_counts": summary_status_counts,
        "paper_dates": paper_dates,
        "papers": papers,
    }


def filter_papers_for_configured_categories(papers: list[dict], configured_categories: list[str]) -> list[dict]:
    allowed = set(configured_categories)
    filtered: list[dict] = []
    for paper in papers:
        if paper.get("display_category") not in allowed:
            continue
        matched = [category for category in paper.get("matched_categories", []) if category in allowed]
        if not matched:
            matched = [paper["display_category"]]
        normalized = paper | {
            "matched_categories": matched,
            "display_category": matched[0],
        }
        filtered.append(normalized)
    return filtered


def filter_papers_for_current_source(papers: list[dict], source_name: str) -> list[dict]:
    return [paper for paper in papers if paper.get("source") == source_name]


def merge_papers(existing_papers: list[dict], fetched_papers: list[dict]) -> tuple[list[dict], int, int]:
    by_id = {paper["id"]: paper for paper in existing_papers}
    added = 0
    updated = 0

    for fetched in fetched_papers:
        existing = by_id.get(fetched["id"])
        if existing is None:
            by_id[fetched["id"]] = fetched
            added += 1
            continue

        merged = existing | fetched
        if existing.get("summary_status") == "ok" and fetched.get("summary_status") == "pending":
            merged["summary_status"] = existing["summary_status"]
            merged["summary_zh"] = existing.get("summary_zh", "")
            merged["summary_sections"] = existing.get("summary_sections", fetched.get("summary_sections", {}))
            merged["summary_input_source"] = existing.get("summary_input_source", "")
        elif existing.get("summary_status", "").startswith("fallback") and fetched.get("summary_status") == "pending":
            merged["summary_status"] = existing["summary_status"]
            merged["summary_zh"] = existing.get("summary_zh", "")
            merged["summary_sections"] = existing.get("summary_sections", fetched.get("summary_sections", {}))
            merged["summary_input_source"] = existing.get("summary_input_source", "")

        if existing.get("fulltext_markdown") and not fetched.get("fulltext_markdown"):
            merged["fulltext_markdown"] = existing.get("fulltext_markdown", "")
            merged["fulltext_source"] = existing.get("fulltext_source", "")
            merged["fulltext_status"] = existing.get("fulltext_status", "ok")

        if merged != existing:
            updated += 1
        by_id[fetched["id"]] = merged

    merged_papers = sorted(
        by_id.values(),
        key=lambda item: (item["updated_date"], item["published_date"], item["id"]),
        reverse=True,
    )
    return merged_papers, added, updated


def drop_previous_day_duplicates(fetched_papers: list[dict], previous_papers: list[dict]) -> tuple[list[dict], int]:
    previous_ids = {paper["id"] for paper in previous_papers}
    filtered = [paper for paper in fetched_papers if paper["id"] not in previous_ids]
    skipped = len(fetched_papers) - len(filtered)
    return filtered, skipped


def main() -> None:
    args = parse_args()
    config = load_config()
    target_date = date.fromisoformat(args.date) if args.date else date.today()
    configured_categories = list(config["arxiv"]["categories"])

    ensure_docs_data_dir()
    ensure_state_data_dir()
    fetched_papers, stats = fetch_papers(config)
    previous_path = previous_daily_path(target_date)
    previous_papers: list[dict] = []
    if previous_path is not None:
        previous_payload = read_json(previous_path)
        previous_papers = previous_payload.get("papers", [])

    fetched_papers, skipped_previous_day = drop_previous_day_duplicates(fetched_papers, previous_papers)
    output_path = daily_path(target_date)
    existing_papers: list[dict] = []
    if output_path.exists():
        with output_path.open("r", encoding="utf-8") as handle:
            existing_payload = load_json(handle)
        existing_papers = existing_payload.get("papers", [])

    existing_papers = filter_papers_for_configured_categories(existing_papers, configured_categories)
    existing_papers = filter_papers_for_current_source(existing_papers, "arxiv_new")
    papers, added, updated = merge_papers(existing_papers, fetched_papers)
    payload = build_daily_payload(config, target_date, papers)
    write_json(output_path, payload, pretty=config["output"].get("write_pretty_json", True))

    removed = prune_old_files(int(config["output"].get("keep_days", 90)), today=target_date)
    print(
        {
            "target_date": target_date.isoformat(),
            "output_path": str(output_path),
            "fetched": stats.fetched,
            "kept": stats.kept,
            "duplicates": stats.duplicates,
            "skipped_previous_day": skipped_previous_day,
            "added": added,
            "updated": updated,
            "paper_dates": payload["paper_dates"][:5],
            "removed": [path.name for path in removed],
        }
    )


if __name__ == "__main__":
    main()

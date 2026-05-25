from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone

from common import ensure_docs_data_dir, iter_daily_files, load_config, public_daily_path, read_json, write_json


HEAVY_PAPER_FIELDS = {
    "fulltext_markdown",
    "fulltext_source",
    "fulltext_status",
}


def strip_heavy_fields_from_payload(payload: dict) -> dict:
    cloned = deepcopy(payload)
    cloned["papers"] = [
        {key: value for key, value in paper.items() if key not in HEAVY_PAPER_FIELDS}
        for paper in payload.get("papers", [])
    ]
    return cloned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build docs/data/index.json from daily files.")
    parser.add_argument("--latest-date", default=None, help="Optional explicit latest date.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    ensure_docs_data_dir()

    daily_files = iter_daily_files()
    available_dates = [path.stem for path in daily_files]
    categories = list(config["arxiv"]["categories"])

    counts_by_date: dict[str, int] = {}
    for path in daily_files:
        try:
            payload = read_json(path)
            counts_by_date[path.stem] = len(payload.get("papers", []))
            sanitized = strip_heavy_fields_from_payload(payload)
            sanitized["categories"] = categories
            write_json(
                public_daily_path(date.fromisoformat(path.stem)),
                sanitized,
                pretty=config["output"].get("write_pretty_json", True),
            )
        except Exception:  # noqa: BLE001
            counts_by_date[path.stem] = 0

    latest_date = args.latest_date or (available_dates[0] if available_dates else date.today().isoformat())

    index_payload = {
        "site": {
            "title": config["site"]["title"],
            "subtitle": config["site"]["subtitle"],
            "description": config["site"]["description"],
            "base_url": config["site"]["base_url"],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "available_dates": available_dates,
        "categories": categories,
        "counts_by_date": counts_by_date,
        "latest_date": latest_date,
        "mode": "daily_snapshot",
    }
    write_json(
        ensure_docs_data_dir() / "index.json",
        index_payload,
        pretty=config["output"].get("write_pretty_json", True),
    )
    print({"latest_date": latest_date, "days": len(available_dates)})


if __name__ == "__main__":
    main()

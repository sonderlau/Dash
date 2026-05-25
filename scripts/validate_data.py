from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated JSON payloads.")
    parser.add_argument("paths", nargs="+", help="JSON file paths to validate.")
    return parser.parse_args()


def validate_payload(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Empty file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if path.name == "index.json":
        if not payload.get("available_dates"):
            raise ValueError("index.json has no available_dates")
        return

    papers = payload.get("papers", [])
    if payload.get("paper_count", len(papers)) <= 0:
        raise ValueError(f"{path.name} has no papers")
    if not isinstance(papers, list):
        raise TypeError(f"{path.name} papers is not a list")


def main() -> None:
    args = parse_args()
    for raw_path in args.paths:
        validate_payload(Path(raw_path))
    print({"validated": args.paths})


if __name__ == "__main__":
    main()

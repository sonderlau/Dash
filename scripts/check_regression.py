"""Refuse to publish a build that shrinks any still-retained day's paper_count.

Compares the freshly-built docs/data/*.json (excluding index.json) against the
copy on the `data` branch (passed as previous_dir). Exits non-zero if any day
inside the keep_days window has fewer papers than before, or is missing.
Dates older than the window are allowed to expire — that is keep_days working,
not a regression.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guard against shrinking published daily snapshots.")
    parser.add_argument("previous_dir", help="docs/data from the data branch.")
    parser.add_argument("new_dir", help="Freshly built docs/data.")
    parser.add_argument("--as-of", default=None, help="Run date YYYY-MM-DD (default: today UTC).")
    parser.add_argument("--keep-days", type=int, default=90, help="Retention window; must match output.keep_days.")
    return parser.parse_args(argv)


def collect_counts(directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not directory.exists():
        return counts
    for path in sorted(directory.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  ! cannot parse {path.name}: {exc}", file=sys.stderr)
            continue
        counts[path.name] = int(payload.get("paper_count", 0))
    return counts


def snapshot_date(name: str) -> date | None:
    try:
        return datetime.strptime(Path(name).stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    args = parse_args(argv[1:])
    previous = Path(args.previous_dir)
    new = Path(args.new_dir)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    cutoff = as_of - timedelta(days=args.keep_days - 1)

    if not previous.exists():
        print("No previous data directory; skipping regression guard.")
        return 0

    prev_counts = collect_counts(previous)
    new_counts = collect_counts(new)

    regressions: list[str] = []
    expired: list[str] = []
    for name, old_count in prev_counts.items():
        day = snapshot_date(name)
        if day is not None and day < cutoff:
            if name not in new_counts:
                expired.append(f"{name}: expired before {cutoff.isoformat()} (was {old_count})")
            continue
        if name not in new_counts:
            regressions.append(f"{name}: missing in new build (was {old_count})")
            continue
        new_count = new_counts[name]
        if new_count < old_count:
            regressions.append(f"{name}: {old_count} -> {new_count}")

    if expired:
        print("Expired dates dropped by keep_days:")
        for entry in expired:
            print(f"  - {entry}")

    if regressions:
        print("REGRESSIONS DETECTED:")
        for entry in regressions:
            print(f"  - {entry}")
        return 1

    print(
        f"Regression guard: OK ({len(prev_counts)} historical days checked, "
        f"{len(expired)} expired, window {cutoff.isoformat()}..{as_of.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

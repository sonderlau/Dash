"""Refuse to publish a build that shrinks any historical day's paper_count.

Compares the freshly-built docs/data/*.json (excluding index.json) against the
copy on the `data` branch (passed as ref_dir). Exits non-zero if any day has
fewer papers in the new build than the previous version, or if a previously
published day is missing entirely. Schema/build bugs that silently zero out
history get caught here before Stage 5 commits the bad data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_regression.py <previous_dir> <new_dir>", file=sys.stderr)
        return 2

    previous = Path(argv[1])
    new = Path(argv[2])

    if not previous.exists():
        print("No previous data directory; skipping regression guard.")
        return 0

    prev_counts = collect_counts(previous)
    new_counts = collect_counts(new)

    regressions: list[str] = []
    for name, old_count in prev_counts.items():
        if name not in new_counts:
            regressions.append(f"{name}: missing in new build (was {old_count})")
            continue
        new_count = new_counts[name]
        if new_count < old_count:
            regressions.append(f"{name}: {old_count} -> {new_count}")

    if regressions:
        print("REGRESSIONS DETECTED:")
        for entry in regressions:
            print(f"  - {entry}")
        return 1

    print(f"Regression guard: OK ({len(prev_counts)} historical days verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

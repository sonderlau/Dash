from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"


@dataclass
class StageResult:
    name: str
    status: str
    detail: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local Dash pipeline through explicit stages.")
    parser.add_argument("--date", default=None, help="Target run date as YYYY-MM-DD.")
    parser.add_argument(
        "--from-stage",
        default="fetch_dedup",
        choices=["fetch_dedup", "extract_fulltext", "summarize", "build_site", "validate"],
        help="Start running from this stage.",
    )
    parser.add_argument(
        "--to-stage",
        default="validate",
        choices=["fetch_dedup", "extract_fulltext", "summarize", "build_site", "validate"],
        help="Stop after this stage.",
    )
    parser.add_argument("--skip-summarize", action="store_true", help="Skip the summarize stage.")
    parser.add_argument("--summarize-limit", type=int, default=None, help="Only summarize the first N papers.")
    parser.add_argument("--refresh-ok", action="store_true", help="Re-summarize papers already marked ok.")
    parser.add_argument("--extract-limit", type=int, default=None, help="Only extract fulltext for the first N papers.")
    parser.add_argument("--extract-refresh", action="store_true", help="Re-extract cached fulltext.")
    parser.add_argument("--extract-max-workers", type=int, default=None, help="Override extract worker count.")
    parser.add_argument("--summary-max-workers", type=int, default=None, help="Override summary worker count.")
    return parser.parse_args()


def run_stage(name: str, cmd: Sequence[str], cwd: Path = ROOT) -> StageResult:
    print({"stage": name, "status": "started", "cmd": list(cmd)})
    completed = subprocess.run(cmd, cwd=cwd, text=True)
    if completed.returncode == 0:
        print({"stage": name, "status": "ok"})
        return StageResult(name=name, status="ok")
    print({"stage": name, "status": "failed", "returncode": completed.returncode})
    raise SystemExit(completed.returncode)


def state_file_for(run_date: date) -> Path:
    return ROOT / "tmp" / "state" / f"{run_date.isoformat()}.json"


def load_paper_count(state_path: Path) -> int:
    if not state_path.exists():
        return 0
    import json

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return int(payload.get("paper_count", 0))


STAGE_ORDER = ["fetch_dedup", "extract_fulltext", "summarize", "build_site", "validate"]


def stage_enabled(stage_name: str, from_stage: str, to_stage: str) -> bool:
    start_index = STAGE_ORDER.index(from_stage)
    end_index = STAGE_ORDER.index(to_stage)
    current_index = STAGE_ORDER.index(stage_name)
    return start_index <= current_index <= end_index


def main() -> None:
    args = parse_args()
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    if STAGE_ORDER.index(args.from_stage) > STAGE_ORDER.index(args.to_stage):
        raise SystemExit("--from-stage must not be after --to-stage")

    fetch_cmd = [str(PYTHON), "scripts/run_daily.py", "--date", run_date.isoformat()]
    extract_cmd = [str(PYTHON), "scripts/extract_fulltext.py", "--date", run_date.isoformat()]
    build_cmd = [str(PYTHON), "scripts/build_site_data.py", "--latest-date", run_date.isoformat()]
    validate_cmd = [
        str(PYTHON),
        "scripts/validate_data.py",
        f"tmp/state/{run_date.isoformat()}.json",
        "docs/data/index.json",
        f"docs/data/{run_date.isoformat()}.json",
    ]
    if args.extract_limit and args.extract_limit > 0:
        extract_cmd.extend(["--limit", str(args.extract_limit)])
    if args.extract_refresh:
        extract_cmd.append("--refresh")
    if args.extract_max_workers and args.extract_max_workers > 0:
        extract_cmd.extend(["--max-workers", str(args.extract_max_workers)])

    if stage_enabled("fetch_dedup", args.from_stage, args.to_stage):
        run_stage("fetch_dedup", fetch_cmd)

    state_path = state_file_for(run_date)
    paper_count = load_paper_count(state_path)
    print({"stage": "fetch_dedup", "paper_count": paper_count})

    if paper_count <= 0 and stage_enabled("fetch_dedup", args.from_stage, args.to_stage):
        run_stage("build_site", build_cmd)
        run_stage("validate", validate_cmd)
        print({"status": "ok", "message": "No new papers after dedup; summarize skipped."})
        return

    if stage_enabled("extract_fulltext", args.from_stage, args.to_stage):
        run_stage("extract_fulltext", extract_cmd)

    if not args.skip_summarize and stage_enabled("summarize", args.from_stage, args.to_stage):
        summarize_cmd = [str(PYTHON), "scripts/summarize.py", "--date", run_date.isoformat()]
        if args.summarize_limit and args.summarize_limit > 0:
            summarize_cmd.extend(["--limit", str(args.summarize_limit)])
        if args.refresh_ok:
            summarize_cmd.append("--refresh-ok")
        if args.summary_max_workers and args.summary_max_workers > 0:
            summarize_cmd.extend(["--max-workers", str(args.summary_max_workers)])
        run_stage("summarize", summarize_cmd)

    if stage_enabled("build_site", args.from_stage, args.to_stage):
        run_stage("build_site", build_cmd)
    if stage_enabled("validate", args.from_stage, args.to_stage):
        run_stage("validate", validate_cmd)
    print({"status": "ok", "date": run_date.isoformat()})


if __name__ == "__main__":
    main()

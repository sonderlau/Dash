from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean local intermediate pipeline artifacts.")
    parser.add_argument("--state", action="store_true", help="Remove tmp/state JSON files.")
    parser.add_argument("--pdf-cache", action="store_true", help="Remove tmp/paper_cache.")
    parser.add_argument("--pdf-extract", action="store_true", help="Remove tmp/pdf_extract.")
    parser.add_argument("--docs-data", action="store_true", help="Remove docs/data JSON files.")
    parser.add_argument("--all", action="store_true", help="Remove all intermediate/generated artifacts.")
    return parser.parse_args()


def remove_glob(pattern: str) -> list[str]:
    removed: list[str] = []
    for path in ROOT.glob(pattern):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(ROOT)))
    return removed


def remove_tree(relative_path: str) -> list[str]:
    target = ROOT / relative_path
    if not target.exists():
        return []
    shutil.rmtree(target)
    return [str(target.relative_to(ROOT))]


def main() -> None:
    args = parse_args()
    if not any([args.state, args.pdf_cache, args.pdf_extract, args.docs_data, args.all]):
        raise SystemExit("Specify at least one cleanup target, or use --all")

    removed: list[str] = []
    clean_state = args.all or args.state
    clean_pdf_cache = args.all or args.pdf_cache
    clean_pdf_extract = args.all or args.pdf_extract
    clean_docs_data = args.all or args.docs_data

    if clean_state:
        removed.extend(remove_glob("tmp/state/*.json"))
    if clean_pdf_cache:
        removed.extend(remove_tree("tmp/paper_cache"))
    if clean_pdf_extract:
        removed.extend(remove_tree("tmp/pdf_extract"))
    if clean_docs_data:
        removed.extend(remove_glob("docs/data/*.json"))

    print({"removed": removed})


if __name__ == "__main__":
    main()

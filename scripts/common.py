from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DOCS_DATA_DIR = ROOT / "docs" / "data"
STATE_DATA_DIR = ROOT / "tmp" / "state"


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    config: Path = CONFIG_PATH
    docs_data: Path = DOCS_DATA_DIR
    state_data: Path = STATE_DATA_DIR


def load_config() -> dict[str, Any]:
    load_local_env()
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return apply_env_overrides(config)


def load_local_env(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env.local"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:]
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        clean_key = key.strip()
        clean_value = value.strip()
        if len(clean_value) >= 2 and clean_value[0] == clean_value[-1] and clean_value[0] in {"'", '"'}:
            clean_value = clean_value[1:-1]
        os.environ.setdefault(clean_key, clean_value)


def parse_env_list(raw: str) -> list[str]:
    return [item for item in re.split(r"[\s,，;；|]+", raw.strip()) if item]


def apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    raw_categories = os.getenv("CATEGORIES", "").strip()
    if raw_categories:
        categories = parse_env_list(raw_categories)
        if categories:
            config.setdefault("arxiv", {})["categories"] = categories
    return config


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_deepseek_settings() -> dict[str, Any]:
    load_local_env()
    return {
        "enabled": env_flag("LLM_ENABLED", default=False),
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
        "model": os.getenv("MODEL_NAME", "deepseek-v4-flash"),
        "language": os.getenv("LANGUAGE", "zh-CN"),
        "timeout_seconds": int(os.getenv("LLM_TIMEOUT_SECONDS", "600")),
        "retry_times": int(os.getenv("LLM_RETRY_TIMES", "3")),
    }


def ensure_docs_data_dir() -> Path:
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DOCS_DATA_DIR


def ensure_state_data_dir() -> Path:
    STATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DATA_DIR


def resolve_root_path(raw_path: str | Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return ROOT / path


def ensure_dir(path: str | Path) -> Path:
    target = resolve_root_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_json(path: Path, payload: Any, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if pretty:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def daily_path(target_date: date) -> Path:
    return STATE_DATA_DIR / f"{target_date.isoformat()}.json"


def public_daily_path(target_date: date) -> Path:
    return DOCS_DATA_DIR / f"{target_date.isoformat()}.json"


def iter_daily_files() -> list[Path]:
    if not STATE_DATA_DIR.exists():
        return []
    return sorted(
        (path for path in STATE_DATA_DIR.glob("*.json") if path.name != "index.json"),
        reverse=True,
    )


def previous_daily_path(target_date: date) -> Path | None:
    candidates: list[tuple[date, Path]] = []
    for path in iter_daily_files():
        try:
            path_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if path_date < target_date:
            candidates.append((path_date, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def prune_old_files(keep_days: int, today: date | None = None) -> list[Path]:
    current = today or date.today()
    cutoff = current - timedelta(days=keep_days - 1)
    removed: list[Path] = []
    for path in iter_daily_files():
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed

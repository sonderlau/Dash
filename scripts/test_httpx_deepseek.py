from __future__ import annotations

import json
from pathlib import Path

import httpx


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:]
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        clean_value = value.strip()
        if len(clean_value) >= 2 and clean_value[0] == clean_value[-1] and clean_value[0] in {"'", '"'}:
            clean_value = clean_value[1:-1]
        values[key.strip()] = clean_value
    return values


def main() -> None:
    env = load_env_file(Path(".env.local"))
    base_url = env.get("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    api_key = env.get("OPENAI_API_KEY", "")
    model_name = env.get("MODEL_NAME", "deepseek-v4-flash")

    payload = {
        "model": model_name,
        "temperature": 0.1,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Reply with a compact JSON object only."},
            {"role": "user", "content": 'Return {"ok":true,"provider":"deepseek"}'},
        ],
    }

    for trust_env in (True, False):
        try:
            with httpx.Client(timeout=30, trust_env=trust_env) as client:
                response = client.post(
                    base_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            result = {
                "trust_env": trust_env,
                "status_code": response.status_code,
                "body_preview": response.text[:300],
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "trust_env": trust_env,
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            }
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

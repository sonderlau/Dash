from __future__ import annotations

import json
from pathlib import Path

import httpx

from common import load_config, load_deepseek_settings, read_json
from summarize import build_request_payload, daily_path


def main() -> None:
    config = load_config()
    llm_settings = load_deepseek_settings()
    payload = read_json(daily_path(__import__("datetime").date.fromisoformat("2026-05-16")))
    paper = payload["papers"][0]
    request_payload = build_request_payload(llm_settings, paper, max_tokens=420)

    with httpx.Client(timeout=180, trust_env=False) as client:
        response = client.post(
            f"{llm_settings['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm_settings['api_key']}",
                "Content-Type": "application/json",
            },
            json=request_payload,
        )
        response.raise_for_status()
        data = response.json()

    print(json.dumps(data, ensure_ascii=False, indent=2)[:12000])


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from tqdm import tqdm

try:
    from common import daily_path, load_config, load_deepseek_settings, load_keywords, read_json, write_json
    from snapshot_writer import SnapshotWriter
except ModuleNotFoundError:  # pragma: no cover - local package-style invocation
    from scripts.common import daily_path, load_config, load_deepseek_settings, load_keywords, read_json, write_json
    from scripts.snapshot_writer import SnapshotWriter


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "prompts"
DEFAULT_MAX_TOKENS = 1300
MAX_SUMMARY_TOKENS = 1800
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


class SummaryError(RuntimeError):
    """Base class for summary failures."""


class RetryableSummaryError(SummaryError):
    """Error that should trigger a retry."""


class JsonOutputError(RetryableSummaryError):
    """Model returned malformed or incomplete JSON."""


class LengthLimitError(RetryableSummaryError):
    """Model stopped because of output length limit."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize papers into Chinese with DeepSeek.")
    parser.add_argument("--date", required=True, help="Target daily file date, format YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only summarize the first N papers for local development.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Initial max_tokens for one summary request.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.getenv("SUMMARY_MAX_WORKERS", "4")),
        help="Maximum number of concurrent summary workers.",
    )
    parser.add_argument(
        "--refresh-ok",
        action="store_true",
        help="Re-summarize papers already marked ok within the current processing scope.",
    )
    return parser.parse_args()


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def compact_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in str(value).splitlines()).strip()


def format_metadata_list(values: list[str] | tuple[str, ...]) -> str:
    cleaned = [compact_text(value) for value in values if compact_text(value)]
    return ", ".join(cleaned) if cleaned else ""


def prepare_metadata_context(paper: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Title: {compact_text(paper.get('title', ''))}",
            f"Authors: {format_metadata_list(paper.get('authors', []))}",
            f"Matched categories: {format_metadata_list(paper.get('matched_categories', []))}",
            f"All categories: {format_metadata_list(paper.get('categories', []))}",
            f"Primary category: {compact_text(paper.get('primary_category', ''))}",
            f"Published date: {compact_text(paper.get('published_date', ''))}",
            f"Updated date: {compact_text(paper.get('updated_date', ''))}",
            f"arXiv comment: {compact_text(paper.get('comment', ''))}",
            f"Journal reference: {compact_text(paper.get('journal_ref', ''))}",
            f"DOI: {compact_text(paper.get('doi', ''))}",
            f"arXiv abstract URL: {compact_text(paper.get('abs_url', ''))}",
            "",
            "Abstract:",
            compact_text(paper.get("abstract_en", "")),
        ]
    ).strip()


def render_system(template_name: str, language: str) -> str:
    """Render a system prompt with language baked in.

    System prompts are designed to be the cacheable prefix: as long as
    `language` is constant across calls (which it is in practice), the
    rendered string is byte-for-byte identical between requests, so DeepSeek's
    prompt cache can hit on the entire system message.

    We use plain string replacement instead of ``str.format`` because the
    prompts contain literal JSON examples with ``{`` and ``}`` characters.
    Doubling those for ``format`` would obscure the example; the trade-off is
    that the only supported placeholder is ``{language}``.
    """
    return load_prompt(template_name).replace("{language}", language)


def build_messages(
    system_prompt: str,
    user_prompt: str,
    paper: dict[str, Any],
    keywords: list[str],
) -> list[dict[str, str]]:
    user_content = user_prompt.format(
        title=paper["title"],
        authors=format_metadata_list(paper.get("authors", [])),
        categories=", ".join(paper["matched_categories"]),
        all_categories=format_metadata_list(paper.get("categories", [])),
        primary_category=paper.get("primary_category", ""),
        published_date=paper.get("published_date", ""),
        updated_date=paper.get("updated_date", ""),
        comment=paper.get("comment", ""),
        journal_ref=paper.get("journal_ref", ""),
        doi=paper.get("doi", ""),
        abs_url=paper.get("abs_url", ""),
        abstract_en=paper["abstract_en"],
        metadata_context=prepare_metadata_context(paper),
        keywords="\n".join(f"- {keyword}" for keyword in keywords),
        relevance_instruction=(
            "请根据上面的关键词给出 0-100 的 relevance_score，分数表示这篇论文是否值得我优先阅读。"
            if keywords
            else "关键词列表为空；不要计算 relevance_score，请把 relevance_score 设为英文空字符串 \"\"。"
        ),
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def build_request_payload(
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    max_tokens: int,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    system_prompt = render_system("summary_system.txt", llm_settings["language"])
    user_prompt = load_prompt("summary_user.txt")
    return {
        "model": llm_settings["model"],
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": build_messages(system_prompt, user_prompt, paper, keywords or []),
    }


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])
    raise JsonOutputError("no_json_object_found")


def normalize_sections(parsed: dict[str, Any]) -> dict[str, str]:
    sections = {
        "tldr": str(parsed.get("tldr", "")).strip(),
        "motivation": str(parsed.get("motivation", "")).strip(),
        "method": str(parsed.get("method", "")).strip(),
        "result": str(parsed.get("result", "")).strip(),
        "conclusion": str(parsed.get("conclusion", "")).strip(),
        "relevance_score": str(parsed.get("relevance_score", "")).strip(),
    }
    if not any(value for key, value in sections.items() if key != "relevance_score"):
        raise JsonOutputError("json_fields_empty")
    return sections


def parse_summary_response(payload: dict[str, Any]) -> dict[str, str]:
    choices = payload.get("choices") or []
    if not choices:
        raise RetryableSummaryError("missing_choices")

    choice = choices[0]
    finish_reason = choice.get("finish_reason", "")
    if finish_reason == "length":
        raise LengthLimitError("finish_reason_length")

    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    reasoning_content = (message.get("reasoning_content") or "").strip()
    parsed = extract_json_object(content or reasoning_content)
    return normalize_sections(parsed)


def is_retryable_http_error(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code in RETRYABLE_STATUS_CODES


def compute_backoff_seconds(
    attempt: int,
    exc: Exception | None = None,
    response: httpx.Response | None = None,
) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return max(1.0, min(float(retry_after), 60.0))
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return max(1.0, min(float(retry_after), 60.0))
    return min(5.0 * (attempt + 1), 30.0)


def request_summary(
    client: httpx.Client,
    llm_settings: dict[str, Any],
    paper: dict[str, Any] | None,
    max_tokens: int,
    keywords: list[str] | None = None,
    payload_override: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload = payload_override or build_request_payload(llm_settings, paper or {}, max_tokens, keywords or [])
    response = client.post(
        f"{llm_settings['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_settings['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    data = response.json()
    sections = parse_summary_response(data)
    usage = data.get("usage") or {}
    telemetry = {
        "finish_reason": ((data.get("choices") or [{}])[0]).get("finish_reason", ""),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        "max_tokens": max_tokens,
    }
    return sections, telemetry


def summarize_text(
    client: httpx.Client,
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    retries: int,
    initial_max_tokens: int,
    keywords: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    max_tokens = initial_max_tokens
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            sections, telemetry = request_summary(client, llm_settings, paper, max_tokens, keywords or [])
            telemetry["attempt"] = attempt + 1
            telemetry["summary_mode"] = "metadata"
            return sections, telemetry
        except LengthLimitError as exc:
            last_error = exc
            max_tokens = min(max_tokens + 320, MAX_SUMMARY_TOKENS)
            if attempt >= retries:
                break
            print(
                {
                    "paper_id": paper["id"],
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "next_max_tokens": max_tokens,
                }
            )
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except JsonOutputError as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                {
                    "paper_id": paper["id"],
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "next_max_tokens": max_tokens,
                }
            )
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if not is_retryable_http_error(exc) or attempt >= retries:
                break
            print(
                {
                    "paper_id": paper["id"],
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "status_code": exc.response.status_code,
                    "next_max_tokens": max_tokens,
                }
            )
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                {
                    "paper_id": paper["id"],
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "next_max_tokens": max_tokens,
                }
            )
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                {
                    "paper_id": paper["id"],
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "next_max_tokens": max_tokens,
                }
            )
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            break

    if last_error is None:
        raise SummaryError("unknown_summary_error")
    raise last_error


def apply_fallback(config: dict[str, Any], paper: dict[str, Any], error_message: str) -> None:
    if config["output"].get("fallback_to_english_abstract", True):
        paper["summary_zh"] = f"摘要生成失败，保留英文摘要：{paper['abstract_en']}"
    else:
        paper["summary_zh"] = ""
    paper["summary_sections"] = {
        "tldr": paper["summary_zh"],
        "motivation": "",
        "method": "",
        "result": "",
        "conclusion": "",
        "relevance_score": "",
    }
    paper["summary_status"] = f"fallback:{error_message}"
    paper["summary_input_source"] = "metadata"


def reset_fallback_summary(paper: dict[str, Any]) -> None:
    paper["summary_status"] = "pending"
    paper["summary_zh"] = ""
    paper["summary_input_source"] = ""
    paper["summary_sections"] = {
        "tldr": "",
        "motivation": "",
        "method": "",
        "result": "",
        "conclusion": "",
        "relevance_score": "",
    }


def should_skip(
    paper: dict[str, Any],
    keywords: list[str] | None = None,
) -> bool:
    if paper.get("summary_status") != "ok" or not paper.get("summary_zh"):
        return False
    if keywords and not paper.get("summary_sections", {}).get("relevance_score"):
        return False
    return True


def flatten_sections(sections: dict[str, str]) -> str:
    ordered = [
        ("TL;DR", sections.get("tldr", "").strip()),
        ("Motivation", sections.get("motivation", "").strip()),
        ("Method", sections.get("method", "").strip()),
        ("Result", sections.get("result", "").strip()),
        ("Conclusion", sections.get("conclusion", "").strip()),
        ("Relevance", sections.get("relevance_score", "").strip()),
    ]
    return "\n".join(f"{label}: {value}" for label, value in ordered if value)


def refresh_summary_counts(payload: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for paper in payload.get("papers", []):
        status = paper.get("summary_status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    payload["summary_status_counts"] = counts


def persist_progress(target: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    refresh_summary_counts(payload)
    write_json(target, payload, pretty=config["output"].get("write_pretty_json", True))


def summarize_one_paper(
    paper: dict[str, Any],
    llm_settings: dict[str, Any],
    retries: int,
    initial_max_tokens: int,
    client: httpx.Client,
    keywords: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    return summarize_text(
        client=client,
        llm_settings=llm_settings,
        paper=paper,
        retries=retries,
        initial_max_tokens=initial_max_tokens,
        keywords=keywords or [],
    )


def build_summary_http_client(llm_settings: dict[str, Any], max_workers: int) -> httpx.Client:
    timeout = httpx.Timeout(llm_settings["timeout_seconds"])
    pool = max(max_workers, 1) * 2
    limits = httpx.Limits(
        max_connections=pool,
        max_keepalive_connections=pool,
        keepalive_expiry=60.0,
    )
    return httpx.Client(timeout=timeout, trust_env=False, limits=limits)


def main() -> None:
    args = parse_args()
    config = load_config()
    llm_settings = load_deepseek_settings()
    target = daily_path(date.fromisoformat(args.date))
    payload = read_json(target)
    limit = args.limit if args.limit and args.limit > 0 else None
    keywords = load_keywords()

    if not llm_settings["enabled"]:
        for index, paper in enumerate(payload["papers"]):
            if limit is not None and index >= limit:
                break
            if should_skip(paper):
                continue
            apply_fallback(config, paper, "llm_disabled")
        persist_progress(target, payload, config)
        print({"status": "skipped", "reason": "llm_disabled", "papers": len(payload["papers"])})
        return

    if not llm_settings["api_key"]:
        raise RuntimeError("Missing OPENAI_API_KEY")

    retries = int(llm_settings["retry_times"])
    success_count = 0
    fallback_count = 0

    max_workers = max(1, args.max_workers)
    work_items: list[tuple[int, dict[str, Any]]] = []

    for index, paper in enumerate(payload["papers"]):
        if limit is not None and index >= limit:
            break
        if args.refresh_ok and paper.get("summary_status") == "ok":
            reset_fallback_summary(paper)
        if limit is not None and paper.get("summary_status", "").startswith("fallback"):
            reset_fallback_summary(paper)
        if should_skip(paper, keywords):
            success_count += 1
            print(
                {
                    "paper_index": index + 1,
                    "paper_id": paper["id"],
                    "status": "skipped_existing_ok",
                }
            )
            continue

        print(
            {
                "paper_index": index + 1,
                "paper_id": paper["id"],
                "status": "started",
                "model": llm_settings["model"],
            }
        )
        work_items.append((index, deepcopy(paper)))

    pretty = config["output"].get("write_pretty_json", True)
    with build_summary_http_client(llm_settings, max_workers) as client, ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor, SnapshotWriter(
        target,
        payload,
        pretty=pretty,
        on_flush=refresh_summary_counts,
    ) as writer:
        future_to_index = {
            executor.submit(
                summarize_one_paper,
                paper,
                llm_settings,
                retries,
                args.max_tokens,
                client,
                keywords,
            ): index
            for index, paper in work_items
        }

        progress = tqdm(total=len(work_items), desc="Summarize papers", unit="paper")
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            paper = payload["papers"][index]
            try:
                sections, telemetry = future.result()
                paper["summary_sections"] = sections
                paper["summary_zh"] = flatten_sections(sections)
                paper["summary_status"] = "ok"
                paper["summary_input_source"] = "metadata"
                success_count += 1
                print(
                    {
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "ok",
                        "summary_input_source": paper["summary_input_source"],
                        "relevance_enabled": bool(keywords),
                        **telemetry,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                error_name = exc.__class__.__name__
                error_detail = str(exc).strip() or error_name
                apply_fallback(config, paper, error_name)
                fallback_count += 1
                print(
                    {
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "fallback",
                        "error": error_name,
                        "detail": error_detail[:200],
                    }
                )
            writer.mark_dirty()
            progress.update(1)
        progress.close()

    print({"status": "ok", "success": success_count, "fallback": fallback_count})


if __name__ == "__main__":
    main()

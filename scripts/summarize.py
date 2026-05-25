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
    from common import daily_path, load_config, load_deepseek_settings, read_json, write_json
    from snapshot_writer import SnapshotWriter
except ModuleNotFoundError:  # pragma: no cover - local package-style invocation
    from scripts.common import daily_path, load_config, load_deepseek_settings, read_json, write_json
    from scripts.snapshot_writer import SnapshotWriter


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "prompts"
DEFAULT_MAX_TOKENS = 1300
DEFAULT_REDUCE_MAX_TOKENS = 1600
MAX_SUMMARY_TOKENS = 1800
FULLTEXT_CHAR_BUDGET = 0
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


def truncate_fulltext(fulltext: str, budget: int = FULLTEXT_CHAR_BUDGET) -> str:
    normalized = compact_text(fulltext)
    if budget <= 0 or len(normalized) <= budget:
        return normalized

    lines = normalized.splitlines()
    kept: list[str] = []
    total = 0
    for line in lines:
        extra = len(line) + 1
        if total + extra > budget:
            break
        kept.append(line)
        total += extra

    if kept:
        return "\n".join(kept).strip()
    return normalized[:budget]


def prepare_summary_context(paper: dict[str, Any]) -> str:
    fulltext = compact_text(paper.get("fulltext_markdown", ""))
    if fulltext:
        return truncate_fulltext(fulltext)
    return "\n".join(
        [
            f"# {paper['title']}",
            "",
            "## Abstract",
            paper.get("abstract_en", "").strip(),
        ]
    ).strip()


def split_text_into_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    normalized = compact_text(text)
    if not normalized or chunk_size <= 0:
        return [normalized] if normalized else []

    chunks: list[str] = []
    start = 0
    text_length = len(normalized)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        if end < text_length:
            newline_boundary = normalized.rfind("\n", start, end)
            if newline_boundary > start + (chunk_size // 2):
                end = newline_boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_length:
            break
        start = max(end - overlap, end)
    return chunks


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
) -> list[dict[str, str]]:
    user_content = user_prompt.format(
        title=paper["title"],
        categories=", ".join(paper["matched_categories"]),
        abstract_en=paper["abstract_en"],
        fulltext_context=prepare_summary_context(paper),
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def build_prompt_content(template_name: str, **kwargs: Any) -> str:
    template = load_prompt(template_name)
    return template.format(**kwargs)


def build_request_payload(
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    system_prompt = render_system("summary_system.txt", llm_settings["language"])
    user_prompt = load_prompt("summary_user.txt")
    return {
        "model": llm_settings["model"],
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": build_messages(system_prompt, user_prompt, paper),
    }


def build_custom_payload(
    llm_settings: dict[str, Any],
    system_prompt: str,
    user_content: str,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": llm_settings["model"],
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
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
    }
    if not any(sections.values()):
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
    payload_override: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload = payload_override or build_request_payload(llm_settings, paper or {}, max_tokens)
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
) -> tuple[dict[str, str], dict[str, Any]]:
    max_tokens = initial_max_tokens
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            sections, telemetry = request_summary(client, llm_settings, paper, max_tokens)
            telemetry["attempt"] = attempt + 1
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


def summarize_custom_payload(
    client: httpx.Client,
    llm_settings: dict[str, Any],
    payload: dict[str, Any],
    retries: int,
    initial_max_tokens: int,
    paper_id: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    max_tokens = initial_max_tokens
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            sections, telemetry = request_summary(
                client=client,
                llm_settings=llm_settings,
                paper=None,
                max_tokens=max_tokens,
                payload_override={**payload, "max_tokens": max_tokens},
            )
            telemetry["attempt"] = attempt + 1
            return sections, telemetry
        except LengthLimitError as exc:
            last_error = exc
            max_tokens = min(max_tokens + 320, MAX_SUMMARY_TOKENS)
            if attempt >= retries:
                break
            print({"paper_id": paper_id, "status": "retrying", "attempt": attempt + 1, "error": exc.__class__.__name__, "next_max_tokens": max_tokens})
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except JsonOutputError as exc:
            last_error = exc
            if attempt >= retries:
                break
            print({"paper_id": paper_id, "status": "retrying", "attempt": attempt + 1, "error": exc.__class__.__name__, "next_max_tokens": max_tokens})
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if not is_retryable_http_error(exc) or attempt >= retries:
                break
            print({"paper_id": paper_id, "status": "retrying", "attempt": attempt + 1, "error": exc.__class__.__name__, "status_code": exc.response.status_code, "next_max_tokens": max_tokens})
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt >= retries:
                break
            print({"paper_id": paper_id, "status": "retrying", "attempt": attempt + 1, "error": exc.__class__.__name__, "next_max_tokens": max_tokens})
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= retries:
                break
            print({"paper_id": paper_id, "status": "retrying", "attempt": attempt + 1, "error": exc.__class__.__name__, "next_max_tokens": max_tokens})
            time.sleep(compute_backoff_seconds(attempt, exc=exc))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            break

    if last_error is None:
        raise SummaryError("unknown_summary_error")
    raise last_error


def summarize_via_chunks(
    client: httpx.Client,
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    retries: int,
    initial_max_tokens: int,
    chunk_size: int,
    overlap: int,
    reduce_max_tokens: int | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    fulltext = prepare_summary_context(paper)
    chunks = split_text_into_chunks(fulltext, chunk_size=chunk_size, overlap=overlap)
    if len(chunks) <= 1:
        return summarize_text(client, llm_settings, paper, retries, initial_max_tokens)

    chunk_system_prompt = render_system("summary_chunk_system.txt", llm_settings["language"])
    reduce_system_prompt = render_system("summary_reduce_system.txt", llm_settings["language"])
    chunk_results: list[dict[str, str]] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    attempts = 0

    for chunk_index, chunk_content in enumerate(chunks, start=1):
        user_content = build_prompt_content(
            "summary_chunk_user.txt",
            title=paper["title"],
            categories=", ".join(paper["matched_categories"]),
            chunk_index=chunk_index,
            chunk_total=len(chunks),
            chunk_content=chunk_content,
        )
        payload = build_custom_payload(llm_settings, chunk_system_prompt, user_content, initial_max_tokens)
        sections, telemetry = summarize_custom_payload(
            client=client,
            llm_settings=llm_settings,
            payload=payload,
            retries=retries,
            initial_max_tokens=initial_max_tokens,
            paper_id=f"{paper['id']}:chunk:{chunk_index}",
        )
        chunk_results.append(sections)
        total_prompt_tokens += telemetry.get("prompt_tokens") or 0
        total_completion_tokens += telemetry.get("completion_tokens") or 0
        total_cache_hit_tokens += telemetry.get("prompt_cache_hit_tokens") or 0
        total_cache_miss_tokens += telemetry.get("prompt_cache_miss_tokens") or 0
        attempts += telemetry.get("attempt", 1)

    chunk_summaries = []
    for index, sections in enumerate(chunk_results, start=1):
        chunk_summaries.append(
            "\n".join(
                [
                    f"### Chunk {index}",
                    f"TL;DR: {sections.get('tldr', '').strip()}",
                    f"Motivation: {sections.get('motivation', '').strip()}",
                    f"Method: {sections.get('method', '').strip()}",
                    f"Result: {sections.get('result', '').strip()}",
                    f"Conclusion: {sections.get('conclusion', '').strip()}",
                ]
            ).strip()
        )

    reduce_user_content = build_prompt_content(
        "summary_reduce_user.txt",
        title=paper["title"],
        categories=", ".join(paper["matched_categories"]),
        chunk_summaries="\n\n".join(chunk_summaries),
    )
    effective_reduce_max_tokens = reduce_max_tokens if reduce_max_tokens is not None else DEFAULT_REDUCE_MAX_TOKENS
    reduce_payload = build_custom_payload(llm_settings, reduce_system_prompt, reduce_user_content, effective_reduce_max_tokens)
    final_sections, reduce_telemetry = summarize_custom_payload(
        client=client,
        llm_settings=llm_settings,
        payload=reduce_payload,
        retries=retries,
        initial_max_tokens=effective_reduce_max_tokens,
        paper_id=f"{paper['id']}:reduce",
    )
    total_prompt_tokens += reduce_telemetry.get("prompt_tokens") or 0
    total_completion_tokens += reduce_telemetry.get("completion_tokens") or 0
    total_cache_hit_tokens += reduce_telemetry.get("prompt_cache_hit_tokens") or 0
    total_cache_miss_tokens += reduce_telemetry.get("prompt_cache_miss_tokens") or 0
    attempts += reduce_telemetry.get("attempt", 1)

    telemetry = {
        "attempt": attempts,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "prompt_cache_hit_tokens": total_cache_hit_tokens,
        "prompt_cache_miss_tokens": total_cache_miss_tokens,
        "max_tokens": reduce_telemetry.get("max_tokens", initial_max_tokens),
        "finish_reason": reduce_telemetry.get("finish_reason", ""),
        "chunk_count": len(chunks),
        "summary_mode": "chunk_reduce",
    }
    return final_sections, telemetry


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
    }
    paper["summary_status"] = f"fallback:{error_message}"


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
    }


def should_skip(paper: dict[str, Any]) -> bool:
    return paper.get("summary_status") == "ok" and bool(paper.get("summary_zh"))


def flatten_sections(sections: dict[str, str]) -> str:
    ordered = [
        ("TL;DR", sections.get("tldr", "").strip()),
        ("Motivation", sections.get("motivation", "").strip()),
        ("Method", sections.get("method", "").strip()),
        ("Result", sections.get("result", "").strip()),
        ("Conclusion", sections.get("conclusion", "").strip()),
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
    chunk_trigger_chars: int,
    chunk_size: int,
    chunk_overlap: int,
    client: httpx.Client,
    reduce_max_tokens: int | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    if len(paper.get("fulltext_markdown", "")) >= chunk_trigger_chars:
        return summarize_via_chunks(
            client=client,
            llm_settings=llm_settings,
            paper=paper,
            retries=retries,
            initial_max_tokens=initial_max_tokens,
            chunk_size=chunk_size,
            overlap=chunk_overlap,
            reduce_max_tokens=reduce_max_tokens,
        )
    return summarize_text(
        client=client,
        llm_settings=llm_settings,
        paper=paper,
        retries=retries,
        initial_max_tokens=initial_max_tokens,
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
    arxiv_config = config.get("arxiv", {})
    chunk_trigger_chars = int(arxiv_config.get("summary_chunk_trigger_chars", 18000))
    chunk_size = int(arxiv_config.get("summary_chunk_char_budget", 12000))
    chunk_overlap = int(arxiv_config.get("summary_chunk_overlap_chars", 1200))
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
        if should_skip(paper):
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
                chunk_trigger_chars,
                chunk_size,
                chunk_overlap,
                client,
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
                paper["summary_input_source"] = (
                    "pdf_fulltext" if paper.get("fulltext_markdown") else "abstract_only"
                )
                success_count += 1
                print(
                    {
                        "paper_index": index + 1,
                        "paper_id": paper["id"],
                        "status": "ok",
                        "summary_input_source": paper["summary_input_source"],
                        "fulltext_chars": len(paper.get("fulltext_markdown", "")),
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

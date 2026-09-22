from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import httpx

try:
    from common import daily_path, load_config, load_keywords, load_llm_settings, read_json, write_json
    from snapshot_writer import SnapshotWriter
except ModuleNotFoundError:  # pragma: no cover - local package-style invocation
    from scripts.common import daily_path, load_config, load_keywords, load_llm_settings, read_json, write_json
    from scripts.snapshot_writer import SnapshotWriter


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "prompts"
MAX_SUMMARY_TOKENS = 1800
SUMMARY_FIELDS = ("tldr", "motivation", "method", "result", "conclusion", "relevance_score")


class SummaryError(RuntimeError):
    """Base class for summary failures."""


class RetryableSummaryError(SummaryError):
    """Error that should trigger a retry."""


class JsonOutputError(RetryableSummaryError):
    """Model returned malformed or incomplete JSON."""


class LengthLimitError(RetryableSummaryError):
    """Model stopped because of output length limit."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize one daily snapshot with streaming MiMo JSON.")
    parser.add_argument("--date", required=True, help="Target daily file date, format YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Only summarize the first N papers for local development.")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_SUMMARY_TOKENS,
        help="max_completion_tokens for each summary.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.getenv("SUMMARY_MAX_WORKERS", "4")),
        help="How many papers to summarize at once.",
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


def render_system(template_name: str, language: str) -> str:
    """Render a system prompt with language baked in.

    Plain string replacement keeps the JSON examples in the prompt intact.
    The only supported placeholder is ``{language}``.
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
        keywords="\n".join(f"- {keyword}" for keyword in keywords),
        relevance_instruction=(
            "请按上面的关键词给出 0-100 的 relevance_score：这是个人阅读优先级，不是论文质量。"
            "按命中程度最高的那一条关键词打分，不要求同时命中全部。"
            "只做意义匹配：training 不是 rainfall，cloud computing 不是云，token/sales/traffic forecast 在关键词是天气预报时不算。"
            "用完整量表，不要扎堆在 0、5、10、15、85、95。"
            "判别参考（抄区分度，不要抄分数）：雷达 0–2h 降水临近预报 94；"
            "GOES 全圆盘云临近预报 89；大气或海洋的 EnKF/4D-Var/生成式 DA 86；"
            "全球 ML 天气集合 91；古气候 4D-Var 74；只用了 NWP 辐射库的 IASI 反演 42；"
            "垃圾填埋气体 nowcast 28；交通或医学影像/临床 QA 4；机器人手或代码 MoE 2。"
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
        "model": str(llm_settings["model"]).strip().lower(),
        "temperature": 0.15,
        "max_completion_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
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
    if not isinstance(parsed, dict):
        raise JsonOutputError("json_not_object")
    missing = [field for field in SUMMARY_FIELDS if field not in parsed]
    if missing:
        raise JsonOutputError("missing_fields:" + ",".join(missing))
    sections: dict[str, str] = {}
    for field in SUMMARY_FIELDS:
        value = parsed[field]
        if field == "relevance_score" and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise JsonOutputError(f"{field}_not_string")
        sections[field] = value.strip()
    score = sections["relevance_score"]
    if score and (not score.isdigit() or int(score) > 100):
        raise JsonOutputError("relevance_score_invalid")
    if not any(sections[field] for field in SUMMARY_FIELDS if field != "relevance_score"):
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
    return not needs_new_summary(paper, keywords or [])


def needs_new_summary(paper: dict[str, Any], keywords: list[str] | None = None) -> bool:
    status = str(paper.get("summary_status") or "")
    if status == "ok" and paper.get("summary_zh"):
        if keywords and not str((paper.get("summary_sections") or {}).get("relevance_score") or "").strip():
            return True
        return False
    if status.startswith("fallback"):
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

def write_github_output(values: dict[str, str]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def build_summary_http_client(llm_settings: dict[str, Any]) -> httpx.Client:
    timeout = httpx.Timeout(llm_settings["timeout_seconds"])
    return httpx.Client(timeout=timeout, trust_env=False)


def _api_root(llm_settings: dict[str, Any]) -> str:
    return str(llm_settings["base_url"]).rstrip("/")


def _auth_headers(llm_settings: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {llm_settings['api_key']}",
        "Content-Type": "application/json",
    }


def apply_success(paper: dict[str, Any], sections: dict[str, str]) -> None:
    paper["summary_sections"] = sections
    paper["summary_zh"] = flatten_sections(sections)
    paper["summary_status"] = "ok"
    paper["summary_input_source"] = "metadata"


def usage_from_body(body: dict[str, Any]) -> dict[str, int]:
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens") or 0
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "prompt_cache_hit_tokens": int(hit or 0),
        "prompt_cache_miss_tokens": int(miss or 0),
    }


def papers_to_summarize(
    payload: dict[str, Any],
    *,
    keywords: list[str],
    limit: int | None,
    refresh_ok: bool,
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    for index, paper in enumerate(payload.get("papers") or []):
        if limit is not None and index >= limit:
            break
        if refresh_ok and paper.get("summary_status") == "ok":
            reset_fallback_summary(paper)
        if limit is not None and str(paper.get("summary_status") or "").startswith("fallback"):
            reset_fallback_summary(paper)
        if needs_new_summary(paper, keywords):
            chosen.append(paper)
    return chosen


def settle_disabled(config: dict[str, Any], payload: dict[str, Any], *, limit: int | None, keywords: list[str]) -> None:
    for index, paper in enumerate(payload.get("papers") or []):
        if limit is not None and index >= limit:
            break
        if needs_new_summary(paper, keywords):
            apply_fallback(config, paper, "llm_disabled")


def require_api_key(llm_settings: dict[str, Any]) -> None:
    if not llm_settings["api_key"]:
        raise RuntimeError("Missing OPENAI_API_KEY")


def iter_stream_chunks(response: httpx.Response) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for line in response.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        payload = json.loads(data)
        if isinstance(payload, dict) and payload.get("error"):
            message = payload["error"]
            detail = message.get("message") if isinstance(message, dict) else str(message)
            raise RetryableSummaryError(str(detail)[:180])
        if isinstance(payload, dict):
            chunks.append(payload)
    return chunks


def assemble_stream(chunks: list[dict[str, Any]]) -> tuple[str, str, dict[str, int]]:
    parts: list[str] = []
    finish_reason = ""
    usage: dict[str, int] = {}
    for chunk in chunks:
        if chunk.get("usage"):
            usage = usage_from_body(chunk)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content:
            parts.append(str(content))
    return "".join(parts), finish_reason, usage


def stream_summary(
    client: httpx.Client,
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    *,
    max_tokens: int,
    keywords: list[str],
) -> tuple[dict[str, str], dict[str, int]]:
    with client.stream(
        "POST",
        f"{_api_root(llm_settings)}/chat/completions",
        headers=_auth_headers(llm_settings),
        json=build_request_payload(llm_settings, paper, max_tokens, keywords),
    ) as response:
        if response.status_code >= 400:
            detail = response.read().decode("utf-8", "replace")[:300]
            raise RetryableSummaryError(f"http_{response.status_code}:{detail}")
        content, finish_reason, usage = assemble_stream(iter_stream_chunks(response))
    if finish_reason == "length":
        raise LengthLimitError("finish_reason_length")
    sections = normalize_sections(extract_json_object(content))
    return sections, usage


def summarize_with_retries(
    llm_settings: dict[str, Any],
    paper: dict[str, Any],
    *,
    max_tokens: int,
    keywords: list[str],
) -> tuple[dict[str, str], dict[str, int]]:
    attempts = max(1, int(llm_settings["retry_times"]) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with build_summary_http_client(llm_settings) as client:
                return stream_summary(
                    client,
                    llm_settings,
                    paper,
                    max_tokens=max_tokens,
                    keywords=keywords,
                )
        except (httpx.HTTPError, json.JSONDecodeError, RetryableSummaryError) as exc:
            last_error = exc
            print(
                {
                    "paper_id": paper.get("id"),
                    "attempt": attempt + 1,
                    "error": exc.__class__.__name__,
                    "detail": str(exc)[:180],
                }
            )
    raise RetryableSummaryError(str(last_error)[:180] if last_error else "summary_failed")


def persist_payload(path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    refresh_summary_counts(payload)
    write_json(path, payload, pretty=config["output"].get("write_pretty_json", True))


def run_summaries(
    path: Path,
    *,
    limit: int | None,
    refresh_ok: bool,
    skip: bool,
    max_tokens: int,
    workers: int,
) -> None:
    config = load_config()
    llm_settings = load_llm_settings()
    keywords = load_keywords()
    payload = read_json(path)
    if skip:
        print({"status": "skipped", "reason": "skip_summarize"})
        return
    if not llm_settings["enabled"]:
        settle_disabled(config, payload, limit=limit, keywords=keywords)
        persist_payload(path, payload, config)
        print({"status": "skipped", "reason": "llm_disabled"})
        return

    papers = papers_to_summarize(payload, keywords=keywords, limit=limit, refresh_ok=refresh_ok)
    if not papers:
        persist_payload(path, payload, config)
        print({"status": "ok", "summarized": 0})
        return
    require_api_key(llm_settings)

    writer = SnapshotWriter(
        path,
        payload,
        pretty=config["output"].get("write_pretty_json", True),
        on_flush=lambda current: refresh_summary_counts(current),
    )

    def summarize_paper(paper: dict[str, Any]) -> None:
        try:
            sections, usage = summarize_with_retries(
                llm_settings,
                paper,
                max_tokens=max_tokens,
                keywords=keywords,
            )
            apply_success(paper, sections)
            print({"paper_id": paper.get("id"), "summary_status": "ok", **usage})
        except (httpx.HTTPError, json.JSONDecodeError, SummaryError) as exc:
            apply_fallback(config, paper, exc.__class__.__name__)
            print(
                {
                    "paper_id": paper.get("id"),
                    "summary_status": paper["summary_status"],
                    "detail": str(exc)[:180],
                }
            )
        writer.mark_dirty()

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(summarize_paper, papers))
    finally:
        writer.close()
    print({"status": "ok", "summarized": len(papers), "model": llm_settings["model"]})


def main() -> None:
    args = parse_args()
    run_summaries(
        daily_path(date.fromisoformat(args.date)),
        limit=args.limit if args.limit and args.limit > 0 else None,
        refresh_ok=args.refresh_ok,
        skip=False,
        max_tokens=args.max_tokens,
        workers=args.max_workers,
    )


if __name__ == "__main__":
    main()

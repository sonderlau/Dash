from __future__ import annotations

import argparse
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import feedparser

from common import load_config


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_LIST_URL = "https://arxiv.org/list/{category}/new"
USER_AGENT = "Dash/0.1 (+https://github.com/sonderlau/Dash)"
ID_PATTERN = re.compile(r"^([0-9]{4}\.[0-9]{4,5})(v\d+)?$")
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_FETCH_RETRIES = 4


@dataclass
class FetchStats:
    fetched: int = 0
    kept: int = 0
    duplicates: int = 0


class ArxivNewListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.target_categories: set[str] = set()
        self.paper_categories: dict[str, set[str]] = {}
        self._current_id: str | None = None
        self._capture_primary_subject = False
        self._capture_subject_text = False
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "a":
            href = attrs_map.get("href") or ""
            title = attrs_map.get("title") or ""
            if title == "Abstract" and href.startswith("/abs/"):
                paper_id = href.rsplit("/", 1)[-1]
                match = ID_PATTERN.match(paper_id)
                if match:
                    self._current_id = match.group(1)
                    self.paper_categories.setdefault(self._current_id, set())
                else:
                    self._current_id = None

        class_attr = attrs_map.get("class") or ""
        class_names = set(class_attr.split())
        if tag == "span" and "primary-subject" in class_names:
            self._capture_primary_subject = True
            self._current_text_parts = []
        elif tag == "div" and "list-subjects" in class_names and self._current_id:
            self._capture_subject_text = True
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_primary_subject or self._capture_subject_text:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._capture_primary_subject:
            self._capture_primary_subject = False
            self._flush_current_subject_text()
        elif tag == "div" and self._capture_subject_text:
            self._capture_subject_text = False
            self._flush_current_subject_text()
            self._current_id = None
        elif tag == "dd" and not self._capture_subject_text:
            self._current_id = None

    def _flush_current_subject_text(self) -> None:
        if not self._current_id:
            self._current_text_parts = []
            return
        raw_text = " ".join(part.strip() for part in self._current_text_parts if part.strip())
        self._current_text_parts = []
        if not raw_text:
            return
        categories = set(re.findall(r"\(([^)]+)\)", raw_text))
        if categories:
            self.paper_categories.setdefault(self._current_id, set()).update(categories)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch papers from arXiv category new pages.")
    return parser.parse_args()


def _retry_after_seconds(headers, attempt: int) -> float:
    """Honor Retry-After when present, else exponential backoff with cap."""
    raw = ""
    if headers is not None:
        try:
            raw = (headers.get("Retry-After") or "").strip()
        except AttributeError:
            raw = ""
    if raw.isdigit():
        return max(1.0, min(float(raw), 90.0))
    return min(2.0 ** attempt, 30.0)


def fetch_url(url: str, timeout: int = 60, retries: int = DEFAULT_FETCH_RETRIES) -> bytes:
    """GET a URL with retry on 429/5xx and transient transport errors.

    arXiv occasionally rate-limits the API endpoint and the public list pages,
    especially from shared egress IPs (CI runners, cloud). We retry up to
    `retries` times with exponential backoff, honoring `Retry-After` when the
    server provides it.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRYABLE_HTTP_CODES or attempt >= retries:
                raise
            wait = _retry_after_seconds(exc.headers, attempt)
            print(
                {
                    "stage": "fetch_url",
                    "url": url,
                    "status": exc.code,
                    "attempt": attempt + 1,
                    "retry_in_s": round(wait, 1),
                }
            )
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            wait = _retry_after_seconds(None, attempt)
            print(
                {
                    "stage": "fetch_url",
                    "url": url,
                    "error": exc.__class__.__name__,
                    "detail": str(exc.reason)[:120] if hasattr(exc, "reason") else str(exc)[:120],
                    "attempt": attempt + 1,
                    "retry_in_s": round(wait, 1),
                }
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch_url exhausted retries without recording an error")


def fetch_new_category_ids(category: str) -> dict[str, list[str]]:
    html = fetch_url(ARXIV_LIST_URL.format(category=urllib.parse.quote(category)))
    parser = ArxivNewListParser()
    parser.feed(html.decode("utf-8", errors="ignore"))

    matched: dict[str, list[str]] = {}
    for paper_id, categories in parser.paper_categories.items():
        normalized = sorted(categories)
        if category in categories:
            matched[paper_id] = normalized
    return matched


def fetch_feed_entries_by_ids(
    arxiv_ids: list[str],
    max_workers: int = 1,
    request_delay_seconds: float = 3.0,
) -> list[feedparser.FeedParserDict]:
    """Fetch arXiv API metadata in 50-id chunks.

    Defaults to serial calls with a 3-second pause between chunks, matching
    arXiv's published API guidance. The CI runner shares egress IPs with many
    other tenants, so even a 4-way burst gets 429'd. Bumping `max_workers`
    above 1 only makes sense from a private network.
    """
    if not arxiv_ids:
        return []
    chunk_size = 50
    chunks = [arxiv_ids[start : start + chunk_size] for start in range(0, len(arxiv_ids), chunk_size)]

    def fetch_one(chunk: list[str]) -> list[feedparser.FeedParserDict]:
        query = urllib.parse.urlencode(
            {
                "id_list": ",".join(chunk),
                "start": 0,
                "max_results": len(chunk),
            }
        )
        url = f"{ARXIV_API_URL}?{query}"
        feed = feedparser.parse(fetch_url(url))
        return list(feed.entries)

    if len(chunks) == 1:
        return fetch_one(chunks[0])

    entries: list[feedparser.FeedParserDict] = []
    workers = max(1, min(max_workers, len(chunks)))
    if workers == 1:
        # Serial path with arXiv-recommended 3s gap between chunks.
        for index, chunk in enumerate(chunks):
            entries.extend(fetch_one(chunk))
            if index < len(chunks) - 1 and request_delay_seconds > 0:
                time.sleep(request_delay_seconds)
        return entries

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for chunk_entries in executor.map(fetch_one, chunks):
            entries.extend(chunk_entries)
    return entries


def normalize_paper(
    entry: feedparser.FeedParserDict,
    configured_categories: list[str],
    categories_from_new_page: list[str] | None = None,
) -> dict:
    raw_id = entry.id.rsplit("/", 1)[-1]
    if "v" in raw_id:
        base_id = raw_id.split("v", 1)[0]
    else:
        base_id = raw_id

    raw_categories = [tag["term"] for tag in entry.get("tags", []) if "term" in tag]
    if categories_from_new_page:
        for category in categories_from_new_page:
            if category not in raw_categories:
                raw_categories.append(category)

    matched_categories = [cat for cat in configured_categories if cat in raw_categories]
    display_category = matched_categories[0] if matched_categories else "other"

    published_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    updated_dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

    pdf_url = next(
        (
            link.href
            for link in entry.get("links", [])
            if getattr(link, "title", "") == "pdf" or link.get("type") == "application/pdf"
        ),
        f"https://arxiv.org/pdf/{base_id}",
    )

    return {
        "id": base_id,
        "title": " ".join(entry.title.split()),
        "authors": [author.name for author in entry.get("authors", [])],
        "categories": raw_categories,
        "matched_categories": matched_categories,
        "display_category": display_category,
        "abs_url": entry.link,
        "pdf_url": pdf_url,
        "abstract_en": " ".join(entry.summary.split()),
        "fulltext_markdown": "",
        "fulltext_source": "",
        "fulltext_status": "pending",
        "summary_zh": "",
        "summary_input_source": "",
        "summary_sections": {
            "tldr": "",
            "motivation": "",
            "method": "",
            "result": "",
            "conclusion": "",
        },
        "summary_status": "pending",
        "published_date": published_dt.date().isoformat(),
        "updated_date": updated_dt.date().isoformat(),
        "source": "arxiv_new",
    }


def fetch_papers(config: dict) -> tuple[list[dict], FetchStats]:
    categories = list(config["arxiv"]["categories"])
    list_workers = max(1, int(os.getenv("ARXIV_LIST_WORKERS", str(min(8, max(1, len(categories)))))))
    api_workers = max(1, int(os.getenv("ARXIV_API_WORKERS", "1")))

    by_id: OrderedDict[str, dict] = OrderedDict()
    matched_categories_by_id: dict[str, set[str]] = {}
    stats = FetchStats()

    if categories:
        with ThreadPoolExecutor(max_workers=min(list_workers, len(categories))) as executor:
            results_in_order = list(executor.map(fetch_new_category_ids, categories))
    else:
        results_in_order = []

    for category_matches in results_in_order:
        stats.fetched += len(category_matches)
        for paper_id, page_categories in category_matches.items():
            bucket = matched_categories_by_id.setdefault(paper_id, set())
            previous_size = len(bucket)
            bucket.update(page_categories)
            if previous_size != 0:
                stats.duplicates += 1

    entries = fetch_feed_entries_by_ids(list(matched_categories_by_id.keys()), max_workers=api_workers)
    entry_by_id: dict[str, feedparser.FeedParserDict] = {}
    for entry in entries:
        raw_id = entry.id.rsplit("/", 1)[-1]
        base_id = raw_id.split("v", 1)[0]
        entry_by_id[base_id] = entry

    for paper_id, page_categories in matched_categories_by_id.items():
        entry = entry_by_id.get(paper_id)
        if entry is None:
            continue
        paper = normalize_paper(entry, categories, sorted(page_categories))
        if not paper["matched_categories"]:
            continue
        by_id[paper_id] = paper
        stats.kept += 1

    papers = sorted(
        by_id.values(),
        key=lambda item: (item["updated_date"], item["published_date"], item["id"]),
        reverse=True,
    )
    return papers, stats


def main() -> None:
    parse_args()
    config = load_config()
    papers, stats = fetch_papers(config)
    category_counts: dict[str, int] = {}
    for paper in papers:
        category_counts[paper["display_category"]] = category_counts.get(paper["display_category"], 0) + 1

    paper_dates = sorted({paper["published_date"] for paper in papers}, reverse=True)

    print(
        {
            "fetched": stats.fetched,
            "kept": stats.kept,
            "duplicates": stats.duplicates,
            "paper_dates": paper_dates[:5],
            "category_counts": category_counts,
        }
    )


if __name__ == "__main__":
    main()

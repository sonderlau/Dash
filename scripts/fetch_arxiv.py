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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser

import feedparser

from common import load_config


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_LIST_URL = "https://arxiv.org/list/{category}/new"
USER_AGENT = "Dash/0.1 (+https://github.com/sonderlau/Dash)"
ID_PATTERN = re.compile(r"^([0-9]{4}\.[0-9]{4,5})(v\d+)?$")
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_FETCH_RETRIES = 6
DEFAULT_API_REQUEST_DELAY_SECONDS = 10.0
RATE_LIMIT_BASE_DELAY_SECONDS = 30.0
RATE_LIMIT_MAX_DELAY_SECONDS = 180.0


@dataclass
class FetchStats:
    fetched: int = 0
    kept: int = 0
    duplicates: int = 0
    api_backfill_status: str = "ok"
    api_backfill_error: str = ""
    api_backfill_entries: int = 0


@dataclass
class ArxivListPaper:
    id: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    primary_category: str = ""
    abs_url: str = ""
    pdf_url: str = ""


class ArxivNewListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.target_categories: set[str] = set()
        self.papers: OrderedDict[str, ArxivListPaper] = OrderedDict()
        self._current_id: str | None = None
        self._capture_field: str | None = None
        self._capture_primary_subject = False
        self._capture_subject_text = False
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "a":
            href = attrs_map.get("href") or ""
            if href.startswith("/abs/"):
                paper_id = href.rsplit("/", 1)[-1]
                match = ID_PATTERN.match(paper_id)
                if match:
                    paper_id = match.group(1)
                    self._current_id = paper_id
                    paper = self.papers.setdefault(paper_id, ArxivListPaper(id=paper_id))
                    paper.abs_url = f"https://arxiv.org/abs/{paper_id}"
                else:
                    self._current_id = None
            elif href.startswith("/pdf/") and self._current_id:
                paper = self.papers.setdefault(self._current_id, ArxivListPaper(id=self._current_id))
                paper.pdf_url = f"https://arxiv.org/pdf/{self._current_id}"

        class_attr = attrs_map.get("class") or ""
        class_names = set(class_attr.split())
        if tag == "div" and self._current_id:
            if "list-title" in class_names:
                self._capture_field = "title"
                self._current_text_parts = []
            elif "list-authors" in class_names:
                self._capture_field = "authors"
                self._current_text_parts = []
            elif "list-subjects" in class_names:
                self._capture_subject_text = True
                self._current_text_parts = []

        if tag == "span" and "primary-subject" in class_names:
            self._capture_primary_subject = True
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_field or self._capture_primary_subject or self._capture_subject_text:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._capture_field:
            self._flush_current_field_text()
            self._capture_field = None
            return
        if tag == "span" and self._capture_primary_subject:
            self._capture_primary_subject = False
            self._flush_current_subject_text(primary=True)
        elif tag == "div" and self._capture_subject_text:
            self._capture_subject_text = False
            self._flush_current_subject_text(primary=False)
            self._current_id = None
        elif tag == "dd" and not self._capture_subject_text:
            self._current_id = None

    def _flush_current_field_text(self) -> None:
        if not self._current_id:
            self._current_text_parts = []
            return
        raw_text = _clean_text(" ".join(self._current_text_parts))
        self._current_text_parts = []
        paper = self.papers.setdefault(self._current_id, ArxivListPaper(id=self._current_id))
        if self._capture_field == "title":
            paper.title = _strip_descriptor(raw_text, "Title:")
        elif self._capture_field == "authors":
            authors_text = _strip_descriptor(raw_text, "Authors:")
            paper.authors = [author.strip() for author in authors_text.split(",") if author.strip()]

    def _flush_current_subject_text(self, primary: bool) -> None:
        if not self._current_id:
            self._current_text_parts = []
            return
        raw_text = _clean_text(" ".join(self._current_text_parts))
        self._current_text_parts = []
        if not raw_text:
            return
        paper = self.papers.setdefault(self._current_id, ArxivListPaper(id=self._current_id))
        categories = set(re.findall(r"\(([^)]+)\)", raw_text))
        if categories:
            existing = set(paper.categories or [])
            paper.categories = sorted(existing | categories)
        if primary:
            match = re.search(r"\(([^)]+)\)", raw_text)
            if match:
                paper.primary_category = match.group(1)


def _clean_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _strip_descriptor(value: str, descriptor: str) -> str:
    if value.startswith(descriptor):
        return value[len(descriptor) :].strip()
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch papers from arXiv category new pages.")
    return parser.parse_args()


def _retry_after_seconds(headers, attempt: int, status_code: int | None = None) -> float:
    """Honor Retry-After when present, else exponential backoff with cap."""
    raw = ""
    if headers is not None:
        try:
            raw = (headers.get("Retry-After") or "").strip()
        except AttributeError:
            raw = ""
    if raw.isdigit():
        cap = RATE_LIMIT_MAX_DELAY_SECONDS if status_code == 429 else 90.0
        return max(1.0, min(float(raw), cap))
    if status_code == 429:
        return min(RATE_LIMIT_BASE_DELAY_SECONDS * (2.0**attempt), RATE_LIMIT_MAX_DELAY_SECONDS)
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
            wait = _retry_after_seconds(exc.headers, attempt, exc.code)
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
        except TimeoutError as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            wait = _retry_after_seconds(None, attempt)
            print(
                {
                    "stage": "fetch_url",
                    "url": url,
                    "error": exc.__class__.__name__,
                    "detail": str(exc)[:120],
                    "attempt": attempt + 1,
                    "retry_in_s": round(wait, 1),
                }
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch_url exhausted retries without recording an error")


def fetch_new_category_papers(category: str) -> OrderedDict[str, ArxivListPaper]:
    html = fetch_url(ARXIV_LIST_URL.format(category=urllib.parse.quote(category)))
    parser = ArxivNewListParser()
    parser.feed(html.decode("utf-8", errors="ignore"))

    matched: OrderedDict[str, ArxivListPaper] = OrderedDict()
    for paper_id, paper in parser.papers.items():
        if category in paper.categories:
            matched[paper_id] = paper
    return matched


def fetch_new_category_ids(category: str) -> dict[str, list[str]]:
    papers = fetch_new_category_papers(category)
    return {paper_id: sorted(paper.categories) for paper_id, paper in papers.items()}


def fetch_feed_entries_by_ids(
    arxiv_ids: list[str],
    max_workers: int = 1,
    request_delay_seconds: float = DEFAULT_API_REQUEST_DELAY_SECONDS,
) -> list[feedparser.FeedParserDict]:
    """Fetch arXiv API metadata in 50-id chunks.

    Defaults to serial calls with a conservative pause between chunks. arXiv's
    published minimum is 3 seconds, but GitHub-hosted CI runners share egress
    IPs with many tenants, so a longer gap avoids inheriting a hot IP's rate
    limit. Bumping `max_workers` above 1 only makes sense from a private
    network.
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


def summary_sections_template() -> dict[str, str]:
    return {
        "tldr": "",
        "motivation": "",
        "method": "",
        "result": "",
        "conclusion": "",
        "relevance_score": "",
    }


def normalize_list_paper(
    paper: ArxivListPaper,
    configured_categories: list[str],
    snapshot_date: date | None = None,
) -> dict:
    raw_categories = list(paper.categories)
    matched_categories = [cat for cat in configured_categories if cat in raw_categories]
    display_category = matched_categories[0] if matched_categories else "other"
    fallback_date = (snapshot_date or datetime.now(timezone.utc).date()).isoformat()

    return {
        "id": paper.id,
        "title": paper.title,
        "authors": list(paper.authors),
        "categories": raw_categories,
        "matched_categories": matched_categories,
        "display_category": display_category,
        "primary_category": paper.primary_category,
        "abs_url": paper.abs_url or f"https://arxiv.org/abs/{paper.id}",
        "pdf_url": paper.pdf_url or f"https://arxiv.org/pdf/{paper.id}",
        "abstract_en": "",
        "comment": "",
        "journal_ref": "",
        "doi": "",
        "summary_zh": "",
        "summary_input_source": "",
        "summary_sections": summary_sections_template(),
        "summary_status": "pending",
        "published_date": fallback_date,
        "updated_date": fallback_date,
        "source": "arxiv_new",
        "metadata_source": "arxiv_list",
    }


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
    primary_category = ""
    if entry.get("arxiv_primary_category"):
        primary_category = entry.arxiv_primary_category.get("term", "")

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
        "primary_category": primary_category,
        "abs_url": entry.link,
        "pdf_url": pdf_url,
        "abstract_en": " ".join(entry.summary.split()),
        "comment": " ".join(str(entry.get("arxiv_comment", "")).split()),
        "journal_ref": " ".join(str(entry.get("arxiv_journal_ref", "")).split()),
        "doi": " ".join(str(entry.get("arxiv_doi", "")).split()),
        "summary_zh": "",
        "summary_input_source": "",
        "summary_sections": summary_sections_template(),
        "summary_status": "pending",
        "published_date": published_dt.date().isoformat(),
        "updated_date": updated_dt.date().isoformat(),
        "source": "arxiv_new",
        "metadata_source": "arxiv_api",
    }


def fetch_papers(config: dict) -> tuple[list[dict], FetchStats]:
    categories = list(config["arxiv"]["categories"])
    list_workers = max(1, int(os.getenv("ARXIV_LIST_WORKERS", str(min(8, max(1, len(categories)))))))
    api_workers = max(1, int(os.getenv("ARXIV_API_WORKERS", "1")))
    api_request_delay = max(
        0.0,
        float(os.getenv("ARXIV_API_REQUEST_DELAY_SECONDS", str(DEFAULT_API_REQUEST_DELAY_SECONDS))),
    )

    by_id: OrderedDict[str, dict] = OrderedDict()
    matched_categories_by_id: dict[str, set[str]] = {}
    list_papers_by_id: OrderedDict[str, ArxivListPaper] = OrderedDict()
    stats = FetchStats()

    if categories:
        with ThreadPoolExecutor(max_workers=min(list_workers, len(categories))) as executor:
            results_in_order = list(executor.map(fetch_new_category_papers, categories))
    else:
        results_in_order = []

    for category_matches in results_in_order:
        stats.fetched += len(category_matches)
        for paper_id, list_paper in category_matches.items():
            existing_list_paper = list_papers_by_id.get(paper_id)
            if existing_list_paper is None:
                list_papers_by_id[paper_id] = list_paper
            else:
                existing_categories = set(existing_list_paper.categories)
                existing_list_paper.categories = sorted(existing_categories | set(list_paper.categories))
                if not existing_list_paper.primary_category:
                    existing_list_paper.primary_category = list_paper.primary_category

            bucket = matched_categories_by_id.setdefault(paper_id, set())
            previous_size = len(bucket)
            bucket.update(list_paper.categories)
            if previous_size != 0:
                stats.duplicates += 1

    entries: list[feedparser.FeedParserDict] = []
    try:
        entries = fetch_feed_entries_by_ids(
            list(matched_categories_by_id.keys()),
            max_workers=api_workers,
            request_delay_seconds=api_request_delay,
        )
    except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        stats.api_backfill_status = "degraded"
        stats.api_backfill_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
        print(
            {
                "stage": "arxiv_api_backfill",
                "status": stats.api_backfill_status,
                "error": stats.api_backfill_error,
                "fallback": "using arxiv list page metadata",
            }
        )

    stats.api_backfill_entries = len(entries)
    entry_by_id: dict[str, feedparser.FeedParserDict] = {}
    for entry in entries:
        raw_id = entry.id.rsplit("/", 1)[-1]
        base_id = raw_id.split("v", 1)[0]
        entry_by_id[base_id] = entry

    for paper_id, page_categories in matched_categories_by_id.items():
        entry = entry_by_id.get(paper_id)
        if entry is not None:
            paper = normalize_paper(entry, categories, sorted(page_categories))
        else:
            list_paper = list_papers_by_id.get(paper_id)
            if list_paper is None:
                continue
            paper = normalize_list_paper(list_paper, categories)
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

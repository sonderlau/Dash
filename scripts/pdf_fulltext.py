from __future__ import annotations

import importlib.resources as resources
import json
import re
import subprocess
import shutil
import urllib.request
from pathlib import Path
from typing import Any

try:
    from common import ensure_dir, load_config
except ModuleNotFoundError:  # pragma: no cover - local package-style invocation
    from scripts.common import ensure_dir, load_config


_JAR_NAME = "opendataloader-pdf-cli.jar"
_jar_path_cache: Path | None = None


def _resolve_jar_path() -> Path:
    global _jar_path_cache
    if _jar_path_cache is not None:
        return _jar_path_cache
    jar_ref = resources.files("opendataloader_pdf").joinpath("jar", _JAR_NAME)
    with resources.as_file(jar_ref) as jar_path:
        _jar_path_cache = Path(jar_path)
    return _jar_path_cache


SECTION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("introduction", ("introduction",)),
    ("related_work", ("related work", "background", "preliminaries", "preliminary", "literature review")),
    ("method", ("method", "methods", "approach", "approaches", "framework", "model", "algorithm", "proposed method")),
    ("experiments", ("experiments", "experiment", "experimental setup", "evaluation", "results", "analysis", "ablation")),
    ("conclusion", ("conclusion", "conclusions", "discussion", "limitations", "future work")),
    ("references", ("references", "bibliography")),
    ("appendix", ("appendix", "appendices", "supplementary material", "supplementary")),
]

PREFERRED_SECTION_ORDER = ["introduction", "related_work", "method", "experiments", "conclusion"]
MAX_SECTION_CHARS = 7000
MAX_PRELUDE_CHARS = 4000
MAX_CONSECUTIVE_PIPE_LINES = 5


class PdfExtractTimeoutError(RuntimeError):
    pass


def normalize_heading(text: str) -> str:
    cleaned = text.strip().lower()
    cleaned = re.sub(r"^[#\-\s]+", "", cleaned)
    cleaned = re.sub(r"^(section\s+)?[ivx\d\.\-]+\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def detect_section_kind(line: str) -> str | None:
    normalized = normalize_heading(line)
    if not normalized:
        return None
    for section_kind, candidates in SECTION_PATTERNS:
        if normalized in candidates:
            return section_kind
        if any(normalized.startswith(candidate + " ") for candidate in candidates):
            return section_kind
        if any(candidate in normalized for candidate in candidates):
            if len(normalized) <= 80:
                return section_kind
    return None


def split_sections(markdown_text: str) -> tuple[str, dict[str, list[str]]]:
    prelude: list[str] = []
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        section_kind = detect_section_kind(line)
        if section_kind is not None:
            current_section = section_kind
            sections.setdefault(section_kind, []).append(line)
            continue

        if current_section is None:
            prelude.append(line)
        else:
            sections.setdefault(current_section, []).append(line)

    return "\n".join(prelude).strip(), sections


def is_formula_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) <= 18 and re.fullmatch(r"[\dA-Za-z\-\+\=\(\)\[\]\{\}\.\,\s]+", stripped):
        return True
    if stripped.startswith("######") and len(stripped) <= 40:
        return True
    return False


def is_figure_or_table_caption(line: str) -> bool:
    stripped = line.strip()
    lowered = stripped.lower()
    return (
        lowered.startswith("- figure ")
        or lowered.startswith("figure ")
        or lowered.startswith("- table ")
        or lowered.startswith("table ")
    )


def sanitize_markdown(markdown_text: str) -> str:
    cleaned_lines: list[str] = []
    pipe_run = 0

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        lowered = stripped.lower()

        if not stripped:
            pipe_run = 0
            if cleaned_lines and cleaned_lines[-1] == "":
                continue
            cleaned_lines.append("")
            continue

        if lowered.startswith("project page:") or lowered.startswith("correspondence:"):
            continue

        if is_formula_noise(line):
            continue

        if is_figure_or_table_caption(line):
            continue

        if stripped.startswith("|"):
            pipe_run += 1
            if pipe_run > MAX_CONSECUTIVE_PIPE_LINES:
                continue
        else:
            pipe_run = 0

        # Skip extremely sparse table-like rows.
        if stripped.startswith("|") and stripped.count("|") >= 6:
            non_pipe = stripped.replace("|", "").replace("-", "").strip()
            if not non_pipe:
                continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def trim_text_block(text: str, max_chars: int) -> str:
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip()


def build_structured_excerpt(markdown_text: str) -> str:
    prelude, sections = split_sections(markdown_text)
    parts: list[str] = []

    if prelude:
        parts.append("## Front Matter\n" + trim_text_block(prelude, MAX_PRELUDE_CHARS))

    for section_name in PREFERRED_SECTION_ORDER:
        section_lines = sections.get(section_name)
        if not section_lines:
            continue
        section_text = trim_text_block("\n".join(section_lines), MAX_SECTION_CHARS)
        pretty_name = section_name.replace("_", " ").title()
        parts.append(f"## {pretty_name}\n{section_text}")

    if not parts:
        fallback = trim_text_block(markdown_text, MAX_PRELUDE_CHARS + (MAX_SECTION_CHARS * 2))
        return fallback

    return "\n\n".join(part for part in parts if part.strip()).strip()


def get_pdf_paths(config: dict[str, Any], paper_id: str) -> dict[str, Path]:
    arxiv_config = config.get("arxiv", {})
    cache_dir = ensure_dir(arxiv_config.get("pdf_cache_dir", "tmp/paper_cache"))
    extract_dir = ensure_dir(arxiv_config.get("pdf_extract_dir", "tmp/pdf_extract"))
    paper_extract_dir = extract_dir / paper_id
    paper_extract_dir.mkdir(parents=True, exist_ok=True)
    return {
        "pdf": cache_dir / f"{paper_id}.pdf",
        "extract_dir": paper_extract_dir,
        "markdown": paper_extract_dir / f"{paper_id}.md",
        "json": paper_extract_dir / f"{paper_id}.json",
        "meta": paper_extract_dir / "meta.json",
    }


def download_pdf(pdf_url: str, destination: Path, timeout_seconds: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        pdf_url,
        headers={
            "User-Agent": "Dash/0.1 (+https://github.com/sonderlau/Dash)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def run_opendataloader_extract(
    pdf_path: Path,
    output_dir: Path,
    format_value: str,
    timeout_seconds: int,
) -> None:
    cmd = [
        "java",
        "-Djava.awt.headless=true",
        "-Dapple.awt.UIElement=true",
        "-jar",
        str(_resolve_jar_path()),
        str(pdf_path),
        "--output-dir",
        str(output_dir),
        "--format",
        format_value,
        "--quiet",
        "--use-struct-tree",
        "--reading-order",
        "xycut",
        "--table-method",
        "cluster",
        "--image-output",
        "off",
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfExtractTimeoutError(f"pdf_extract_timeout_after_{timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:500]
        raise RuntimeError(detail or "pdf_extract_failed") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("java_not_found") from exc


def find_extracted_files(output_dir: Path) -> tuple[Path | None, Path | None]:
    markdown = next(iter(sorted(output_dir.glob("*.md"))), None)
    json_path = next(iter(sorted(output_dir.glob("*.json"))), None)
    return markdown, json_path


def read_markdown(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def read_structured_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_fulltext_context(paper: dict[str, Any], markdown_text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in markdown_text.splitlines())
    normalized = normalized.strip()
    if not normalized:
        return ""

    sanitized = sanitize_markdown(normalized)
    excerpt = build_structured_excerpt(sanitized)

    return "\n".join(
        [
            f"# {paper['title']}",
            "",
            "## Metadata",
            f"- arXiv ID: {paper['id']}",
            f"- Categories: {', '.join(paper.get('matched_categories') or paper.get('categories') or [])}",
            "",
            "## Abstract",
            paper.get("abstract_en", "").strip(),
            "",
            "## Structured Full Text Excerpt",
            excerpt,
        ]
    ).strip()


def ensure_fulltext_for_paper(
    paper: dict[str, Any],
    force_refresh: bool = False,
) -> dict[str, Any]:
    config = load_config()
    arxiv_config = config.get("arxiv", {})
    if not arxiv_config.get("pdf_fulltext_enabled", True):
        return {"status": "disabled", "fulltext_markdown": "", "source": "disabled"}

    download_result = download_pdf_for_paper(paper, force_refresh=force_refresh, config=config)
    if download_result.get("status") != "ok":
        return download_result
    return extract_pdf_to_markdown(paper, force_refresh=force_refresh, config=config)


def download_pdf_for_paper(
    paper: dict[str, Any],
    force_refresh: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download the PDF for one paper. Pure network I/O — safe to parallelize widely."""
    if config is None:
        config = load_config()
    arxiv_config = config.get("arxiv", {})
    if not arxiv_config.get("pdf_fulltext_enabled", True):
        return {"status": "disabled", "fulltext_markdown": "", "source": "disabled"}

    paths = get_pdf_paths(config, paper["id"])
    timeout_seconds = int(arxiv_config.get("pdf_download_timeout_seconds", 180))

    if force_refresh:
        for target in [paths["pdf"], paths["markdown"], paths["json"], paths["meta"]]:
            target.unlink(missing_ok=True)

    if not paths["pdf"].exists():
        try:
            download_pdf(paper["pdf_url"], paths["pdf"], timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            paths["pdf"].unlink(missing_ok=True)
            return {
                "status": f"download_failed:{exc.__class__.__name__}",
                "fulltext_markdown": "",
                "source": "abstract_only",
            }
    return {"status": "ok", "pdf_path": str(paths["pdf"])}


def extract_pdf_to_markdown(
    paper: dict[str, Any],
    force_refresh: bool = False,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the JVM extractor on a previously-downloaded PDF and build the summary context."""
    if config is None:
        config = load_config()
    arxiv_config = config.get("arxiv", {})
    if not arxiv_config.get("pdf_fulltext_enabled", True):
        return {"status": "disabled", "fulltext_markdown": "", "source": "disabled"}

    paths = get_pdf_paths(config, paper["id"])
    extract_timeout_seconds = int(arxiv_config.get("pdf_extract_timeout_seconds", 180))
    format_value = str(arxiv_config.get("pdf_fulltext_format", "markdown,json"))

    if not paths["pdf"].exists():
        return {
            "status": "extract_failed:NoPdf",
            "fulltext_markdown": "",
            "source": "abstract_only",
        }

    if force_refresh or not paths["markdown"].exists():
        try:
            run_opendataloader_extract(
                paths["pdf"],
                paths["extract_dir"],
                format_value,
                extract_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            error_meta = {
                "paper_id": paper["id"],
                "pdf_url": paper["pdf_url"],
                "pdf_path": str(paths["pdf"]),
                "status": "extract_failed",
                "error": exc.__class__.__name__,
                "detail": str(exc).strip()[:500],
            }
            paths["meta"].write_text(json.dumps(error_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {
                "status": f"extract_failed:{exc.__class__.__name__}",
                "fulltext_markdown": "",
                "markdown_path": str(paths["markdown"]),
                "json_path": str(paths["json"]),
                "source": "abstract_only",
            }
        markdown_path, json_path = find_extracted_files(paths["extract_dir"])
        if markdown_path and markdown_path != paths["markdown"]:
            shutil.copyfile(markdown_path, paths["markdown"])
        if json_path and json_path != paths["json"]:
            shutil.copyfile(json_path, paths["json"])

    markdown_text = read_markdown(paths["markdown"])
    structured = read_structured_json(paths["json"])
    context = build_fulltext_context(paper, markdown_text)
    sanitized_markdown = sanitize_markdown(markdown_text)
    prelude, sections = split_sections(sanitized_markdown)

    meta = {
        "paper_id": paper["id"],
        "pdf_url": paper["pdf_url"],
        "markdown_path": str(paths["markdown"]),
        "json_path": str(paths["json"]),
        "has_fulltext": bool(context),
        "raw_markdown_length": len(markdown_text),
        "sanitized_markdown_length": len(sanitized_markdown),
        "context_length": len(context),
        "detected_sections": sorted(sections.keys()),
        "prelude_length": len(prelude),
        "structured_keys": sorted(structured.keys()) if structured else [],
    }
    paths["meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "ok" if context else "empty",
        "fulltext_markdown": context,
        "markdown_path": str(paths["markdown"]),
        "json_path": str(paths["json"]),
        "source": "pdf_markdown",
    }

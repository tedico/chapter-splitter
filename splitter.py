"""Chapter detection, splitting, and Markdown extraction for PDF books.

Detection strategy:
1. Embedded PDF bookmarks (outline) — free, instant, exact.
2. Fallback: Gemini reads the first pages (cover + TOC) and proposes
   chapter titles; we locate each chapter's real PDF page by searching
   for its heading text, calibrating printed-page offset as backup.

All page numbers in this module are 0-based PDF page indexes unless a
variable is explicitly named ``printed_page``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import pymupdf4llm

logger = logging.getLogger("chapter-splitter")

GEMINI_MODEL = "gemini-2.5-flash"
TOC_SCAN_PAGES = 20          # pages sent to Gemini in fallback mode
MIN_CHAPTERS = 2             # fewer than this -> detection considered failed
SCANNED_TEXT_THRESHOLD = 40  # avg chars/page below this -> "looks scanned"


class SplitError(Exception):
    """A user-facing failure with a clear message."""


@dataclass
class Chapter:
    title: str
    start: int  # inclusive, 0-based
    end: int    # exclusive


# ---------------------------------------------------------------- detection

def detect_chapters(doc: fitz.Document) -> tuple[list[Chapter], str]:
    """Return (chapters, method) where method is 'bookmarks' or 'ai'."""
    _reject_scanned(doc)
    chapters = _from_bookmarks(doc)
    if chapters:
        return chapters, "bookmarks"
    logger.info("no usable bookmarks; falling back to AI detection")
    return _from_gemini(doc), "ai"


def _reject_scanned(doc: fitz.Document) -> None:
    sample = range(0, doc.page_count, max(1, doc.page_count // 20))
    chars = [len(doc[p].get_text()) for p in sample]
    if statistics.mean(chars) < SCANNED_TEXT_THRESHOLD:
        raise SplitError(
            "This PDF has little or no selectable text — it looks scanned. "
            "Only born-digital PDFs are supported right now."
        )


def _from_bookmarks(doc: fitz.Document) -> list[Chapter] | None:
    toc = doc.get_toc()
    if not toc:
        return None

    # Prefer level-1 entries; if there are too few (e.g. only "Part I/II"),
    # drop one level down — textbooks often nest chapters under parts.
    for level in (1, 2):
        starts = [
            (page - 1, title.strip())
            for lvl, title, page in toc
            if lvl == level and page > 0 and title.strip()
        ]
        if len(starts) >= 3 or (level == 1 and len(starts) >= MIN_CHAPTERS):
            break
    else:
        return None
    if len(starts) < MIN_CHAPTERS:
        return None

    # Sort and drop entries that share a page with the previous one.
    starts.sort(key=lambda s: s[0])
    deduped = [starts[0]]
    for page, title in starts[1:]:
        if page > deduped[-1][0]:
            deduped.append((page, title))
    if len(deduped) < MIN_CHAPTERS:
        return None
    return _build_ranges(deduped, doc.page_count)


def _build_ranges(starts: list[tuple[int, str]], page_count: int) -> list[Chapter]:
    chapters = []
    if starts[0][0] > 0:
        chapters.append(Chapter("Front Matter", 0, starts[0][0]))
    for i, (page, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else page_count
        if end > page:
            chapters.append(Chapter(title, page, end))
    return chapters


# ------------------------------------------------------------- AI fallback

def _from_gemini(doc: fitz.Document) -> list[Chapter]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SplitError(
            "This PDF has no bookmarks and GEMINI_API_KEY is not configured, "
            "so AI chapter detection is unavailable."
        )

    head_pages = min(TOC_SCAN_PAGES, doc.page_count)
    head_text = "\n".join(
        f"[PDF page {p + 1}]\n{doc[p].get_text()}" for p in range(head_pages)
    )

    from google import genai
    from google.genai import errors as genai_errors

    client = genai.Client(api_key=api_key)
    prompt = (
        "Below are the first pages of a book PDF, including its table of "
        "contents. List the book's top-level chapters in reading order. "
        "Use the chapter titles as printed in the table of contents, and "
        "the page number printed next to each (the book's own page "
        "numbering, not the PDF page markers). Include only real chapters "
        "(and named parts only if the book has no chapters) — skip the "
        "preface, index, appendices listed as back matter, and the table "
        "of contents itself.\n\n"
        'Respond with JSON: {"chapters": [{"title": str, "printed_page": int}]}\n\n'
        + head_text
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
    except genai_errors.APIError as exc:
        raise SplitError(
            "The AI service is temporarily unavailable (high demand). "
            "Please try again in a few minutes."
        ) from exc
    try:
        guesses = json.loads(response.text)["chapters"]
        guesses = [
            (str(g["title"]).strip(), int(g["printed_page"]))
            for g in guesses
            if str(g.get("title", "")).strip()
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SplitError(
            "AI chapter detection returned an unusable answer for this PDF."
        ) from exc
    if len(guesses) < MIN_CHAPTERS:
        raise SplitError(
            "AI chapter detection could not find a chapter structure in this PDF."
        )

    starts = _locate_chapters(doc, guesses, search_from=head_pages)
    if len(starts) < MIN_CHAPTERS:
        raise SplitError(
            "Found a table of contents but could not locate the chapter "
            "start pages inside the PDF."
        )
    return _build_ranges(starts, doc.page_count)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _locate_chapters(
    doc: fitz.Document, guesses: list[tuple[str, int]], search_from: int
) -> list[tuple[int, str]]:
    """Map (title, printed_page) guesses to real PDF page indexes.

    Primary: search forward for a page whose text contains the chapter
    title. Backup: place unfound chapters using the median offset between
    printed and PDF page numbers observed on the found ones.
    """
    page_texts: dict[int, str] = {}

    def page_text(p: int) -> str:
        if p not in page_texts:
            page_texts[p] = _normalize(doc[p].get_text())
        return page_texts[p]

    def needle_for(title: str) -> str:
        # Headings often omit the TOC's numbering prefix ("3. Limits" -> "Limits").
        bare = _normalize(re.sub(r"^\s*(chapter\s+)?[\divxlc]+[.:)\s]+", "", title, flags=re.I))
        return bare if len(bare) >= 4 else _normalize(title)

    needles = [(title, printed, needle_for(title)) for title, printed in guesses]

    # Skip past the TOC itself: pages where several chapter titles appear
    # together are the table of contents, not chapter starts.
    toc_scan_end = min(search_from, doc.page_count)
    toc_pages = [
        p
        for p in range(toc_scan_end)
        if sum(1 for _, _, n in needles if n and n in page_text(p)) >= 2
    ]
    cursor = (max(toc_pages) + 1) if toc_pages else 0

    found: list[tuple[int, str, int]] = []  # (pdf_page, title, printed_page)
    for title, printed, needle in needles:
        if not needle:
            continue
        for p in range(cursor, doc.page_count):
            if needle in page_text(p):
                found.append((p, title, printed))
                cursor = p + 1
                break

    if len(found) >= MIN_CHAPTERS:
        offsets = [pdf - (printed - 1) for pdf, _, printed in found]
        offset = round(statistics.median(offsets))
        located = {title: pdf for pdf, title, _ in found}
        starts = []
        for title, printed in guesses:
            page = located.get(title, printed - 1 + offset)
            if 0 <= page < doc.page_count:
                starts.append((page, title))
        starts.sort(key=lambda s: s[0])
        # Drop out-of-order/duplicate placements from the offset backup.
        deduped = [starts[0]]
        for page, title in starts[1:]:
            if page > deduped[-1][0]:
                deduped.append((page, title))
        return deduped

    return [(p, title) for p, title, _ in found]


# ----------------------------------------------------------------- output

def _safe_filename(title: str, max_len: int = 60) -> str:
    safe = re.sub(r"[^\w\s.,()'-]", "", title).strip()
    safe = re.sub(r"\s+", " ", safe)
    return safe[:max_len].strip() or "Untitled"


def split_book(
    pdf_path: Path,
    out_dir: Path,
    on_progress=lambda stage: None,
) -> dict:
    """Split ``pdf_path`` into per-chapter PDFs + Markdown in ``out_dir``.

    Returns a manifest dict: {"method": ..., "chapters": [{title, start_page,
    end_page, pdf, md}]} with 1-based page numbers for display.
    """
    doc = fitz.open(pdf_path)
    try:
        if doc.needs_pass:
            raise SplitError("This PDF is password-protected — please decrypt it first.")
        if doc.page_count < 2:
            raise SplitError("This PDF has fewer than 2 pages — nothing to split.")

        on_progress("Detecting chapters…")
        chapters, method = detect_chapters(doc)

        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"method": method, "chapters": []}
        for i, ch in enumerate(chapters):
            on_progress(f"Writing chapter {i + 1} of {len(chapters)}: {ch.title}")
            stem = f"{i:02d} - {_safe_filename(ch.title)}"

            part = fitz.open()
            part.insert_pdf(doc, from_page=ch.start, to_page=ch.end - 1)
            part.save(out_dir / f"{stem}.pdf")
            part.close()

            md = pymupdf4llm.to_markdown(
                doc, pages=list(range(ch.start, ch.end)), show_progress=False
            )
            (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")

            manifest["chapters"].append(
                {
                    "title": ch.title,
                    "start_page": ch.start + 1,
                    "end_page": ch.end,
                    "pdf": f"{stem}.pdf",
                    "md": f"{stem}.md",
                }
            )
        return manifest
    finally:
        doc.close()

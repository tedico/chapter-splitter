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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import pymupdf4llm

logger = logging.getLogger("chapter-splitter")

GEMINI_MODEL = "gemini-2.5-flash"
TOC_SCAN_PAGES = 20          # pages sent to Gemini in fallback mode
MIN_CHAPTERS = 2             # fewer than this -> detection considered failed
SCANNED_TEXT_THRESHOLD = 40  # avg chars/page below this -> "looks scanned"
TEXTLESS_PAGE_CHARS = 50     # page text at/below this -> no usable text layer
MIN_OCR_PAGES = 3            # OCR only when at least this many pages need it
OCR_TIMEOUT_S = 3 * 60 * 60
OFFSET_DRIFT_TOLERANCE = 5   # pages a heading match may deviate from the
                             # printed-page offset before it's a false match
OCR_FILENAME = "_ocr.pdf"
SCAN_IMAGE_COVERAGE = 0.5    # image covering this much of a page -> scan page
SCAN_CHAPTER_FRACTION = 0.6  # chapter mostly scan pages -> use text layer


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
            "This PDF has little or no selectable text, even after OCR — "
            "it may be a very low-quality scan."
        )


def _page_ranges(pages: list[int]) -> str:
    """Compress 0-based page indexes into a 1-based ocrmypdf --pages arg."""
    runs: list[list[int]] = []
    for p in pages:
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ",".join(f"{a+1}" if a == b else f"{a+1}-{b+1}" for a, b in runs)


def _textless_pages(doc: fitz.Document) -> list[int]:
    return [
        p for p in range(doc.page_count)
        if len(doc[p].get_text().strip()) <= TEXTLESS_PAGE_CHARS
    ]


def ensure_text_layer(
    pdf_path: Path, work_dir: Path, on_progress, redo_ocr: bool = False
) -> Path:
    """Give scanned pages a text layer so the rest of the pipeline works.

    Default mode OCRs only the pages that have no text; existing text
    layers (and born-digital pages) are left untouched. A handful of
    textless pages (blanks, photo plates) is normal and not worth a pass.

    ``redo_ocr`` additionally replaces existing *invisible* OCR layers on
    scan pages with a fresh Tesseract pass (ocrmypdf --redo-ocr) — old
    scans often carry decades-old OCR that modern Tesseract beats easily.
    Born-digital visible text is never touched, and books with no scan
    pages skip the pass entirely.

    Returns ``pdf_path`` unchanged or the path of the OCR'd copy in
    ``work_dir``.
    """
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        missing = _textless_pages(doc)
        sample = list(range(0, page_count, max(1, page_count // 30)))
        scan_frac = sum(_is_scan_page(doc[p]) for p in sample) / len(sample)

    # A few full-page images are just plates in a born-digital book; redo
    # mode only makes sense when the book is substantially a scan. Anything
    # else falls through to the default targeted-OCR logic.
    if redo_ocr and scan_frac >= 0.3:
        mode_args = ["--redo-ocr"]
        stage = (
            f"Re-running OCR on all {page_count} pages — roughly 3s per page, "
            "so a big book can take half an hour. Safe to close this page; "
            "the job keeps running…"
        )
    elif len(missing) >= max(MIN_OCR_PAGES, page_count // 50):
        # Name the pages explicitly: --skip-text would skip any page
        # containing even a stray fragment of text (a page number, a
        # running header), which is exactly what half-OCR'd scans have
        # on their unreadable pages.
        mode_args = ["--force-ocr", "--pages", _page_ranges(missing)]
        stage = f"Running OCR on {len(missing)} scanned pages (of {page_count}) — this can take several minutes…"
    else:
        return pdf_path

    on_progress(stage)
    work_dir.mkdir(parents=True, exist_ok=True)
    ocr_path = work_dir / OCR_FILENAME
    cmd = [
        sys.executable, "-m", "ocrmypdf",
        *mode_args,
        "--output-type", "pdf", "--optimize", "0", "--quiet",
        str(pdf_path), str(ocr_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=OCR_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise SplitError(
            "OCR timed out on this PDF — it may be unusually large or complex."
        ) from exc
    except subprocess.CalledProcessError as exc:
        logger.error(
            "ocrmypdf failed (rc=%s): %s",
            exc.returncode, exc.stderr.decode(errors="replace")[-2000:],
        )
        raise SplitError(
            "This PDF contains scanned pages and OCR failed on it. "
            "The failure has been logged."
        ) from exc
    logger.info("OCR added a text layer to %d pages", len(missing))
    return ocr_path


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
        # A heading text match can be a false positive — books often repeat
        # chapter titles in an introduction. True starts agree with the
        # printed-page offset; outliers get re-placed by the offset instead.
        located = {
            title: pdf
            for pdf, title, printed in found
            if abs(pdf - (printed - 1) - offset) <= OFFSET_DRIFT_TOLERANCE
        }
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

def _is_scan_page(page: fitz.Page) -> bool:
    area = abs(page.rect)
    if not area:
        return False
    covered = 0.0
    for info in page.get_image_info():
        covered += abs(fitz.Rect(info["bbox"]) & page.rect)
        if covered / area >= SCAN_IMAGE_COVERAGE:
            return True
    return False


def _is_scan_chapter(doc: fitz.Document, ch: Chapter) -> bool:
    pages = range(ch.start, ch.end)
    scan = sum(1 for p in pages if _is_scan_page(doc[p]))
    return scan >= SCAN_CHAPTER_FRACTION * len(pages)


_MD_PLACEHOLDER = re.compile(r"\*\*==>.*?<==\*\*")


def _alnum_len(text: str) -> int:
    return sum(c.isalnum() for c in text)


def _chapter_plain_text(doc: fitz.Document, ch: Chapter) -> str:
    return "\n".join(
        doc[p].get_text("text").strip() for p in range(ch.start, ch.end)
    )


def _markdown_is_degenerate(md: str, plain_text: str) -> bool:
    """True when pymupdf4llm produced placeholders instead of the page text.

    Happens on scanned pages: OCR text layers are invisible (alpha 0), and
    pymupdf4llm drops invisible spans, leaving only picture placeholders —
    while plain get_text() still reads them.
    """
    plain = _alnum_len(plain_text)
    if plain < 500:  # near-empty pages; structured output is fine
        return False
    return _alnum_len(_MD_PLACEHOLDER.sub("", md)) < 0.3 * plain


def _safe_filename(title: str, max_len: int = 60) -> str:
    safe = re.sub(r"[^\w\s.,()'-]", "", title).strip()
    safe = re.sub(r"\s+", " ", safe)
    return safe[:max_len].strip() or "Untitled"


def split_book(
    pdf_path: Path,
    out_dir: Path,
    on_progress=lambda stage: None,
    redo_ocr: bool = False,
) -> dict:
    """Split ``pdf_path`` into per-chapter PDFs + Markdown in ``out_dir``.

    Returns a manifest dict: {"method": ..., "chapters": [{title, start_page,
    end_page, pdf, md}]} with 1-based page numbers for display.
    """
    with fitz.open(pdf_path) as probe:
        if probe.needs_pass:
            raise SplitError("This PDF is password-protected — please decrypt it first.")
        if probe.page_count < 2:
            raise SplitError("This PDF has fewer than 2 pages — nothing to split.")

    src_path = ensure_text_layer(pdf_path, out_dir, on_progress, redo_ocr=redo_ocr)
    doc = fitz.open(src_path)
    try:
        on_progress("Detecting chapters…")
        chapters, method = detect_chapters(doc)

        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"method": method, "ocr": src_path != pdf_path, "chapters": []}
        for i, ch in enumerate(chapters):
            on_progress(f"Writing chapter {i + 1} of {len(chapters)}: {ch.title}")
            stem = f"{i:02d} - {_safe_filename(ch.title)}"

            part = fitz.open()
            part.insert_pdf(doc, from_page=ch.start, to_page=ch.end - 1)
            part.save(out_dir / f"{stem}.pdf")
            part.close()

            plain_text = _chapter_plain_text(doc, ch)
            if _is_scan_chapter(doc, ch):
                # pymupdf4llm mishandles scan pages: it drops the invisible
                # OCR text layer and slowly re-OCRs the page images with
                # scrambled layout. The text layer is the honest output.
                md = f"# {ch.title}\n\n{plain_text}\n"
                md_source = "text-layer"
            else:
                md = pymupdf4llm.to_markdown(
                    doc, pages=list(range(ch.start, ch.end)), show_progress=False
                )
                md_source = "structured"
                if _markdown_is_degenerate(md, plain_text):
                    md = f"# {ch.title}\n\n{plain_text}\n"
                    md_source = "text-layer"
            (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")

            manifest["chapters"].append(
                {
                    "title": ch.title,
                    "start_page": ch.start + 1,
                    "end_page": ch.end,
                    "pdf": f"{stem}.pdf",
                    "md": f"{stem}.md",
                    "md_source": md_source,
                }
            )
        return manifest
    finally:
        doc.close()

"""Build test PDFs: bookmarked, printed-TOC-only, and a scanned (image-only)
rasterization of the latter for exercising the OCR path."""
import fitz

CHAPTERS = [
    ("Introduction to Limits", 4),
    ("Derivatives and Their Applications", 9),
    ("The Fundamental Theorem of Calculus", 15),
    ("Sequences and Series", 21),
]
BACK_MATTER = ("Notes", 25)
PAGES = 28
BODY = ("The quick brown fox jumps over the lazy dog. " * 6 + "\n") * 5


def build(path: str, with_bookmarks: bool) -> None:
    doc = fitz.open()
    for _ in range(PAGES):
        doc.new_page()

    doc[0].insert_text((72, 100), "Calculus: A Test Fixture", fontsize=24)
    doc[1].insert_text((72, 80), "Contents", fontsize=20)
    y = 130
    for i, (title, page) in enumerate(CHAPTERS, 1):
        # Printed page numbers: body numbering starts at PDF page 4 -> printed 1
        doc[1].insert_text((72, y), f"{i}. {title}", fontsize=12)
        doc[1].insert_text((480, y), str(page - 3), fontsize=12)
        y += 24
    doc[1].insert_text((72, y), BACK_MATTER[0], fontsize=12)
    doc[1].insert_text((480, y), str(BACK_MATTER[1] - 3), fontsize=12)
    # Realistic density below the TOC: a sparse page makes Tesseract's
    # auto-segmentation (used when the scanned variant is OCR'd) drop lines.
    doc[1].insert_textbox(fitz.Rect(72, y + 40, 540, 750), BODY, fontsize=10)
    doc[2].insert_text((72, 100), "Preface\n\nThis is the preface.", fontsize=12)

    for idx in range(3, PAGES):
        page = doc[idx]
        starting = [(i, t) for i, (t, p) in enumerate(CHAPTERS, 1) if p == idx]
        y = 90
        if starting:
            i, title = starting[0]
            page.insert_text((72, y), f"Chapter {i}", fontsize=16)
            page.insert_text((72, y + 30), title, fontsize=20)
            y += 80
        elif idx == BACK_MATTER[1]:
            page.insert_text((72, y), BACK_MATTER[0], fontsize=20)
            y += 50
        page.insert_textbox(fitz.Rect(72, y, 540, 750), BODY, fontsize=10)
        page.insert_text((300, 770), str(idx - 2), fontsize=9)  # printed page no.

    if with_bookmarks:
        doc.set_toc([[1, t, p + 1] for t, p in CHAPTERS])
    doc.save(path)
    doc.close()
    print("wrote", path)


def rasterize(src_path: str, dst_path: str) -> None:
    """Turn a fixture into a pure image scan: no text layer, no bookmarks."""
    src = fitz.open(src_path)
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=300)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    out.save(dst_path, deflate=True)
    print("wrote", dst_path)


build("/tmp/fixture-bookmarked.pdf", True)
build("/tmp/fixture-plain.pdf", False)
rasterize("/tmp/fixture-plain.pdf", "/tmp/fixture-scanned.pdf")

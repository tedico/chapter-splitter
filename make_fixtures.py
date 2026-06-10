"""Build test PDFs: one with bookmarks, one with only a printed TOC."""
import fitz

CHAPTERS = [
    ("Introduction to Limits", 4),
    ("Derivatives and Their Applications", 9),
    ("The Fundamental Theorem of Calculus", 15),
    ("Sequences and Series", 21),
]
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
        doc[1].insert_text((72, y), f"{i}. {title} .......... {page - 3}", fontsize=12)
        y += 24
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
        page.insert_textbox(fitz.Rect(72, y, 540, 750), BODY, fontsize=10)
        page.insert_text((300, 770), str(idx - 2), fontsize=9)  # printed page no.

    if with_bookmarks:
        doc.set_toc([[1, t, p + 1] for t, p in CHAPTERS])
    doc.save(path)
    doc.close()
    print("wrote", path)


build("/tmp/fixture-bookmarked.pdf", True)
build("/tmp/fixture-plain.pdf", False)

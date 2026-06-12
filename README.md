# Chapter Splitter

Upload a book or textbook PDF through a web page; get back **one PDF and one
Markdown file per chapter**, individually or as a single ZIP.

## Why this exists

I built this because I kept wanting to work through textbooks with an LLM —
and feeding it the whole book is the wrong move. **A chapter is the right
unit of context.** Here's the reasoning:

- **It fits.** A 500-page textbook is roughly 300–500k tokens — bigger than
  most context windows. A chapter almost always fits, with room to spare for
  your actual conversation.
- **It's cheaper.** You pay per input token, and in a chat the attached
  context is re-sent with every message. Ask ten questions about Chapter 3
  with the whole book attached and you've paid for the other 480 pages ten
  times over.
- **It's more accurate.** Even models that *can* hold an entire book get
  measurably worse at recalling specifics as context grows — the relevant
  details get diluted across hundreds of pages of unrelated material. A
  chapter is a semantically coherent unit: nearly every token in context is
  actually about your topic. This is the same logic behind why RAG systems
  chunk documents at semantic boundaries.

Chapters also beat fixed-size chunking (every N pages or tokens) because the
split points are the ones the author chose — an explanation never gets cut
in half.

So the workflow is: **split the book once, then attach just the chapter
you're working with.** The Markdown output is ideal for pasting straight
into a prompt; the per-chapter PDF is ideal for apps that take file
attachments.

### When a single chapter isn't enough

Honest caveats: questions that synthesize across the book ("how does the
argument develop?") genuinely need multiple chapters — attach several, or
fall back to a long-context model with the full book. And for some dense
textbooks, a *section* would be tighter still; chapters are simply the best
default boundary.

## Using it

1. Open the app and drop in a book PDF (up to 200 MB) — born-digital or
   scanned; scanned pages get an OCR text layer automatically.
2. Wait while it detects chapters and splits — progress is shown live.
3. Download what you need: per-chapter **PDF**, per-chapter **MD**, or
   everything as one ZIP. Files are named `01 - Chapter Title.pdf` etc.

Password-protected PDFs must be decrypted first.

### Scanned books

Books come in three flavors, and all three work:

- **Born-digital** — real selectable text. The fast path.
- **Image-only scans** — every page is a picture. The app detects the
  textless pages and runs [OCRmyPDF](https://ocrmypdf.readthedocs.io/)
  (Tesseract) on exactly those pages before splitting. Expect a few extra
  minutes for a full book.
- **Searchable scans / hybrids** — scans with an existing (often partial)
  invisible OCR text layer. Pages that already have text are kept as-is;
  only the truly textless ones are OCR'd (`--force-ocr --pages`, with the
  page list computed by the app — ocrmypdf's own `--skip-text` would skip
  any page carrying even a stray header fragment).

**Re-OCR toggle:** old scans often carry decades-old OCR layers full of
typos (`Cayley-Hanulton`, `DIAGOI\JALIU,TIOI\I`). Checking *"Re-OCR scanned
pages"* replaces invisible OCR layers with a fresh Tesseract pass
(`--redo-ocr`) — visible born-digital text is never touched, and books that
aren't substantially scans skip the pass automatically. Budget ~3s/page:
a 631-page textbook took ~31 minutes, and produced `Cayley-Hamilton` and
`DIAGONALIZATION` where the original layer had garbage. The job runs in the
background; closing the page doesn't cancel it.

**AI Markdown toggle:** for the best Markdown from scans, *"AI Markdown for
scanned pages"* sends each scan chapter's page images to Gemini in batches
of 8 and gets back real structure — section headings, paragraphs, tables,
and TeX math — instead of raw OCR text. Costs Gemini credits (well under $1
for a full book with `gemini-2.5-flash`). Any batch that fails after three
attempts silently falls back to the text layer for that chapter, so API
weather can degrade quality but never fail a job. The manifest records
which chapters used it (`md_source: "ai-vision"`).

**OCR language:** Tesseract defaults to English; set the `OCR_LANGUAGES`
environment variable (e.g. `eng+Devanagari`) for books in other scripts —
the corresponding `tesseract-ocr-script-*` package must be installed.

For scan pages, Markdown extraction (without the AI toggle) uses the text
layer directly instead of pymupdf4llm: OCR text layers are *invisible* (alpha 0), which pymupdf4llm
drops — it would otherwise slowly re-OCR each page image with scrambled
layout. The manifest records which path produced each chapter
(`md_source: "structured" | "text-layer"`). Scanned chapters therefore give
plain text rather than structured Markdown — honest output beats pretty
garbage.

## How it works

```
upload.pdf ──► detect chapters ──► split PDFs ──► extract Markdown ──► ZIP
                  │
                  ├─ 1. embedded PDF bookmarks (free, exact, no AI)
                  └─ 2. fallback: Gemini reads the first ~20 pages (the
                       table of contents), proposes chapter titles; start
                       pages are located by searching for the headings,
                       calibrated by printed-page offset
```

Two guards keep the AI fallback honest. Heading matches that disagree with
the median printed-page offset by more than a few pages are treated as false
positives (books love repeating chapter titles in their introductions) and
re-placed by the offset instead. And Gemini also reports where the back
matter begins (notes, bibliography, index), so the final chapter ends there
instead of swallowing the rest of the book — the back matter comes out as
its own labeled chapter.

- **Backend:** FastAPI ([main.py](main.py)) + PyMuPDF ([splitter.py](splitter.py))
- **Frontend:** a single static page ([static/index.html](static/index.html))
  that uploads, polls `GET /jobs/{id}`, and renders download links
- **Jobs:** each upload gets `jobs/<uuid>/` with the source PDF, the chapter
  files, the ZIP, and a `status.json` (results survive restarts). Jobs older
  than 7 days are purged on startup.
- **AI fallback model:** `gemini-2.5-flash` via `google-genai`. Only called
  when a PDF has no usable bookmarks — most published textbooks have them,
  so most splits never touch an LLM.

## API

| Route | What it does |
| --- | --- |
| `POST /upload` (multipart `file`) | Validates (PDF magic bytes, ≤200 MB), queues a job, returns `{job_id}` |
| `GET /jobs/{id}` | `{status: queued\|working\|done\|error, stage?, error?, chapters?}` |
| `GET /jobs/{id}/files/{name}` | Download one chapter file (manifest-checked names only) |
| `GET /jobs/{id}/zip` | Download everything as `<book>-chapters.zip` |
| `GET /` | The UI |

## Self-hosting

```bash
git clone https://github.com/tedico/chapter-splitter && cd chapter-splitter
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'GEMINI_API_KEY=your-key-here' > .env   # only needed for bookmark-less PDFs
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8765
```

Then open `http://localhost:8765`. To try it without a real book,
`make_fixtures.py` generates small test PDFs (one with bookmarks, one with
only a printed TOC).

## Error handling

No silent failures:

- Expected problems (OCR failure, encrypted PDF, no chapter structure found,
  AI service overloaded) surface as readable messages in the UI.
- Unexpected crashes are logged to `app.log` and trigger an alert (SMS via
  the [Zo](https://zo.computer) CLI in my deployment — swap `alert_sms()` in
  [main.py](main.py) for whatever channel you use).

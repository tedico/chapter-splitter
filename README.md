# Chapter Splitter

Upload a book/textbook PDF through a web page; get back one **PDF** and one
**Markdown** file per chapter, individually or as a single ZIP.

Built for born-digital PDFs (selectable text). Scanned PDFs are rejected
with a clear message — no OCR support yet.

## How it works

```
upload.pdf ──► detect chapters ──► split PDFs ──► extract Markdown ──► ZIP
                  │
                  ├─ 1. embedded PDF bookmarks (free, exact)
                  └─ 2. fallback: Gemini reads the first ~20 pages (TOC),
                       proposes chapters; start pages located by searching
                       for headings, calibrated by printed-page offset
```

- **Backend:** FastAPI ([main.py](main.py)) + PyMuPDF ([splitter.py](splitter.py))
- **Frontend:** single static page ([static/index.html](static/index.html)) that
  uploads, polls `GET /jobs/{id}`, and renders download links
- **Jobs:** each upload gets `jobs/<uuid>/` with `upload.pdf`, `chapters/`,
  `chapters.zip`, and `status.json` (results survive restarts). Jobs older
  than 7 days are purged on startup.
- **AI fallback model:** `gemini-2.5-flash` via `google-genai`, key in `.env`
  (`GEMINI_API_KEY=...`). Only called when a PDF has no usable bookmarks.

## API

| Route | What it does |
| --- | --- |
| `POST /upload` (multipart `file`) | Validates (PDF magic bytes, ≤200 MB), queues job, returns `{job_id}` |
| `GET /jobs/{id}` | `{status: queued\|working\|done\|error, stage?, error?, chapters?}` |
| `GET /jobs/{id}/files/{name}` | Download one chapter file (manifest-checked names only) |
| `GET /jobs/{id}/zip` | Download everything as `<book>-chapters.zip` |
| `GET /` | The UI |

## Error handling

No silent failures:
- Expected problems (scanned PDF, encrypted PDF, no chapter structure found)
  surface as readable messages in the UI.
- Unexpected crashes are logged to `app.log` **and** trigger an SMS alert
  via the `zo` CLI.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'GEMINI_API_KEY=...' > .env
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8765
```

On Zo it runs as a registered user service (label `chapter-splitter`,
port 8765), which gives it a stable URL and restarts it automatically.

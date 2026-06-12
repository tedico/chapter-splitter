"""Chapter Splitter — upload a book PDF, download per-chapter PDFs + Markdown.

Run: .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8765

Jobs are processed in a background thread. Each job lives in jobs/<id>/ with
a status.json so results survive a restart. Jobs older than RETENTION_DAYS
are purged on startup. Unhandled pipeline failures are logged and trigger an
SMS alert via the `zo` CLI (no silent failures).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from splitter import SplitError, split_book

APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = APP_DIR / "jobs"
LOG_FILE = APP_DIR / "app.log"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
RETENTION_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("chapter-splitter")


def _load_dotenv() -> None:
    env_file = APP_DIR / ".env"
    if not env_file.exists():
        return
    import os

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def alert_sms(message: str) -> None:
    """Fire-and-forget SMS via the Zo agent CLI; never raises."""
    try:
        subprocess.Popen(
            ["zo", f"Send me an SMS: [chapter-splitter] {message[:300]}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("failed to dispatch SMS alert")


# ------------------------------------------------------------ job storage

def _status_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "status.json"


def _write_status(job_id: str, data: dict) -> None:
    path = _status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _read_status(job_id: str) -> dict | None:
    # Job ids are uuid4 hex; reject anything else before touching the fs.
    if not (len(job_id) == 32 and job_id.isalnum()):
        return None
    path = _status_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _purge_old_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for job_dir in JOBS_DIR.iterdir():
        if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.info("purged old job %s", job_dir.name)


# --------------------------------------------------------------- pipeline

executor = ThreadPoolExecutor(max_workers=1)


def _process_job(job_id: str, filename: str, redo_ocr: bool) -> None:
    job_dir = JOBS_DIR / job_id
    pdf_path = job_dir / "upload.pdf"
    out_dir = job_dir / "chapters"

    def progress(stage: str) -> None:
        _write_status(job_id, {"status": "working", "stage": stage, "filename": filename})

    try:
        progress("Opening PDF…")
        manifest = split_book(pdf_path, out_dir, on_progress=progress, redo_ocr=redo_ocr)

        progress("Packaging ZIP…")
        zip_path = job_dir / "chapters.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for ch in manifest["chapters"]:
                zf.write(out_dir / ch["pdf"], ch["pdf"])
                zf.write(out_dir / ch["md"], ch["md"])

        _write_status(
            job_id,
            {"status": "done", "filename": filename, **manifest},
        )
        logger.info(
            "job %s done: %d chapters via %s (%s)",
            job_id, len(manifest["chapters"]), manifest["method"], filename,
        )
    except SplitError as exc:
        logger.warning("job %s failed (user-facing): %s", job_id, exc)
        _write_status(job_id, {"status": "error", "filename": filename, "error": str(exc)})
    except Exception as exc:
        logger.error("job %s crashed: %s\n%s", job_id, exc, traceback.format_exc())
        _write_status(
            job_id,
            {
                "status": "error",
                "filename": filename,
                "error": "Unexpected server error — it has been logged and Ted alerted.",
            },
        )
        alert_sms(f"job failed on '{filename}': {type(exc).__name__}: {exc}")


# -------------------------------------------------------------------- app

app = FastAPI(title="Chapter Splitter")
_purge_old_jobs()


@app.post("/upload")
async def upload(file: UploadFile, redo_ocr: bool = Form(False)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 200 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That doesn't look like a PDF file.")

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "upload.pdf").write_bytes(data)

    filename = Path(file.filename or "book.pdf").name
    _write_status(job_id, {"status": "queued", "filename": filename})
    executor.submit(_process_job, job_id, filename, redo_ocr)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    status = _read_status(job_id)
    if status is None:
        raise HTTPException(404, "Unknown job.")
    return status


def _manifest_file(job_id: str, name: str) -> Path:
    """Serve only files listed in the job's manifest — no path tricks."""
    status = _read_status(job_id)
    if status is None:
        raise HTTPException(404, "Unknown job.")
    if status.get("status") != "done":
        raise HTTPException(409, "Job is not finished.")
    allowed = {ch["pdf"] for ch in status["chapters"]} | {ch["md"] for ch in status["chapters"]}
    if name not in allowed:
        raise HTTPException(404, "No such file in this job.")
    return JOBS_DIR / job_id / "chapters" / name


@app.get("/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str):
    path = _manifest_file(job_id, name)
    return FileResponse(path, filename=name)


@app.get("/jobs/{job_id}/zip")
def job_zip(job_id: str):
    status = _read_status(job_id)
    if status is None or status.get("status") != "done":
        raise HTTPException(404, "Job not found or not finished.")
    stem = Path(status.get("filename", "book")).stem
    return FileResponse(JOBS_DIR / job_id / "chapters.zip", filename=f"{stem}-chapters.zip")


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")

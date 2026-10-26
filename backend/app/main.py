import json
import logging
import os

from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import convert, photo
from .processor import DetectionError, process_pdf, tesseract_available

logger = logging.getLogger("cv-privacy")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "15"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Headers the browser is allowed to read. Without `expose_headers` the browser
# hides X-Output-Filename / X-CV-Code from JavaScript, which is what made the
# downloaded file lose its name and forced people to "print to PDF".
EXPOSED_HEADERS = ["Content-Disposition", "X-CV-Code", "X-Output-Filename", "X-Report", "X-Editable"]
MAX_FILES = 10

app = FastAPI(title="CV Privacy & Watermark API", version="5.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=EXPOSED_HEADERS,
)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": app.version,
        "ocr": tesseract_available(),
        "photo_detector": photo.detector_name(),
        "word_upload": convert.office_available(),
        "word_export": convert.docx_export_available(),
        "formats": {"in": ["pdf", "jpg", "png", "webp", "docx", "doc"], "out": ["pdf", "docx", "png", "jpg"]},
    }


def _err(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})


@app.post("/api/process-cv")
async def process_cv(
    file: Optional[UploadFile] = File(None),
    files: List[UploadFile] = File(default=[]),
    name_override: str = Form(""),
    phone_override: str = Form(""),
):
    """Accepts a PDF, a Word file, or one/several images (pages of one CV)."""
    uploads = ([file] if file is not None else []) + list(files or [])
    uploads = [u for u in uploads if u is not None and u.filename]
    if not uploads:
        return _err(400, "EMPTY_FILE", "Please upload a CV (PDF, Word or image).")
    if len(uploads) > MAX_FILES:
        return _err(400, "TOO_MANY_FILES", f"Please upload at most {MAX_FILES} files.")

    blobs = []
    for u in uploads:
        data = await u.read()
        if not data:
            return _err(400, "EMPTY_FILE", f"{u.filename} is empty.")
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            return _err(413, "TOO_LARGE", f"{u.filename} is larger than {MAX_UPLOAD_MB} MB.")
        blobs.append((u.filename, data, convert.detect_kind(u.filename, data)))

    kinds = {k for _, _, k in blobs}
    if "unknown" in kinds:
        return _err(400, "UNSUPPORTED", "Unsupported file. Please upload a PDF, Word (DOCX) or image (JPG/PNG).")
    if kinds - {"image"} and len(blobs) > 1:
        return _err(400, "MIXED_FILES", "Upload a single PDF/Word file, or several images of the same CV.")

    source = "image" if kinds == {"image"} else next(iter(kinds))
    name = blobs[0][0]
    try:
        if source == "image":
            pdf = await run_in_threadpool(convert.images_to_pdf, [b for _, b, _ in blobs])
        elif source == "word":
            pdf = await run_in_threadpool(convert.word_to_pdf, blobs[0][1], name)
        else:
            pdf = blobs[0][1]
        result = await run_in_threadpool(process_pdf, pdf, name, name_override[:120], phone_override[:40])
    except DetectionError as exc:
        logger.warning("Detection failed: code=%s missing=%s file=%s source=%s", exc.code, exc.missing, name, source)
        return JSONResponse(status_code=422, content={"detail": exc.as_detail()})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": {"code": "INVALID", "message": str(exc)}})
    except Exception:
        logger.exception("CV processing failed")
        return _err(500, "SERVER_ERROR", "CV processing failed unexpectedly. Please try again.")

    result.report["source"] = source
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"',
        "X-CV-Code": result.cv_code,
        "X-Output-Filename": result.filename,
        "X-Report": json.dumps(result.report, ensure_ascii=True, separators=(",", ":")),
        "Cache-Control": "no-store",
    }
    return Response(content=result.pdf_bytes, media_type="application/pdf", headers=headers)


@app.post("/api/export")
async def export_cv(
    file: UploadFile = File(...),
    format: str = Form("docx"),
    base_name: str = Form("CV"),
    cv_code: str = Form(""),
):
    """Convert an already-protected PDF into an editable Word file or images.
    (Stateless: the browser sends back the PDF it received from /api/process-cv.)"""
    fmt = format.lower().strip()
    if fmt not in ("docx", "png", "jpg", "jpeg"):
        return _err(400, "UNSUPPORTED", "Export format must be docx, png or jpg.")
    data = await file.read()
    if not data[:1024].lstrip().startswith(b"%PDF") or len(data) > MAX_UPLOAD_MB * 2 * 1024 * 1024:
        return _err(400, "NOT_A_PDF", "Export needs the protected PDF.")
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in base_name)[:60] or "CV"
    try:
        if fmt == "docx":
            content, editable = await run_in_threadpool(convert.pdf_to_docx, data, cv_code[:40])
            return Response(
                content=content,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f'attachment; filename="{base}.docx"',
                         "X-Output-Filename": f"{base}.docx", "X-Editable": "1" if editable else "0",
                         "Cache-Control": "no-store"},
            )
        content, fname, mime = await run_in_threadpool(convert.pdf_to_images, data, fmt, 170, base)
        return Response(content=content, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"',
                                 "X-Output-Filename": fname, "Cache-Control": "no-store"})
    except Exception:
        logger.exception("Export failed")
        return _err(500, "SERVER_ERROR", "Export failed. Please download the PDF instead.")

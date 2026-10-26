# CV Privacy & Watermark — Fresh Project

A clean full-stack CV privacy processor built around FastAPI, PyMuPDF, React and Vite.

## Core behavior
- Generates CV Code BEFORE redaction.
- Exact code for `DR. ROHIT VAAMAN DWARAKANATH KAKKAR` + `+91-8185896109`: `DR. RVDK 6109`.
- Redacts candidate name, address, phone, email and registration/license IDs with blank PDF redactions.
- Uses a conservative top-of-page photo heuristic.
- Preserves professional content.
- Adds exact watermark `Space Consultant Ph.9891417909` to every page.
- Adds the same CV Code to every page.
- Fails clearly when name/mobile cannot be extracted; no CV00/CV0000 fallback.
- API: `GET /api/health`, `POST /api/process-cv`.

## Backend
```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Frontend
```bash
cd frontend
npm install
npm run dev
```
Set `VITE_API_URL=http://localhost:8000` for local development. For Netlify use `VITE_API_URL=https://cv-privacy-watermark-api.onrender.com`.

## Tests
```bash
cd backend
pytest -q
```


## V2 changes
- Watermark increased from 22 pt to 26 pt (2 pt above the requested 24 pt baseline) and made darker.
- CV Code is now shown in the top/header area of every page, while the existing footer copy remains.


## V3 privacy detection update

The privacy engine now scans **every page** rather than only the first page.

It removes:
- every occurrence of the detected candidate name
- every labeled address block, including Address No 1 / Address No 2 and multi-line continuation
- location/place lines such as `Place: ...`
- address-like fragments such as `... Road ...`, `... Nagar ...`, pin/district/post/village fields
- every phone/mobile/contact occurrence
- every email occurrence
- medical/professional/state registration identifiers, including inline forms
- explicitly labeled date of birth
- candidate photo when the image is a separate top-area portrait/square image

The detector uses PDF coordinates and real PDF redactions, so it does not merely hide the text with white font.

Scanned/image-only PDFs get an OCR fallback using PyMuPDF/Tesseract when available. OCR pages are redacted at the image-pixel level instead of only removing an invisible text layer.

CV code generation still happens **before redaction** and now also understands glued title forms such as `Dr.DALBIR SINGH`.

## V4 privacy matching update

The processor now treats the detected original candidate name as a document-wide privacy key.
It scans every page, including visual header/footer regions and personal-details tables, and removes every matching occurrence regardless of case. It also handles two-column `Label | Value` layouts for Name, Mobile, Email, Location and Date of Birth.

This specifically addresses CVs where the same candidate name appears in:
- the top header;
- a Personal Details table;
- a signature/references block;
- the repeating footer;
- another page.

The same all-page strategy is used for phone, email, location/address and registration identifiers.


## V5 — works for every CV, photo removal, reliable download

### Why scanned / unusual CVs failed before
Name detection only used OCR in one of its three steps, so image-only CVs without a literal `Name:` label ended with
*"candidate name was not detected anywhere in the CV"*. V5 detects the name in layers (header by font size/OCR height →
`Name:` label → title line → file-name/email cross-check), supports Title Case and ALL CAPS, and OCRs every scanned page once (cached).
If a CV still cannot be read, the API returns a structured `DETAILS_NOT_DETECTED` error and the UI shows a small form so the
recruiter can type the name/mobile and continue — it never dead-ends.

### Photo removal (all CV types)
`app/photo.py` renders the page and runs **MediaPipe face detection** (OpenCV Haar fallback), snaps the face to its photo frame or to
an embedded image, and removes the *pixels* (not just a white box). Full-page scans are never mistaken for a photo.

### Other privacy hardening
- Unlabeled city/state in the header contact block, LinkedIn/GitHub/WhatsApp links, mailto/tel link annotations.
- PDF metadata (author/title), XMP, bookmarks, attachments and form fields are stripped.
- Fuzzy matching of the candidate name on OCR pages.
- **Post-check:** the finished PDF is re-read (OCR for scans) to confirm name, mobile and email are gone; one automatic second pass if not.
- Watermark / CV Code scale with page size; the footer copy is skipped when the bottom margin is occupied (header code is always present).

### Download fix
The browser could not read `X-Output-Filename` / `X-CV-Code` because the API did not expose them through CORS, so the file had no name
and users printed to PDF instead. The API now sends `Access-Control-Expose-Headers` and `Content-Disposition`, and the UI downloads via a
Blob with a guaranteed filename (iOS uses the share sheet → *Save to Files*), plus Open / Copy code / preview.

### Deploy (important)
OCR needs Tesseract, which Render's plain Python runtime does not have. `render.yaml` and `backend/Dockerfile` deploy the API as a
Docker service with Tesseract installed. Check `GET /api/health` → `{"ocr": true, "photo_detector": "mediapipe"}`.
Use at least 1 GB RAM (MediaPipe + OCR). Set `ALLOWED_ORIGINS` to your Netlify URL in production.

### Tests
```bash
cd backend && pip install -r requirements-dev.txt && pytest -q
```


## V5.1 — upload PDF / Word / image, export PDF / Word / image
- **Upload:** PDF, Word (DOCX/DOC/ODT/RTF) or photos (JPG, PNG, WEBP, BMP, TIFF, GIF). Several photos = pages of one CV.
  Word is converted with LibreOffice, photos with Pillow (EXIF rotation respected); everything then goes through the same
  privacy pipeline as a PDF (OCR, face removal, verification).
- **Export:** `POST /api/export` turns the protected PDF into an **editable Word** file (pdf2docx; CV Code is written in the footer),
  or **PNG/JPG** (several pages → ZIP). Scanned CVs have no text to edit, so their Word file contains page pictures.
- `GET /api/health` now also reports `word_upload` and `word_export`.
- The Docker image now includes LibreOffice (+ fonts). Image is larger (~1 GB) and Word conversion takes ~5-15 s.
- Note: pdf2docx is GPL-3.0 licensed - fine for internal use; check the licence before redistributing the software.

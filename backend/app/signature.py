"""Handwritten-signature detection for CV pages (typed, Word-exported and scanned).

A signature is found in three ways, all pixel based so they work on every PDF type:

1. Embedded signature images: small, mostly-white image, thin dark strokes, near a signature
   keyword or in the lower part of the page (a photo has a busy background, a logo sits in the
   header, so neither matches).
2. Ink scribbles next to an anchor line ("Signature", "Signed", "Yours faithfully", "Declaration",
   "Sd/-"): connected stroke blobs in the neighbourhood that are *not* text and are not plain rules.
3. Drawn (vector) signatures are caught by the same blob logic because the page is rendered first.

Everything fails soft: without OpenCV/numpy the module reports no signatures.
"""
from __future__ import annotations

import logging
import re

import fitz

logger = logging.getLogger("cv-privacy")

try:  # pragma: no cover - import guard
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None

SIGN_ANCHOR_RE = re.compile(
    r"(?i)^\s*\(?\s*(?:candidate'?s?\s+|applicant'?s?\s+|doctor'?s?\s+|authori[sz]ed\s+)?"
    r"(?:signature|signed|signatory|sign\b|sd\s*/?\s*-?\s*$)"
)
CLOSING_RE = re.compile(r"(?i)^\s*(?:yours\s+(?:faithfully|sincerely|truly|obediently)|thanking\s+you|with\s+regards|sincerely)\b")
DECLARATION_RE = re.compile(r"(?i)\b(?:i\s+hereby\s+declare|declaration)\b")
TYPED_SIGN_ONLY_RE = re.compile(r"(?i)^\s*\(?\s*sd\s*/?\s*-?\s*\)?\s*$")

DPI = 100
ZOOM = DPI / 72.0


def available() -> bool:
    return cv2 is not None


def _gray(page: fitz.Page, clip: fitz.Rect):
    pix = page.get_pixmap(clip=clip, dpi=DPI, colorspace=fitz.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width), pix


def _embedded_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects = []
    for img in page.get_images(full=True):
        try:
            rects.extend(fitz.Rect(r) for r in page.get_image_rects(img[0]))
        except Exception:
            continue
    return rects


def anchors(lines: list[dict]) -> list[dict]:
    """Lines that announce a signature: {'rect', 'kind'}."""
    out = []
    for ln in lines:
        t = ln["text"].strip()
        if not t:
            continue
        if SIGN_ANCHOR_RE.match(t):
            out.append({"rect": fitz.Rect(ln["rect"]), "kind": "sign", "text": t})
        elif CLOSING_RE.match(t):
            out.append({"rect": fitz.Rect(ln["rect"]), "kind": "closing", "text": t})
        elif DECLARATION_RE.search(t) and len(t) < 400:
            out.append({"rect": fitz.Rect(ln["rect"]), "kind": "declaration", "text": t})
    return out


def _regions(page: fitz.Page, a: dict) -> list[fitz.Rect]:
    r, W, H = a["rect"], page.rect.width, page.rect.height
    if a["kind"] == "sign":
        regs = [
            fitz.Rect(r.x0 - 25, r.y0 - 75, min(W, r.x1 + 170), r.y0 - 0.5),       # scribble above the label
            fitz.Rect(r.x1 + 1, r.y0 - 30, min(W, r.x1 + 200), r.y1 + 12),         # "Signature: <scribble>"
            fitz.Rect(r.x0 - 25, r.y1 + 0.5, min(W, r.x1 + 170), r.y1 + 55),       # scribble below
        ]
    elif a["kind"] == "closing":
        regs = [fitz.Rect(r.x0 - 10, r.y1 + 0.5, min(W, r.x0 + 260), r.y1 + 85)]
    else:  # declaration paragraph: signature normally sits right under it, left or right aligned
        regs = [fitz.Rect(15, r.y1 + 0.5, W - 15, min(H, r.y1 + 120))]
    return [x & page.rect for x in regs if not (x & page.rect).is_empty]


def _blobs(page: fitz.Page, region: fitz.Rect, word_rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """Stroke-like ink blobs inside `region` that are not text and not plain horizontal rules."""
    if region.width < 10 or region.height < 8:
        return []
    arr, pix = _gray(page, region)
    mask = (arr < 150)
    # blank out text so letters never count as a signature
    for wr in word_rects:
        inter = wr & region
        if inter.is_empty:
            continue
        x0 = max(0, int((inter.x0 - region.x0) * ZOOM) - 2)
        x1 = min(mask.shape[1], int((inter.x1 - region.x0) * ZOOM) + 3)
        y0 = max(0, int((inter.y0 - region.y0) * ZOOM) - 2)
        y1 = min(mask.shape[0], int((inter.y1 - region.y0) * ZOOM) + 3)
        mask[y0:y1, x0:x1] = False
    if mask.sum() < 25:
        return []
    m8 = mask.astype(np.uint8)
    joined = cv2.dilate(m8, np.ones((7, 7), np.uint8))               # join the strokes of one signature
    n, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        ink = int(m8[y:y + h, x:x + w].sum())
        w_pt, h_pt = w / ZOOM, h / ZOOM
        fill = ink / max(w * h, 1)
        # a rule is thin; a table border / box is big and empty; a signature is a mid-sized sparse tangle
        if h_pt >= 8 and 18 <= w_pt <= 280 and h_pt <= 95 and ink >= 40 and fill <= 0.45:
            # boxes, table borders and rules are made of long straight runs; handwriting is curved
            sub = m8[y:y + h, x:x + w]
            hor = cv2.morphologyEx(sub, cv2.MORPH_OPEN, np.ones((1, 35), np.uint8))
            ver = cv2.morphologyEx(sub, cv2.MORPH_OPEN, np.ones((35, 1), np.uint8))
            straight = int(((hor > 0) | (ver > 0)).sum()) / max(ink, 1)
            if straight > 0.6:
                continue
            out.append(fitz.Rect(region.x0 + x / ZOOM, region.y0 + y / ZOOM,
                                 region.x0 + (x + w) / ZOOM, region.y0 + (y + h) / ZOOM))
    return out


def _signature_image(page: fitz.Page, rect: fitz.Rect, near_anchor: bool) -> bool:
    page_area = max(page.rect.get_area(), 1)
    if not (0.0008 <= rect.get_area() / page_area <= 0.08) or rect.height > 140 or rect.width > 320:
        return False
    if rect.y1 < page.rect.height * 0.12:                      # header logos
        return False
    arr, _ = _gray(page, rect & page.rect)
    if arr.size == 0:
        return False
    light = float((arr > 215).mean())
    dark = float((arr < 150).mean())
    if light < 0.78 or not (0.004 <= dark <= 0.22):             # photos / filled logos fail here
        return False
    lower = rect.y0 > page.rect.height * 0.55
    wide = rect.width / max(rect.height, 1) >= 1.3
    return near_anchor or (lower and wide)


def detect_signature_rects(page: fitz.Page, lines: list[dict], word_rects: list[fitz.Rect]):
    """Return (ink_rects, typed_rects): pixels to blank, and typed "Sd/-" marks whose text must go."""
    if not available():
        return [], []
    found: list[fitz.Rect] = []
    typed: list[fitz.Rect] = []
    try:
        anc = anchors(lines)
        anchor_zone = [fitz.Rect(a["rect"].x0 - 60, a["rect"].y0 - 110, a["rect"].x1 + 260, a["rect"].y1 + 110) for a in anc]

        for er in _embedded_image_rects(page):
            near = any(er.intersects(z) for z in anchor_zone)
            if _signature_image(page, er, near):
                found.append(er)

        for a in anc:
            if TYPED_SIGN_ONLY_RE.match(a["text"]):
                typed.append(a["rect"])                              # typed "Sd/-"
            for region in _regions(page, a):
                found.extend(_blobs(page, region, word_rects))
    except Exception as exc:  # never break a CV because of this heuristic
        logger.warning("Signature detection failed on page %s: %s", page.number + 1, exc)

    final: list[fitz.Rect] = []
    for r in sorted(found, key=lambda r: -r.get_area()):
        if all((r & f).get_area() < 0.6 * r.get_area() for f in final):
            final.append(r)
    return final, typed

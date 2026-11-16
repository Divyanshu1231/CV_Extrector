"""Candidate-photo detection that works for every kind of CV PDF.

Strategy (independent of how the PDF was produced):
1. Render the page to pixels, so embedded photos, scanned CVs and photos
   baked into a full-page image are all treated the same way.
2. Detect faces with OpenCV Haar cascades (frontal + profile, several
   cascades, multi-scale) and reject implausible detections.
3. Snap each face to its photo frame: the connected "non-background"
   region around the face, if it is a sensible photo size; otherwise a
   head-and-shoulders box derived from the face.
4. If the face sits inside a real embedded image that is photo-sized, that
   image's exact rectangle is used.
5. If no face is found, fall back to a conservative portrait-image rule for
   embedded images near the top of the page.

Everything degrades gracefully: if OpenCV is missing the module reports
`available() == False` and the caller uses the embedded-image fallback only.
"""
from __future__ import annotations

import logging

import fitz

logger = logging.getLogger("cv-privacy")

try:  # pragma: no cover - import guard
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None

try:  # pragma: no cover - import guard
    import mediapipe as mp
    _MP_OK = hasattr(mp, "solutions")
except Exception:  # pragma: no cover
    mp = None
    _MP_OK = False

_MP_DETECTORS: dict = {}

_CASCADES = None
_CASCADE_FILES = (
    "haarcascade_frontalface_default.xml",
    "haarcascade_frontalface_alt2.xml",
    "haarcascade_profileface.xml",
)
RENDER_WIDTH_PX = 1100


def available() -> bool:
    return cv2 is not None


def detector_name() -> str:
    if cv2 is None:
        return "none"
    return "mediapipe" if _MP_OK else "opencv-haar"


def _cascades():
    global _CASCADES
    if _CASCADES is None:
        _CASCADES = []
        for name in _CASCADE_FILES:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if not c.empty():
                _CASCADES.append((name, c))
    return _CASCADES


def _render(page: fitz.Page):
    zoom = RENDER_WIDTH_PX / max(page.rect.width, 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return img, zoom


def _iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _detect_faces_mediapipe(img, conf: float = 0.55, frac=(0.03, 0.32)) -> list[tuple[int, int, int, int]]:
    """MediaPipe BlazeFace: far more reliable than Haar for glasses, beards,
    small/low-contrast photos and photos on busy backgrounds."""
    if conf not in _MP_DETECTORS:
        _MP_DETECTORS[conf] = [
            mp.solutions.face_detection.FaceDetection(model_selection=sel, min_detection_confidence=conf)
            for sel in (1, 0)
        ]
    h, w = img.shape[:2]
    found = []
    for det in _MP_DETECTORS[conf]:
        res = det.process(np.ascontiguousarray(img))
        for d in (res.detections or []):
            b = d.location_data.relative_bounding_box
            x, y = max(0, int(b.xmin * w)), max(0, int(b.ymin * h))
            fw, fh = int(b.width * w), int(b.height * h)
            if frac[0] * w <= fw <= frac[1] * w and fh >= frac[0] * w:
                found.append((x, y, fw, fh))
    merged = []
    for f in sorted(found, key=lambda r: -r[2] * r[3]):
        if all(_iou(f, m) < 0.3 for m in merged):
            merged.append(f)
    return [f for f in merged if 0.6 <= f[2] / max(f[3], 1) <= 1.6]


def _detect_faces(img, conf: float = 0.55, frac=(0.03, 0.32)) -> list[tuple[int, int, int, int]]:
    if _MP_OK:
        try:
            return _detect_faces_mediapipe(img, conf, frac)
        except Exception as exc:
            logger.warning("MediaPipe face detection failed, using Haar: %s", exc)
    return _detect_faces_haar(img)


def _faces_in_rect(page: fitz.Page, rect: fitz.Rect, conf: float = 0.45, width_px: int = 720) -> list[fitz.Rect]:
    """Detect faces inside one region rendered large. A small face in a big photo is only a few
    pixels in a full-page render (borderline for the detector); the same face in a zoomed crop is easy."""
    rect = fitz.Rect(rect) & page.rect
    if rect.is_empty or rect.width < 20 or rect.height < 20:
        return []
    zoom = min(max(width_px / rect.width, 1.0), 4.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, colorspace=fitz.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    out = []
    for (fx, fy, fw, fh) in _detect_faces(img, conf=conf, frac=(0.05, 0.6)):
        out.append(fitz.Rect(rect.x0 + fx / zoom, rect.y0 + fy / zoom,
                             rect.x0 + (fx + fw) / zoom, rect.y0 + (fy + fh) / zoom))
    return out


def _detect_faces_haar(img) -> list[tuple[int, int, int, int]]:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = cv2.equalizeHist(gray)
    min_size = max(24, int(w * 0.035))
    max_size = int(w * 0.32)
    found: list[tuple[int, int, int, int]] = []
    for name, cascade in _cascades():
        frames = [gray]
        if "profile" in name:
            frames.append(cv2.flip(gray, 1))
        for k, frame in enumerate(frames):
            rects = cascade.detectMultiScale(
                frame, scaleFactor=1.08, minNeighbors=6,
                minSize=(min_size, min_size), maxSize=(max_size, max_size),
            )
            for (x, y, fw, fh) in rects:
                if k == 1:  # mirrored profile detection
                    x = w - x - fw
                found.append((int(x), int(y), int(fw), int(fh)))
    # merge duplicates
    merged: list[tuple[int, int, int, int]] = []
    for f in sorted(found, key=lambda r: -r[2] * r[3]):
        if all(_iou(f, m) < 0.3 for m in merged):
            merged.append(f)
    # sanity: a face is roughly square and not absurdly elongated
    return [f for f in merged if 0.7 <= f[2] / max(f[3], 1) <= 1.4]


def _frame_from_face(img, face) -> tuple[int, int, int, int] | None:
    """Return the bounding box of the non-background region around a face."""
    h, w = img.shape[:2]
    fx, fy, fw, fh = face
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    # background = bright and unsaturated (paper)
    bg = (gray > 236) & (hsv[:, :, 1] < 28)
    mask = (~bg).astype(np.uint8) * 255
    k = max(3, int(w * 0.006))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cx, cy = fx + fw // 2, fy + fh // 2
    label = labels[min(cy, h - 1), min(cx, w - 1)]
    if label == 0:
        return None
    x, y, bw, bh, area = stats[label]
    fill = area / max(bw * bh, 1)
    # accept only photo-like frames: sensible size, mostly filled, contains face
    if (bw <= w * 0.36 and bh <= h * 0.30 and bw >= fw * 1.15 and bh >= fh * 1.15
            and fill >= 0.55):
        return int(x), int(y), int(bw), int(bh)
    return None


def _head_and_shoulders(img, face) -> tuple[int, int, int, int]:
    h, w = img.shape[:2]
    fx, fy, fw, fh = face
    x0 = max(0, int(fx - fw * 0.75))
    x1 = min(w, int(fx + fw * 1.75))
    y0 = max(0, int(fy - fh * 0.65))
    y1 = min(h, int(fy + fh * 2.0))
    return x0, y0, x1 - x0, y1 - y0


def _embedded_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects = []
    for img in page.get_images(full=True):
        try:
            for r in page.get_image_rects(img[0]):
                rects.append(fitz.Rect(r))
        except Exception:
            continue
    return rects


def _dedupe_rects(rects: list[fitz.Rect], thr: float = 0.5) -> list[fitz.Rect]:
    out: list[fitz.Rect] = []
    for r in sorted(rects, key=lambda r: -r.get_area()):
        if all((r & f).get_area() < thr * r.get_area() for f in out):
            out.append(r)
    return out


def _scan_like(page: fitz.Page, embedded: list[fitz.Rect]) -> bool:
    page_area = max(page.rect.get_area(), 1)
    return sum(r.get_area() for r in embedded) / page_area > 0.6 or not page.get_text("words")


def detect_photo_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Return page-coordinate rectangles that contain a candidate photo."""
    page_area = max(page.rect.get_area(), 1)
    out: list[fitz.Rect] = []
    embedded = _embedded_image_rects(page)

    if available():
        try:
            img, zoom = _render(page)
            faces: list[fitz.Rect] = []
            # (1) whole page
            for (fx, fy, fw, fh) in _detect_faces(img):
                faces.append(fitz.Rect(fx / zoom, fy / zoom, (fx + fw) / zoom, (fy + fh) / zoom))
            # (2) every embedded picture on its own, zoomed (catches small faces in event/podium photos)
            for er in embedded:
                if 0.004 <= er.get_area() / page_area <= 0.9 and not any(er.intersects(f) for f in faces):
                    faces.extend(_faces_in_rect(page, er))
            # (3) scanned / photographed pages: look tile by tile in the top 60% of the page
            if not faces and _scan_like(page, embedded):
                W, H = page.rect.width, page.rect.height
                tw, th = W * 0.6, H * 0.34
                for ty in (0.0, H * 0.2):
                    for tx in (0.0, W * 0.4):
                        faces.extend(_faces_in_rect(page, fitz.Rect(tx, ty, tx + tw, ty + th), conf=0.5))
            faces = _dedupe_rects(faces)

            for face_rect in faces:
                fx, fy = face_rect.x0 * zoom, face_rect.y0 * zoom
                face = (int(fx), int(fy), int(face_rect.width * zoom), int(face_rect.height * zoom))

                # (a) exact embedded photo image that contains the face
                chosen = None
                for er in embedded:
                    if (er.contains(face_rect) or er.intersect(face_rect).get_area() >= 0.8 * face_rect.get_area()) \
                            and 0.004 <= er.get_area() / page_area <= 0.40:
                        if chosen is None or er.get_area() < chosen.get_area():
                            chosen = er
                if chosen is not None:
                    out.append(chosen)
                    continue

                # (b) photo frame detected from pixels, (c) head-and-shoulders
                frame = _frame_from_face(img, face)
                x, y, bw, bh = frame if frame else _head_and_shoulders(img, face)
                out.append(fitz.Rect(x / zoom, y / zoom, (x + bw) / zoom, (y + bh) / zoom))
        except Exception as exc:  # never let photo logic break a CV
            logger.warning("Face detection failed on page %s: %s", page.number + 1, exc)

    if not out:
        # Fallback for environments without OpenCV / faces the detector missed:
        # a photographic embedded image near the top, but never a full-page scan.
        for r in embedded:
            ratio = r.width / max(r.height, 1)
            area_ratio = r.get_area() / page_area
            if r.y0 <= page.rect.height * 0.35 and 0.008 <= area_ratio <= 0.30 and 0.55 <= ratio <= 1.9 \
                    and _looks_photographic(page, r):
                out.append(r)

    return _dedupe_rects(out, 0.6)


def _looks_photographic(page: fitz.Page, rect: fitz.Rect) -> bool:
    """Photos have a busy, mostly non-white picture; logos/rules/signatures are mostly white or flat."""
    if not available():
        return True
    try:
        pix = page.get_pixmap(clip=rect & page.rect, dpi=40, colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        return float((arr < 235).mean()) > 0.55 and float(arr.std()) > 28
    except Exception:
        return True

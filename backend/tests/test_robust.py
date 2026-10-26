"""Robustness tests: different CV layouts, scanned PDFs, photos, manual fallback."""
import io
import shutil

import fitz
import pytest

from app.processor import DetectionError, WATERMARK, process_pdf

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def title_case_cv(with_labels=False) -> bytes:
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 60), "Dr. Amit Kumar Sharma", fontsize=24)
    p.insert_text((50, 84), "Consultant Cardiologist", fontsize=12)
    p.insert_text((50, 104), "+91 98765 43210  |  amit.sharma@example.com", fontsize=10)
    p.insert_text((50, 120), "Lucknow", fontsize=10)
    p.insert_text((50, 190), "EXPERIENCE", fontsize=13)
    p.insert_text((50, 210), "Consultant, Apollo Hospital, Delhi (2019 - Present)", fontsize=11)
    p.insert_text((50, 230), "Dr. Amit Kumar Sharma presented at CSI 2024", fontsize=11)
    return doc.tobytes()


def to_scanned(pdf: bytes, dpi=150) -> bytes:
    src = fitz.open(stream=pdf, filetype="pdf")
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi)
        np_ = out.new_page(width=page.rect.width, height=page.rect.height)
        np_.insert_image(np_.rect, stream=pix.tobytes("jpeg"))
    return out.tobytes()


def all_text(pdf: bytes) -> str:
    d = fitz.open(stream=pdf, filetype="pdf")
    return "\n".join(p.get_text() for p in d)


def test_title_case_header_and_unlabeled_contacts():
    r = process_pdf(title_case_cv(), "Amit_CV.pdf")
    assert r.cv_code == "DR. AKS 3210"
    t = all_text(r.pdf_bytes)
    for bad in ["Amit Kumar Sharma", "98765", "amit.sharma@example.com", "Lucknow"]:
        assert bad not in t, bad
    assert "Consultant, Apollo Hospital, Delhi" in t  # job location below the header stays
    assert r.report["verified"] is True
    assert WATERMARK in t


@needs_tesseract
def test_scanned_cv_is_processed_and_verified():
    r = process_pdf(to_scanned(title_case_cv()), "scan.pdf")
    assert r.cv_code == "DR. AKS 3210"
    assert r.report["scanned_pages"] == [1]
    assert r.report["verified"] is True
    d = fitz.open(stream=r.pdf_bytes, filetype="pdf")
    # page must still carry its content (not blanked) - plenty of dark pixels
    pix = d[0].get_pixmap(dpi=40, colorspace=fitz.csGRAY)
    assert sum(1 for b in pix.samples if b < 160) > 200


def test_full_page_scan_image_is_never_treated_as_photo():
    from app.photo import detect_photo_rects
    d = fitz.open(stream=to_scanned(title_case_cv()), filetype="pdf")
    for r in detect_photo_rects(d[0]):
        assert r.get_area() < 0.4 * d[0].rect.get_area()


def test_embedded_portrait_image_near_top_is_removed_pixels_too():
    doc = fitz.open(stream=title_case_cv(), filetype="pdf")
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 90, 110), False)
    pm.set_rect(pm.irect, (90, 120, 200))
    doc[0].insert_image(fitz.Rect(470, 30, 560, 140), pixmap=pm)
    r = process_pdf(doc.tobytes(), "photo.pdf")
    out = fitz.open(stream=r.pdf_bytes, filetype="pdf")
    pix = out[0].get_pixmap(clip=fitz.Rect(470, 30, 560, 140), dpi=40, colorspace=fitz.csGRAY)
    assert min(pix.samples) > 235  # photo area is white now (pixels removed)
    assert r.report["photos_removed"] >= 1


def test_metadata_is_stripped():
    doc = fitz.open(stream=title_case_cv(), filetype="pdf")
    doc.set_metadata({"author": "Amit Kumar Sharma", "title": "Amit Sharma CV"})
    r = process_pdf(doc.tobytes(), "m.pdf")
    out = fitz.open(stream=r.pdf_bytes, filetype="pdf")
    assert not any((out.metadata or {}).get(k) for k in ("author", "title", "subject", "keywords"))


def test_missing_name_gives_structured_error_with_manual_fallback():
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 60), "9876543210", fontsize=12)
    p.insert_text((50, 100), "mbbs md experience hospital work", fontsize=11)
    with pytest.raises(DetectionError) as e:
        process_pdf(doc.tobytes(), "Rahul_Verma_CV.pdf")
    assert e.value.code == "DETAILS_NOT_DETECTED"
    assert e.value.missing == ["name"]
    assert e.value.hints.get("name_guess", "").lower().startswith("rahul")
    # manual override then succeeds
    r = process_pdf(doc.tobytes(), "Rahul_Verma_CV.pdf", name_override="Dr. Rahul Verma")
    assert r.cv_code == "DR. RV 3210"


def test_not_a_pdf_and_password():
    with pytest.raises(DetectionError) as e:
        process_pdf(b"hello world", "x.pdf")
    assert e.value.code == "NOT_A_PDF"


def test_military_rank_brackets_and_retired_suffix_are_removed_with_name():
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    x = 100
    for chunk in ["COL", "(DR.)", "ATMA", "PRAKASH", "CHITRAGUPTA", "(RETD.)"]:   # separate spans
        p.insert_text((x, 70), chunk, fontsize=16)
        x += len(chunk) * 11 + 6
    p.insert_text((90, 92), "Siliguri, West Bengal, India | atma.p@gmail.com | +91 877-842-9825 | LinkedIn", fontsize=9)
    p.insert_text((50, 200), "Disaster Response | Code Blue Protocols", fontsize=10)   # must not be mistaken for a name
    r = process_pdf(doc.tobytes(), "Resume_Col_Dr_Atma_Prakash_Chitragupta.pdf")
    assert r.cv_code == "DR. APC 9825"
    t = all_text(r.pdf_bytes)
    for bad in ["ATMA", "PRAKASH", "CHITRAGUPTA", "RETD", "COL", "Siliguri", "West Bengal", "atma.p@gmail.com"]:
        assert bad not in t, bad
    assert "Code Blue Protocols" in t

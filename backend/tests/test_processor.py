from pathlib import Path
import fitz
from app.processor import generate_cv_code, process_pdf, WATERMARK

def make_test_pdf():
    doc = fitz.open()
    for page_no in range(3):
        p = doc.new_page()
        y = 60
        lines = [
            "NAME: DR. ROHIT VAAMAN DWARAKANATH KAKKAR",
            "ADDRESS: CLEMENTOWN, DEHRADUN.",
            "MOBILE: +91-8185896109",
            "Email: rohit.kakkar@gmail.com",
            "ISA No - R2900A",
            "QUALIFICATIONS",
            "MBBS, MD (Medicine)",
            "PROFESSIONAL EXPERIENCE",
            "Consultant Physician, ABC Hospital",
        ]
        for line in lines:
            p.insert_text((45, y), line, fontsize=12)
            y += 25
    return doc.tobytes()

def test_cv_code():
    assert generate_cv_code("DR. ROHIT VAAMAN DWARAKANATH KAKKAR", "+91-8185896109") == "DR. RVDK 6109"
    assert generate_cv_code("Dr. Amit Kumar Sharma", "9876543210") == "DR. AKS 3210"
    assert generate_cv_code("Rahul Singh", "8840133918") == "RS 3918"
    assert generate_cv_code("Mrs. Neha Verma", "9999912345") == "MRS. NV 2345"

def test_full_processing():
    result = process_pdf(make_test_pdf(), "test.pdf")
    assert result.cv_code == "DR. RVDK 6109"
    assert result.filename == "DR_RVDK_6109_Space_Consultant.pdf"

    doc = fitz.open(stream=result.pdf_bytes, filetype="pdf")
    assert doc.page_count == 3
    for page in doc:
        text = page.get_text()
        assert WATERMARK in text
        assert "CV Code: DR. RVDK 6109" in text
        assert "DR. ROHIT VAAMAN DWARAKANATH KAKKAR" not in text
        assert "CLEMENTOWN, DEHRADUN" not in text
        assert "8185896109" not in text
        assert "R2900A" not in text
        assert "[REMOVED]" not in text
        assert "CV0000" not in text
        assert "MBBS, MD (Medicine)" in text
        assert "Consultant Physician" in text
    doc.close()


def test_repeated_private_fields_are_removed_everywhere():
    doc = fitz.open()
    for page_no in range(2):
        p = doc.new_page()
        y = 55
        lines = [
            "Name: Dr. Sample Doctor",
            "Address: House No 12, Green Road, Delhi",
            "Mobile: +91-9876543210",
            "Email: sample.doctor@gmail.com",
            "Registration No: NMC123456",
            "QUALIFICATIONS",
            "MBBS, MD Medicine",
            "Experience at City Hospital",
        ]
        if page_no == 1:
            lines.insert(1, "Address No 2: Flat 44, Lake Nagar, Noida")
            lines.insert(5, "Name: Dr. Sample Doctor")
            lines.insert(6, "Phone: 9876543210")
            lines.insert(7, "Email: sample.doctor@gmail.com")
            lines.insert(8, "ISA No - R9999")
        for line in lines:
            p.insert_text((45, y), line, fontsize=11)
            y += 22
    result = process_pdf(doc.tobytes(), "repeat.pdf")
    out = fitz.open(stream=result.pdf_bytes, filetype="pdf")
    all_text = "\n".join(p.get_text() for p in out)

    for bad in [
        "Dr. Sample Doctor", "House No 12", "Green Road, Delhi",
        "Flat 44", "Lake Nagar", "9876543210",
        "sample.doctor@gmail.com", "NMC123456", "R9999",
    ]:
        assert bad not in all_text
    assert "MBBS, MD Medicine" in all_text
    assert "Experience at City Hospital" in all_text
    assert all(WATERMARK in p.get_text() for p in out)
    assert all("CV Code:" in p.get_text() for p in out)
    out.close()


def test_glued_title_generation():
    assert generate_cv_code("Dr.DALBIR SINGH", "+917087552507") == "DR. DS 2507"


def test_header_footer_and_personal_table_are_removed_everywhere():
    doc = fitz.open()
    for page_no in range(3):
        p = doc.new_page(width=595, height=842)
        if page_no == 0:
            p.insert_text((45, 70), "SWAPNIL NIDVANCHE", fontsize=22)
            p.insert_text((370, 70), "Mobile: 6303535394", fontsize=9)
            p.insert_text((370, 84), "Email: swapnilnidvanche123@gmail.com", fontsize=9)
            p.insert_text((370, 98), "Location: Rohtak, 124001", fontsize=9)
            p.insert_text((45, 150), "PROFESSIONAL PROFILE", fontsize=13)
            p.insert_text((45, 180), "MD Anesthesia and Critical Care", fontsize=11)
        if page_no == 2:
            p.insert_text((45, 300), "Name", fontsize=10)
            p.insert_text((170, 300), "Swapnil Nidvanche", fontsize=10)
            p.insert_text((45, 322), "Date of Birth", fontsize=10)
            p.insert_text((170, 322), "16 February 1997", fontsize=10)
            p.insert_text((45, 344), "Location", fontsize=10)
            p.insert_text((170, 344), "Rohtak, 124001", fontsize=10)
            p.insert_text((45, 366), "Email", fontsize=10)
            p.insert_text((170, 366), "swapnilnidvanche123@gmail.com", fontsize=10)
            p.insert_text((45, 388), "Mobile", fontsize=10)
            p.insert_text((170, 388), "6303535394", fontsize=10)
            p.insert_text((45, 430), "SWAPNIL NIDVANCHE", fontsize=12)
        p.insert_text((45, 810), "Swapnil Nidvanche | MD Anesthesia & Critical Care", fontsize=8)
    result = process_pdf(doc.tobytes(), "swapnil-test.pdf")
    assert result.cv_code == "SN 5394"
    out = fitz.open(stream=result.pdf_bytes, filetype="pdf")
    all_text = "\n".join(p.get_text() for p in out)
    for bad in [
        "SWAPNIL NIDVANCHE", "Swapnil Nidvanche", "6303535394",
        "swapnilnidvanche123@gmail.com", "Rohtak, 124001", "16 February 1997"
    ]:
        assert bad not in all_text, bad
    assert all(WATERMARK in p.get_text() for p in out)
    assert all("CV Code: SN 5394" in p.get_text() for p in out)
    assert "MD Anesthesia and Critical Care" in all_text
    out.close()

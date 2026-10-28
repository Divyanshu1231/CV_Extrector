from __future__ import annotations

import difflib
import io
import json
import logging
import re
import shutil
from dataclasses import dataclass, field

import fitz

from . import photo

logger = logging.getLogger("cv-privacy")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

WATERMARK = "Space Consultant Ph.9891417909"

TITLE_RE = re.compile(r"^(DR|MR|MRS|MS|PROF)\.?\s*$", re.I)

# Deliberately broad phone detection. A value is accepted as a phone only
# after normalization to a plausible 10-digit Indian/international number.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s().-]*)?(?:\d[\s().-]*){9,13}\d(?!\d)"
)
EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])"
)

NAME_LABEL_RE = re.compile(
    r"^\s*(?:candidate\s+name|full\s+name|name)\s*(?:[:\-–—])?\s*(.*)$", re.I
)
PHONE_LABEL_RE = re.compile(
    r"^\s*(?:mobile|phone|telephone|contact|contact\s+no|contact\s+number|"
    r"phone\s+no|phone\s+number|mobile\s+no|mobile\s+number|"
    r"m(?=\s*(?:[:\-–—])))\.?\s*(?:number|no\.?)?\s*(?:[:\-–—])?\s*(.*)$", re.I
)
PLACE_LABEL_RE = re.compile(
    r"^\s*(?:place|residence|residing\s+at)\s*(?:[:\-–—])\s*(.*)$", re.I
)
LOCATION_LABEL_RE = re.compile(
    r"^\s*(?:location|current\s+location|present\s+location|city|address\s*/\s*location)\s*(?:[:\-–—])?\s*(.*)$", re.I
)
EMAIL_LABEL_RE = re.compile(
    r"^\s*(?:e[-\s]?mail|email\s+id|email\s+i['’]?d)\s*(?:[:\-–—])?\s*(.*)$", re.I
)
ADDRESS_LABEL_RE = re.compile(
    r"^\s*(?:address(?:\s*\.?\s*(?:no|number)\s*\d*)?|"
    r"permanent\s+address|current\s+address|residential\s+address|"
    r"residence|residential\s+location|home\s+address)\s*(?:[:\-–—])?\s*(.*)$", re.I
)
DOB_LABEL_RE = re.compile(
    r"^\s*(?:date\s+of\s+birth|dob)\s*(?:[:\-–—])?\s*(.*)$", re.I
)
ADDRESS_HEURISTIC_RE = re.compile(
    r"(?i)(?:\b(?:house\s*(?:no|number)?|h\.\s*no\.?|flat\s*(?:no|number)?|"
    r"plot\s*(?:no|number)?|near|district|distt?\.?|pin(?:\s*code)?|pincode|"
    r"post\s+office|village|tehsil)\b[^,;\n]{0,80}|"
    r"\b[A-Za-z0-9.-]+\s+(?:road|street|lane|nagar|colony|sector)\b[^,;\n]{0,60})"
)

REG_LABEL_RE = re.compile(
    r"^\s*(?:registration(?:\s+number|\s+no\.?)?|reg\.?\s*(?:no|number)?|"
    r"medical\s+(?:council\s+)?registration|state\s+medical\s+council|"
    r"mci\s*/?\s*nmc\s+registration|isa\s*(?:number|no\.?)?|"
    r"professional\s+registration|license(?:\s+number|\s+no\.?)?)\b"
    r"\s*(?:[:\-–—])?\s*(.*)$", re.I
)

# Registration IDs are redacted even when the label/value occur inline.
REG_INLINE_RE = re.compile(
    r"(?i)\b(?:ISA\s*(?:No|Number)?\b|Registration\s*(?:No|Number)?\b|"
    r"Reg\.\s*(?:No|Number)?\b|Medical\s+(?:Council\s+)?Registration\b|"
    r"State\s+Medical\s+Council\b|MCI\s*/?\s*NMC\s+Registration\b|"
    r"Professional\s+Registration\b|License\s*(?:No|Number)?\b)"
    r"\s*[:\-–—]?\s*[A-Z0-9][A-Z0-9./\- ]{2,35}"
)

# --- Professional / government IDs: any wording, any line, all pages ------------------
_ID = r"(?:[A-Z]{1,6}[\s\-/.]*)?\d{3,}(?:[/\-]\d+)*[A-Z]?"
_REGWORD = r"(?:Regd?|Regn|Registration|Registered|Reg)"
REG_EXTRA_RES = [
    # "Regd. No. HN 26086", "Registration No: 12345", "Reg No - MH/2012/1234"
    re.compile(r"(?i)\b(?:Regd?|Regn|Registration|Reg)\.?\s*(?:No\.?|Number|Num|#)\s*[:\-–—.]?\s*" + _ID),
    # "Registration: 12345" (colon form)
    re.compile(r"(?i)\b(?:Regd?|Regn|Registration)\.?\s*[:\-–—]\s*" + _ID),
    # "Delhi Medical Council Regd. No. HN 26086", "Haryana Medical Council, 12345"
    re.compile(r"(?i)\b(?:[A-Za-z]+\s+){0,2}(?:Medical|Dental|Nursing|Pharmacy|State)\s+Council(?:\s+of\s+[A-Za-z]+)?"
               r"[\s,;:|\-–—]*(?:" + _REGWORD + r"\.?\s*(?:No\.?|Number|Num|#)?\s*[:\-–—.]?\s*)?" + _ID),
    # "NMC UID 12345", "MCI No: 54321", "NMR 12345"
    re.compile(r"(?i)\b(?:NMC|MCI|NMR|SMC|UID)\s*(?:UID|ID|Reg(?:istration)?\.?|No\.?|Number|#)?\s*[:\-–—#.]?\s*" + _ID),
    # Government identity numbers
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),                                                  # PAN
    re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"),                                                 # Aadhaar 4-4-4
    re.compile(r"(?i)\b(?:Aadhaar|Aadhar|Adhar)\s*(?:No\.?|Number|Card)?\s*[:\-–—#]?\s*\d[\d\s]{9,14}\d"),
    re.compile(r"(?i)\bPassport\s*(?:No\.?|Number)?\s*[:\-–—#]?\s*[A-Z]\d{7}\b"),
    re.compile(r"(?i)\b(?:Driving\s+Licen[cs]e|DL)\s*(?:No\.?|Number)?\s*[:\-–—#]?\s*[A-Z0-9][A-Z0-9\-/ ]{7,18}"),
    re.compile(r"(?i)\b(?:Voter\s*ID|EPIC)\s*(?:No\.?|Number|Card)?\s*[:\-–—#]?\s*[A-Z]{3}\d{7}"),
    re.compile(r"(?i)\b(?:UAN|PF\s*(?:No|Number)|ESIC?\s*(?:No|Number))\.?\s*[:\-–—#]?\s*\d{8,14}"),
]
# label at the end of a line, value on the next line
REG_LABEL_ONLY_RE = re.compile(r"(?i)\b(?:Regd?|Regn|Registration|Reg)\.?\s*(?:No\.?|Number|Num|#)\s*[:\-–—.]?\s*$")
REG_VALUE_ONLY_RE = re.compile(r"^\s*" + _ID + r"\s*$", re.I)

# Degrees / credentials written after a name ("Dr. Sudesh Kumar MBBS, MS, MCh").
DEGREE_TOKENS = {
    "mbbs", "md", "ms", "mch", "dm", "dnb", "mnams", "frcs", "mrcs", "fics", "facs", "fcps", "mrcp", "frcp",
    "mrcog", "frcog", "dgo", "dch", "dortho", "dmrd", "ddvl", "bds", "mds", "bams", "bhms", "bpt", "mpt",
    "phd", "msc", "bsc", "mba", "mph", "mmed", "ficcm", "ficcm", "fnb", "mcps", "dip", "frcr", "mrcpch",
}

# DOB is personal information even when it is not labeled as "address/contact".
DOB_RE = re.compile(
    r"(?i)\b(?:date\s+of\s+birth|dob)\s*[:\-–—]?\s*"
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+)?"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)?\s*"
    r"\d{1,2}(?:st|nd|rd|th)?[,\-/ ]+\d{2,4}\b"
)

class DetectionError(ValueError):
    """Raised when a CV cannot be processed automatically.

    `code` is machine readable, `missing` lists the fields the user can supply
    manually (name / mobile) and `hints` carries best guesses for the UI.
    """

    def __init__(self, code: str, message: str, missing=None, hints=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing = list(missing or [])
        self.hints = dict(hints or {})

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message,
                "missing": self.missing, "hints": self.hints}


MAX_PAGES = 30

SOCIAL_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|facebook\.com|instagram\.com|"
    r"twitter\.com|x\.com|wa\.me|api\.whatsapp\.com|t\.me)/[^\s,;]+"
)

_CITIES = """
delhi new delhi noida greater noida gurgaon gurugram faridabad ghaziabad mumbai navi mumbai thane pune nagpur nashik
aurangabad solapur kolhapur bengaluru bangalore mysuru mysore mangaluru mangalore hubli belgaum belagavi chennai
coimbatore madurai salem tiruchirappalli trichy tirunelveli vellore hyderabad secunderabad warangal vijayawada
visakhapatnam vizag guntur tirupati kolkata howrah siliguri durgapur asansol kharagpur patna gaya muzaffarpur bhagalpur
ranchi jamshedpur dhanbad bokaro bhubaneswar cuttack rourkela puri raipur bhilai bilaspur durg bhopal indore gwalior
jabalpur ujjain sagar ahmedabad surat vadodara baroda rajkot bhavnagar jamnagar gandhinagar jaipur jodhpur udaipur kota
ajmer bikaner alwar bharatpur sikar lucknow kanpur varanasi banaras allahabad prayagraj agra meerut aligarh bareilly
moradabad gorakhpur mathura saharanpur jhansi dehradun haridwar rishikesh haldwani roorkee chandigarh mohali panchkula
ludhiana amritsar jalandhar patiala bathinda ambala karnal panipat rohtak hisar sonipat sonepat yamunanagar kurukshetra
shimla solan dharamshala jammu srinagar leh guwahati dibrugarh jorhat silchar shillong imphal aizawl kohima agartala
itanagar gangtok panaji margao goa kochi cochin thiruvananthapuram trivandrum kozhikode calicut thrissur kollam kannur
puducherry pondicherry port blair sri ganganagar
andhra arunachal assam bihar chhattisgarh gujarat haryana himachal jharkhand karnataka kerala madhya maharashtra manipur
meghalaya mizoram nagaland odisha orissa punjab rajasthan sikkim tamil telangana tripura uttar uttarakhand bengal
pradesh nadu ladakh lakshadweep andaman nicobar india dadra nagar haveli daman diu
west east north south central
""".split()
PLACE_WORDS = set(_CITIES)


@dataclass
class ProcessResult:
    pdf_bytes: bytes
    cv_code: str
    filename: str
    logs: dict
    report: dict = field(default_factory=dict)


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    # Indian +91/91/0 and international numbers: retain the final 10 digits.
    if len(digits) >= 10:
        candidate = digits[-10:]
        if candidate[0] in "6789":
            return candidate
    return ""


def clean_words(value: str) -> list[str]:
    return [w for w in re.sub(r"\s+", " ", value).strip().split(" ") if w]


def parse_title_and_name(name: str) -> tuple[str, str]:
    words = clean_words(name)
    title = ""
    if words:
        first = words[0]
        # Support both "Dr. Rohit" and OCR/PDF forms such as "Dr.DALBIR".
        glued = re.match(r"^(DR|MR|MRS|MS|PROF)\.(?=[A-Za-z])(.+)$", first, re.I)
        if glued:
            title = glued.group(1).upper() + "."
            words[0] = glued.group(2)
        elif TITLE_RE.match(first):
            title = first.upper()
            if title in {"DR", "MR", "MRS", "MS", "PROF"}:
                title += "."
            words.pop(0)
    return title, " ".join(words)


def generate_cv_code(original_name: str, original_mobile: str) -> str:
    title, name_without_title = parse_title_and_name(original_name)
    words = clean_words(name_without_title)
    if not words:
        raise ValueError("Unable to generate CV Code because candidate name was not detected.")
    mobile = normalize_phone(original_mobile)
    if not mobile:
        raise ValueError("Unable to generate CV Code because candidate mobile number was not detected.")
    initials = "".join(
        word[0].upper() for word in words
        if re.search(r"[A-Za-z]", word)
    )
    if not initials:
        raise ValueError("Unable to generate CV Code because candidate name initials could not be determined.")
    code = f"{title + ' ' if title else ''}{initials} {mobile[-4:]}"
    if re.match(r"^CV\d", code, re.I) or "UNKNOWN" in code.upper() or code.endswith(" 0000"):
        raise ValueError("CV Code generation failed; refusing to create a placeholder code.")
    return code


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def page_needs_ocr(page: fitz.Page) -> bool:
    """True for scans / image-only pages and pages that only carry a tiny text
    layer (stamp, footer) on top of a raster scan."""
    words = page.get_text("words")
    if len(words) >= 40:
        return False
    if not words:
        return True
    try:
        covered = sum(r.get_area() for r in photo._embedded_image_rects(page))
    except Exception:
        covered = 0
    return covered / max(page.rect.get_area(), 1) > 0.6


def _ocr_store(page: fitz.Page) -> dict:
    doc = page.parent
    store = getattr(doc, "_cv_ocr_cache", None)
    if store is None:
        store = {}
        try:
            doc._cv_ocr_cache = store
        except Exception:
            pass
    return store


def clear_ocr_cache(doc: fitz.Document):
    try:
        doc._cv_ocr_cache = {}
    except Exception:
        pass


def get_ocr_textpage(page: fitz.Page):
    """OCR textpage for scanned pages, None for pages with a real text layer.

    Results are cached per page so a multi-step pipeline OCRs each page once.
    """
    store = _ocr_store(page)
    if page.number in store:
        return store[page.number]
    tp = None
    if page_needs_ocr(page):
        if not tesseract_available():
            raise DetectionError(
                "OCR_UNAVAILABLE",
                "This CV is a scanned/image PDF but the OCR engine (Tesseract) is not installed on the server.",
            )
        dpi = int(max(150, min(300, 2600 / (page.rect.width / 72))))
        try:
            tp = page.get_textpage_ocr(language="eng", dpi=dpi, full=True)
        except Exception as exc:
            logger.warning("OCR failed on page %s: %s", page.number + 1, exc)
            raise DetectionError("OCR_FAILED", f"OCR could not read this scanned CV: {exc}")
    store[page.number] = tp
    return tp


def line_items(page: fitz.Page, textpage=None) -> list[dict]:
    """
    Return logical PDF text lines built from PyMuPDF word coordinates.
    This is much more reliable than page.get_text('blocks') for CVs with
    labels in one column and values in another.
    """
    groups = {}
    words_source = textpage.extractWORDS() if textpage is not None else page.get_text("words")
    for w in words_source:
        x0, y0, x1, y1, text, block, line, word = w
        key = (block, line)
        groups.setdefault(key, []).append(w)

    result = []
    for _, words in groups.items():
        words.sort(key=lambda x: (x[0], x[1]))
        text = " ".join(w[4] for w in words).strip()
        if not text:
            continue
        rect = fitz.Rect(
            min(w[0] for w in words),
            min(w[1] for w in words),
            max(w[2] for w in words),
            max(w[3] for w in words),
        )
        result.append({"text": text, "rect": rect, "words": words})
    result.sort(key=lambda x: (x["rect"].y0, x["rect"].x0))
    return result


def padded_rect(page: fitz.Page, rect: fitz.Rect, px=2, py=1.5) -> fitz.Rect:
    return fitz.Rect(
        max(0, rect.x0-px),
        max(0, rect.y0-py),
        min(page.rect.width, rect.x1+px),
        min(page.rect.height, rect.y1+py),
    )


def search_rects(page: fitz.Page, text: str, textpage=None) -> list[fitz.Rect]:
    """Case-insensitive text search that works on text layers and OCR textpages.

    (page.search_for(textpage=...) insists on the very same Page object that
    created the textpage, which breaks once pages are re-fetched; searching the
    textpage directly avoids that.)"""
    if textpage is not None:
        return [fitz.Rect(q.rect) if hasattr(q, 'rect') else fitz.Rect(q) for q in textpage.search(text)]
    return [fitz.Rect(r) for r in page.search_for(text)]


def rects_for_text(page: fitz.Page, text: str, textpage=None) -> list[fitz.Rect]:
    if not text:
        return []
    return [padded_rect(page, r) for r in search_rects(page, text, textpage)]


def visual_row_matches(page: fitz.Page, pattern: re.Pattern, textpage) -> list[tuple[str, fitz.Rect]]:
    """OCR fallback: cluster words into visual rows (by vertical overlap), strip
    spaces and run the regex. Returns (matched_text, rect) pairs."""
    words = sorted(textpage.extractWORDS(), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in words:
        cy = (w[1] + w[3]) / 2
        for row in rows:
            if abs(row["cy"] - cy) <= 0.6 * max(row["h"], w[3] - w[1]):
                row["words"].append(w)
                break
        else:
            rows.append({"cy": cy, "h": w[3] - w[1], "words": [w]})
    out = []
    for row in rows:
        ws = sorted(row["words"], key=lambda w: w[0])
        compact, spans = "", []
        for w in ws:
            spans.append((len(compact), len(compact) + len(w[4]), w))
            compact += w[4]
        for m in pattern.finditer(compact):
            sel = [w for a, b, w in spans if b > m.start() and a < m.end()]
            if sel:
                r = fitz.Rect(min(w[0] for w in sel), min(w[1] for w in sel),
                              max(w[2] for w in sel), max(w[3] for w in sel))
                out.append((m.group(0), padded_rect(page, r)))
    return out


def rects_for_visual_rows_compact(page: fitz.Page, pattern: re.Pattern, textpage) -> list[fitz.Rect]:
    return [r for _, r in visual_row_matches(page, pattern, textpage)]


def contact_location_rects(page: fitz.Page, lines: list[dict]) -> list[fitz.Rect]:
    """City/state in the header contact block without a label, e.g. a map-pin
    icon + "Jaipur", or "Siliguri, West Bengal, India | email | phone".
    Lines are split into segments at | • · – separators; a segment made *entirely*
    of place words is removed. Only the top 30% of the page is considered, so
    job locations lower in the CV are untouched."""
    out = []
    limit = page.rect.height * 0.30
    for line in lines:
        if line["rect"].y0 > limit:
            continue
        segments, cur = [], []
        for w in line["words"]:
            tok = re.sub(r"[^a-z]", "", w[4].lower())
            if not tok:                       # separator such as | • – or an icon glyph
                if cur:
                    segments.append(cur)
                cur = []
            else:
                cur.append((tok, w))
        if cur:
            segments.append(cur)
        for seg in segments:
            if 1 <= len(seg) <= 6 and all(t in PLACE_WORDS for t, _ in seg):
                for _, w in seg:
                    out.append(padded_rect(page, fitz.Rect(w[:4])))
    return out


def rects_for_regex_lines(page: fitz.Page, pattern: re.Pattern, textpage=None) -> list[fitz.Rect]:
    """
    Regex over every logical text line, then map each match back to the
    exact words covered by the match. This catches values in any page,
    not just page 1.
    """
    out = []
    for line in line_items(page, textpage):
        text = line["text"]
        for match in pattern.finditer(text):
            start, end = match.span()
            cursor = 0
            selected = []
            for w in line["words"]:
                word = w[4]
                ws = cursor
                we = cursor + len(word)
                # account for the single separator inserted between words
                if we > start and ws < end:
                    selected.append(w)
                cursor = we + 1
            if selected:
                r = fitz.Rect(
                    min(w[0] for w in selected),
                    min(w[1] for w in selected),
                    max(w[2] for w in selected),
                    max(w[3] for w in selected),
                )
                out.append(padded_rect(page, r))
    return out


BAD_NAME_RE = re.compile(
    r"\b(?:name|candidate|full|curriculum|vitae|vite|resume|cv|profile|objective|summary|experience|education|"
    r"skills?|anesthesia|anaesthesia|critical|care|professional|mobile|e-?mail|location|address|phone|contact|"
    r"page|personal|details|qualifications?|hospital|college|collage|university|institute|clinic|medical|medicine|"
    r"department|consultant|resident|specialist|physician|surgeon|surgery|senior|junior|registrar|intern|"
    r"internship|fellow|fellowship|lecturer|professor|assistant|associate|certified|licensed|registered|"
    r"references?|declaration|awards?|publications?|achievements?|training|activities|honou?rs|interests|"
    r"languages?|projects?|dob|date|birth|gender|nationality|marital|status|father|mother|husband|wife|"
    r"work|history|career|employment|summary|about|me|present|current|links?|social|linkedin|github|"
    r"\w+ologist|\w+iatrist|\w+ician|\w+ologists)\b",
    re.I,
)
NAME_STOP_FILE_WORDS = {
    "cv", "resume", "updated", "update", "final", "new", "latest", "copy", "doc", "docx", "pdf", "profile",
    "curriculum", "vitae", "scan", "scanned", "file", "version", "dr", "mr", "mrs", "ms", "prof", "my", "the",
    "of", "and", "for", "medical", "doctor", "cvs", "resumes", "image", "img", "whatsapp", "download",
    "col", "colonel", "maj", "major", "gen", "capt", "captain", "brig", "retd", "ret", "retired", "lt", "cdr", "surg",
}


RANK_RE = re.compile(
    r"^(?:(?:lt\.?\s*)?(?:col|colonel|maj|major|gen|general|brig|brigadier|capt|captain|cdr|cmdr|commander|surg|"
    r"sqn\s*ldr|wg\s*cdr|gp\s*capt|air\s*cmde|lt|lieutenant|subedar|hony|honorary)\b\.?\s*)+",
    re.I,
)
RETIRED_RE = re.compile(r"\(?\s*\b(?:retd|ret|retired|rtd)\b\.?\s*\)?", re.I)
BRACKET_TITLE_RE = re.compile(r"\(\s*(dr|prof|mr|mrs|ms)\s*\.?\s*\)", re.I)


def clean_name_text(text: str) -> str:
    t = BRACKET_TITLE_RE.sub(lambda m: m.group(1) + ".", text)  # "(DR.)" -> "DR."
    t = RETIRED_RE.sub(" ", t)                                  # "(RETD.)" -> ""
    t = re.sub(r"[|_•·\\/\[\]{}()<>@#*~^`=+:;,\"“”]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—.")
    m = RANK_RE.match(t)
    if m and len(t[m.end():].split()) >= 2:                     # keep "Major Singh"
        t = t[m.end():].strip()
    return strip_trailing_degrees(t)


def strip_trailing_degrees(t: str) -> str:
    """"Dr. Sudesh Kumar MBBS MS MCh" -> "Dr. Sudesh Kumar" (credentials are not part of the name)."""
    words = t.split()
    while len(words) > 2 and re.sub(r"[^a-z]", "", words[-1].lower()) in DEGREE_TOKENS:
        words.pop()
    return " ".join(words)


def looks_like_name(text: str) -> bool:
    t = clean_name_text(text)
    if not t or len(t) > 70 or any(ch.isdigit() for ch in t):
        return False
    if BAD_NAME_RE.search(t):
        return False
    title, body = parse_title_and_name(t)
    words = clean_words(body)
    n = len(words)
    if not ((2 <= n <= 6) or (title and 1 <= n <= 6)):
        return False
    for w in words:
        if not re.fullmatch(r"[A-Za-z][A-Za-z.'’-]*", w):
            return False
        if not w[0].isupper():
            return False
    alpha = re.sub(r"[^A-Za-z]", "", body)
    return len(alpha) >= 5 and any(len(re.sub(r"[^A-Za-z]", "", w)) >= 3 for w in words)


def _is_name_candidate(text: str) -> bool:  # backwards compatible alias
    return looks_like_name(text)


def filename_name_hints(filename: str) -> list[str]:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename or "")
    toks = [t.lower() for t in re.split(r"[^A-Za-z]+", stem) if len(t) >= 3]
    return [t for t in toks if t not in NAME_STOP_FILE_WORDS]


def _email_local_hints(doc: fitz.Document) -> list[str]:
    out = []
    for page in list(doc)[:2]:
        try:
            tp = get_ocr_textpage(page)
        except DetectionError:
            tp = None
        text = " ".join(l["text"] for l in line_items(page, tp))
        for m in EMAIL_RE.finditer(text.replace(" @ ", "@")):
            out.append(m.group(0).split("@")[0].lower())
    return out


def _name_candidates(doc: fitz.Document, max_pages=2):
    """Collect header-ish lines that look like a person's name, with the
    visual prominence (font size / OCR line height) used for ranking."""
    cands = []
    for pno, page in enumerate(doc):
        if pno >= max_pages:
            break
        H = page.rect.height
        tp = get_ocr_textpage(page)
        if tp is not None:
            for line in line_items(page, tp):
                if line["rect"].y0 > H * 0.35:
                    continue
                text = clean_name_text(line["text"])
                if looks_like_name(text):
                    cands.append({"text": text, "page": pno, "rect": line["rect"],
                                  "size": line["rect"].height, "bold": False})
            continue
        data = page.get_text("dict")
        entries = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = " ".join(sp.get("text", "") for sp in spans).strip()
                bbox = fitz.Rect(line.get("bbox", (0, 0, 0, 0)))
                if not text or bbox.y0 > H * 0.35:
                    continue
                size = max((float(sp.get("size", 0)) for sp in spans), default=0)
                bold = any("bold" in str(sp.get("font", "")).lower() or (sp.get("flags", 0) & 16) for sp in spans)
                entries.append({"text": text, "rect": bbox, "size": size, "bold": bool(bold)})
        # Text pieces on the same visual row (rank / title / name / suffix often sit in
        # separate text blocks) are merged left-to-right before judging them.
        entries.sort(key=lambda e: (round(e["rect"].y0), e["rect"].x0))
        rows = []
        for e in entries:
            for row in rows:
                last = row[-1]
                overlap = min(last["rect"].y1, e["rect"].y1) - max(last["rect"].y0, e["rect"].y0)
                gap = e["rect"].x0 - last["rect"].x1
                if overlap > 0.6 * min(last["rect"].height, e["rect"].height) and -2 <= gap <= 2.0 * max(last["rect"].height, e["rect"].height):
                    row.append(e)
                    break
            else:
                rows.append([e])
        for row in rows:
            row.sort(key=lambda e: e["rect"].x0)
            rect = fitz.Rect(row[0]["rect"])
            for e in row[1:]:
                rect |= e["rect"]
            text = clean_name_text(" ".join(e["text"] for e in row))
            if looks_like_name(text):
                cands.append({"text": text, "page": pno, "rect": rect,
                              "size": max(e["size"] for e in row),
                              "bold": any(e["bold"] for e in row)})
    return cands


def _rank_candidates(cands, doc, filename: str):
    file_hints = filename_name_hints(filename)
    emails = _email_local_hints(doc) if cands else []
    ranked = []
    for c in cands:
        page_h = doc[c["page"]].rect.height
        score = c["size"] * 10 + (12 if c["bold"] else 0)
        score += max(0.0, 30.0 * (1 - c["rect"].y0 / (page_h * 0.35)))
        toks = [re.sub(r"[^a-z]", "", t.lower()) for t in c["text"].split()]
        if any(h in toks for h in file_hints):
            score += 60
        if any(len(t) >= 4 and any(t in e for e in emails) for t in toks):
            score += 25
        if parse_title_and_name(c["text"])[0]:
            score += 10
        ranked.append((score, c))
    ranked.sort(key=lambda x: -x[0])
    return [c for _, c in ranked]








def page_width_guard(rect: fitz.Rect) -> float:
    return max(180.0, rect.width * 8)


def _same_row_value(lines: list[dict], label_rect: fitz.Rect):
    candidates = []
    for line in lines:
        r = line["rect"]
        vertical_overlap = min(r.y1, label_rect.y1) - max(r.y0, label_rect.y0)
        if vertical_overlap <= 0 or r.x0 <= label_rect.x1 + 2:
            continue
        if r.x0 > label_rect.x1 + page_width_guard(label_rect):
            continue
        candidates.append(line)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["rect"].x0)
    c = candidates[0]
    return c["text"], c["rect"]


def find_labeled_value(lines: list[dict], label_re: re.Pattern):
    for idx, line in enumerate(lines):
        m = label_re.match(line["text"])
        if not m:
            continue
        inline = m.group(1).strip(" :,-–—")
        if inline:
            return inline, line["rect"]
        same = _same_row_value(lines, line["rect"])
        if same:
            return same
        if idx + 1 < len(lines):
            nxt = lines[idx + 1]
            if 0 <= nxt["rect"].y0 - line["rect"].y1 < 30:
                return nxt["text"], nxt["rect"]
    return None


def name_rects_in_line(page: fitz.Page, line: dict, candidate_name: str, fuzzy: bool = False) -> list[fitz.Rect]:
    title, body = parse_title_and_name(candidate_name)
    variants = []
    for nm in (candidate_name, body):
        toks = re.sub(r"[^a-z0-9]+", " ", nm.lower()).strip().split()
        if toks and toks not in variants:
            variants.append(toks)
    words = line["words"]
    normalized = [re.sub(r"[^a-z0-9]+", "", w[4].lower()) for w in words]
    out = []
    for target_tokens in variants:
        target_compact = "".join(target_tokens)
        if not target_compact:
            continue
        for i in range(len(words)):
            compact = ""
            selected = []
            for j in range(i, min(len(words), i + len(target_tokens) + 2)):
                if not normalized[j]:
                    continue
                compact += normalized[j]
                selected.append(words[j])
                matched = compact == target_compact
                if not matched and fuzzy and len(target_compact) >= 8 and len(compact) >= len(target_compact) * 0.8:
                    matched = difflib.SequenceMatcher(None, compact, target_compact).ratio() >= 0.86
                if matched:
                    out.append(padded_rect(page, fitz.Rect(
                        min(w[0] for w in selected), min(w[1] for w in selected),
                        max(w[2] for w in selected), max(w[3] for w in selected)), 2.5, 1.5))
                    break
                if len(compact) >= len(target_compact) * (1.25 if fuzzy else 1):
                    break
    return out


def detect_name(doc: fitz.Document, filename: str = "") -> tuple[str, int | None, fitz.Rect | None]:
    """Layered name detection that works for text PDFs, scans and mixed PDFs.

    1. Most prominent name-like line in the page header (font size / OCR height,
       cross-checked against file name and email address).
    2. Explicit "Name:" label anywhere (two-column tables included).
    3. A line with a title (Dr./Mr./...) near the top.
    4. Any name-like line (top 35%) that agrees with the file name.
    Returns ("", None, None) when nothing reliable is found; the caller then
    asks the user to type the name instead of failing silently.
    """
    cands = _rank_candidates(_name_candidates(doc), doc, filename)
    if cands:
        best = cands[0]
        return best["text"], best["page"], best["rect"]

    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        found = find_labeled_value(line_items(page, textpage), NAME_LABEL_RE)
        if found:
            value = clean_name_text(found[0])
            if 2 <= len(clean_words(value)) <= 6 and not any(ch.isdigit() for ch in value):
                return value, pno, found[1]

    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        for line in line_items(page, textpage):
            if line["rect"].y0 > page.rect.height * 0.30:
                continue
            title, words = parse_title_and_name(line["text"])
            if title and len(clean_words(words)) >= 2:
                return clean_name_text(line["text"]), pno, line["rect"]

    hints = filename_name_hints(filename)
    if hints:
        for pno, page in enumerate(doc):
            textpage = get_ocr_textpage(page)
            for line in line_items(page, textpage):
                text = clean_name_text(line["text"])
                toks = [re.sub(r"[^a-z]", "", t.lower()) for t in text.split()]
                if looks_like_name(text) and any(h in toks for h in hints):
                    return text, pno, line["rect"]
    return "", None, None


def detect_phone(doc: fitz.Document) -> tuple[str, int | None, fitz.Rect | None]:
    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        found = find_labeled_value(line_items(page, textpage), PHONE_LABEL_RE)
        if found and normalize_phone(found[0]):
            return found[0], pno, found[1]
    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        for line in line_items(page, textpage):
            m = PHONE_RE.search(line["text"])
            if m and normalize_phone(m.group(0)):
                return m.group(0), pno, line["rect"]
    # OCR sometimes splits one number across several fragments ("76772 21268").
    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        if textpage is None:
            continue
        for text, rect in visual_row_matches(page, PHONE_RE, textpage):
            if normalize_phone(text):
                return text, pno, rect
    return "", None, None


def next_nonempty_lines(lines: list[dict], idx: int, max_lines=6):
    for j in range(idx+1, min(len(lines), idx+1+max_lines)):
        yield j, lines[j]


def is_section_or_new_field(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return True
    field_patterns = [
        NAME_LABEL_RE, PHONE_LABEL_RE, EMAIL_LABEL_RE,
        ADDRESS_LABEL_RE, REG_LABEL_RE,
    ]
    if any(p.match(text) for p in field_patterns):
        return True
    return bool(re.match(
        r"^(career objective|objective|profile|summary|education|academic qualification|"
        r"experience|employment|skills|projects|publications|certifications|"
        r"professional experience|work experience|references|languages|interests|"
        r"achievements|conferences)\b", t, re.I
    ))


def redact_rects(page: fitz.Page, rects: list[fitz.Rect], logs: list[str], label: str):
    # Deduplicate overlapping rectangles, then apply real PDF redactions.
    unique = []
    for r in sorted(rects, key=lambda r: -r.get_area()):   # biggest first, so a wide rect is never dropped for a smaller one
        if r.get_area() <= 0:
            continue
        def overlap_ratio(a, b):
            inter = a & b
            if inter.is_empty:
                return 0.0
            return inter.get_area() / max(1.0, min(a.get_area(), b.get_area()))
        if not any(overlap_ratio(r, u) > 0.95 for u in unique):
            unique.append(r)
    for r in unique:
        page.add_redact_annot(r, fill=(1, 1, 1), cross_out=False)
        logs.append(str(tuple(round(v, 2) for v in r)))
    return len(unique)


def extend_to_row(page: fitz.Page, rect: fitz.Rect, textpage=None) -> fitz.Rect:
    """Grow a name-line rect to the words immediately left/right of it on the
    same visual row (rank prefix "COL", suffix "(RETD.)" ...). Stops at a wide
    gap so a second column is never swallowed. Trailing degrees ("MBBS, MS, MCh")
    are *not* part of the name and stay visible."""
    words = textpage.extractWORDS() if textpage is not None else page.get_text("words")
    h = max(rect.height, 6.0)
    row = [w for w in words
           if min(w[3], rect.y1) - max(w[1], rect.y0) >= 0.5 * min(w[3] - w[1], rect.height)]
    row.sort(key=lambda w: w[0])
    out = fitz.Rect(rect)
    changed = True
    while changed:
        changed = False
        for w in row:
            if w[0] < out.x0 and out.x0 - w[2] <= 2.0 * h:
                out.x0 = w[0]; changed = True
            elif w[2] > out.x1 and w[0] - out.x1 <= 2.0 * h:
                out.x1 = w[2]; changed = True
    chain = [w for w in row if w[0] >= out.x0 - 1 and w[2] <= out.x1 + 1]
    while len(chain) > 2 and re.sub(r"[^a-z]", "", chain[-1][4].lower()) in (DEGREE_TOKENS | {""}):
        chain.pop()
    if chain:
        out = fitz.Rect(min(w[0] for w in chain), min(w[1] for w in chain),
                        max(w[2] for w in chain), max(w[3] for w in chain))
    return out


def redact_sensitive_content(
    doc: fitz.Document,
    original_name: str,
    name_page: int | None,
    original_phone: str,
    name_line_rect: fitz.Rect | None = None,
):
    logs = {
        "name_rects": [], "address_rects": [], "phone_rects": [],
        "email_rects": [], "registration_rects": [], "dob_rects": [],
        "social_rects": [], "photo_rects": [],
        "pages": doc.page_count,
        "ocr_pages": [],
    }

    # Exact candidate name is private wherever it appears, not only in header.
    name_variants = {original_name, original_name.replace(" ", "  ")}
    title, body_name = parse_title_and_name(original_name)
    if title:
        name_variants.add(body_name)
        name_variants.add(f"{title} {body_name}")
    phone_digits = normalize_phone(original_phone)

    for pno, page in enumerate(doc):
        textpage = get_ocr_textpage(page)
        if textpage is not None:
            logs["ocr_pages"].append(pno + 1)
        lines = line_items(page, textpage)
        rects = []

        # 1) Candidate name is a document-wide privacy key. Remove every
        # occurrence, including headers, footers, signatures and personal tables.
        for line in lines:
            for r in name_rects_in_line(page, line, original_name, fuzzy=textpage is not None):
                rects.append(("name", r))
        for variant in name_variants:
            for r in rects_for_text(page, variant, textpage):
                rects.append(("name", r))
        for idx, line in enumerate(lines):
            if NAME_LABEL_RE.match(line["text"]):
                rects.append(("name", padded_rect(page, line["rect"], 3, 2)))
                same = _same_row_value(lines, line["rect"])
                if same:
                    rects.append(("name", padded_rect(page, same[1], 3, 2)))
                elif idx+1 < len(lines) and lines[idx+1]["rect"].y0-line["rect"].y1 < 30:
                    rects.append(("name", padded_rect(page, lines[idx+1]["rect"], 3, 2)))

        # 1b) The detected name line itself (e.g. "COL (DR.) NAME (RETD.)"): rank,
        # brackets and suffixes around the name must disappear with it.
        if name_line_rect is not None and name_page == pno:
            rects.append(("name", padded_rect(page, extend_to_row(page, fitz.Rect(name_line_rect), textpage), 3, 2)))

        # 2) Addresses: detect EVERY labeled address block, including
        # "Address No 1", "Address No 2", etc. Continue until another field
        # or a new section. This fixes multi-line address failures.
        for idx, line in enumerate(lines):
            if not ADDRESS_LABEL_RE.match(line["text"]):
                continue
            rects.append(("address", padded_rect(page, line["rect"], 3, 2)))
            # Capture the value on same line and continuation lines.
            base_y = line["rect"].y1
            for j, nxt in next_nonempty_lines(lines, idx, max_lines=6):
                gap = nxt["rect"].y0 - base_y
                if gap > 34:
                    break
                if is_section_or_new_field(nxt["text"]):
                    break
                # Address continuation is allowed to span several lines.
                rects.append(("address", padded_rect(page, nxt["rect"], 3, 2)))
                base_y = nxt["rect"].y1

        # 2a) Place/location lines at the end of CVs are also personal location data.
        for line in lines:
            if PLACE_LABEL_RE.match(line["text"]) or LOCATION_LABEL_RE.match(line["text"]):
                rects.append(("address", padded_rect(page, line["rect"], 3, 2)))
                same = _same_row_value(lines, line["rect"])
                if same:
                    rects.append(("address", padded_rect(page, same[1], 3, 2)))

        # 2b) Address-like fragments that occur without an "Address:" label.
        # We remove the address phrase, not the whole line, so professional
        # text such as "Kendriya Vidyalaya" can remain visible.
        for line in lines:
            for m in ADDRESS_HEURISTIC_RE.finditer(line["text"]):
                phrase = m.group(0).strip()
                # Require a meaningful location signal; don't redact generic
                # words such as "road" in an unrelated sentence by themselves.
                low = phrase.lower()
                signals = sum(k in low for k in (
                    "road", "street", "lane", "nagar", "colony", "sector",
                    "district", "distt", "pin", "pincode", "post", "village",
                    "house", "flat", "plot", "near", "tehsil"
                ))
                if signals < 1 or len(phrase) < 5:
                    continue
                for r in search_rects(page, phrase, textpage):
                    rects.append(("address", padded_rect(page, r, 2, 1.5)))

        # 3) Phone: every labeled phone and every phone-looking value on every page.
        for line in lines:
            if PHONE_LABEL_RE.match(line["text"]):
                rects.append(("phone", padded_rect(page, line["rect"], 3, 2)))
        for r in rects_for_regex_lines(page, PHONE_RE, textpage):
            # Only accept valid normalized numbers, avoiding dates/years.
            # Determine text covered approximately from its PDF rect.
            covered = " ".join(
                w[4] for w in (textpage.extractWORDS() if textpage is not None else page.get_text("words"))
                if fitz.Rect(r).intersects(fitz.Rect(w[:4]))
            )
            if len(normalize_phone(covered)) == 10:
                rects.append(("phone", r))

        # 4) Email: all occurrences, including footer/signature.
        for r in rects_for_regex_lines(page, EMAIL_RE, textpage):
            rects.append(("email", r))
        if textpage is not None:
            for r in rects_for_visual_rows_compact(page, EMAIL_RE, textpage):
                rects.append(("email", r))
        for line in lines:
            if EMAIL_LABEL_RE.match(line["text"]):
                rects.append(("email", padded_rect(page, line["rect"], 3, 2)))

        # 4b) Social / messaging profile links (linkedin, github, wa.me ...).
        for r in rects_for_regex_lines(page, SOCIAL_RE, textpage):
            rects.append(("social", r))

        # 4c) Unlabeled city/state in the header contact block.
        for r in (contact_location_rects(page, lines) if pno == 0 else []):
            rects.append(("address", r))

        # 5) Medical/professional/state registration: all occurrences/pages.
        for line in lines:
            if REG_LABEL_RE.match(line["text"]):
                rects.append(("registration", padded_rect(page, line["rect"], 3, 2)))
        for r in rects_for_regex_lines(page, REG_INLINE_RE, textpage):
            rects.append(("registration", r))
        # Council / registration / government-ID formats (wording-independent).
        for pattern in REG_EXTRA_RES:
            for r in rects_for_regex_lines(page, pattern, textpage):
                rects.append(("registration", r))
            if textpage is not None:
                for r in rects_for_visual_rows_compact(page, pattern, textpage):
                    rects.append(("registration", r))
        # "Regd. No." at the end of a line, number on the next line.
        for idx, line in enumerate(lines[:-1]):
            nxt = lines[idx + 1]
            if REG_LABEL_ONLY_RE.search(line["text"]) and REG_VALUE_ONLY_RE.match(nxt["text"]) \
                    and 0 <= nxt["rect"].y0 - line["rect"].y1 < 30:
                rects.append(("registration", padded_rect(page, line["rect"], 3, 2)))
                rects.append(("registration", padded_rect(page, nxt["rect"], 3, 2)))

        # 6) DOB: personal data, including two-column personal-detail tables.
        for idx, line in enumerate(lines):
            if DOB_LABEL_RE.match(line["text"]):
                rects.append(("dob", padded_rect(page, line["rect"], 3, 2)))
                same = _same_row_value(lines, line["rect"])
                if same:
                    rects.append(("dob", padded_rect(page, same[1], 3, 2)))
                elif idx + 1 < len(lines) and 0 <= lines[idx+1]["rect"].y0 - line["rect"].y1 < 30:
                    rects.append(("dob", padded_rect(page, lines[idx+1]["rect"], 3, 2)))
        for r in rects_for_regex_lines(page, DOB_RE, textpage):
            rects.append(("dob", r))

        # Tidy: header separators (| • ·) left dangling next to a removed value.
        if rects:
            all_words = textpage.extractWORDS() if textpage is not None else page.get_text("words")
            for w in all_words:
                if w[4] not in ("|", "•", "·", "||") or w[1] > page.rect.height * 0.30:
                    continue
                wr = fitz.Rect(w[:4])
                for _, r in list(rects):
                    same_row = min(wr.y1, r.y1) - max(wr.y0, r.y0) > 0.5 * wr.height
                    near = 0 <= wr.x0 - r.x1 <= 8 or 0 <= r.x0 - wr.x1 <= 8
                    if same_row and near:
                        rects.append(("address", padded_rect(page, wr, 1.5, 1.5)))
                        break

        by_label = {}
        for label, r in rects:
            by_label.setdefault(label, []).append(r)

        redact_rects(page, by_label.get("name", []), logs["name_rects"], "name")
        redact_rects(page, by_label.get("address", []), logs["address_rects"], "address")
        redact_rects(page, by_label.get("phone", []), logs["phone_rects"], "phone")
        redact_rects(page, by_label.get("email", []), logs["email_rects"], "email")
        redact_rects(page, by_label.get("social", []), logs["social_rects"], "social")
        redact_rects(page, by_label.get("registration", []), logs["registration_rects"], "registration")
        redact_rects(page, by_label.get("dob", []), logs["dob_rects"], "dob")

        # IMPORTANT: apply after all annotations for this page.
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_PIXELS if textpage is not None else fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )

    return logs


def redact_candidate_photos(doc: fitz.Document, logs: dict):
    """Remove the candidate's photo from every kind of CV.

    Detection is pixel based (see photo.py): it works for embedded images,
    scans and photos baked into a full-page image. Redaction removes the
    *pixels* (PDF_REDACT_IMAGE_PIXELS), so the photo cannot be recovered from
    the PDF, and the vector frame around it is removed too. Text is untouched.
    """
    logs["photo_detector"] = photo.detector_name()
    line_art = getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", fitz.PDF_REDACT_LINE_ART_NONE)
    text_none = getattr(fitz, "PDF_REDACT_TEXT_NONE", fitz.PDF_REDACT_TEXT_REMOVE)
    for pno, page in enumerate(doc):
        if pno >= 3:  # candidate photos live on the first pages
            break
        rects = photo.detect_photo_rects(page)
        if not rects:
            continue
        for r in rects:
            pad = fitz.Rect(r.x0 - 3, r.y0 - 3, r.x1 + 3, r.y1 + 3) & page.rect
            if pad.is_empty:
                continue
            page.add_redact_annot(pad, fill=(1, 1, 1), cross_out=False)
            logs["photo_rects"].append(f"p{pno + 1}:" + str(tuple(round(v, 1) for v in pad)))
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_PIXELS,
            graphics=line_art,
            text=text_none,
        )


def _band_is_busy(page: fitz.Page, rect: fitz.Rect) -> bool:
    """True when the band already contains visible content (text/graphics)."""
    try:
        pix = page.get_pixmap(clip=rect & page.rect, dpi=40, colorspace=fitz.csGRAY, alpha=False)
        samples = pix.samples
        dark = sum(1 for b in samples if b < 200)
        return dark / max(len(samples), 1) > 0.004
    except Exception:
        return False


def add_watermark_and_code(doc: fitz.Document, cv_code: str):
    for page in doc:
        # Everything scales with page size so scanned/A3/phone-size pages look
        # exactly as professional as a normal A4 CV.
        k = max(0.8, min(3.2, page.rect.width / 595.0))
        header = fitz.Rect(24 * k, 12 * k, page.rect.width - 24 * k, 36 * k)
        page.insert_textbox(
            header, f"CV Code: {cv_code}", fontsize=10 * k, fontname="hebo",
            color=(0.22, 0.22, 0.22), align=fitz.TEXT_ALIGN_LEFT, overlay=True
        )
        footer = fitz.Rect(18 * k, page.rect.height - 34 * k, page.rect.width - 18 * k, page.rect.height - 12 * k)
        # Footer copy only when the bottom margin is free, so it never prints
        # on top of CV content (the header code is always present).
        if not _band_is_busy(page, footer):
            page.insert_textbox(
                footer, f"CV Code: {cv_code}", fontsize=9 * k, fontname="helv",
                color=(0.35, 0.35, 0.35), align=fitz.TEXT_ALIGN_CENTER, overlay=True
            )
        point = fitz.Point(page.rect.width / 2 - 150 * k, page.rect.height / 2 + 110 * k)
        rot = fitz.Matrix(1, 1).prerotate(40)
        page.insert_text(
            point, WATERMARK, fontsize=26 * k, fontname="helv",
            color=(0.65, 0.65, 0.65), fill_opacity=0.32,
            stroke_opacity=0, morph=(fitz.Point(page.rect.width / 2, page.rect.height / 2), rot), overlay=True
        )


def sanitize_document(doc: fitz.Document):
    """Strip identity hidden outside the visible page: document metadata (author,
    title often contains the candidate name), XMP, bookmarks, attachments,
    mailto/tel/social links and form fields."""
    try:
        doc.set_metadata({})
    except Exception:
        pass
    try:
        doc.del_xml_metadata()
    except Exception:
        pass
    try:
        doc.set_toc([])
    except Exception:
        pass
    try:
        for name in list(doc.embfile_names()):
            doc.embfile_del(name)
    except Exception:
        pass
    risky = re.compile(r"(?i)^(?:mailto:|tel:|sms:|https?://(?:www\.)?(?:wa\.me|api\.whatsapp\.com|linkedin\.com|github\.com|facebook\.com|instagram\.com|twitter\.com|x\.com|t\.me))")
    for page in doc:
        for link in page.get_links():
            uri = link.get("uri") or ""
            if link.get("kind") == fitz.LINK_URI and risky.match(uri):
                try:
                    page.delete_link(link)
                except Exception:
                    pass
        try:
            for widget in list(page.widgets() or []):
                page.delete_widget(widget)
        except Exception:
            pass


def safe_filename(cv_code: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", cv_code).strip("_")
    return f"{safe}_Space_Consultant.pdf"


def _page_text(page: fitz.Page) -> str:
    tp = get_ocr_textpage(page)
    return tp.extractText() if tp is not None else page.get_text()


def verify_output(pdf_bytes: bytes, original_name: str, original_phone: str) -> list[str]:
    """Re-read the finished PDF (OCR for scanned pages) and confirm the name,
    mobile number and email addresses are really gone."""
    issues: list[str] = []
    title, body = parse_title_and_name(original_name)
    name_compact = re.sub(r"[^a-z0-9]", "", (body or original_name).lower())
    phone = normalize_phone(original_phone)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for pno, page in enumerate(doc):
            try:
                text = _page_text(page)
            except DetectionError:
                issues.append(f"page {pno + 1}: could not be re-checked (OCR unavailable)")
                continue
            compact = re.sub(r"[^a-z0-9]", "", text.lower())
            digits = re.sub(r"\D", "", text)
            if name_compact and len(name_compact) >= 5 and name_compact in compact:
                issues.append(f"page {pno + 1}: name still readable")
            if phone and phone in digits:
                issues.append(f"page {pno + 1}: mobile number still readable")
            if EMAIL_RE.search(text) or re.search(r"[\w.+-]+\s*@\s*[\w-]+\s*\.\s*\w{2,}", text):
                issues.append(f"page {pno + 1}: email still readable")
    finally:
        doc.close()
    return issues


def _save(doc: fitz.Document) -> bytes:
    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True, encryption=fitz.PDF_ENCRYPT_NONE)
    return out.getvalue()


def process_pdf(
    data: bytes,
    original_filename: str = "input.pdf",
    name_override: str = "",
    phone_override: str = "",
) -> ProcessResult:
    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise DetectionError("NOT_A_PDF", "The uploaded file is not a valid PDF.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise DetectionError("NOT_A_PDF", "The uploaded PDF could not be opened (file may be damaged).")
    if doc.needs_pass:
        raise DetectionError("PASSWORD_PROTECTED", "This PDF is password protected. Please remove the password and upload again.")
    if doc.page_count == 0:
        raise DetectionError("EMPTY_PDF", "The uploaded PDF has no pages.")
    if doc.page_count > MAX_PAGES:
        raise DetectionError("TOO_MANY_PAGES", f"This PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.")

    try:
        name_guess, name_page, name_rect = ("", None, None)
        phone_guess, phone_page, _ = ("", None, None)
        if not name_override.strip():
            name_guess, name_page, name_rect = detect_name(doc, original_filename)
        if not phone_override.strip():
            phone_guess, phone_page, _ = detect_phone(doc)

        original_name = clean_name_text(name_override) if name_override.strip() else name_guess
        original_phone = phone_override.strip() or phone_guess

        missing = []
        if not original_name or len(clean_words(parse_title_and_name(original_name)[1])) < 1:
            missing.append("name")
        if not normalize_phone(original_phone):
            missing.append("mobile")
        if missing:
            hints = {}
            fh = filename_name_hints(original_filename)
            if fh:
                hints["name_guess"] = " ".join(t.capitalize() for t in fh[:3])
            if "name" in missing and "mobile" in missing:
                msg = "We could not detect the candidate name and mobile number automatically. Please enter them below."
            elif "name" in missing:
                msg = "We could not detect the candidate name automatically. Please enter it below."
            else:
                msg = "We could not detect the candidate mobile number automatically. Please enter it below."
            raise DetectionError("DETAILS_NOT_DETECTED", msg, missing=missing, hints=hints)

        cv_code = generate_cv_code(original_name, original_phone)
        logger.info("Name detected: %s | Mobile detected | CV Code: %s | pages=%s", bool(original_name), cv_code, doc.page_count)

        logs = redact_sensitive_content(doc, original_name, name_page, original_phone, name_rect)
        redact_candidate_photos(doc, logs)
        sanitize_document(doc)
        add_watermark_and_code(doc, cv_code)
        pdf_bytes = _save(doc)
        doc.close()

        issues = verify_output(pdf_bytes, original_name, original_phone)
        passes = 1
        if issues:
            # Second, stricter pass on the already processed file.
            doc2 = fitz.open(stream=pdf_bytes, filetype="pdf")
            clear_ocr_cache(doc2)
            redact_sensitive_content(doc2, original_name, name_page, original_phone)
            pdf_bytes = _save(doc2)
            doc2.close()
            issues = verify_output(pdf_bytes, original_name, original_phone)
            passes = 2

        report = {
            "pages": logs["pages"],
            "scanned_pages": logs["ocr_pages"],
            "name_removed": len(logs["name_rects"]),
            "phone_removed": len(logs["phone_rects"]),
            "email_removed": len(logs["email_rects"]),
            "address_removed": len(logs["address_rects"]),
            "registration_removed": len(logs["registration_rects"]),
            "dob_removed": len(logs["dob_rects"]),
            "links_removed": len(logs["social_rects"]),
            "photos_removed": len(logs["photo_rects"]),
            "photo_detector": logs.get("photo_detector", "none"),
            "verified": not issues,
            "issues": issues,
            "passes": passes,
        }
        return ProcessResult(pdf_bytes, cv_code, safe_filename(cv_code), logs, report)
    finally:
        try:
            if not doc.is_closed:
                doc.close()
        except Exception:
            pass

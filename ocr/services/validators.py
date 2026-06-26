import re

from django.conf import settings

_ZERO_MARKER_COLLAPSED = frozenset({"2x", "x2", "*"})
_CROSS_SINGLE = re.compile(r"^[x×✕✗*]$", re.IGNORECASE)
_EXPLICIT_DITTO_MARKS = frozenset({'"', "''", "do", "ditto", "-do-", ",,"})
_DIGIT_SLASH_DIGIT = re.compile(r"(?<=\d)[/\\|\u2044\u2215](?=\d)")
_VALIDATION_CONFIDENCE_THRESHOLD = 0.85
_MAX_NUMERIC_VALUE = 1000
_OCR_SUSPICIOUS_ASCII_CHARS = frozenset("|/-~")


_AZURE_CHECKBOX_PATTERN = re.compile(
    r":selected:|:unselected:|selected|unselected",
    re.IGNORECASE,
)


def _strip_azure_checkbox_markers(raw):
    return re.sub(r":selected:|:unselected:", "", raw, flags=re.IGNORECASE)


def normalize_fs_ocr_text(value):
    if value is None:
        return ""
    raw = str(value)
    if not _AZURE_CHECKBOX_PATTERN.search(raw):
        return raw

    without_markers = _strip_azure_checkbox_markers(raw)
    without_markers = without_markers.replace("\u00d7", "x").replace("×", "x")
    without_markers = re.sub(r"\s+", " ", without_markers).strip()

    compact_digits = re.sub(r"\s+", "", without_markers)
    if compact_digits.isdigit():
        if compact_digits == "2":
            return "0"
        return compact_digits

    if not without_markers:
        return "X"

    if _CROSS_SINGLE.match(without_markers):
        return "X"

    collapsed = re.sub(r"\s+", "", without_markers.lower())
    if collapsed in _ZERO_MARKER_COLLAPSED:
        return "X"

    return "X"


def _clean_fs_raw(value):
    if value is None:
        return ""
    raw = normalize_fs_ocr_text(value)
    if raw == "X":
        return "X"
    raw = raw.replace("\u00d7", "x").replace("×", "x")
    raw = raw.replace("\u00b0", "")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def prepare_fs_cell_value(value):
    return _clean_fs_raw(value)


def normalize_n_ocr_text(value):
    if value is None:
        return ""
    raw = str(value)
    raw = re.sub(r":selected:|:unselected:", "", raw, flags=re.IGNORECASE)
    raw = raw.replace("\u00d7", "X").replace("Ã—", "X")
    raw = raw.replace("\u00b0", "")
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = _DIGIT_SLASH_DIGIT.sub("1", raw)
    return raw


def _canonicalize_fs_text(value):
    return _clean_fs_raw(value).lower()


def is_zero_marker(value):
    text = _canonicalize_fs_text(value)
    if not text:
        return False
    collapsed = re.sub(r"\s+", "", text)
    return collapsed in _ZERO_MARKER_COLLAPSED


def is_cross_marker(value):
    text = _clean_fs_raw(value)
    if not text:
        return False
    if is_zero_marker(text):
        return True
    collapsed = re.sub(r"\s+", "", text.lower())
    if collapsed == "2":
        return True
    return bool(_CROSS_SINGLE.match(text))


def _is_explicit_ditto(value):
    return _canonicalize_fs_text(value) in _EXPLICIT_DITTO_MARKS


def _is_lone_one(value):
    compact = re.sub(r"\s+", "", _clean_fs_raw(value))
    return compact == "1"


def parse_fs_numeric(value):
    text = _clean_fs_raw(value)
    if not text or is_cross_marker(text) or _is_lone_one(text):
        return None
    compact_digits = re.sub(r"\s+", "", text)
    if compact_digits.isdigit():
        if compact_digits == "2":
            return "0"
        return compact_digits
    return None


def normalize_fs_value(value):
    cleaned = _clean_fs_raw(value)
    if not cleaned:
        return ""
    if _is_lone_one(cleaned):
        return "1"
    if is_cross_marker(cleaned):
        return "0"
    numeric = parse_fs_numeric(cleaned)
    if numeric is not None:
        return numeric
    return "0"


def validation_confidence_threshold():
    return float(getattr(settings, "OCR_VALIDATION_CONFIDENCE_THRESHOLD", _VALIDATION_CONFIDENCE_THRESHOLD))


def has_ocr_suspicious_marker(content):
    value = "" if content is None else str(content)
    return any(ord(char) > 127 or char in _OCR_SUSPICIOUS_ASCII_CHARS for char in value)


def is_suspicious_data_value(raw_content, normalized_value=None, field_type=None):
    if not has_ocr_suspicious_marker(raw_content):
        return False

    raw = "" if raw_content is None else str(raw_content)
    if field_type in ("F", "S") and is_cross_marker(raw):
        return False

    value = raw if normalized_value is None else str(normalized_value)
    compact = re.sub(r"\s+", "", value)
    if not compact.isdigit():
        if field_type in ("F", "S"):
            numeric = parse_fs_numeric(raw)
            compact = numeric or compact
        elif field_type == "N":
            compact = re.sub(r"\s+", "", normalize_n_ocr_text(raw))
    return bool(compact and compact.isdigit())


def is_ui_noise(content):
    return is_suspicious_data_value(content)


def validate_cell(val, confidence, field_type=None):
    value = (val or "").strip()
    reasons = []
    try:
        score = float(confidence or 0)
    except (TypeError, ValueError):
        score = 0.0

    threshold = validation_confidence_threshold()
    if score < threshold:
        reasons.append(f"Low confidence score ({score:.2f})")

    compact = re.sub(r"\s+", "", value)
    if field_type == "N":
        if compact and not re.fullmatch(r"\d{2,4}", compact):
            reasons.append("N field must contain 2 to 4 digits")
    elif field_type in ("F", "S"):
        if compact and not compact.isdigit():
            reasons.append(f"{field_type} field must contain only numbers")

    if compact.isdigit() and int(compact) > _MAX_NUMERIC_VALUE:
        reasons.append(f"Value is an unusually high outlier ({compact})")

    return {
        "value": value,
        "is_uncertain": bool(reasons),
        "reason": "; ".join(reasons),
        "confidence": score,
    }


def resolve_fs_columns(cells):
    fs_cells = {}
    for cell in cells:
        if cell.field_type in ("F", "S"):
            fs_cells[(cell.group_number, cell.field_type, cell.row_number)] = cell

    for group_number in (1, 2, 3):
        for field_type in ("F", "S"):
            last_value = None
            for row_number in range(1, 26):
                cell = fs_cells.get((group_number, field_type, row_number))
                if cell is None:
                    continue

                cleaned = prepare_fs_cell_value(cell.value)

                numeric = parse_fs_numeric(cleaned)
                if numeric is not None:
                    cell.value = numeric
                    last_value = numeric
                    continue

                if is_cross_marker(cleaned):
                    cell.value = "0"
                    last_value = "0"
                    continue

                if not cleaned or _is_explicit_ditto(cleaned) or _is_lone_one(cleaned):
                    cell.value = last_value if last_value is not None else ""
                    continue

                cell.value = "0"
                last_value = "0"

    return cells


def apply_fs_validation(cells):
    return resolve_fs_columns(cells)


def validate_cell_value(field_type, value):
    value = (value or "").strip()
    if field_type == "N":
        if value and not re.fullmatch(r"\d{2,4}", value):
            raise ValueError("N fields must be 2 to 4 digits.")
    elif field_type in ("F", "S"):
        return normalize_fs_value(value)
    else:
        raise ValueError("Unknown field type.")
    return value

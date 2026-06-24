import re

_ZERO_MARKER_COLLAPSED = frozenset({"2x", "x2", "*"})
_CROSS_SINGLE = re.compile(r"^[x×✕✗*]$", re.IGNORECASE)
_EXPLICIT_DITTO_MARKS = frozenset({'"', "''", "do", "ditto", "-do-", ",,"})
_DIGIT_SLASH_DIGIT = re.compile(r"(?<=\d)[/\\|\u2044\u2215](?=\d)")


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

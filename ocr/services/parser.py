import logging
import re

from django.conf import settings

from .base import OCRCell, OCRProviderError
from .validators import normalize_fs_ocr_text, prepare_fs_cell_value, resolve_fs_columns

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column index → (group_number, field_type)
# Azure's header row (row=0) gives us these column indices reliably:
#   col 0 = S.N  (skip)
#   col 1 = N.1  → group 1, N
#   col 2 = F    → group 1, F
#   col 3 = S    → group 1, S
#   col 4 = N.2  → group 2, N
#   col 5 = F    → group 2, F
#   col 6 = S    → group 2, S
#   col 7 = N.3  → group 3, N
#   col 8 = F    → group 3, F
#   col 9 = S    → group 3, S
# ---------------------------------------------------------------------------
COLUMN_INDEX_MAP = {
    1: (1, "N"),
    2: (1, "F"),
    3: (1, "S"),
    4: (2, "N"),
    5: (2, "F"),
    6: (2, "S"),
    7: (3, "N"),
    8: (3, "F"),
    9: (3, "S"),
}

# Normalise Azure's OCR variants of the handwritten "x" marker
X_VARIANTS = re.compile(r'^[xX×✕✗\*]$')

# Spatial tolerance (pixels) when matching data cell x-center to header anchors
HEADER_ANCHOR_TOLERANCE = 30.0


def parse_azure_result(raw_response):
    """
    Parse Azure Document Intelligence response using the table cell grid.

    Pipeline
    --------
    1. Extract cells from Azure's table structure (x-proximity column mapping).
    2. Resolve F/S columns top-to-bottom — propagate ditto marks from blanks,
       normalize cross markers to 0, and preserve numeric values per column.

    Returns a list of OCRCell objects.
    """
    table = _get_first_table(raw_response)

    if table:
        logger.info("Using table-cell parser (%d cells detected)", len(table.get("cells", [])))
        cells = _parse_table_cells(table, raw_response)
        if cells:
            return resolve_fs_columns(cells)
        logger.warning("Table-cell parser returned no cells; falling back to word parser")

    logger.warning("No table found in Azure response; using word-level fallback")
    cells = _parse_with_word_fallback(raw_response)
    return resolve_fs_columns(cells)


# ---------------------------------------------------------------------------
# Primary path: table cells
# ---------------------------------------------------------------------------

def _get_first_table(raw_response):
    """Return the first table dict from the Azure response, or None."""
    # Azure SDK v1 wraps everything under analyzeResult
    analyze = raw_response.get("analyzeResult") or raw_response
    tables = analyze.get("tables") or []
    return tables[0] if tables else None


def _cell_cx(cell):
    """
    Return the x-center of a table cell.
    Handles both normalised (0-1) and pixel/inch polygon coordinates
    by always returning the raw polygon value — consistent across
    header and data cells since both come from the same Azure response.
    Falls back to columnIndex * 60 only when no polygon is available.
    """
    regions = cell.get("boundingRegions") or []
    if regions:
        poly = regions[0].get("polygon") or []
        if len(poly) >= 6:
            # poly is [x0,y0, x1,y1, x2,y2, x3,y3]
            # x-center = average of all x coords (indices 0,2,4,6)
            xs = [poly[i] for i in range(0, len(poly), 2)]
            return sum(xs) / len(xs)
    # Fallback: use columnIndex as proxy
    return float(cell.get("columnIndex", 0)) * 60


def _parse_table_cells(table, raw_response):
    """
    Build OCRCell objects from Azure table cells using x-proximity mapping.

    Strategy
    --------
    1.  Read the header row to get x-anchor positions for each of the 9
        logical data columns (N.1/F/S × 3 groups).  This is robust to Azure
        inventing phantom columns from printed grid lines.
    2.  For every data row find the S.N cell (leftmost, col=0) to get the
        sheet row number.
    3.  For every non-SN cell in that row, assign it to the closest header
        anchor by x-distance — completely ignoring columnIndex.
    4.  If two cells in the same row map to the same logical column, keep the
        one with higher content priority (non-empty beats empty, longer beats
        shorter).
    """
    cells_raw = table.get("cells", [])
    if not cells_raw:
        return []

    # ---- Step 1: build x-anchor list from header --------------------------
    anchor_x_list = _build_column_map(cells_raw)

    # ---- Step 2: group ALL cells by rowIndex ------------------------------
    rows_by_index: dict[int, list[dict]] = {}
    for cell in cells_raw:
        ri = cell.get("rowIndex")
        if ri is None:
            continue
        rows_by_index.setdefault(ri, []).append(cell)

    # ---- Step 3: emit OCRCell per data row --------------------------------
    ocr_cells = []
    threshold = _confidence_threshold()

    for row_index in sorted(rows_by_index):
        if row_index == 0:
            continue  # header row

        row_cells = rows_by_index[row_index]

        # Find S.N cell — it is always the leftmost cell (smallest x-center)
        sn_cell = min(row_cells, key=_cell_cx)
        sn_value = _clean_content(sn_cell.get("content", ""))
        if not sn_value.isdigit():
            continue
        row_number = int(sn_value)
        if row_number < 1 or row_number > 25:
            continue

        # ---- Step 4: assign every non-SN cell to closest anchor -----------
        # Use a left-to-right stable mapping per row to avoid single-cell
        # x-center drift causing misassignment (e.g. last-row N mapped to F).
        accumulated: dict[tuple, str] = {}

        data_cells = [c for c in row_cells if c is not sn_cell]
        data_cells.sort(key=_cell_cx)

        # Anchor-centric assignment:
        # 1) For each anchor (left-to-right) pick the nearest non-empty cell within tolerance
        # 2) For remaining unassigned non-empty cells, assign left-to-right to remaining anchors
        anchor_index = {(g, f): i for i, (_cx, g, f) in enumerate(anchor_x_list)}

        assigned_cells = set()
        assigned_anchors = set()

        # Precompute cleaned values per field type when assigning to anchors.
        cell_raw = {id(c): c.get("content", "") or "" for c in data_cells}

        def _cell_value_for_field(cell_id, field_type):
            raw = cell_raw.get(cell_id, "")
            if field_type in ("F", "S"):
                return prepare_fs_cell_value(raw)
            return _normalize_value(_clean_content(raw))

        # Pass 1: anchor-centric nearest assignment
        for idx, (anchor_cx, gnum, ftype) in enumerate(anchor_x_list):
            best_cell = None
            best_dist = None
            for cell in data_cells:
                cid = id(cell)
                if cid in assigned_cells:
                    continue
                val = _cell_value_for_field(cid, ftype)
                if not val:
                    continue
                dist = abs(_cell_cx(cell) - anchor_cx)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_cell = cell
            if best_cell is not None and best_dist is not None and best_dist <= HEADER_ANCHOR_TOLERANCE:
                key = (gnum, ftype)
                accumulated[key] = _cell_value_for_field(id(best_cell), ftype)
                assigned_cells.add(id(best_cell))
                assigned_anchors.add(idx)

        # Pass 2: assign remaining non-empty cells left-to-right to remaining anchors
        remaining_anchors = [i for i in range(len(anchor_x_list)) if i not in assigned_anchors]
        remaining_cells = [
            c for c in data_cells
            if id(c) not in assigned_cells and cell_raw.get(id(c), "").strip()
        ]
        # Greedily assign each remaining cell to the nearest remaining anchor
        LEFT_BIAS = 8.0
        for cell in sorted(remaining_cells, key=_cell_cx):
            if not remaining_anchors:
                break
            cx = _cell_cx(cell)
            # compute distances to remaining anchors
            dists = [(ai, abs(anchor_x_list[ai][0] - cx)) for ai in remaining_anchors]
            dists.sort(key=lambda t: t[1])
            best_idx, best_dist = dists[0]
            # prefer a leftward anchor if it's nearly as close
            left_candidates = [ai for ai,dist in dists if ai < best_idx and dist <= best_dist + LEFT_BIAS]
            if left_candidates:
                chosen_idx = min(left_candidates)
            else:
                chosen_idx = best_idx

            _cx, gnum, ftype = anchor_x_list[chosen_idx]
            key = (gnum, ftype)
            value = _cell_value_for_field(id(cell), ftype)
            existing = accumulated.get(key)
            if existing is None or len(value) > len(existing):
                accumulated[key] = value
            remaining_anchors.remove(chosen_idx)

        # ---- Step 5: emit one OCRCell per logical column ------------------
        for cx, group_number, field_type in anchor_x_list:
            key = (group_number, field_type)
            value = accumulated.get(key, "")
            # find candidate raw cells that mapped to this logical slot
            candidates = [
                c for c in row_cells if c is not sn_cell and
                _assign_column_by_x(_cell_cx(c), anchor_x_list)[0:2] == (group_number, field_type)
            ]
            confidence = 0.0
            bounding_box = None
            if candidates:
                best = max(candidates, key=lambda c: len(str(c.get("content") or "")))
                confidence = _cell_confidence(best)
                bounding_box = _cell_bounding_box(best)

            ocr_cells.append(OCRCell(
                row_number=row_number,
                group_number=group_number,
                field_type=field_type,
                value=value,
                confidence=confidence,
                bounding_box=bounding_box,
            ))

    logger.info(
        "Table-cell parser produced %d provisional OCRCell objects from %d data rows",
        len(ocr_cells),
        len([ri for ri in rows_by_index if ri > 0]),
    )

    # Ensure full 25x9 grid: for any missing (row,group,field) emit empty cell
    final_index = {(c.row_number, c.group_number, c.field_type): c for c in ocr_cells}
    final_cells = []
    for row_num in range(1, 26):
        for group_num in range(1, 4):
            for field_type in ("N", "F", "S"):
                key = (row_num, group_num, field_type)
                if key in final_index:
                    final_cells.append(final_index[key])
                else:
                    final_cells.append(OCRCell(
                        row_number=row_num,
                        group_number=group_num,
                        field_type=field_type,
                        value="",
                        confidence=0.0,
                        bounding_box=None,
                    ))

    logger.info("Table-cell parser finalized to %d OCRCell objects", len(final_cells))
    return final_cells


def _build_column_map(cells_raw):
    """
    Build a mapping from Azure columnIndex → (group_number, field_type).

    THE CORE PROBLEM THIS SOLVES
    -----------------------------
    Azure detects phantom columns from printed grid lines, so a 10-column
    sheet ends up with 13 Azure column slots.  The header row is also sparse —
    it only has cells for the labelled columns (S.N, N.1, F, S, N.2 …), not
    for the phantom ones.  This means you cannot use columnIndex directly.

    THE APPROACH
    ------------
    1. Read the header row cells and sort them by their x-center position.
    2. Walk them left-to-right and assign logical labels in order:
       S.N, N.1, F, S, N.2, F, S, N.3, F, S  (exactly 10 logical columns).
    3. For each header cell, record its x-center as the "anchor x" for that
       logical column.
    4. For data cells, find the closest anchor x by Euclidean distance and
       assign the cell to that logical column — completely ignoring columnIndex.

    This is robust to any number of phantom columns Azure invents.
    """
    header_cells = [c for c in cells_raw if c.get("rowIndex") == 0]
    if not header_cells:
        raise OCRProviderError("No header row found in table; cannot build column anchors.")

    # ── Step 1: get x-center for each header cell ──────────────────────────
    def _header_cx(cell):
        regions = cell.get("boundingRegions") or []
        poly = regions[0].get("polygon", []) if regions else []
        if len(poly) >= 6:
            xs = [poly[i] for i in range(0, len(poly), 2)]
            return sum(xs) / len(xs)
        # fall back to columnIndex as a proxy (not ideal but safe)
        return float(cell.get("columnIndex", 0)) * 60

    header_cells.sort(key=_header_cx)

    # ── Step 2: walk header left-to-right and assign logical labels ─────────
    # Expected sequence (ignoring empty/phantom header cells):
    #   S.N  N.1  F  S  N.2  F  S  N.3  F  S
    LOGICAL_SEQUENCE = [
        "SN",
        "N", "F", "S",   # group 1
        "N", "F", "S",   # group 2
        "N", "F", "S",   # group 3
    ]

    def _normalise_label(raw):
        return "".join(ch.upper() for ch in raw if ch.isalnum())

    def _matches_logical(norm_label, logical):
        if logical == "SN":
            return norm_label in ("SN", "SNO", "SN0")
        if logical == "N":
            return norm_label.startswith("N")
        return norm_label == logical  # "F" or "S"

    # Pair each header cell with its logical slot, in order
    logical_idx = 0
    # anchor_x_map: logical_slot_index (0-9) → x_center
    anchor_x_list = []   # [(x_center, group_number, field_type), ...]

    for cell in header_cells:
        if logical_idx >= len(LOGICAL_SEQUENCE):
            break
        norm = _normalise_label(_clean_content(cell.get("content", "")))
        if not norm:
            continue  # phantom/empty header cell — skip
        # Advance logical pointer until we find a matching slot
        while logical_idx < len(LOGICAL_SEQUENCE):
            if _matches_logical(norm, LOGICAL_SEQUENCE[logical_idx]):
                break
            logical_idx += 1
        if logical_idx >= len(LOGICAL_SEQUENCE):
            break

        cx = _header_cx(cell)
        slot = LOGICAL_SEQUENCE[logical_idx]
        if slot != "SN":
            # group_number = which N we've seen so far (1, 2, or 3)
            n_count = sum(1 for s in LOGICAL_SEQUENCE[:logical_idx+1] if s == "N")
            group_number = n_count
            field_type = slot  # "N", "F", or "S"
            anchor_x_list.append((cx, group_number, field_type))

        logical_idx += 1

    if len(anchor_x_list) < 9:
        raise OCRProviderError(
            "Header x-anchor detection incomplete (%d/9); expected 9 data column anchors." % len(anchor_x_list)
        )

    logger.info(
        "Header x-anchors: %s",
        [(round(cx, 1), g, f) for cx, g, f in anchor_x_list],
    )
    return anchor_x_list  # list of (anchor_x, group, field_type)


def _assign_column_by_x(data_cell_cx, anchor_x_list):
    """
    Given a data cell's x-center and the list of anchor x-positions from the
    header, return (group_number, field_type, distance) for the closest anchor.
    """
    best = min(anchor_x_list, key=lambda a: abs(a[0] - data_cell_cx))
    distance = abs(best[0] - data_cell_cx)
    if distance > HEADER_ANCHOR_TOLERANCE:
        logger = logging.getLogger(__name__)
        logger.debug(
            "Data cell at x=%.1f is %.1f px from nearest header anchor; tolerance=%.1f",
            data_cell_cx, distance, HEADER_ANCHOR_TOLERANCE,
        )
    return best[1], best[2], distance


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------

def _clean_content(raw: str) -> str:
    """
    Strip whitespace and Azure's :selected: / :unselected: checkbox markers.

    Azure sometimes emits  'X\n:selected:'  for a ticked checkbox cell —
    the newline must be removed before the X-variant regex can match.
    """
    if raw is None:
        return ""
    # Remove Azure checkbox annotations
    raw = re.sub(r':selected:|:unselected:', '', raw)
    # Normalize multiplication sign variants to ASCII X
    raw = raw.replace('\u00d7', 'X').replace('×', 'X')
    # Strip degree symbol when present
    raw = raw.replace('\u00b0', '')
    # Collapse whitespace/newlines
    raw = re.sub(r'\s+', ' ', raw).strip()
    return raw


def _normalize_value(value: str) -> str:
    """
    Normalise a single cell value.

    - Handwritten 'x' variants  → 'x'
    - Leading zeros are PRESERVED (values stay as strings)
    - Empty / whitespace-only   → ''
    """
    if not value:
        return ""
    # Preserve asterisk marker
    if value == '*':
        return value
    if X_VARIANTS.match(value):
        return 'X'
    # Strip trailing degree symbol if any remained
    if value.endswith('°'):
        value = value[:-1]
    return value.strip()


# ---------------------------------------------------------------------------
# Confidence + bounding box helpers
# ---------------------------------------------------------------------------

def _confidence_threshold():
    try:
        return float(settings.OCR_CONFIDENCE_THRESHOLD)
    except Exception:
        # Django settings may not be configured in standalone runs; default to 0.7
        return 0.7


def _cell_confidence(cell: dict) -> float:
    confidence = cell.get("confidence")
    if confidence is None:
        return 0.0
    try:
        return float(confidence)
    except (TypeError, ValueError):
        return 0.0


def _cell_bounding_box(cell: dict):
    regions = cell.get("boundingRegions") or []
    if not regions:
        return None
    return {"polygon": regions[0].get("polygon")}


# ---------------------------------------------------------------------------
# Fallback: word-level parser (kept from original, unchanged in logic)
# This is only reached when Azure returns no table at all.
# ---------------------------------------------------------------------------

DATA_TOP = 0.16
DATA_BOTTOM = 0.96
COLUMN_BOUNDARIES = [0.0, 0.08, 0.20, 0.31, 0.38, 0.47, 0.58, 0.67, 0.80, 0.90, 1.0]
COLUMN_LABELS = ["S.N", "N.1", "F1", "S1", "N.2", "F2", "S2", "N.3", "F3", "S3"]
ROW_BOUNDARIES = [
    DATA_TOP + ((DATA_BOTTOM - DATA_TOP) * i / 25)
    for i in range(26)
]
FIELD_MAP = {
    1: (1, "N"),
    2: (1, "F"),
    3: (1, "S"),
    4: (2, "N"),
    5: (2, "F"),
    6: (2, "S"),
    7: (3, "N"),
    8: (3, "F"),
    9: (3, "S"),
}
HEADER_WORDS = {
    "sn", "sno", "serial", "n1", "n2", "n3", "f", "s", "fs",
    "dr", "drn", "name", "date", "pz", "enterprise", "enterprises",
}


def _parse_with_word_fallback(raw_response):
    words = _extract_words(raw_response)
    row_boundaries = ROW_BOUNDARIES
    threshold = _confidence_threshold()

    buckets = {
        (row_number, col_index): []
        for row_number in range(1, 26)
        for col_index in range(1, 10)
    }

    for word in words:
        if _is_header_word(word):
            continue
        col_index = _column_for_x(word["center_x"])
        if col_index is None or col_index == 0 or col_index not in FIELD_MAP:
            continue
        row_number = _row_for_y(word["center_y"], row_boundaries)
        if row_number is None:
            continue
        buckets[(row_number, col_index)].append(word)

    cells = []
    for row_number in range(1, 26):
        for col_index in range(1, 10):
            group_number, field_type = FIELD_MAP[col_index]
            cell_words = sorted(
                buckets[(row_number, col_index)],
                key=lambda w: (w["center_y"], w["center_x"])
            )
            joined = " ".join(w["content"] for w in cell_words).strip()
            if field_type in ("F", "S"):
                value = prepare_fs_cell_value(joined)
            else:
                value = _normalize_value(_clean_content(joined))
            confidence = min((w["confidence"] for w in cell_words), default=1.0)
            cells.append(OCRCell(
                row_number=row_number,
                group_number=group_number,
                field_type=field_type,
                value=value,
                confidence=confidence,
                bounding_box=None,
            ))

    logger.info("Word fallback produced %d OCRCell objects", len(cells))
    return cells


def _extract_words(raw_response):
    words = []
    analyze = raw_response.get("analyzeResult") or raw_response
    for page in analyze.get("pages") or []:
        pw = float(page.get("width") or 1)
        ph = float(page.get("height") or 1)
        for word in page.get("words") or []:
            poly = word.get("polygon") or []
            norm = _normalized_polygon(poly, pw, ph)
            if not norm:
                continue
            content = str(word.get("content") or "").strip()
            if not content:
                continue
            cx = sum(p[0] for p in norm) / len(norm)
            cy = sum(p[1] for p in norm) / len(norm)
            conf = word.get("confidence")
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 1.0
            words.append({"content": content, "confidence": conf,
                          "center_x": cx, "center_y": cy})
    return words


def _normalized_polygon(polygon, pw, ph):
    if not polygon or len(polygon) < 6:
        return []
    if all(isinstance(v, (int, float)) for v in polygon):
        pts = [(float(polygon[i]), float(polygon[i+1])) for i in range(0, len(polygon)-1, 2)]
    else:
        return []
    if max(p[0] for p in pts) <= 1 and max(p[1] for p in pts) <= 1:
        return pts
    return [(x / pw, y / ph) for x, y in pts]


def _is_header_word(word):
    norm = "".join(ch.lower() for ch in word["content"] if ch.isalnum())
    return norm in HEADER_WORDS


def _column_for_x(cx):
    for i in range(10):
        if COLUMN_BOUNDARIES[i] <= cx < COLUMN_BOUNDARIES[i+1]:
            return i
    if cx == COLUMN_BOUNDARIES[-1]:
        return 9
    return None


def _row_for_y(cy, boundaries):
    for i in range(25):
        if boundaries[i] <= cy < boundaries[i+1]:
            return i + 1
    if cy == boundaries[-1]:
        return 25
    return None
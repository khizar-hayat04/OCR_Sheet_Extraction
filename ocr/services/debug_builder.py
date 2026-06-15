"""Debug data builder for Azure OCR visualization"""

from .parser import (
    _extract_words, _column_for_x, _row_for_y, _is_header_word,
    COLUMN_LABELS, FIELD_MAP, COLUMN_BOUNDARIES
)
from django.conf import settings


def build_azure_debug_data(raw_response):
    """
    Build debug data structure for visualization and logging.
    """
    words = _extract_words(raw_response)
    y_values = sorted(word['center_y'] for word in words)
    data_top = getattr(settings, 'OCR_TABLE_TOP', 0.16)
    data_bottom = getattr(settings, 'OCR_TABLE_BOTTOM', 0.96)
    row_boundaries = [data_top + ((data_bottom - data_top) * i / 25) for i in range(26)]
    
    word_rows = []
    for word in words:
        cx, cy = word['center_x'], word['center_y']
        col_index = _column_for_x(cx)
        row_number = _row_for_y(cy, row_boundaries)
        is_header = _is_header_word(word)
        ignored_reason = ''
        
        if is_header:
            ignored_reason, row_number, col_index = 'header', None, None
        elif col_index == 0:
            ignored_reason, row_number = 'sn_column', None
        elif row_number is None:
            ignored_reason = 'outside_row_bands'
        elif col_index is None:
            ignored_reason = 'outside_column_bands'
        
        assigned_col = COLUMN_LABELS[col_index] if col_index is not None and 0 <= col_index < len(COLUMN_LABELS) else ''
        final_key = ''
        if col_index in FIELD_MAP and row_number:
            grp, fld = FIELD_MAP[col_index]
            final_key = f'R{row_number}:G{grp}:{fld}'
        
        word_rows.append({
            'text': word['content'],
            'content': word['content'],
            'confidence': word['confidence'],
            'x_center': cx,
            'y_center': cy,
            'center_x': cx,
            'center_y': cy,
            'polygon': [],
            'normalized_polygon': [],
            'ignored_reason': ignored_reason,
            'row_anchor': None,
            'assigned_row': row_number,
            'assigned_column': assigned_col,
            'group_number': None,
            'field_type': '',
            'assigned_col_index': col_index,
            'final_cell_key': final_key,
            'boundary_risk': False
        })
    
    row_centers = []
    if len(y_values) >= 2:
        clusters, current = [], [y_values[0]]
        for y in y_values[1:]:
            if abs(y - sum(current)/len(current)) > 0.01:
                clusters.append(current)
                current = [y]
            else:
                current.append(y)
        clusters.append(current)
        row_centers = [sum(c)/len(c) for c in clusters[:25]]
    
    return {
        'words': words,
        'word_rows': word_rows,
        'constants': {
            'DATA_TOP': data_top,
            'DATA_BOTTOM': data_bottom,
            'COLUMN_BOUNDARIES': COLUMN_BOUNDARIES,
            'COLUMN_LABELS': COLUMN_LABELS
        },
        'row_anchor_count': 0,
        'row_anchor_positions': {},
        'row_detection_method': 'fixed_fallback',
        'detected_cluster_count': len(row_centers),
        'row_centers': row_centers,
        'row_boundaries': row_boundaries,
        'first_row_center': row_centers[0] if row_centers else None,
        'last_row_center': row_centers[-1] if row_centers else None,
        'row_coverage_ratio': (row_centers[-1] - row_centers[0]) / (data_bottom - data_top) if len(row_centers) >= 2 else 0,
        'fallback_used': True,
        'words_mapped_by_row': {},
        'joined_cells': []
    }

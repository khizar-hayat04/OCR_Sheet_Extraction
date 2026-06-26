import json

from django.conf import settings
from django.db import transaction

from .azure_document_intelligence import AzureDocumentIntelligenceProvider
from .azure_cell_crop import AzureCellCropProvider
from .base import OCRCell, OCRProviderError
from .mock import MockOCRProvider
from .validators import is_suspicious_data_value, resolve_fs_columns


def get_provider():
    provider = settings.OCR_PROVIDER.lower()
    if provider == "azure":
        if settings.OCR_EXTRACTION_MODE.lower() == "cell_crop":
            return AzureCellCropProvider()
        return AzureDocumentIntelligenceProvider()
    if provider == "mock":
        return MockOCRProvider()
    raise OCRProviderError(f"Unsupported OCR provider: {settings.OCR_PROVIDER}")


@transaction.atomic
def deduct_quota_once(sheet):
    if sheet.quota_deducted:
        return
    quota = sheet.user.sheet_quota
    quota.remaining_sheets -= 1
    quota.save(update_fields=["remaining_sheets", "updated_at"])
    sheet.quota_deducted = True
    sheet.save(update_fields=["quota_deducted", "updated_at"])


def process_sheet(sheet, provider=None):
    from ocr.models import ExtractedCell, SheetUpload

    provider = provider or get_provider()
    sheet.status = SheetUpload.STATUS_PROCESSING
    sheet.error_message = ""
    sheet.save(update_fields=["status", "error_message", "updated_at"])
    deduct_quota_once(sheet)

    try:
        raw_response = provider.analyze(sheet.image.path)
        parsed_cells = provider.parse_cells(raw_response)
        flagged_cells = {
            (cell.row_number, cell.group_number, cell.field_type): (
                cell.is_flagged or is_suspicious_data_value(cell.value, cell.value, cell.field_type)
            )
            for cell in parsed_cells
        }
        parsed_cells = resolve_fs_columns(parsed_cells)
        if not parsed_cells:
            raise OCRProviderError("OCR returned no editable cells.")
    except Exception as exc:
        sheet.status = SheetUpload.STATUS_FAILED
        sheet.error_message = str(exc)
        sheet.save(update_fields=["status", "error_message", "updated_at"])
        raise

    threshold = settings.OCR_CONFIDENCE_THRESHOLD
    with transaction.atomic():
        sheet.raw_ocr_response = raw_response
        sheet.status = SheetUpload.STATUS_REVIEW_REQUIRED
        sheet.save(update_fields=["raw_ocr_response", "status", "updated_at"])
        ExtractedCell.objects.filter(sheet=sheet).delete()
        ExtractedCell.objects.bulk_create(
            [
                ExtractedCell(
                    sheet=sheet,
                    row_number=cell.row_number,
                    group_number=cell.group_number,
                    field_type=cell.field_type,
                    extracted_value=cell.value,
                    confidence=cell.confidence,
                    corrected_value="",
                    is_low_confidence=cell.confidence < threshold,
                    is_flagged=flagged_cells.get((cell.row_number, cell.group_number, cell.field_type), False),
                    bounding_box=cell.bounding_box,
                )
                for cell in parsed_cells
            ]
        )
    return sheet


def build_review_rows(sheet):
    cells = sheet.cells.all()
    by_key = {(cell.row_number, cell.group_number, cell.field_type): cell for cell in cells}
    rows = []
    for row_number in range(1, 26):
        rows.append(
            {
                "number": row_number,
                "cells": [
                    by_key.get((row_number, group, field_type))
                    for group in (1, 2, 3)
                    for field_type in ("N", "F", "S")
                ],
            }
        )
    return rows


def confirmed_payload(sheet):
    resolved_cells = [
        OCRCell(
            row_number=cell.row_number,
            group_number=cell.group_number,
            field_type=cell.field_type,
            value=cell.review_value,
            confidence=cell.confidence,
            is_flagged=cell.is_flagged,
        )
        for cell in sheet.cells.all()
    ]
    resolve_fs_columns(resolved_cells)
    by_key = {(cell.row_number, cell.group_number, cell.field_type): cell for cell in resolved_cells}

    rows = []
    totals = {
        "F": {"group_1": 0, "group_2": 0, "group_3": 0, "overall": 0},
        "S_numeric": {"group_1": 0, "group_2": 0, "group_3": 0, "overall": 0},
    }
    for row_number in range(1, 26):
        row_payload = {"S.N": row_number, "groups": []}
        for group in range(3):
            n_cell = by_key.get((row_number, group + 1, "N"))
            f_cell = by_key.get((row_number, group + 1, "F"))
            s_cell = by_key.get((row_number, group + 1, "S"))
            f_value = f_cell.value if f_cell else ""
            s_value = s_cell.value if s_cell else ""
            _add_numeric_total(totals["F"], group + 1, f_value)
            _add_numeric_total(totals["S_numeric"], group + 1, s_value)
            row_payload["groups"].append(
                {
                    "group_number": group + 1,
                    "N": n_cell.value if n_cell else "",
                    "F": f_value,
                    "S": s_value,
                }
            )
        rows.append(row_payload)
    return {"rows": rows, "totals": totals}


def sync_extracted_data_json(sheet):
    if sheet.final_data is None:
        return
    output_path = settings.BASE_DIR / "extracted_data.json"
    output_path.write_text(json.dumps(sheet.final_data, indent=2), encoding="utf-8")


def _add_numeric_total(total_map, group_number, value):
    if value and value.isdigit():
        amount = int(value)
        total_map[f"group_{group_number}"] += amount
        total_map["overall"] += amount

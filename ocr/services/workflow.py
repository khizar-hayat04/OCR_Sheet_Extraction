from django.conf import settings
from django.db import transaction

from .azure_document_intelligence import AzureDocumentIntelligenceProvider
from .azure_cell_crop import AzureCellCropProvider
from .base import OCRProviderError
from .mock import MockOCRProvider


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
    rows = []
    totals = {
        "F": {"group_1": 0, "group_2": 0, "group_3": 0, "overall": 0},
        "S_numeric": {"group_1": 0, "group_2": 0, "group_3": 0, "overall": 0},
    }
    for row in build_review_rows(sheet):
        row_payload = {"S.N": row["number"], "groups": []}
        for group in range(3):
            cells = row["cells"][group * 3 : group * 3 + 3]
            f_value = cells[1].review_value if cells[1] else ""
            s_value = cells[2].review_value if cells[2] else ""
            _add_numeric_total(totals["F"], group + 1, f_value)
            _add_numeric_total(totals["S_numeric"], group + 1, s_value)
            row_payload["groups"].append(
                {
                    "group_number": group + 1,
                    "N": cells[0].review_value if cells[0] else "",
                    "F": f_value,
                    "S": s_value,
                }
            )
        rows.append(row_payload)
    return {"rows": rows, "totals": totals}


def _add_numeric_total(total_map, group_number, value):
    if value and value.isdigit():
        amount = int(value)
        total_map[f"group_{group_number}"] += amount
        total_map["overall"] += amount

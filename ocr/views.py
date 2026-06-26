import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ConfirmSheetForm, SheetUploadForm
from .models import ExtractedCell, SheetUpload
from .services.debug_overlay import generate_debug_overlay
from .services.debug_builder import build_azure_debug_data
from .services.validators import validate_cell_value
from .services.workflow import build_review_rows, confirmed_payload, process_sheet, sync_extracted_data_json


LIMIT_MESSAGE = "Your sheet limit is finished. Please contact admin."
logger = logging.getLogger(__name__)


def _available_quota(user):
    quota = getattr(user, "sheet_quota", None)
    return quota if quota and quota.has_available_sheet() else None


@login_required
def upload_sheet_view(request):
    if request.method == "POST":
        form = SheetUploadForm(request.POST, request.FILES)
        if form.is_valid():
            quota = _available_quota(request.user)
            if not quota:
                messages.error(request, LIMIT_MESSAGE)
                return render(request, "ocr/upload.html", {"form": form})

            sheet = SheetUpload.objects.create(user=request.user, image=form.cleaned_data["image"])
            try:
                process_sheet(sheet)
            except Exception:
                logger.exception("OCR processing raised an exception for sheet %s", sheet.pk)
                sheet.refresh_from_db()
                if sheet.status != SheetUpload.STATUS_FAILED and sheet.cells.exists():
                    return redirect("ocr:review", pk=sheet.pk)
                return render(request, "ocr/error.html", {"sheet": sheet})
            return redirect("ocr:review", pk=sheet.pk)
    else:
        form = SheetUploadForm()
    return render(request, "ocr/upload.html", {"form": form})


@login_required
def review_sheet_view(request, pk):
    sheet = get_object_or_404(SheetUpload, pk=pk, user=request.user)
    if sheet.status == SheetUpload.STATUS_FAILED:
        return render(request, "ocr/error.html", {"sheet": sheet})
    cells = sheet.cells.all()
    form = ConfirmSheetForm(cells=cells)
    return render(request, "ocr/review.html", {"sheet": sheet, "rows": build_review_rows(sheet), "form": form})


@login_required
@require_POST
def confirm_sheet_view(request, pk):
    sheet = get_object_or_404(SheetUpload, pk=pk, user=request.user)
    cells = sheet.cells.all()
    form = ConfirmSheetForm(request.POST, cells=cells)
    if not form.is_valid():
        return render(request, "ocr/review.html", {"sheet": sheet, "rows": build_review_rows(sheet), "form": form})

    with transaction.atomic():
        form.save()
        sheet.final_data = confirmed_payload(sheet)
        sheet.status = SheetUpload.STATUS_CONFIRMED
        sheet.save(update_fields=["final_data", "status", "updated_at"])
        sync_extracted_data_json(sheet)
    messages.success(request, "Sheet confirmed successfully.")
    return redirect("ocr:review", pk=sheet.pk)


@login_required
@require_POST
def correct_cell_view(request, pk, cell_pk):
    sheet = get_object_or_404(SheetUpload, pk=pk, user=request.user)
    cell = get_object_or_404(ExtractedCell, pk=cell_pk, sheet=sheet)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid correction payload."}, status=400)

    try:
        corrected_value = validate_cell_value(cell.field_type, payload.get("value", ""))
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    with transaction.atomic():
        old_value = cell.review_value
        changed_cells = []

        cell.corrected_value = corrected_value
        cell.is_flagged = False
        cell.is_confirmed = True
        cell.save(update_fields=["corrected_value", "is_flagged", "is_confirmed"])
        changed_cells.append(_cell_payload(cell))

        if cell.field_type in (ExtractedCell.FIELD_F, ExtractedCell.FIELD_S):
            changed_cells.extend(_propagate_ditto_correction(cell, old_value, corrected_value))

        sheet.final_data = confirmed_payload(sheet)
        sheet.save(update_fields=["final_data", "updated_at"])
        sync_extracted_data_json(sheet)

    return JsonResponse(
        {
            "ok": True,
            "cell": _cell_payload(cell),
            "changed_cells": changed_cells,
            "final_data": sheet.final_data,
        }
    )


def _cell_payload(cell):
    return {
        "id": cell.pk,
        "value": cell.review_value,
        "is_flagged": cell.is_flagged,
        "row_number": cell.row_number,
        "group_number": cell.group_number,
        "field_type": cell.field_type,
    }


def _propagate_ditto_correction(source_cell, old_value, corrected_value):
    changed_cells = []
    if old_value == corrected_value:
        return changed_cells

    queryset = (
        ExtractedCell.objects.filter(
            sheet=source_cell.sheet,
            group_number=source_cell.group_number,
            field_type=source_cell.field_type,
            row_number__gt=source_cell.row_number,
        )
        .order_by("row_number")
    )
    for cell in queryset:
        if cell.corrected_value or cell.review_value != old_value:
            break
        cell.corrected_value = corrected_value
        cell.save(update_fields=["corrected_value"])
        changed_cells.append(_cell_payload(cell))
    return changed_cells


@login_required
@require_POST
def rerun_ocr_view(request, pk):
    sheet = get_object_or_404(SheetUpload, pk=pk, user=request.user)
    try:
        process_sheet(sheet)
    except Exception:
        logger.exception("OCR rerun raised an exception for sheet %s", sheet.pk)
        sheet.refresh_from_db()
        if sheet.status != SheetUpload.STATUS_FAILED and sheet.cells.exists():
            return redirect("ocr:review", pk=sheet.pk)
        return render(request, "ocr/error.html", {"sheet": sheet})
    return redirect("ocr:review", pk=sheet.pk)


@staff_member_required
def debug_sheet_view(request, pk):
    sheet = get_object_or_404(SheetUpload, pk=pk)
    raw_response = sheet.raw_ocr_response or {}
    if raw_response.get("mode") == "cell_crop":
        return render(request, "ocr/debug.html", {"sheet": sheet, "cell_crop_debug": raw_response})

    debug_data = build_azure_debug_data(raw_response)
    overlay_url = generate_debug_overlay(sheet, debug_data)
    return render(request, "ocr/debug.html", {"sheet": sheet, "debug": debug_data, "overlay_url": overlay_url})

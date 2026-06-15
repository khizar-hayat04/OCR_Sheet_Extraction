from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ConfirmSheetForm, SheetUploadForm
from .models import SheetUpload
from .services.debug_overlay import generate_debug_overlay
from .services.debug_builder import build_azure_debug_data
from .services.workflow import build_review_rows, confirmed_payload, process_sheet


LIMIT_MESSAGE = "Your sheet limit is finished. Please contact admin."


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
    messages.success(request, "Sheet confirmed successfully.")
    return redirect("ocr:review", pk=sheet.pk)


@login_required
@require_POST
def rerun_ocr_view(request, pk):
    sheet = get_object_or_404(SheetUpload, pk=pk, user=request.user)
    try:
        process_sheet(sheet)
    except Exception:
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

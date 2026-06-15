from django.contrib import admin

from .models import ExtractedCell, SheetQuota, SheetUpload


@admin.register(SheetQuota)
class SheetQuotaAdmin(admin.ModelAdmin):
    list_display = ("user", "remaining_sheets", "updated_at")
    search_fields = ("user__username", "user__email")


class ExtractedCellInline(admin.TabularInline):
    model = ExtractedCell
    extra = 0
    readonly_fields = ("row_number", "group_number", "field_type", "extracted_value", "confidence", "is_low_confidence")


@admin.register(SheetUpload)
class SheetUploadAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "quota_deducted", "created_at", "updated_at")
    list_filter = ("status", "quota_deducted")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("raw_ocr_response", "final_data", "created_at", "updated_at")
    inlines = [ExtractedCellInline]


@admin.register(ExtractedCell)
class ExtractedCellAdmin(admin.ModelAdmin):
    list_display = ("sheet", "row_number", "group_number", "field_type", "extracted_value", "corrected_value", "confidence", "is_low_confidence", "is_confirmed")
    list_filter = ("field_type", "group_number", "is_low_confidence", "is_confirmed")

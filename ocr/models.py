from django.conf import settings
from django.db import models


class SheetQuota(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sheet_quota")
    remaining_sheets = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - {self.remaining_sheets} sheets"

    def has_available_sheet(self):
        return self.remaining_sheets > 0


class SheetUpload(models.Model):
    STATUS_UPLOADED = "uploaded"
    STATUS_PROCESSING = "processing"
    STATUS_EXTRACTED = "extracted"
    STATUS_REVIEW_REQUIRED = "review_required"
    STATUS_CONFIRMED = "confirmed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_EXTRACTED, "Extracted"),
        (STATUS_REVIEW_REQUIRED, "Review required"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_FAILED, "Failed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ocr_sheets")
    image = models.ImageField(upload_to="ocr/sheets/")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UPLOADED)
    raw_ocr_response = models.JSONField(null=True, blank=True)
    final_data = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    quota_deducted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Sheet {self.pk} ({self.status})"


class ExtractedCell(models.Model):
    FIELD_N = "N"
    FIELD_F = "F"
    FIELD_S = "S"

    FIELD_CHOICES = [
        (FIELD_N, "N"),
        (FIELD_F, "F"),
        (FIELD_S, "S"),
    ]

    sheet = models.ForeignKey(SheetUpload, on_delete=models.CASCADE, related_name="cells")
    row_number = models.PositiveSmallIntegerField()
    group_number = models.PositiveSmallIntegerField()
    field_type = models.CharField(max_length=1, choices=FIELD_CHOICES)
    extracted_value = models.CharField(max_length=32, blank=True, default="")
    confidence = models.FloatField(default=0)
    corrected_value = models.CharField(max_length=32, blank=True, default="")
    is_low_confidence = models.BooleanField(default=False)
    is_flagged = models.BooleanField(default=False)
    is_confirmed = models.BooleanField(default=False)
    bounding_box = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["row_number", "group_number", "field_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["sheet", "row_number", "group_number", "field_type"],
                name="unique_sheet_grid_cell",
            )
        ]

    def __str__(self):
        return f"R{self.row_number} G{self.group_number} {self.field_type}"

    @property
    def review_value(self):
        return self.corrected_value if self.corrected_value != "" else self.extracted_value

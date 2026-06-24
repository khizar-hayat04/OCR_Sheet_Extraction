import json
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import ExtractedCell, SheetQuota, SheetUpload
from .services.base import OCRCell, OCRProviderError
from .services.azure_cell_crop import AzureCellCropProvider
from .services.parser import parse_azure_result
from .services.debug_builder import build_azure_debug_data
from .services.validators import is_zero_marker, normalize_fs_ocr_text, normalize_fs_value, normalize_n_ocr_text, resolve_fs_columns
from .services.workflow import process_sheet
from .utils.image_preprocessing import crop_grid_cells


def fake_image(name="sheet.png"):
    image = BytesIO()
    Image.new("RGB", (1, 1), "white").save(image, format="PNG")
    image.seek(0)
    return SimpleUploadedFile(
        name,
        image.getvalue(),
        content_type="image/png",
    )


def fake_cells():
    cells = []
    for row in range(1, 26):
        for group in (1, 2, 3):
            cells.append(OCRCell(row, group, "N", f"{row:02d}{group}", 0.95))
            cells.append(OCRCell(row, group, "F", "100", 0.89 if row == 1 and group == 1 else 0.95))
            cells.append(OCRCell(row, group, "S", "x", 0.95))
    return cells


class FakeProvider:
    def analyze(self, file_path):
        return {"tables": [{"rowCount": 25, "columnCount": 10}]}

    def parse_cells(self, raw_response):
        return fake_cells()


class FailingProvider(FakeProvider):
    def analyze(self, file_path):
        raise OCRProviderError("boom")


def azure_word(content, row, col, confidence=0.95):
    col_width = 0.10
    row_top = 0.16
    row_height = (0.96 - 0.16) / 25
    center_x = (col * col_width) + (col_width / 2)
    center_y = row_top + ((row - 1) * row_height) + (row_height / 2)
    half_w = 0.01
    half_h = 0.006
    return {
        "content": content,
        "confidence": confidence,
        "polygon": [
            center_x - half_w,
            center_y - half_h,
            center_x + half_w,
            center_y - half_h,
            center_x + half_w,
            center_y + half_h,
            center_x - half_w,
            center_y + half_h,
        ],
    }


def azure_position_word(content, center_x, center_y, confidence=0.95):
    half_w = 0.01
    half_h = 0.006
    return {
        "content": content,
        "confidence": confidence,
        "polygon": [
            center_x - half_w,
            center_y - half_h,
            center_x + half_w,
            center_y - half_h,
            center_x + half_w,
            center_y + half_h,
            center_x - half_w,
            center_y + half_h,
        ],
    }


def azure_serial_anchors():
    return [azure_word(str(row), row, 0, 0.99) for row in range(1, 26)]


def fake_azure_response(words):
    return {"pages": [{"width": 1, "height": 1, "words": words}]}


class AzureWordsProvider:
    def analyze(self, file_path):
        return fake_azure_response(
            [
                azure_word("193", 1, 1, 0.97),
                azure_word("100", 1, 2, 0.88),
                azure_word("x", 1, 3, 0.93),
                azure_word("42", 25, 9, 0.91),
            ]
        )

    def parse_cells(self, raw_response):
        return parse_azure_result(raw_response)


class CellCropProvider:
    def analyze(self, file_path):
        cells = []
        for row in range(1, 26):
            for col in range(10):
                cells.append(
                    {
                        "row_number": row,
                        "column_index": col,
                        "column_label": ["S.N", "N.1", "F1", "S1", "N.2", "F2", "S2", "N.3", "F3", "S3"][col],
                        "text": "9/90" if row == 14 and col == 4 else ("" if row == 2 and col == 1 else f"{row}{col}"),
                        "confidence": 0 if row == 2 and col == 1 else 0.95,
                        "box": {"left": col, "top": row},
                        "crop_url": "",
                    }
                )
        return {"mode": "cell_crop", "cells": cells, "grid_overlay_url": "", "normalized_table_url": ""}

    def parse_cells(self, raw_response):
        return AzureCellCropProvider(endpoint="endpoint", key="key").parse_cells(raw_response)


class SingleCallCellCropProvider(AzureCellCropProvider):
    def __init__(self):
        super().__init__(endpoint="endpoint", key="key")
        self.calls = 0

    def _analyze_image_bytes(self, image_bytes):
        self.calls += 1
        return {
            "pages": [
                {
                    "width": 1000,
                    "height": 2500,
                    "words": [
                        {"content": "193", "confidence": 0.96, "polygon": [90, 10, 150, 10, 150, 80, 90, 80]},
                        {"content": "50", "confidence": 0.92, "polygon": [205, 10, 270, 10, 270, 80, 205, 80]},
                        {"content": "x", "confidence": 0.91, "polygon": [320, 10, 360, 10, 360, 80, 320, 80]},
                    ],
                }
            ]
        }


class FSValueValidationTests(TestCase):
    def test_numeric_values_are_preserved(self):
        for value in ("25", "17", "0", "100", "738"):
            self.assertEqual(normalize_fs_value(value), value)

    def test_zero_markers_normalize_to_zero(self):
        for value in ("2 x", "x 2", "2x", "x2", "*", " 2  x ", "X2", "2 X"):
            self.assertTrue(is_zero_marker(value))
            self.assertEqual(normalize_fs_value(value), "0")

    def test_spaced_numeric_values_compact_to_digits(self):
        self.assertEqual(normalize_fs_value("1 000"), "1000")

    def test_lone_two_normalizes_to_zero(self):
        for value in ("2", " 2 ", "2 "):
            self.assertEqual(normalize_fs_value(value), "0")

    def test_numbers_containing_two_are_preserved(self):
        for value in ("12", "20", "25", "200"):
            self.assertEqual(normalize_fs_value(value), value)

    def test_lone_one_normalizes_without_becoming_numeric(self):
        self.assertEqual(normalize_fs_value("1"), "1")
        self.assertEqual(normalize_fs_value(" 1 "), "1")

    def test_azure_checkbox_ocr_normalizes_to_x_before_fs_logic(self):
        for value in (":selected:", ":unselected:", "x :selected:", "X :selected:", "× :selected:"):
            self.assertEqual(normalize_fs_ocr_text(value), "X")
        self.assertEqual(normalize_fs_value(":selected:"), "0")

    def test_numeric_with_selected_checkbox_preserves_number(self):
        self.assertEqual(normalize_fs_ocr_text("25\n:selected:"), "25")
        self.assertEqual(normalize_fs_ocr_text("25 :selected:"), "25")
        self.assertEqual(normalize_fs_ocr_text("50:selected:"), "50")
        self.assertEqual(normalize_fs_ocr_text("1 000\n:unselected:"), "1000")
        self.assertEqual(normalize_fs_value("25\n:selected:"), "25")

    def test_non_numeric_non_marker_values_normalize_to_zero(self):
        self.assertEqual(normalize_fs_value("x"), "0")
        self.assertEqual(normalize_fs_value("abc"), "0")


class NValueValidationTests(TestCase):
    def test_slash_between_digits_normalizes_to_one(self):
        self.assertEqual(normalize_n_ocr_text("9/90"), "9190")
        self.assertEqual(normalize_n_ocr_text("2/62"), "2162")

    def test_non_numeric_slashes_are_preserved(self):
        self.assertEqual(normalize_n_ocr_text("/90"), "/90")
        self.assertEqual(normalize_n_ocr_text("9/"), "9/")


class FSColumnPostProcessingTests(TestCase):
    def _column_cells(self, raw_values, group_number=1, field_type="F"):
        return [
            OCRCell(row_number=row_number, group_number=group_number, field_type=field_type, value=value, confidence=0.9)
            for row_number, value in raw_values.items()
        ]

    def test_ditto_chain_with_cross_break_matches_spec_example(self):
        raw_values = {
            1: "25",
            2: "",
            3: "",
            4: "X",
            5: "",
            6: "",
            7: "50",
            8: "",
        }
        cells = self._column_cells(raw_values)
        resolve_fs_columns(cells)
        expected = {
            1: "25",
            2: "25",
            3: "25",
            4: "0",
            5: "0",
            6: "0",
            7: "50",
            8: "50",
        }
        for row_number, value in expected.items():
            self.assertEqual(cells[row_number - 1].value, value)

    def test_blank_ocr_cells_copy_last_value_per_column(self):
        cells = self._column_cells({1: "15", 2: "   ", 3: None, 4: ""})
        resolve_fs_columns(cells)
        self.assertEqual([cell.value for cell in cells], ["15", "15", "15", "15"])

    def test_cross_marker_with_azure_checkbox_normalizes_to_zero(self):
        cells = self._column_cells({1: "25", 2: "x\n:selected:", 3: ""})
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "25")
        self.assertEqual(cells[1].value, "0")
        self.assertEqual(cells[2].value, "0")

    def test_bare_selected_checkbox_breaks_ditto_chain(self):
        cells = self._column_cells({1: "25", 2: ":selected:", 3: ""})
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "25")
        self.assertEqual(cells[1].value, "0")
        self.assertEqual(cells[2].value, "0")

    def test_numeric_with_selected_checkbox_is_preserved_in_column(self):
        cells = self._column_cells({1: "92", 2: "25\n:selected:", 3: ""})
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "92")
        self.assertEqual(cells[1].value, "25")
        self.assertEqual(cells[2].value, "25")

    def test_columns_are_processed_independently(self):
        cells = self._column_cells({1: "10", 2: ""}, group_number=1, field_type="F")
        cells.extend(self._column_cells({1: "30", 2: ""}, group_number=1, field_type="S"))
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "10")
        self.assertEqual(cells[1].value, "10")
        self.assertEqual(cells[2].value, "30")
        self.assertEqual(cells[3].value, "30")

    def test_n_columns_are_not_modified(self):
        cells = [
            OCRCell(1, 1, "N", "", 0.9),
            OCRCell(2, 1, "N", "193", 0.9),
        ]
        cells.extend(self._column_cells({1: "25", 2: ""}))
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "")
        self.assertEqual(cells[1].value, "193")
        self.assertEqual(cells[2].value, "25")
        self.assertEqual(cells[3].value, "25")

    def test_lone_one_is_treated_as_ditto_not_numeric(self):
        raw_values = {1: "25", 2: "1", 3: "1", 4: "50", 5: "1"}
        cells = self._column_cells(raw_values)
        resolve_fs_columns(cells)
        expected = {1: "25", 2: "25", 3: "25", 4: "50", 5: "50"}
        for row_number, value in expected.items():
            self.assertEqual(cells[row_number - 1].value, value)

    def test_numbers_containing_one_are_preserved(self):
        cells = self._column_cells({1: "10", 2: "1", 3: "21"})
        resolve_fs_columns(cells)
        self.assertEqual(cells[0].value, "10")
        self.assertEqual(cells[1].value, "10")
        self.assertEqual(cells[2].value, "21")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), OCR_CONFIDENCE_THRESHOLD=0.90, OCR_PROVIDER="mock")
class OCRWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="scanner", password="pass12345")
        self.client.force_login(self.user)

    def test_login_redirect_works(self):
        self.client.logout()
        upload_url = reverse("ocr:upload")
        response = self.client.get(upload_url)
        self.assertRedirects(response, f"{reverse('login')}?next={upload_url}", fetch_redirect_response=False)

        response = self.client.post(
            reverse("login"),
            {"username": "scanner", "password": "pass12345", "next": upload_url},
        )
        self.assertRedirects(response, upload_url)

    def test_authenticated_user_can_access_upload(self):
        response = self.client.get(reverse("ocr:upload"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload handwritten sheet")

    def test_unauthenticated_user_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("ocr:upload"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertIn("next=/ocr/upload/", response["Location"])

    def test_missing_quota_record_blocks_upload(self):
        response = self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        self.assertContains(response, "Your sheet limit is finished. Please contact admin.")
        self.assertEqual(SheetUpload.objects.count(), 0)

    def test_user_with_zero_quota_cannot_upload(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=0)
        response = self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        self.assertContains(response, "Your sheet limit is finished. Please contact admin.")
        self.assertEqual(SheetUpload.objects.count(), 0)

    def test_user_with_available_quota_can_upload(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        response = self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SheetUpload.objects.count(), 1)
        sheet = SheetUpload.objects.get()
        self.assertEqual(sheet.status, SheetUpload.STATUS_REVIEW_REQUIRED)
        self.assertEqual(sheet.cells.count(), 225)
        self.assertIsNone(sheet.final_data)
        self.assertEqual(sheet.user.sheet_quota.remaining_sheets, 0)

    def test_invalid_image_upload_shows_form_error(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        bad_file = SimpleUploadedFile("bad.txt", b"not an image", content_type="text/plain")
        response = self.client.post(reverse("ocr:upload"), {"image": bad_file})
        self.assertContains(response, "Upload a valid image")
        self.assertEqual(SheetUpload.objects.count(), 0)

    def test_ocr_response_is_stored_and_cells_are_created(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=2)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        sheet.refresh_from_db()
        self.assertEqual(sheet.raw_ocr_response["tables"][0]["rowCount"], 25)
        self.assertEqual(sheet.cells.count(), 225)

    def test_low_confidence_cells_are_marked(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        self.assertTrue(ExtractedCell.objects.get(sheet=sheet, row_number=1, group_number=1, field_type="F").is_low_confidence)

    def test_user_can_edit_extracted_values_and_confirm(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        post_data = {f"cell_{cell.pk}": cell.review_value for cell in sheet.cells.all()}
        target = sheet.cells.get(row_number=1, group_number=1, field_type="N")
        post_data[f"cell_{target.pk}"] = "193"
        response = self.client.post(reverse("ocr:confirm", args=[sheet.pk]), post_data)
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        sheet.refresh_from_db()
        self.assertEqual(target.corrected_value, "193")
        self.assertEqual(sheet.status, SheetUpload.STATUS_CONFIRMED)
        self.assertEqual(sheet.final_data["rows"][0]["groups"][0]["N"], "193")

    def test_invalid_correction_shows_friendly_error(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        post_data = {f"cell_{cell.pk}": cell.review_value for cell in sheet.cells.all()}
        target = sheet.cells.get(row_number=1, group_number=1, field_type="N")
        post_data[f"cell_{target.pk}"] = "ABC"
        response = self.client.post(reverse("ocr:confirm", args=[sheet.pk]), post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "N fields must be 2 to 4 digits.")
        sheet.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(sheet.status, SheetUpload.STATUS_REVIEW_REQUIRED)
        self.assertEqual(target.corrected_value, "")

    def test_duplicate_confirmation_does_not_corrupt_data_or_deduct_quota(self):
        quota = SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        post_data = {f"cell_{cell.pk}": cell.review_value for cell in sheet.cells.all()}
        target = sheet.cells.get(row_number=1, group_number=1, field_type="N")
        post_data[f"cell_{target.pk}"] = "193"

        self.client.post(reverse("ocr:confirm", args=[sheet.pk]), post_data)
        self.client.post(reverse("ocr:confirm", args=[sheet.pk]), post_data)

        quota.refresh_from_db()
        sheet.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(quota.remaining_sheets, 0)
        self.assertEqual(sheet.status, SheetUpload.STATUS_CONFIRMED)
        self.assertEqual(target.extracted_value, "011")
        self.assertEqual(target.corrected_value, "193")
        self.assertEqual(sheet.final_data["rows"][0]["groups"][0]["N"], "193")

    def test_failed_ocr_marks_sheet_failed(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        with self.assertRaises(OCRProviderError):
            process_sheet(sheet, provider=FailingProvider())
        sheet.refresh_from_db()
        self.assertEqual(sheet.status, SheetUpload.STATUS_FAILED)
        self.assertIn("boom", sheet.error_message)

    @patch("ocr.views.process_sheet")
    def test_ocr_failure_shows_error_page(self, mocked_process):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)

        def fail(sheet):
            sheet.status = SheetUpload.STATUS_FAILED
            sheet.error_message = "mock failure"
            sheet.save(update_fields=["status", "error_message"])
            raise OCRProviderError("mock failure")

        mocked_process.side_effect = fail
        response = self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        self.assertContains(response, "mock failure")
        self.assertEqual(SheetUpload.objects.get().status, SheetUpload.STATUS_FAILED)

    def test_mock_ocr_creates_exactly_225_cells(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        self.assertEqual(ExtractedCell.objects.count(), 225)

    def test_crop_grid_cells_returns_25_by_10_crops(self):
        image = Image.new("RGB", (1000, 2500), "white")
        table_image, crops = crop_grid_cells(image)
        self.assertEqual(table_image.size, (1000, 2500))
        self.assertEqual(len(crops), 250)
        self.assertEqual(crops[0]["row_number"], 1)
        self.assertEqual(crops[0]["column_label"], "S.N")
        self.assertEqual(crops[-1]["row_number"], 25)
        self.assertEqual(crops[-1]["column_label"], "S3")

    def test_cell_crop_provider_parse_ignores_sn_and_maps_directly(self):
        raw_response = CellCropProvider().analyze("unused")
        cells = AzureCellCropProvider(endpoint="endpoint", key="key").parse_cells(raw_response)
        self.assertEqual(len(cells), 225)
        self.assertNotIn("10", [cell.value for cell in cells])
        first = cells[0]
        self.assertEqual(first.row_number, 1)
        self.assertEqual(first.group_number, 1)
        self.assertEqual(first.field_type, "N")
        self.assertEqual(first.value, "11")
        empty = next(cell for cell in cells if cell.row_number == 2 and cell.group_number == 1 and cell.field_type == "N")
        self.assertEqual(empty.value, "")
        self.assertEqual(empty.confidence, 0)
        n2 = next(cell for cell in cells if cell.row_number == 14 and cell.group_number == 2 and cell.field_type == "N")
        self.assertEqual(n2.value, "9190")

    def test_cell_crop_workflow_creates_225_cells_and_ignores_sn(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=CellCropProvider())
        self.assertEqual(sheet.cells.count(), 225)
        self.assertFalse(sheet.cells.filter(extracted_value="10").exists())
        self.assertEqual(sheet.cells.get(row_number=1, group_number=1, field_type="N").extracted_value, "11")

    @override_settings(OCR_CELL_MODE_STRATEGY="single_page_grid_map")
    def test_cell_crop_single_page_grid_map_calls_azure_once(self):
        provider = SingleCallCellCropProvider()
        image = Image.new("RGB", (1000, 2500), "white")
        path = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        image.save(path)
        raw_response = provider.analyze(path)
        cells = provider.parse_cells(raw_response)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(raw_response["strategy"], "single_page_grid_map")
        self.assertEqual(raw_response["azure_request_count"], 1)
        self.assertEqual(len(cells), 225)
        self.assertEqual(cells[0].value, "193")
        self.assertEqual(cells[1].value, "50")
        self.assertEqual(cells[2].value, "0")

    def test_review_page_displays_25_rows(self):
        SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        self.client.post(reverse("ocr:upload"), {"image": fake_image()})
        sheet = SheetUpload.objects.get()
        response = self.client.get(reverse("ocr:review", args=[sheet.pk]))
        self.assertContains(response, 'data-review-row="', count=25)
        self.assertContains(response, 'data-visible-column="', count=10)
        for column in ("S.N", "N.1", "F1", "S1", "N.2", "F2", "S2", "N.3", "F3", "S3"):
            self.assertContains(response, f'data-visible-column="{column}"')

    def test_quota_is_not_deducted_twice(self):
        quota = SheetQuota.objects.create(user=self.user, remaining_sheets=2)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=FakeProvider())
        process_sheet(sheet, provider=FakeProvider())
        quota.refresh_from_db()
        self.assertEqual(quota.remaining_sheets, 1)

    def test_old_process_route_is_disabled(self):
        response = self.client.post(reverse("process"))
        self.assertContains(response, "Old PDF flow is disabled. Use /ocr/upload/ for handwritten OCR.")

    def test_azure_word_parser_creates_225_cells_without_table(self):
        cells = parse_azure_result(
            fake_azure_response(
                [
                    azure_word("193", 1, 1, 0.97),
                    azure_word("500", 1, 2, 0.86),
                    azure_word("x", 1, 3, 0.92),
                    azure_word("77", 25, 9, 0.91),
                ]
            )
        )
        self.assertEqual(len(cells), 225)
        self.assertEqual(cells[0].value, "193")
        self.assertEqual(cells[1].value, "500")
        self.assertEqual(cells[2].value, "0")
        self.assertEqual(cells[-1].value, "77")
        self.assertIsNone(cells[3].bounding_box)
        self.assertEqual(cells[3].confidence, 0)

    def test_azure_word_parser_joins_multiple_words_in_one_cell(self):
        cells = parse_azure_result(fake_azure_response([azure_word("1", 2, 2), azure_word("000", 2, 2)]))
        target = next(cell for cell in cells if cell.row_number == 2 and cell.group_number == 1 and cell.field_type == "F")
        self.assertEqual(target.value, "1000")
        self.assertIsNotNone(target.bounding_box)

    def test_azure_parser_repairs_digit_slash_digit_in_n_cells(self):
        cells = parse_azure_result(fake_azure_response(azure_serial_anchors() + [azure_word("9/90", 14, 4)]))
        target = next(cell for cell in cells if cell.row_number == 14 and cell.group_number == 2 and cell.field_type == "N")
        self.assertEqual(target.value, "9190")

    def test_raw_dump_repairs_row_14_n2_value(self):
        raw_response = json.loads((Path(__file__).resolve().parent.parent / "azure_raw_dump.json").read_text())
        cells = parse_azure_result(raw_response)
        target = next(cell for cell in cells if cell.row_number == 14 and cell.group_number == 2 and cell.field_type == "N")
        self.assertEqual(target.value, "9190")

    def test_azure_word_parser_ignores_printed_sn_column(self):
        cells = parse_azure_result(fake_azure_response([azure_word("1", 1, 0), azure_word("193", 1, 1)]))
        values = [cell.value for cell in cells]
        self.assertIn("193", values)
        self.assertNotIn("1", values)

    def test_azure_parser_logs_debug_counts(self):
        with self.assertLogs("ocr.services.parser", level="DEBUG") as logs:
            parse_azure_result(fake_azure_response([azure_word("193", 1, 1, 0.70)]))
        output = "\n".join(logs.output)
        self.assertIn("Azure OCR words detected: 1", output)
        self.assertIn("Azure OCR words mapped to cells: 1", output)
        self.assertIn("Azure OCR empty cells: 224", output)
        self.assertIn("Azure OCR low-confidence cells:", output)

    def test_azure_parser_ignores_headers_with_serial_anchors(self):
        words = azure_serial_anchors() + [
            azure_position_word("S.N", 0.05, 0.08),
            azure_position_word("N.1", 0.15, 0.08),
            azure_position_word("F S", 0.25, 0.08),
            azure_position_word("N.2", 0.45, 0.08),
            azure_position_word("N.3", 0.75, 0.08),
            azure_position_word("Dr.N", 0.15, 0.04),
            azure_position_word("Name", 0.35, 0.04),
            azure_position_word("Date", 0.55, 0.04),
            azure_position_word("PZ", 0.75, 0.04),
            azure_position_word("ENTERPRISES", 0.85, 0.04),
            azure_word("709", 3, 1),
        ]
        cells = parse_azure_result(fake_azure_response(words))
        values = [cell.value for cell in cells if cell.value]
        self.assertEqual(len(cells), 225)
        self.assertIn("709", values)
        for header in ("S.N", "N.1", "F S", "N.2", "N.3", "Dr.N", "Name", "Date", "PZ", "ENTERPRISES"):
            self.assertNotIn(header, values)

    def test_azure_serial_numbers_are_anchors_not_saved(self):
        cells = parse_azure_result(fake_azure_response(azure_serial_anchors() + [azure_word("193", 1, 1)]))
        values = [cell.value for cell in cells if cell.value]
        self.assertEqual(len(cells), 225)
        self.assertEqual(values, ["193"])

    def test_azure_adjacent_rows_do_not_merge_with_serial_anchors(self):
        cells = parse_azure_result(
            fake_azure_response(
                azure_serial_anchors()
                + [
                    azure_word("709", 3, 1),
                    azure_word("265", 4, 1),
                ]
            )
        )
        row_3 = next(cell for cell in cells if cell.row_number == 3 and cell.group_number == 1 and cell.field_type == "N")
        row_4 = next(cell for cell in cells if cell.row_number == 4 and cell.group_number == 1 and cell.field_type == "N")
        self.assertEqual(row_3.value, "709")
        self.assertEqual(row_4.value, "265")
        self.assertNotEqual(row_3.value, "709 265")

    def test_azure_anchor_debug_logs_include_anchor_and_ignore_counts(self):
        with self.assertLogs("ocr.services.parser", level="DEBUG") as logs:
            parse_azure_result(fake_azure_response(azure_serial_anchors() + [azure_position_word("N.1", 0.15, 0.08)]))
        output = "\n".join(logs.output)
        self.assertIn("Azure OCR row anchors detected: 25", output)
        self.assertIn("Azure OCR row anchor y positions:", output)
        self.assertIn("Azure OCR header words ignored:", output)
        self.assertIn("Azure OCR S.N column words ignored:", output)
        self.assertIn("Azure OCR words mapped by row:", output)
        self.assertIn("Azure OCR cells with multiple joined words:", output)

    def test_azure_debug_logs_warning_when_fallback_used(self):
        with self.assertLogs("ocr.services.parser", level="WARNING") as logs:
            parse_azure_result(fake_azure_response([]))
        self.assertIn("WARNING: insufficient row anchors; fallback row bands used.", "\n".join(logs.output))

    def test_debug_page_requires_staff(self):
        sheet = SheetUpload.objects.create(
            user=self.user,
            image=fake_image(),
            raw_ocr_response=fake_azure_response(azure_serial_anchors() + [azure_word("193", 1, 1)]),
        )
        response = self.client.get(reverse("ocr:debug", args=[sheet.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_staff_debug_page_shows_word_mapping_data(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        sheet = SheetUpload.objects.create(
            user=self.user,
            image=fake_image(),
            raw_ocr_response=fake_azure_response(azure_serial_anchors() + [azure_word("193", 1, 1)]),
        )
        response = self.client.get(reverse("ocr:debug", args=[sheet.pk]))
        self.assertContains(response, "Detected row anchor count")
        self.assertContains(response, "row_detection_method:")
        self.assertContains(response, "final 25 row centers:")
        self.assertContains(response, "row band boundaries:")
        self.assertContains(response, "row_coverage_ratio:")
        self.assertContains(response, "COLUMN_LABELS:")
        self.assertContains(response, "fallback_used:")
        self.assertContains(response, "final_cell_key")
        self.assertContains(response, "R1:G1:N")
        self.assertContains(response, "N.1")

    def test_y_clustering_separates_adjacent_rows_without_serial_anchors(self):
        words = [
            azure_position_word("709", 0.15, 0.256),
            azure_position_word("265", 0.15, 0.286),
        ]
        debug = build_azure_debug_data(fake_azure_response(words))
        cells = parse_azure_result(fake_azure_response(words))
        row_3 = next(cell for cell in cells if cell.row_number == 3 and cell.group_number == 1 and cell.field_type == "N")
        row_4 = next(cell for cell in cells if cell.row_number == 4 and cell.group_number == 1 and cell.field_type == "N")
        self.assertEqual(len(cells), 225)
        self.assertEqual(debug["row_detection_method"], "y_clustering")
        self.assertFalse(debug["fallback_used"])
        self.assertEqual(row_3.value, "709")
        self.assertEqual(row_4.value, "265")

    def test_y_clustering_row_centers_cover_full_data_region_with_many_clusters(self):
        words = [
            azure_position_word(str(index), 0.15, 0.18 + (index * 0.01))
            for index in range(56)
            if 0.18 + (index * 0.01) < 0.94
        ]
        debug = build_azure_debug_data(fake_azure_response(words))
        self.assertEqual(debug["row_detection_method"], "y_clustering")
        self.assertEqual(len(debug["row_centers"]), 25)
        self.assertGreater(debug["detected_cluster_count"], 25)
        self.assertGreater(debug["last_row_center"], 0.90)
        self.assertGreaterEqual(debug["row_coverage_ratio"], 0.80)

    def test_calibrated_columns_separate_adjacent_values(self):
        words = [
            azure_position_word("10", 0.400, 0.256),
            azure_position_word("738", 0.477, 0.256),
            azure_position_word("265", 0.15, 0.286),
        ]
        debug = build_azure_debug_data(fake_azure_response(words))
        cells = parse_azure_result(fake_azure_response(words))
        n2 = next(cell for cell in cells if cell.row_number == 3 and cell.group_number == 2 and cell.field_type == "N")
        f2 = next(cell for cell in cells if cell.row_number == 3 and cell.group_number == 2 and cell.field_type == "F")
        self.assertEqual(len(cells), 225)
        self.assertEqual(n2.value, "10")
        self.assertEqual(f2.value, "738")
        self.assertEqual(debug["word_rows"][0]["assigned_column"], "N.2")
        self.assertEqual(debug["word_rows"][1]["assigned_column"], "F2")
        self.assertTrue(debug["word_rows"][1]["boundary_risk"])

    def test_azure_words_processing_marks_low_confidence_and_empty_cells(self):
        quota = SheetQuota.objects.create(user=self.user, remaining_sheets=1)
        sheet = SheetUpload.objects.create(user=self.user, image=fake_image())
        process_sheet(sheet, provider=AzureWordsProvider())
        quota.refresh_from_db()
        self.assertEqual(sheet.cells.count(), 225)
        self.assertEqual(quota.remaining_sheets, 0)
        low_cell = sheet.cells.get(row_number=1, group_number=1, field_type="F")
        empty_cell = sheet.cells.get(row_number=2, group_number=1, field_type="N")
        self.assertTrue(low_cell.is_low_confidence)
        self.assertEqual(empty_cell.extracted_value, "")
        self.assertEqual(empty_cell.confidence, 0)

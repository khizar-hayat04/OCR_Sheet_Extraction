from io import BytesIO
from pathlib import Path
import re

from django.conf import settings

from .base import MissingCredentialsError, OCRCell, OCRProvider, OCRProviderError
from .validators import is_suspicious_data_value, normalize_fs_ocr_text, normalize_n_ocr_text, prepare_fs_cell_value, resolve_fs_columns
from ocr.utils.image_preprocessing import COLUMN_LABELS, crop_grid_cells, grid_cell_boxes, normalize_table_image


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


class AzureCellCropProvider(OCRProvider):
    def __init__(self, endpoint=None, key=None):
        self.endpoint = endpoint if endpoint is not None else settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
        self.key = key if key is not None else settings.AZURE_DOCUMENT_INTELLIGENCE_KEY

    def analyze(self, file_path):
        if not self.endpoint or not self.key:
            raise MissingCredentialsError("Azure Document Intelligence credentials are not configured.")

        try:
            from PIL import Image
        except ImportError as exc:
            raise OCRProviderError("Pillow is required for cell-crop OCR.") from exc

        image = Image.open(file_path).convert("RGB")
        table_image, crops = crop_grid_cells(image)
        debug_dir = _debug_dir_for_file(file_path)
        debug_dir.mkdir(parents=True, exist_ok=True)
        normalized_table_url = _save_image(table_image, debug_dir / "normalized_table.jpg")
        grid_overlay_url = _save_grid_overlay(table_image.copy(), debug_dir / "grid_overlay.jpg")

        strategy = settings.OCR_CELL_MODE_STRATEGY.lower()
        if strategy == "single_page_grid_map":
            return self._analyze_single_page_grid_map(table_image, crops, normalized_table_url, grid_overlay_url)
        if strategy != "per_cell":
            raise OCRProviderError(f"Unsupported OCR_CELL_MODE_STRATEGY: {settings.OCR_CELL_MODE_STRATEGY}")

        cell_results = []
        for crop in crops:
            crop_url = _save_image(crop["image"], debug_dir / f"r{crop['row_number']:02d}_c{crop['column_index']:02d}_{crop['column_label']}.jpg")
            result = {"text": "", "confidence": 0.0, "raw": None}
            if crop["column_index"] != 0:
                result = self._ocr_crop(crop["image"], crop["column_index"])
            cell_results.append(
                {
                    "row_number": crop["row_number"],
                    "column_index": crop["column_index"],
                    "column_label": crop["column_label"],
                    "box": crop["box"],
                    "crop_url": crop_url,
                    "text": result["text"],
                    "confidence": result["confidence"],
                    "raw": result["raw"],
                }
            )

        return {
            "mode": "cell_crop",
            "strategy": "per_cell",
            "normalized_table_url": normalized_table_url,
            "grid_overlay_url": grid_overlay_url,
            "cells": cell_results,
            "constants": {
                "ROW_COUNT": 25,
                "COLUMN_COUNT": 10,
                "COLUMN_LABELS": COLUMN_LABELS,
                "OCR_TABLE_TOP": settings.OCR_TABLE_TOP,
                "OCR_TABLE_BOTTOM": settings.OCR_TABLE_BOTTOM,
                "OCR_TABLE_LEFT": settings.OCR_TABLE_LEFT,
                "OCR_TABLE_RIGHT": settings.OCR_TABLE_RIGHT,
                "OCR_GRID_COLUMN_BOUNDARIES": settings.OCR_GRID_COLUMN_BOUNDARIES,
            },
        }

    def _analyze_single_page_grid_map(self, table_image, crops, normalized_table_url, grid_overlay_url):
        raw_response = self._analyze_image_bytes(_image_bytes(table_image))
        words = _extract_words(raw_response, table_image.size)
        cell_results = _map_words_to_grid_cells(words, crops)
        return {
            "mode": "cell_crop",
            "strategy": "single_page_grid_map",
            "normalized_table_url": normalized_table_url,
            "grid_overlay_url": grid_overlay_url,
            "azure_request_count": 1,
            "word_count": len(words),
            "cells": cell_results,
            "raw_single_page_response": raw_response,
            "constants": {
                "ROW_COUNT": 25,
                "COLUMN_COUNT": 10,
                "COLUMN_LABELS": COLUMN_LABELS,
                "OCR_TABLE_TOP": settings.OCR_TABLE_TOP,
                "OCR_TABLE_BOTTOM": settings.OCR_TABLE_BOTTOM,
                "OCR_TABLE_LEFT": settings.OCR_TABLE_LEFT,
                "OCR_TABLE_RIGHT": settings.OCR_TABLE_RIGHT,
                "OCR_GRID_COLUMN_BOUNDARIES": settings.OCR_GRID_COLUMN_BOUNDARIES,
            },
        }

    def _ocr_crop(self, image, column_index):
        raw_response = self._analyze_image_bytes(_image_bytes(image))
        text, confidence = _strongest_text(raw_response)
        group_number, field_type = FIELD_MAP[column_index]
        return {
            "text": _sanitize_text(text, field_type),
            "confidence": confidence,
            "raw": raw_response,
        }

    def _analyze_image_bytes(self, image_bytes):
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
            from azure.core.credentials import AzureKeyCredential
            from azure.core.exceptions import AzureError
        except ImportError as exc:
            raise OCRProviderError("Azure Document Intelligence SDK is not installed.") from exc

        try:
            client = DocumentIntelligenceClient(
                endpoint=self.endpoint,
                credential=AzureKeyCredential(self.key),
            )
            poller = client.begin_analyze_document(
                "prebuilt-read",
                AnalyzeDocumentRequest(bytes_source=image_bytes),
            )
            result = poller.result()
        except AzureError as exc:
            raise OCRProviderError(f"Azure cell OCR request failed: {exc}") from exc

        if hasattr(result, "as_dict"):
            return result.as_dict()
        if isinstance(result, dict):
            return result
        return {}

    def parse_cells(self, raw_response):
        output = []
        by_cell = {
            (cell["row_number"], cell["column_index"]): cell
            for cell in raw_response.get("cells", [])
        }
        for row_number in range(1, 26):
            for column_index, (group_number, field_type) in FIELD_MAP.items():
                result = by_cell.get((row_number, column_index), {})
                raw_text = result.get("raw_text", result.get("text", ""))
                value = _sanitize_text(result.get("text", ""), field_type)
                output.append(
                    OCRCell(
                        row_number=row_number,
                        group_number=group_number,
                        field_type=field_type,
                        value=value,
                        confidence=float(result.get("confidence") or 0),
                        bounding_box=result.get("box"),
                        is_flagged=is_suspicious_data_value(raw_text, value, field_type),
                    )
                )
        return resolve_fs_columns(output)


def _strongest_text(raw_response):
    words = []
    for page in raw_response.get("pages") or []:
        words.extend(page.get("words") or [])
    if words:
        words = sorted(words, key=lambda word: float(word.get("confidence") or 0), reverse=True)
        return str(words[0].get("content") or "").strip(), float(words[0].get("confidence") or 0)
    content = str(raw_response.get("content") or "").strip()
    return content, 0.0 if not content else 0.5


def _extract_words(raw_response, image_size):
    width, height = image_size
    output = []
    for page in raw_response.get("pages") or []:
        page_width = float(page.get("width") or width)
        page_height = float(page.get("height") or height)
        for word in page.get("words") or []:
            polygon = word.get("polygon") or []
            points = _polygon_points(polygon)
            if not points:
                continue
            if max(x for x, y in points) <= 1 and max(y for x, y in points) <= 1:
                pixel_points = [(x * width, y * height) for x, y in points]
            else:
                pixel_points = [(x / page_width * width, y / page_height * height) for x, y in points]
            output.append(
                {
                    "content": str(word.get("content") or "").strip(),
                    "confidence": float(word.get("confidence") or 0),
                    "polygon": polygon,
                    "pixel_polygon": pixel_points,
                    "x_center": sum(x for x, y in pixel_points) / len(pixel_points),
                    "y_center": sum(y for x, y in pixel_points) / len(pixel_points),
                }
            )
    return [word for word in output if word["content"]]


def _polygon_points(polygon):
    if not polygon:
        return []
    if all(isinstance(item, (int, float)) for item in polygon):
        return [(float(polygon[index]), float(polygon[index + 1])) for index in range(0, len(polygon) - 1, 2)]
    points = []
    for point in polygon:
        if isinstance(point, dict):
            x = point.get("x")
            y = point.get("y")
        else:
            x = getattr(point, "x", None)
            y = getattr(point, "y", None)
        if x is not None and y is not None:
            points.append((float(x), float(y)))
    return points


def _map_words_to_grid_cells(words, crops):
    by_cell = {
        (crop["row_number"], crop["column_index"]): {**crop, "words": []}
        for crop in crops
    }
    for word in words:
        for crop in crops:
            box = crop["box"]
            if box["left"] <= word["x_center"] < box["right"] and box["top"] <= word["y_center"] < box["bottom"]:
                by_cell[(crop["row_number"], crop["column_index"])]["words"].append(word)
                break

    results = []
    for row_number in range(1, 26):
        for col_index in range(10):
            cell = by_cell[(row_number, col_index)]
            words_in_cell = sorted(cell["words"], key=lambda word: (word["y_center"], word["x_center"]))
            text = " ".join(word["content"] for word in words_in_cell).strip()
            raw_text = text
            confidence = min((word["confidence"] for word in words_in_cell), default=0.0)
            if col_index in FIELD_MAP:
                group_number, field_type = FIELD_MAP[col_index]
                text = _sanitize_text(text, field_type)
            else:
                text = ""
                confidence = 0.0
            results.append(
                {
                    "row_number": row_number,
                    "column_index": col_index,
                    "column_label": cell["column_label"],
                    "box": cell["box"],
                    "crop_url": cell.get("crop_url", ""),
                    "raw_text": raw_text,
                    "text": text,
                    "confidence": confidence,
                    "words": words_in_cell,
                }
            )
    return results


def _sanitize_text(text, field_type):
    text = (text or "").strip()
    if field_type == "N":
        return "".join(re.findall(r"\d+", normalize_n_ocr_text(text)))
    if field_type in {"F", "S"}:
        return prepare_fs_cell_value(text)
    return text


def _image_bytes(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _debug_dir_for_file(file_path):
    return Path(settings.MEDIA_ROOT) / "ocr" / "cell_debug" / Path(file_path).stem


def _media_url_for_path(path):
    relative = Path(path).relative_to(settings.MEDIA_ROOT)
    return f"{settings.MEDIA_URL}{relative.as_posix()}"


def _save_image(image, path):
    image.save(path, "JPEG", quality=90)
    return _media_url_for_path(path)


def _save_grid_overlay(image, path):
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size
    for row in range(26):
        y = int((row / 25) * height)
        draw.line([(0, y), (width, y)], fill=(0, 120, 255), width=1)
    for index, boundary in enumerate(settings.OCR_GRID_COLUMN_BOUNDARIES):
        x = int(boundary * width)
        draw.line([(x, 0), (x, height)], fill=(255, 120, 0), width=1)
        if index < len(COLUMN_LABELS):
            draw.text((x + 2, 4), COLUMN_LABELS[index], fill=(180, 70, 0), font=font)
    return _save_image(image, path)

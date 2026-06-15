from django.conf import settings
from django.core.exceptions import ValidationError
from pathlib import Path


ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp"}
MAX_IMAGE_SIZE = 15 * 1024 * 1024
ROW_COUNT = 25
COLUMN_COUNT = 10
COLUMN_LABELS = ["S.N", "N.1", "F1", "S1", "N.2", "F2", "S2", "N.3", "F3", "S3"]


def validate_uploaded_image(uploaded_file):
    content_type = getattr(uploaded_file, "content_type", "")
    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError("Upload a JPG, PNG, WEBP, TIFF, or BMP sheet image.")
    if uploaded_file.size > MAX_IMAGE_SIZE:
        raise ValidationError("Image is too large. Maximum size is 15 MB.")
    return uploaded_file


def detect_table_region(image):
    width, height = image.size
    return (
        int(settings.OCR_TABLE_LEFT * width),
        int(settings.OCR_TABLE_TOP * height),
        int(settings.OCR_TABLE_RIGHT * width),
        int(settings.OCR_TABLE_BOTTOM * height),
    )


def deskew_image(image):
    return image


def perspective_correct(image):
    return image


def normalize_table_image(image):
    deskewed = deskew_image(image)
    corrected = perspective_correct(deskewed)
    return corrected.crop(detect_table_region(corrected))


def crop_grid_cells(image):
    table_image = normalize_table_image(image)
    cells = grid_cell_boxes(table_image)
    crops = [
        {
            **cell,
            "image": table_image.crop((cell["box"]["left"], cell["box"]["top"], cell["box"]["right"], cell["box"]["bottom"])),
        }
        for cell in cells
    ]
    return table_image, crops


def grid_cell_boxes(table_image):
    width, height = table_image.size
    row_height = height / ROW_COUNT
    boundaries = settings.OCR_GRID_COLUMN_BOUNDARIES
    if len(boundaries) != COLUMN_COUNT + 1:
        boundaries = [i / COLUMN_COUNT for i in range(COLUMN_COUNT + 1)]

    cells = []
    for row_number in range(1, ROW_COUNT + 1):
        top = int((row_number - 1) * row_height)
        bottom = int(row_number * row_height)
        for col_index in range(COLUMN_COUNT):
            left = int(boundaries[col_index] * width)
            right = int(boundaries[col_index + 1] * width)
            cells.append(
                {
                    "row_number": row_number,
                    "column_index": col_index,
                    "column_label": COLUMN_LABELS[col_index],
                    "box": {
                        "left": left,
                        "top": top,
                        "right": right,
                        "bottom": bottom,
                    },
                }
            )
    return cells


def save_debug_cell_crops(crops, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for crop in crops:
        path = output_dir / f"r{crop['row_number']:02d}_c{crop['column_index']:02d}_{crop['column_label']}.jpg"
        crop["image"].save(path, "JPEG", quality=90)
        saved.append({**crop, "path": str(path)})
    return saved

from pathlib import Path

from django.conf import settings


def generate_debug_overlay(sheet, debug_data):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return ""

    try:
        image = Image.open(sheet.image.path).convert("RGB")
    except Exception:
        return ""

    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for boundary in debug_data["row_boundaries"]:
        y = int(boundary * height)
        draw.line([(0, y), (width, y)], fill=(0, 120, 255), width=1)

    for boundary in debug_data["constants"]["COLUMN_BOUNDARIES"]:
        x = int(boundary * width)
        draw.line([(x, 0), (x, height)], fill=(255, 120, 0), width=1)

    for row_number in range(1, 26):
        top = debug_data["row_boundaries"][row_number - 1]
        bottom = debug_data["row_boundaries"][row_number]
        y = int(((top + bottom) / 2) * height)
        draw.text((4, y - 5), str(row_number), fill=(0, 80, 200), font=font)

    for col_index, label in {
        1: "N.1",
        2: "F1",
        3: "S1",
        4: "N.2",
        5: "F2",
        6: "S2",
        7: "N.3",
        8: "F3",
        9: "S3",
    }.items():
        left = debug_data["constants"]["COLUMN_BOUNDARIES"][col_index]
        right = debug_data["constants"]["COLUMN_BOUNDARIES"][col_index + 1]
        x = int(((left + right) / 2) * width)
        draw.text((x - 8, 4), label, fill=(180, 70, 0), font=font)

    for word in debug_data["word_rows"]:
        polygon = word["normalized_polygon"]
        if not polygon:
            continue
        points = [(int(x * width), int(y * height)) for x, y in polygon]
        color = (180, 180, 180) if word["ignored_reason"] else (0, 160, 0)
        if word["ignored_reason"] == "header":
            color = (180, 0, 0)
        elif word["ignored_reason"] == "sn_column":
            color = (130, 80, 200)
        draw.line(points + [points[0]], fill=color, width=2)

    relative_path = Path("ocr") / "debug" / f"sheet_{sheet.pk}_overlay.jpg"
    output_path = Path(settings.MEDIA_ROOT) / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "JPEG", quality=90)
    return f"{settings.MEDIA_URL}{relative_path.as_posix()}"

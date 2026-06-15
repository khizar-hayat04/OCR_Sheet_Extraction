from .base import OCRCell, OCRProvider


class MockOCRProvider(OCRProvider):
    def analyze(self, file_path):
        return {
            "provider": "mock",
            "source_file": file_path,
            "message": "Generated fixed-format mock OCR response.",
        }

    def parse_cells(self, raw_response):
        cells = []
        for row in range(1, 26):
            for group in (1, 2, 3):
                cells.append(
                    OCRCell(
                        row_number=row,
                        group_number=group,
                        field_type="N",
                        value=f"{row:02d}{group}",
                        confidence=0.96 if (row + group) % 4 else 0.82,
                        bounding_box={"mock": True, "row": row, "group": group, "field": "N"},
                    )
                )
                cells.append(
                    OCRCell(
                        row_number=row,
                        group_number=group,
                        field_type="F",
                        value=str(100 + row + group),
                        confidence=0.94 if (row + group) % 5 else 0.75,
                        bounding_box={"mock": True, "row": row, "group": group, "field": "F"},
                    )
                )
                cells.append(
                    OCRCell(
                        row_number=row,
                        group_number=group,
                        field_type="S",
                        value="x" if (row + group) % 2 else str(group),
                        confidence=0.93 if (row + group) % 6 else 0.68,
                        bounding_box={"mock": True, "row": row, "group": group, "field": "S"},
                    )
                )
        return cells

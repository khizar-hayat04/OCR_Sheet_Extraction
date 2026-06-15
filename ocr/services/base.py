from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OCRCell:
    row_number: int
    group_number: int
    field_type: str
    value: str
    confidence: float
    bounding_box: Optional[Dict[str, Any]] = None


class OCRProviderError(Exception):
    pass


class MissingCredentialsError(OCRProviderError):
    pass


class OCRProvider:
    def analyze(self, file_path: str) -> Dict[str, Any]:
        raise NotImplementedError

    def parse_cells(self, raw_response: Dict[str, Any]) -> List[OCRCell]:
        raise NotImplementedError

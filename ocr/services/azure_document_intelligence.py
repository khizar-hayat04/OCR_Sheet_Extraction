from django.conf import settings

from .base import MissingCredentialsError, OCRProvider, OCRProviderError
from .parser import parse_azure_result


class AzureDocumentIntelligenceProvider(OCRProvider):
    def __init__(self, endpoint=None, key=None):
        self.endpoint = endpoint if endpoint is not None else settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
        self.key = key if key is not None else settings.AZURE_DOCUMENT_INTELLIGENCE_KEY

    def analyze(self, file_path):
        import json
        import logging
        
        logger = logging.getLogger(__name__)
        
        if not self.endpoint or not self.key:
            raise MissingCredentialsError("Azure Document Intelligence credentials are not configured.")

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
            with open(file_path, "rb") as uploaded:
                poller = client.begin_analyze_document(
                    "prebuilt-layout",
                    AnalyzeDocumentRequest(bytes_source=uploaded.read()),
                )
            result = poller.result()
        except AzureError as exc:
            raise OCRProviderError(f"Azure OCR request failed: {exc}") from exc
        except TimeoutError as exc:
            raise OCRProviderError("Azure OCR request timed out.") from exc

        # Convert result to dictionary
        if hasattr(result, "as_dict"):
            result_dict = result.as_dict()
        elif isinstance(result, dict):
            result_dict = result
        else:
            raise OCRProviderError("Azure OCR returned an unsupported response format.")
        
        # Debug: Dump all table cells with their spatial data
        try:
            # Check both nested and flat structures
            if "analyzeResult" in result_dict:
                tables = result_dict["analyzeResult"].get("tables", [])
            else:
                tables = result_dict.get("tables", [])
            
            logger.info(f"Found {len(tables)} tables in Azure response")
            
            # Extract first table for detailed analysis
            table = None
            analyze = result_dict.get("analyzeResult") or result_dict
            tables_list = analyze.get("tables") or []
            if tables_list:
                table = tables_list[0]
            
            if table:
                cells = table.get("cells", [])
                logger.info(f"Total cells: {len(cells)}")
                
                # NEW: All non-empty data cells with hex content
                logger.info("\n=== ALL NON-EMPTY DATA CELLS WITH HEX CONTENT ===")
                for cell in cells:
                    ri = cell.get("rowIndex", 0)
                    ci = cell.get("columnIndex", 0)
                    content = cell.get("content", "")
                    if ri >= 1 and content.strip():
                        hex_repr = content.encode("unicode_escape").decode("ascii")
                        # Flag anything that doesn't look like a plain number or x
                        is_suspicious = any(ord(c) > 127 or c in ('|','/','-','~') for c in content)
                        marker = " <<< SUSPICIOUS" if is_suspicious else ""
                        logger.info(f"  row={ri:2d} col={ci:2d} content={repr(content):30s} hex={hex_repr}{marker}")
                
                # Header row analysis
                logger.info("\n=== HEADER ROW (row=0) ===")
                for cell in cells:
                    if cell.get("rowIndex") == 0:
                        logger.info(f"  col={cell.get('columnIndex')} content={repr(cell.get('content',''))}")
                
                # First 5 data rows - ALL columns
                logger.info("=== FIRST 5 DATA ROWS (rows 1-5) — ALL COLUMNS ===")
                for cell in cells:
                    ri = cell.get("rowIndex", 0)
                    if 1 <= ri <= 5:
                        ci = cell.get("columnIndex")
                        content = cell.get("content", "")
                        confidence = cell.get("confidence", "N/A")
                        regions = cell.get("boundingRegions", [])
                        polygon = regions[0].get("polygon", []) if regions else []
                        cx = round((polygon[0] + polygon[4]) / 2, 1) if len(polygon) >= 6 else "?"
                        logger.info(f"  row={ri} col={ci} cx={cx} confidence={confidence} content={repr(content)}")
                
                # Empty or 'x' cells
                logger.info("=== ALL CELLS WHERE CONTENT IS EMPTY OR 'x' ===")
                for cell in cells:
                    ri = cell.get("rowIndex", 0)
                    content = cell.get("content", "").strip()
                    if ri >= 1 and (content == "" or content.lower() in ["x", "×", "*"]):
                        ci = cell.get("columnIndex")
                        confidence = cell.get("confidence", "N/A")
                        logger.info(f"  row={ri} col={ci} confidence={confidence} content={repr(content)}")
            
            # Log sample cells for debugging
            for table_idx, table_item in enumerate(tables):
                cells = table_item.get("cells", [])
                logger.info(f"Table {table_idx}: {len(cells)} cells")
                
                # Log first 5 cells for debugging
                for cell in cells[:5]:
                    content = cell.get("content", "").strip()
                    row_idx = cell.get("rowIndex")
                    col_idx = cell.get("columnIndex")
                    
                    # Get bounding polygon
                    regions = cell.get("boundingRegions", [])
                    polygon = regions[0].get("polygon", []) if regions else []
                    
                    # Polygon is [x1,y1,x2,y2,x3,y3,x4,y4]
                    # Center x = average of x1 and x3
                    center_x = round((polygon[0] + polygon[4]) / 2, 4) if len(polygon) >= 6 else None
                    
                    logger.debug(f"  Cell: row={row_idx} col={col_idx} center_x={center_x} content={repr(content)}")
            
            # Save full result to file for inspection (Windows compatible path)
            debug_path = "azure_raw_dump.json"
            with open(debug_path, "w") as f:
                json.dump(result_dict, f, indent=2)
            logger.info(f"Full Azure result saved to {debug_path}")
            
        except Exception as e:
            logger.warning(f"Debug logging failed: {e}")
        
        return result_dict

    def parse_cells(self, raw_response):
        return parse_azure_result(raw_response)

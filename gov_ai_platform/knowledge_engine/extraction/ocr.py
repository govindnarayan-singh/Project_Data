"""OCR helpers are implemented in :mod:`knowledge_engine.extraction.pdf`.

This module is reserved for a future provider boundary without changing the
validated extraction algorithm during the initial migration.
"""

from knowledge_engine.extraction.pdf import _mean_word_confidence, _select_ocr_candidate

__all__ = ["_mean_word_confidence", "_select_ocr_candidate"]

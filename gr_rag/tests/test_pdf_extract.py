import unittest

from src.pdf_extract import (
    _mean_word_confidence,
    _select_ocr_candidate,
    _should_use_ocr,
)


class PdfExtractionDecisionTests(unittest.TestCase):
    def test_mean_word_confidence_is_character_weighted(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
            "top\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tlong\n"
            "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t10\tx\n"
        )
        self.assertEqual(_mean_word_confidence(tsv), 74.0)

    def test_rotation_candidate_prefers_tesseract_confidence(self):
        candidates = [
            ("Timeline heading", 25.0),
            ("Proposed road improvement project details", 70.0),
        ]
        self.assertEqual(_select_ocr_candidate(candidates), candidates[1][0])

    def test_uncertain_tie_uses_ocr_when_marathi_signals_are_repaired(self):
        digital = (
            "शासन भनणगय क्रमाुंक पर्यावरण भविार् "
            "Description Estimated Cost Proposed Green Development " * 5
        )
        ocr = (
            "शासन निर्णय क्रमांक पर्यावरण विभाग "
            "Description Estimated Cost Proposed Green Development " * 5
        )
        self.assertTrue(_should_use_ocr(digital, ocr, force_ocr=False))

    def test_short_failed_ocr_is_not_selected(self):
        self.assertFalse(_should_use_ocr("109", "noise", force_ocr=False))


if __name__ == "__main__":
    unittest.main()

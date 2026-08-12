from datetime import date
import unittest

from knowledge_engine.evaluation.retrieval import (
    aggregate_results, first_matching_rank, is_document_hit, is_page_hit,
    page_range_overlaps, parse_expected_pages, recall_at_k,
)
from knowledge_engine.extraction.pdf import _mean_word_confidence, _select_ocr_candidate, _should_use_ocr
from knowledge_engine.extraction.text_quality import digital_text_classification, extracted_text_quality, malformed_devanagari_count, should_ocr
from knowledge_engine.metadata.extractor import date_from_filename, extract_metadata


class TextQualityTests(unittest.TestCase):
    def test_good_marathi_is_kept(self):
        text = "महाराष्ट्र शासन सामान्य प्रशासन विभाग शासन निर्णय दिनांक २० जून २०२४. सदर निर्णय सर्व संबंधित कार्यालयांनी तत्काळ अंमलात आणावा. " * 4
        self.assertEqual(digital_text_classification(text), "GOOD_MARATHI")
        self.assertFalse(should_ocr(text))

    def test_broken_mapping_requires_ocr(self):
        text = "महािाष्ट्र शासन सामान्य प्रशासन रवभाग शासन रनणटय रदनांक"
        self.assertGreaterEqual(malformed_devanagari_count(text), 2)
        self.assertTrue(should_ocr(text))

    def test_good_english_is_kept(self):
        text = "Environment and Climate Change Department, Government of Maharashtra. This page lists action points and evaluation details. " * 4
        self.assertEqual(digital_text_classification(text), "GOOD_ENGLISH")
        self.assertFalse(should_ocr(text))

    def test_empty_and_garbled_text_require_ocr(self):
        self.assertEqual(digital_text_classification(""), "EMPTY_OR_IMAGE_ONLY")
        garbled = "r I ri i g t E E I z i e Ia t E f a tt I E Sv LJI r l t tr i q p o q o F o a " * 8
        self.assertEqual(digital_text_classification(garbled), "SUSPICIOUS_GARBLED_LATIN")
        self.assertTrue(should_ocr(garbled))

    def test_mixed_script_is_uncertain(self):
        text = "शासन निर्णय जोडपत्र Environment Department action points and scheme details for Maharashtra local bodies. " * 4
        self.assertEqual(digital_text_classification(text), "UNCERTAIN")
        self.assertLess(extracted_text_quality(text), 0.55)


class MetadataTests(unittest.TestCase):
    def test_unlabelled_subject_and_text_date(self):
        text = """--- PAGE 1 ---
महापरिक्षा पोर्टल अंतर्गत घेण्यात येणाऱ्या परीक्षा पद्धतीत
आवश्यक बदल करून सुधारित कार्यपद्धतीबाबत.
महाराष्ट्र शासन
शासन निर्णय क्रमांक : मातंस-2020/प्र.क्र.11/से-2/39
दिनांक : २० फेब्रुवारी, २०२०
संदर्भ : पूर्वीचा निर्णय
"""
        metadata = extract_metadata(text, "Test", "19990101-wrong.pdf")
        self.assertEqual(metadata["gr_date"], date(2020, 2, 20))
        self.assertIn("महापरिक्षा पोर्टल", metadata["subject"])
        self.assertNotIn("PAGE", metadata["subject"])

    def test_filename_date_fallback(self):
        file_name = "202002201713214911.pdf"
        self.assertEqual(date_from_filename(file_name), date(2020, 2, 20))
        metadata = extract_metadata("महाराष्ट्र शासन", "Test", file_name)
        self.assertEqual(metadata["gr_date_source"], "filename")


class PdfDecisionTests(unittest.TestCase):
    def test_character_weighted_confidence(self):
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tlong\n5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t10\tx\n"
        self.assertEqual(_mean_word_confidence(tsv), 74.0)

    def test_rotation_prefers_confidence(self):
        values = [("Timeline heading", 25.0), ("Proposed road improvement project details", 70.0)]
        self.assertEqual(_select_ocr_candidate(values), values[1][0])

    def test_short_failed_ocr_is_not_selected(self):
        self.assertFalse(_should_use_ocr("109", "noise", False))


class EvaluationTests(unittest.TestCase):
    def test_page_parser_and_overlap(self):
        self.assertEqual(parse_expected_pages("2,4,7-9"), {2, 4, 7, 8, 9})
        self.assertTrue(page_range_overlaps(2, 3, {3, 4}))
        for invalid in ("", "0", "3-1", "1-2-3", "2,"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_expected_pages(invalid)

    def test_hits_and_metrics(self):
        row = {"file_name": "expected.pdf", "page_start": 2, "page_end": 3}
        self.assertTrue(is_document_hit(row, "expected.pdf"))
        self.assertTrue(is_page_hit(row, "expected.pdf", {3, 4}))
        self.assertEqual(first_matching_rank([False, True]), 2)
        self.assertEqual(recall_at_k([1, 3, None, 7], 3), 0.5)
        metrics = aggregate_results([1, 3, None], [2, None, 5])
        self.assertAlmostEqual(metrics["page"]["recall_at_5"], 2 / 3)

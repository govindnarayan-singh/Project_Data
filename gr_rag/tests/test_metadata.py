from datetime import date
import unittest

from src.metadata import date_from_filename, extract_metadata
from src.text_utils import malformed_devanagari_count, should_ocr


class MarathiPipelineTests(unittest.TestCase):
    def test_broken_pdf_mapping_requires_ocr(self):
        broken = "महािाष्ट्र शासन सामान्य प्रशासन रवभाग शासन रनणटय रदनांक"
        self.assertGreaterEqual(malformed_devanagari_count(broken), 2)
        self.assertTrue(should_ocr(broken))

    def test_unlabelled_header_subject_and_text_date(self):
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
        self.assertNotIn("महाराष्ट्र शासन", metadata["subject"])
        self.assertNotIn("PAGE", metadata["subject"])

    def test_filename_date_is_fallback(self):
        self.assertEqual(date_from_filename("202002201713214911.pdf"), date(2020, 2, 20))
        self.assertIsNone(date_from_filename("GR-number-only.pdf"))


if __name__ == "__main__":
    unittest.main()

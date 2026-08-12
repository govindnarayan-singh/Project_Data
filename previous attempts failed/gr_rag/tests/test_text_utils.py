import unittest

from src.text_utils import (
    digital_text_classification,
    extracted_text_quality,
    should_ocr,
)


class LanguageAwareTextQualityTests(unittest.TestCase):
    def assert_decision(self, text: str, classification: str, ocr: bool) -> None:
        self.assertEqual(digital_text_classification(text), classification)
        self.assertEqual(should_ocr(text), ocr)

    def test_good_marathi_digital_text_is_kept(self):
        text = (
            "महाराष्ट्र शासन सामान्य प्रशासन विभाग शासन निर्णय दिनांक २० जून २०२४. "
            "सदर निर्णय सर्व संबंधित कार्यालयांनी तत्काळ अंमलात आणावा. " * 4
        )
        self.assert_decision(text, "GOOD_MARATHI", False)

    def test_malformed_marathi_requires_ocr(self):
        text = (
            "महाराष्ट्र शासन सामान्य प्रशासन िवभाग शासन िनर्णय दिनांक २० जून २०२४. "
            "सदर निर्णय सर्व संबंधित कार्यालयांनी तत्काळ अंमलात आणावा. " * 4
        )
        self.assert_decision(text, "CORRUPTED_MARATHI", True)

    def test_good_english_digital_text_is_kept(self):
        text = (
            "Environment and Climate Change Department, Government of Maharashtra. "
            "This page lists action points, proposed marks, and evaluation details. " * 4
        )
        self.assert_decision(text, "GOOD_ENGLISH", False)

    def test_empty_page_requires_ocr(self):
        self.assert_decision("", "EMPTY_OR_IMAGE_ONLY", True)

    def test_short_valid_heading_is_kept(self):
        self.assert_decision("Thank you 148", "VERY_SHORT_VALID", False)

    def test_suspicious_hidden_latin_text_requires_ocr(self):
        text = (
            "r I ri i g t E E I z i e Ia t E f a tt I E Sv LJI "
            "r l t tr i q p o q o F o a o q r o e a q c I r a " * 8
        )
        self.assert_decision(text, "SUSPICIOUS_GARBLED_LATIN", True)

    def test_mixed_script_page_is_uncertain(self):
        text = (
            "शासन निर्णय जोडपत्र Environment Department action points and scheme "
            "details for Maharashtra local bodies and participating cities. " * 4
        )
        self.assert_decision(text, "UNCERTAIN", True)
        self.assertLess(extracted_text_quality(text), 0.55)


if __name__ == "__main__":
    unittest.main()

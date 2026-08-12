from __future__ import annotations

from datetime import date
import unittest

from knowledge_engine.metadata.validation import (
    ValidationStatus,
    validate_gr_date,
    validate_gr_number,
    validate_metadata,
    validate_subject,
)


class MetadataValidationTests(unittest.TestCase):
    def test_valid_metadata(self):
        raw = {
            "gr_number": "मावअ-2024/प्र.क्र.50/तां.क.1",
            "gr_date": date(2024, 8, 6),
            "gr_date_source": "text",
            "subject": "वाटंगी ग्रामपंचायतीच्या पर्यावरणीय विकास कामांना प्रशासकीय मान्यता देण्याबाबत",
        }
        result = validate_metadata(raw)
        self.assertEqual(result.gr_number.status, ValidationStatus.VALID)
        self.assertEqual(result.gr_date.status, ValidationStatus.VALID)
        self.assertEqual(result.subject.status, ValidationStatus.VALID)
        self.assertIs(result.gr_number.raw_value, raw["gr_number"])
        self.assertFalse(result.has_warnings)

    def test_missing_values(self):
        result = validate_metadata({"gr_number": " ", "gr_date": None, "subject": ""})
        self.assertEqual(result.gr_number.status, ValidationStatus.MISSING)
        self.assertEqual(result.gr_date.status, ValidationStatus.MISSING)
        self.assertEqual(result.subject.status, ValidationStatus.MISSING)

    def test_implausibly_short_gr_number_is_suspicious(self):
        result = validate_gr_number("म या")
        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)
        self.assertIn("too few", result.reason)

    def test_gr_number_without_structure_is_suspicious(self):
        self.assertEqual(
            validate_gr_number("शासन निर्णय क्रमांक").status,
            ValidationStatus.SUSPICIOUS,
        )

    def test_invalid_calendar_date_is_suspicious(self):
        result = validate_gr_date("2024-02-30", source="text")
        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)
        self.assertEqual(result.raw_value, "2024-02-30")

    def test_filename_date_requires_document_verification(self):
        result = validate_gr_date(date(2020, 2, 20), source="filename")
        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)
        self.assertIn("filename", result.reason)

    def test_short_and_table_like_subjects_are_suspicious(self):
        self.assertEqual(validate_subject("अनुदान बाबत").status, ValidationStatus.SUSPICIOUS)
        table = "| भूमी | वायू | जल | अग्नी | आकाश | गुण 100 200 300"
        result = validate_subject(table)
        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)
        self.assertIn("table", result.reason)

    def test_validation_does_not_rewrite_raw_values(self):
        raw = {"gr_number": "म या", "gr_date": "2024-02-30", "subject": "लघु"}
        snapshot = dict(raw)
        result = validate_metadata(raw)
        self.assertEqual(raw, snapshot)
        self.assertEqual(result.as_dict()["gr_number"]["raw_value"], "म या")

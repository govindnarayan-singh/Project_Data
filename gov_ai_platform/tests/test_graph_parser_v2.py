from django.test import SimpleTestCase

from knowledge_engine.graph.parser import (
    ISSUER_CITING_DOC_HINT_ONLY,
    ISSUER_EXPLICIT_CONTEXT,
    ReferenceParser,
    context_recovery_metadata,
)
from knowledge_engine.graph.types import ReferenceKind


def document(department="Environment Department"):
    return {
        "id": 991, "file_name": "source.pdf", "department": department,
        "gr_number": "SOURCE/2025/1", "gr_date": "2025-01-01",
    }


class ReferenceParserV2Tests(SimpleTestCase):
    def test_act_title_is_recovered_across_adjacent_line(self):
        rows = ReferenceParser().parse_document(document("Government of India"), [{
            "page_number": 1,
            "text": "This Act may be called the Environment\n(Protection) Act, 1986.\n2. Definitions.",
        }])
        assert rows[0].normalized_document_number == "ENVIRONMENT_PROTECTION_ACT_1986"
        assert "LEGAL_TITLE_FROM_BOUNDED_CONTEXT" in rows[0].parser_notes

    def test_schedule_heading_maps_to_named_base_act(self):
        rows = ReferenceParser().parse_document(document("Government of India"), [{
            "page_number": 20,
            "text": "SCHEDULE III\nAMENDMENT TO THE WATER (PREVENTION AND CONTROL OF POLLUTION) ACT, 1974\n(6 OF 1974)",
        }])
        assert rows[0].normalized_document_number == "WATER_PREVENTION_AND_CONTROL_OF_POLLUTION_ACT_1974"
        assert "LEGAL_SCHEDULE_HEADING_TO_BASE_ACT" in rows[0].parser_notes

    def test_amendment_version_semantics_are_preserved(self):
        rows = ReferenceParser().parse_document(document("Government of Maharashtra"), [{
            "page_number": 30,
            "text": (
                "Maharashtra Zilla Parishads and Panchayat\n"
                "Samitis (Second Amendment) Act, 2016 (Mah. XXV of 2016)"
            ),
        }])
        assert rows[0].normalized_document_number == (
            "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_SECOND_AMENDMENT_ACT_2016"
        )

    def test_combined_local_act_title_is_not_collapsed(self):
        rows = ReferenceParser().parse_document(document("Government of Maharashtra"), [{
            "page_number": 19,
            "text": (
                "on commencement of the Maharashtra Zilla Parishads and Panchayat Samitis\n"
                "and Bombay Village Panchayats (Amendment) Act, 1990 (Mah. X of 1990)"
            ),
        }])
        assert rows[0].normalized_document_number == (
            "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_AND_BOMBAY_VILLAGE_"
            "PANCHAYATS_AMENDMENT_ACT_1990"
        )

    def test_split_year_and_adjacent_legal_prefix_are_repaired(self):
        rows = ReferenceParser().parse_document(document("Government of Maharashtra"), [{
            "page_number": 25,
            "text": (
                "commencement of the Maharashtra Slum Areas\n"
                "(Improvement, Clearance and Redevelopment) (Amendment) Act, 20 14 (Mah. IX of 2014)"
            ),
        }])
        assert rows[0].normalized_document_number == (
            "MAHARASHTRA_SLUM_AREAS_IMPROVEMENT_CLEARANCE_AND_REDEVELOPMENT_AMENDMENT_ACT_2014"
        )

    def test_previous_attached_department_line_is_explicit_context(self):
        lines = ["Environment & Climate Change Department", "Government Resolution No. NGT-2021/C.R.07/TC-3"]
        result = context_recovery_metadata(lines, 1, lines[1], ReferenceKind.GOVERNMENT_RESOLUTION)
        assert result["issuer_state"] == ISSUER_EXPLICIT_CONTEXT
        assert result["normalized_issuer"] == "environment climate change department"
        assert result["issuer_evidence_location"] == "previous_line_1"

    def test_citing_department_is_hint_only_and_not_authoritative(self):
        result = context_recovery_metadata(
            ["Government Resolution No. ABC/2020/1"], 0,
            "Government Resolution No. ABC/2020/1", ReferenceKind.GOVERNMENT_RESOLUTION,
            "Finance Department",
        )
        assert result["issuer_state"] == ISSUER_CITING_DOC_HINT_ONLY
        assert result["normalized_issuer"] == ""

    def test_multi_reference_sentence_splits_dates(self):
        rows = ReferenceParser().parse_document(document(), [{
            "page_number": 1,
            "text": (
                "References: Government Resolution No. ABC/2020/1 dated 01/01/2020 "
                "and Government Circular No. XYZ/2021/2 dated 02/02/2021"
            ),
        }])
        assert [(row.normalized_document_number, row.normalized_reference_date.isoformat()) for row in rows] == [
            ("abc/2020/1", "2020-01-01"), ("xyz/2021/2", "2021-02-02"),
        ]

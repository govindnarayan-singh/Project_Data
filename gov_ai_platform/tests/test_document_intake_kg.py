from datetime import date

from django.test import SimpleTestCase

from knowledge_engine.graph.models import ReferenceCandidate
from knowledge_engine.graph.normalization import normalize_document_number
from knowledge_engine.graph.pipeline import KnowledgeGraphPipeline
from knowledge_engine.graph.quality import QualityPolicy
from knowledge_engine.graph.types import ParserStatus, ReferenceKind, ResolutionStatus
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy


def candidate(*, source_id: int, number: str, fingerprint: str) -> ReferenceCandidate:
    return ReferenceCandidate(
        source_document_id=source_id,
        source_file=f"document-{source_id}.pdf",
        source_department="Department A",
        source_page=1,
        reference_kind=ReferenceKind.GOVERNMENT_RESOLUTION,
        raw_evidence_text=f"Government Resolution No. {number}",
        raw_document_number=number,
        normalized_document_number=normalize_document_number(number),
        raw_reference_date="",
        normalized_reference_date=None,
        date_parse_status="MISSING",
        raw_department="",
        normalized_department="",
        pattern_id="STAGE3_TEST",
        parser_version="test",
        parser_status=ParserStatus.VALID,
        parser_notes="",
        relation_signal="",
        candidate_relation_type="",
        evidence_hash=fingerprint,
        fingerprint=fingerprint,
    )


def stored_row(item: ReferenceCandidate) -> dict:
    return {
        "source_document_id": item.source_document_id,
        "source_file": item.source_file,
        "source_department": item.source_department,
        "source_page": item.source_page,
        "reference_kind": item.reference_kind.value,
        "raw_evidence_text": item.raw_evidence_text,
        "raw_document_number": item.raw_document_number,
        "normalized_document_number": item.normalized_document_number,
        "raw_reference_date": item.raw_reference_date,
        "normalized_reference_date": item.normalized_reference_date,
        "date_parse_status": item.date_parse_status,
        "raw_department": item.raw_department,
        "normalized_department": item.normalized_department,
        "pattern_id": item.pattern_id,
        "parser_version": item.parser_version,
        "parser_status": item.parser_status.value,
        "parser_notes": item.parser_notes,
        "relation_signal": item.relation_signal,
        "candidate_relation_type": item.candidate_relation_type,
        "evidence_hash": item.evidence_hash,
        "fingerprint": item.fingerprint,
    }


class FakeRepository:
    def __init__(self):
        self.documents = [
            {
                "id": 1, "file_name": "existing.pdf", "file_path": "existing.pdf",
                "department": "Department A", "document_type": "GOVERNMENT_RESOLUTION",
                "document_kind": "GOVERNMENT_RESOLUTION", "gr_number": "OLD/2020/1",
                "canonical_identity": "", "gr_date": None,
            },
            {
                "id": 2, "file_name": "admitted.pdf", "file_path": "admitted.pdf",
                "department": "Department A", "document_type": "GOVERNMENT_RESOLUTION",
                "document_kind": "GOVERNMENT_RESOLUTION", "gr_number": "NEW/2026/41",
                "canonical_identity": "", "gr_date": None,
            },
            {
                "id": 3, "file_name": "unrelated.pdf", "file_path": "unrelated.pdf",
                "department": "Department B", "document_type": "GOVERNMENT_RESOLUTION",
                "document_kind": "GOVERNMENT_RESOLUTION", "gr_number": "OTHER/2019/5",
                "canonical_identity": "", "gr_date": None,
            },
        ]
        self.pages = [{"document_id": 2, "page_number": 1, "text": "bounded new document page"}]
        self.persisted = None
        self.finished = None

    def load_corpus(self):
        return self.documents, self.pages

    def start_run(self, *args, **kwargs):
        self.started = (args, kwargs)

    def targeted_unresolved_candidates(self, normalized_identifier):
        assert normalized_identifier == normalize_document_number("NEW/2026/41")
        return [stored_row(candidate(source_id=1, number="NEW/2026/41", fingerprint="e" * 64))]

    def persist_run(self, run_id, documents, items):
        self.persisted = items
        return {
            "inserted_candidates": 1,
            "updated_candidates": 1,
            "reference_edges_inserted": 2,
            "issued_by_edges_inserted": 1,
            "evidence_links_inserted": 2,
        }

    def finish_run(self, *args, **kwargs):
        self.finished = (args, kwargs)


class FakeParser:
    def parse_document(self, document, pages):
        assert document["id"] == 2
        assert [page["document_id"] for page in pages] == [2]
        return [candidate(source_id=2, number="OLD/2020/1", fingerprint="n" * 64)]


class TargetedKnowledgeGraphTests(SimpleTestCase):
    def test_new_candidates_and_matching_existing_unresolved_are_bounded(self):
        repository = FakeRepository()
        pipeline = KnowledgeGraphPipeline(
            repository=repository,
            parser=FakeParser(),
            quality_policy=QualityPolicy([]),
            validation_policy=KnowledgeGraphValidationPolicy("auto"),
        )
        result = pipeline.run_targeted(2)
        self.assertEqual(result["new_candidates"], 1)
        self.assertEqual(result["new_resolved"], 1)
        self.assertEqual(result["existing_reprocessed"], 1)
        self.assertEqual(result["existing_newly_resolved"], 1)
        self.assertEqual({item[0].source_document_id for item in repository.persisted}, {1, 2})
        self.assertNotIn(3, {item[0].source_document_id for item in repository.persisted})
        self.assertTrue(all(item[1].status == ResolutionStatus.RESOLVED_EXACT for item in repository.persisted))
        self.assertEqual(result["reference_edges_inserted"], 2)
        self.assertNotIn("vector", str(result).casefold())

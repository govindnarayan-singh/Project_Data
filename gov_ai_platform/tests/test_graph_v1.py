from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from knowledge_engine.graph.models import ReferenceCandidate
from knowledge_engine.graph.normalization import (
    normalize_department_alias,
    normalize_digits,
    normalize_document_number,
    normalize_legal_reference,
    normalize_reference_date,
    normalize_unicode,
)
from knowledge_engine.graph.parser import ReferenceParser, detect_reference_kind
from knowledge_engine.graph.quality import QualityPolicy
from knowledge_engine.graph.resolver import ReferenceResolver
from knowledge_engine.graph.service import GraphService
from knowledge_engine.graph.types import ParserStatus, ReferenceKind, ResolutionStatus, ValidationStatus
from knowledge_engine.graph.validation import (
    KnowledgeGraphValidationPolicy,
    ReviewMode,
    SignalConfidence,
    classify_relation_signal,
)


def document(document_id=1, number="SOURCE-2024/1", date="2024-01-01", department="Finance"):
    return {"id": document_id, "file_name": f"{document_id}.pdf", "department": department, "gr_number": number, "gr_date": date}


def parse(text, *, source=None):
    return ReferenceParser().parse_document(source or document(), [{"page_number": 1, "text": text}])


def policy(rows="document_id,file_name,page_number,field,raw_value,review_status,reviewed_value,reason,kg_usage_policy\n"):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "policy.csv"
        path.write_text(rows, encoding="utf-8")
        return QualityPolicy.from_csv(path)


class GraphNormalizationTests(SimpleTestCase):
    def test_unicode_digits_and_identifier_spacing(self):
        assert normalize_unicode("A\u200b") == "A"
        assert normalize_digits("१२३") == "123"
        assert normalize_document_number("मावअ - २०२१ / प्र. क्र. ७५ / तां. क. १") == "मावअ-2021/प्र.क्र.75/तां.क.1"

    def test_department_alias(self):
        assert normalize_department_alias("Government of Maharashtra - Finance Department") == "finance department"
        assert normalize_department_alias("पर्यावरण व वातावरणीय बदल विभाग") == "environment department"

    def test_act_and_rule_canonicalization(self):
        assert normalize_legal_reference("Rights of Persons with Disabilities Act, 2016", "ACT") == "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016"
        assert normalize_legal_reference("Construction and Demolition Waste Management Rules, 2016", "RULE") == "CONSTRUCTION_AND_DEMOLITION_WASTE_MANAGEMENT_RULES_2016"

    def test_all_observed_date_styles_including_dt(self):
        assert normalize_reference_date("१२ ऑक्टोबर, २०२०").iso_date == "2020-10-12"
        assert normalize_reference_date("5th Oct 2023").iso_date == "2023-10-05"
        assert normalize_reference_date("14.03.2018").iso_date == "2018-03-14"


class ReferenceParserV1Tests(SimpleTestCase):
    def test_marathi_english_letters_acts_and_rules(self):
        assert detect_reference_kind("शासन परिपत्रक क्र. ABC/2020/1") == ReferenceKind.GOVERNMENT_CIRCULAR
        assert detect_reference_kind("Government Resolution No. ABC/2020/1") == ReferenceKind.GOVERNMENT_RESOLUTION
        assert detect_reference_kind("Department Letter No. F. No. 1(46)/PFMS/2022") == ReferenceKind.DEPARTMENT_LETTER
        assert detect_reference_kind("Rights of Persons with Disabilities Act, 2016") == ReferenceKind.ACT
        assert detect_reference_kind("Waste Management Rules, 2016") == ReferenceKind.RULE

    def test_multiline_reference(self):
        candidates = parse("संदर्भ :-\n१) शासन निर्णय क्रमांक ABC-२०२०/प्र.क्र.१\nदिनांक १२ ऑक्टोबर, २०२०\nशासन निर्णय :-")
        assert len(candidates) == 1
        assert candidates[0].normalized_document_number == "abc-2020/प्र.क्र.1"
        assert candidates[0].normalized_reference_date.isoformat() == "2020-10-12"

    def test_multiple_semicolon_references_and_duplicate_prevention(self):
        text = "संदर्भ :-\n१) शासन निर्णय क्र. ABC/2020/1 दि. 01/01/2020; २) शासन परिपत्रक क्र. XYZ/2021/2 दि. 02/02/2021"
        candidates = parse(text)
        assert {item.normalized_document_number for item in candidates} == {"abc/2020/1", "xyz/2021/2"}
        duplicate = parse("संदर्भ :-\n१) शासन निर्णय क्र. ABC/2020/1 दि. 01/01/2020\n१) शासन निर्णय क्र. ABC/2020/1 दि. 01/01/2020")
        assert len(duplicate) == 1

    def test_false_positive_and_self_reference(self):
        assert parse("This policy discussion contains no document identifier.") == []
        candidates = parse("शासन निर्णय क्रमांक SOURCE-2024/1 दिनांक 01/01/2024")
        assert candidates[0].parser_status == ParserStatus.SELF_REFERENCE_REJECTED

    def test_dt_date_and_relation_signal_candidate_only(self):
        candidate = parse("Government Resolution No. ABC/2020/1, Dt. 29th December 2022, amended") [0]
        assert candidate.normalized_reference_date.isoformat() == "2022-12-29"
        assert candidate.candidate_relation_type == "AMENDS"

    def test_fingerprint_is_deterministic(self):
        first = parse("Government Resolution No. ABC/2020/1 dated 1st Jan 2020")[0]
        second = parse("Government Resolution No. ABC/2020/1 dated 1st Jan 2020")[0]
        assert first.fingerprint == second.fingerprint

    def test_explicit_reference_issuer_is_extracted_and_canonicalized(self):
        candidates = parse(
            "संदर्भ :-\n१) पर्यावरण व वातावरणीय बदल विभाग शासन निर्णय क्र. अभियान-2021/प्र.क्र.14/तां.क.1 दि. 01/01/2021"
        )
        assert candidates[0].raw_department == "पर्यावरण व वातावरणीय बदल विभाग"
        assert candidates[0].normalized_department == "environment department"

    def test_english_reference_issuer_is_extracted(self):
        candidate = parse(
            "References: Finance Department Government Resolution No. FIN-2020/CR-1 dated 01/01/2020"
        )[0]
        assert candidate.normalized_department == "finance department"

    def test_issuer_after_document_type_is_extracted(self):
        candidate = parse(
            "वाचा: (१) शासन निर्णय, पर्यावरण व वातावरणीय बदल विभाग क्र. अभियान-2022/प्र.क्र.120/तां.क.1 दि. 05/12/2022"
        )[0]
        assert candidate.normalized_department == "environment department"

    def test_roman_item_prefix_does_not_pollute_issuer(self):
        candidate = parse(
            "संदर्भ: x) संसदीय कार्य विभाग, शासन निर्णय क्रमांक: याचिका-2021/प्र.क्र.95/पाच, दि.25.08.2021"
        )[0]
        assert candidate.normalized_department == "parliamentary affairs department"

    def test_nested_document_type_does_not_pollute_issuer(self):
        candidate = parse(
            "ष्. हा शासन निर्णय, शासन परिपत्रक, वित्त विभाग, क्रमांकः- अर्थसं-2023/प्र.क्र.40/अर्थ-3, दिनांक 01/01/2023"
        )[0]
        assert candidate.raw_department == "वित्त विभाग"
        assert candidate.normalized_department == "finance department"


class ResolverPolicyTests(SimpleTestCase):
    def setUp(self):
        self.empty_policy = policy()

    def test_exact_ambiguous_and_not_in_corpus(self):
        candidate = parse("Government Resolution No. ABC/2020/1")[0]
        exact = ReferenceResolver([document(), document(2, "ABC/2020/1", "")], self.empty_policy, auto_verify_enabled=True).resolve(candidate)
        assert exact.status == ResolutionStatus.RESOLVED_EXACT
        assert exact.validation_status == ValidationStatus.AUTO_VERIFIED
        ambiguous = ReferenceResolver([document(), document(2, "ABC/2020/1", ""), document(3, "ABC/2020/1", "")], self.empty_policy, auto_verify_enabled=True).resolve(candidate)
        assert ambiguous.status == ResolutionStatus.AMBIGUOUS
        assert ambiguous.target_document_id is None
        missing = ReferenceResolver([document()], self.empty_policy, auto_verify_enabled=True).resolve(candidate)
        assert missing.status == ResolutionStatus.NOT_IN_CORPUS
        assert missing.validation_status == ValidationStatus.UNRESOLVED

    def test_quality_override_blocks_auto_verified(self):
        csv_rows = "document_id,file_name,page_number,field,raw_value,review_status,reviewed_value,reason,kg_usage_policy\n1,1.pdf,1,page_text,,REVIEWED,,bad OCR,DO_NOT_AUTO_VERIFY\n"
        candidate = parse("Government Resolution No. ABC/2020/1")[0]
        result = ReferenceResolver([document(), document(2, "ABC/2020/1", "")], policy(csv_rows), auto_verify_enabled=True).resolve(candidate)
        assert result.status == ResolutionStatus.RESOLVED_EXACT
        assert result.validation_status == ValidationStatus.CANDIDATE
        assert result.blocked_by_page_quality


class AutomaticReviewPolicyTests(SimpleTestCase):
    def candidate(self, **changes):
        value = {
            "id": 10, "source_document_id": 1, "resolved_target_document_id": 2,
            "candidate_relation_type": "AMENDS", "reference_kind": "GOVERNMENT_RESOLUTION",
            "raw_evidence_text": "Government Resolution No. ABC/2020/1 is hereby amended",
            "normalized_document_number": "abc/2020/1", "parser_status": "VALID",
            "resolution_status": "RESOLVED_EXACT", "blocked_by_page_quality": False,
            "blocked_by_target_metadata": False,
        }
        value.update(changes)
        return value

    def test_review_mode_parsing_and_invalid_mode(self):
        assert ReviewMode.parse(" strict ") == ReviewMode.STRICT
        assert ReviewMode.parse("AUTO") == ReviewMode.AUTO
        with self.assertRaises(ValueError):
            ReviewMode.parse("unsafe")

    def test_strict_mode_never_machine_promotes_strong_relation(self):
        decision = KnowledgeGraphValidationPolicy("strict").strong_relation_decision(self.candidate())
        assert not decision.approved
        assert decision.validation_status == "CANDIDATE"

    def test_auto_mode_requires_explicit_phrase_and_unique_target(self):
        policy = KnowledgeGraphValidationPolicy("auto")
        approved = policy.strong_relation_decision(self.candidate())
        assert approved.approved
        assert approved.validation_status == "AUTO_VERIFIED"
        contextual = policy.strong_relation_decision(self.candidate(raw_evidence_text="Amendment Act 2021"))
        assert not contextual.approved
        unresolved = policy.strong_relation_decision(self.candidate(
            resolution_status="NOT_IN_CORPUS", resolved_target_document_id=None))
        assert not unresolved.approved

    def test_quality_and_metadata_blocks_are_enforced(self):
        policy = KnowledgeGraphValidationPolicy("auto")
        assert not policy.strong_relation_decision(self.candidate(blocked_by_page_quality=True)).approved
        assert not policy.strong_relation_decision(self.candidate(blocked_by_target_metadata=True)).approved
        assert not policy.strong_relation_decision(self.candidate(
            normalized_department="finance", target_department="Housing")).approved

    def test_ambiguous_target_and_self_relation_are_rejected(self):
        policy = KnowledgeGraphValidationPolicy("auto")
        assert not policy.strong_relation_decision(self.candidate(
            resolution_status="AMBIGUOUS", resolved_target_document_id=None)).approved
        assert not policy.strong_relation_decision(self.candidate(resolved_target_document_id=1)).approved

    def test_phrase_catalogue_separates_explicit_from_contextual(self):
        explicit = classify_relation_signal("SUPERSEDES", "The order is hereby superseded")
        assert explicit.confidence == SignalConfidence.HIGH_CONFIDENCE_EXPLICIT
        contextual = classify_relation_signal("CORRECTS", "Government corrigendum number ABC/2020/1")
        assert contextual.confidence == SignalConfidence.CONTEXT_DEPENDENT

    def test_dry_run_never_claims_human_confirmation(self):
        result = KnowledgeGraphValidationPolicy("auto").dry_run([self.candidate()])
        assert result["approved_by_relation"]["AMENDS"] == 1
        assert result["human_confirmed_created"] == 0

    def test_parser_gate_disables_auto_verify(self):
        candidate = parse("Government Resolution No. ABC/2020/1")[0]
        result = ReferenceResolver([document(), document(2, "ABC/2020/1", "")], policy(), auto_verify_enabled=False).resolve(candidate)
        assert result.validation_status == ValidationStatus.CANDIDATE


class FakeGraphRepository:
    def get_document(self, document_id): return {"id": document_id} if document_id == 1 else None
    def get_outgoing_references(self, document_id): return [{"relation_id": 2}]
    def get_incoming_references(self, document_id): return []
    def get_unresolved_references(self, document_id): return [{"candidate_id": 3}]
    def get_issued_by(self, document_id): return {"canonical_name": "Finance"}
    def get_relation_signals(self, document_id): return []
    def get_relation_evidence(self, relation_id): return [{"candidate_id": 3}]
    def get_graph_summary(self): return {"reference_candidates": 1}


class GraphServiceTests(SimpleTestCase):
    def test_service_queries_and_invalid_identity(self):
        service = GraphService(FakeGraphRepository())
        detail = service.get_document_detail(1)
        assert detail["outgoing_references"][0]["relation_id"] == 2
        assert detail["unresolved_references"][0]["candidate_id"] == 3
        assert service.get_document_detail(999) is None
        assert service.get_relation_evidence(-1) == []

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import ReferenceCandidate, Resolution
from .types import ParserStatus, ResolutionStatus, ValidationStatus


class ReviewMode(StrEnum):
    STRICT = "strict"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str | None) -> "ReviewMode":
        normalized = (value or "auto").strip().casefold()
        try:
            return cls(normalized)
        except ValueError as error:
            raise ValueError("KG_REVIEW_MODE must be 'strict' or 'auto'") from error


class SignalConfidence(StrEnum):
    HIGH_CONFIDENCE_EXPLICIT = "HIGH_CONFIDENCE_EXPLICIT"
    CONTEXT_DEPENDENT = "CONTEXT_DEPENDENT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class RelationSignalAssessment:
    relation_type: str
    confidence: SignalConfidence
    pattern_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    candidate_id: int | None
    relation_type: str
    approved: bool
    validation_status: str
    reason: str
    pattern_id: str = ""
    source_document_id: int | None = None
    target_document_id: int | None = None


EXPLICIT_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "AMENDS": (
        ("AMENDS_HEREBY_EN", re.compile(r"\b(?:is|are)\s+hereby\s+amended\b|\bhereby\s+amends\b", re.I)),
        ("AMENDS_EXPLICIT_MR", re.compile(r"(?:मध्ये|मधील).{0,100}(?:सुधारणा|दुरुस्ती).{0,60}(?:करण्यात|केली)", re.I)),
    ),
    "CORRECTS": (
        ("CORRECTS_EXPLICIT_EN", re.compile(r"\bcorrigendum\s+to\b|\bhereby\s+corrects\b|\bstands?\s+corrected\b", re.I)),
        ("CORRECTS_EXPLICIT_MR", re.compile(r"(?:मध्ये|मधील).{0,100}(?:शुद्धी|दुरुस्ती).{0,60}(?:करण्यात|वाचण्यात)", re.I)),
    ),
    "SUPERSEDES": (
        ("SUPERSEDES_EXPLICIT_EN", re.compile(r"\b(?:is|are)\s+hereby\s+superseded\b|\bin\s+supersession\s+of\b", re.I)),
        ("SUPERSEDES_EXPLICIT_MR", re.compile(r"(?:अधिक्रमित|अधिक्रांत).{0,50}(?:करण्यात|करून|होईल)", re.I)),
    ),
    "EXTENDS": (
        ("EXTENDS_EXPLICIT_EN", re.compile(r"\b(?:period|term|deadline|validity).{0,80}\b(?:is|has\s+been)\s+extended\b", re.I)),
        ("EXTENDS_EXPLICIT_MR", re.compile(r"(?:मुदत|कालावधी).{0,80}(?:वाढविण्यात|वाढवून|मुदतवाढ\s+देण्यात)", re.I)),
    ),
    "CANCELS": (
        ("CANCELS_EXPLICIT_EN", re.compile(r"\b(?:is|are)\s+hereby\s+(?:cancelled|rescinded)\b", re.I)),
        ("CANCELS_EXPLICIT_MR", re.compile(r"(?:रद्द|निरस्त).{0,50}(?:करण्यात|करून|ठरविण्यात)", re.I)),
    ),
    "PARTIALLY_SUPERSEDES": (
        ("PARTIAL_SUPERSESSION_EN", re.compile(r"\bpartially\s+supersed", re.I)),
        ("PARTIAL_SUPERSESSION_MR", re.compile(r"अंशत[ः:].{0,40}(?:अधिक्रमित|अधिक्रांत)", re.I)),
    ),
}

CONTEXT_PATTERNS = re.compile(
    r"\bamend(?:ed|ment)?\b|\bcorrigendum\b|\bsupersed|\bextend|सुधारणा|दुरुस्ती|शुद्धीपत्रक|मुदत\s*वाढ|अधिक्रमित|रद्द|निरस्त",
    re.I,
)


def classify_relation_signal(relation_type: str, evidence: str) -> RelationSignalAssessment:
    relation_type = (relation_type or "").strip().upper()
    for pattern_id, pattern in EXPLICIT_PATTERNS.get(relation_type, ()):
        if pattern.search(evidence or ""):
            return RelationSignalAssessment(
                relation_type, SignalConfidence.HIGH_CONFIDENCE_EXPLICIT, pattern_id,
                "Explicit deterministic wording identifies the administrative relation",
            )
    if CONTEXT_PATTERNS.search(evidence or ""):
        return RelationSignalAssessment(
            relation_type, SignalConfidence.CONTEXT_DEPENDENT, "CONTEXT_ONLY",
            "A relation word is present, but it does not explicitly link the action to the extracted target",
        )
    return RelationSignalAssessment(
        relation_type, SignalConfidence.AMBIGUOUS, "NO_EXPLICIT_PATTERN",
        "No approved explicit relation phrase matched",
    )


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


class KnowledgeGraphValidationPolicy:
    """Central machine-vs-human trust boundary for graph validation."""

    def __init__(self, mode: ReviewMode | str = ReviewMode.AUTO, *, reference_gate_passed: bool = True):
        self.mode = mode if isinstance(mode, ReviewMode) else ReviewMode.parse(mode)
        self.reference_gate_passed = reference_gate_passed

    @classmethod
    def from_env(cls, *, reference_gate_passed: bool = True) -> "KnowledgeGraphValidationPolicy":
        from knowledge_engine.settings import settings
        return cls(settings.kg_review_mode, reference_gate_passed=reference_gate_passed)

    @staticmethod
    def identifier_is_structurally_valid(value: str, reference_kind: str = "") -> bool:
        if not value or len(value) < 5:
            return False
        if reference_kind in {"ACT", "RULE"}:
            return bool(re.search(r"(?:19|20)\d{2}", value) and re.search(r"[A-Za-z\u0900-\u097f]", value))
        return bool(re.search(r"\d", value) and re.search(r"[/.-]", value))

    def reference_validation(
        self, candidate: ReferenceCandidate, status: ResolutionStatus, *, blocked_page: bool, blocked_target: bool,
    ) -> ValidationStatus:
        if status in {ResolutionStatus.NOT_IN_CORPUS, ResolutionStatus.UNPARSEABLE}:
            return ValidationStatus.UNRESOLVED
        if status == ResolutionStatus.SELF_REFERENCE_REJECTED:
            return ValidationStatus.REJECTED
        if (
            self.reference_gate_passed
            and status in {ResolutionStatus.RESOLVED_EXACT, ResolutionStatus.RESOLVED_STRONG}
            and candidate.parser_status == ParserStatus.VALID
            and self.identifier_is_structurally_valid(candidate.normalized_document_number, candidate.reference_kind.value)
            and not blocked_page and not blocked_target
        ):
            return ValidationStatus.AUTO_VERIFIED
        return ValidationStatus.CANDIDATE

    def strong_relation_decision(self, candidate: Any) -> PromotionDecision:
        candidate_id = _value(candidate, "id") or _value(candidate, "candidate_id")
        relation_type = str(_value(candidate, "candidate_relation_type", "") or "").upper()
        assessment = classify_relation_signal(relation_type, str(_value(candidate, "raw_evidence_text", "") or ""))
        base = {
            "candidate_id": int(candidate_id) if candidate_id else None,
            "relation_type": relation_type,
            "validation_status": ValidationStatus.CANDIDATE.value,
            "pattern_id": assessment.pattern_id,
            "source_document_id": _value(candidate, "source_document_id"),
            "target_document_id": _value(candidate, "resolved_target_document_id") or _value(candidate, "target_document_id"),
        }
        if self.mode == ReviewMode.STRICT:
            return PromotionDecision(**base, approved=False, reason="Strict mode requires actual HUMAN_CONFIRMED review")
        if relation_type not in EXPLICIT_PATTERNS:
            return PromotionDecision(**base, approved=False, reason="Relation type is not eligible for automatic promotion")
        if assessment.confidence != SignalConfidence.HIGH_CONFIDENCE_EXPLICIT:
            return PromotionDecision(**base, approved=False, reason=assessment.reason)
        if str(_value(candidate, "parser_status", "")) != ParserStatus.VALID.value:
            return PromotionDecision(**base, approved=False, reason="Parser status is not VALID")
        if not self.identifier_is_structurally_valid(
            str(_value(candidate, "normalized_document_number", "") or ""), str(_value(candidate, "reference_kind", "") or ""),
        ):
            return PromotionDecision(**base, approved=False, reason="A structurally valid specific target identifier is required")
        if str(_value(candidate, "resolution_status", "")) not in {
            ResolutionStatus.RESOLVED_EXACT.value, ResolutionStatus.RESOLVED_STRONG.value,
        } or not base["target_document_id"]:
            return PromotionDecision(**base, approved=False, reason="Target is not uniquely resolved")
        if bool(_value(candidate, "blocked_by_page_quality", False)):
            return PromotionDecision(**base, approved=False, reason="Source page is blocked by the KG quality policy")
        if bool(_value(candidate, "blocked_by_target_metadata", False)):
            return PromotionDecision(**base, approved=False, reason="Target metadata is blocked by the KG quality policy")
        if (
            str(_value(candidate, "target_source_origin", "") or "") == "EXTERNAL_OFFICIAL"
            and not _value(candidate, "target_external_candidate_id")
        ):
            return PromotionDecision(**base, approved=False, reason="External target provenance is incomplete")
        explicit_department = str(_value(candidate, "normalized_department", "") or "")
        target_department = str(_value(candidate, "target_department", "") or "")
        if explicit_department and target_department:
            from .normalization import normalize_department_alias
            if explicit_department != normalize_department_alias(target_department):
                return PromotionDecision(**base, approved=False, reason="Explicit reference department conflicts with target metadata")
        if base["source_document_id"] == base["target_document_id"]:
            return PromotionDecision(**base, approved=False, reason="Self-relations cannot be promoted")
        return PromotionDecision(
            **{**base, "validation_status": ValidationStatus.AUTO_VERIFIED.value}, approved=True,
            reason="All deterministic identity, explicit wording, quality, and provenance gates passed",
        )

    def dry_run(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        decisions = [self.strong_relation_decision(row) for row in candidates if row.get("candidate_relation_type")]
        relation_types = ("AMENDS", "CORRECTS", "SUPERSEDES", "EXTENDS", "CANCELS", "PARTIALLY_SUPERSEDES")
        return {
            "review_mode": self.mode.value,
            "machine_approved_state": ValidationStatus.AUTO_VERIFIED.value,
            "human_confirmed_created": 0,
            "references_auto_verified": sum(
                row.get("validation_status") == ValidationStatus.AUTO_VERIFIED.value
                and row.get("resolution_status") in {
                    ResolutionStatus.RESOLVED_EXACT.value, ResolutionStatus.RESOLVED_STRONG.value
                }
                for row in candidates
            ),
            "signals_evaluated": len(decisions),
            "approved_by_relation": {
                relation_type: sum(item.approved and item.relation_type == relation_type for item in decisions)
                for relation_type in relation_types
            },
            "decisions": [
                {field: getattr(item, field) for field in item.__dataclass_fields__}
                for item in decisions
            ],
        }

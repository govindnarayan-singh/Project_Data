from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .models import ReferenceCandidate, Resolution
from .normalization import normalize_department_alias, normalize_document_number
from .quality import QualityPolicy
from .types import ParserStatus, RESOLVER_VERSION, ResolutionStatus, ValidationStatus
from .validation import KnowledgeGraphValidationPolicy


class ReferenceResolver:
    version = RESOLVER_VERSION

    def __init__(
        self,
        documents: Iterable[dict[str, Any]],
        quality_policy: QualityPolicy,
        *,
        auto_verify_enabled: bool,
        validation_policy: KnowledgeGraphValidationPolicy | None = None,
    ):
        self.documents = [dict(document) for document in documents]
        self.quality_policy = quality_policy
        self.auto_verify_enabled = auto_verify_enabled
        self.validation_policy = validation_policy or KnowledgeGraphValidationPolicy(
            reference_gate_passed=auto_verify_enabled
        )
        self.by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.blocked_by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for document in self.documents:
            raw = str(document.get("gr_number") or "")
            normalized = normalize_document_number(raw)
            if not normalized:
                continue
            if quality_policy.field_blocks_resolution(int(document["id"]), "gr_number"):
                self.blocked_by_number[normalized].append(document)
            else:
                self.by_number[normalized].append(document)

    def _validation(self, candidate: ReferenceCandidate, status: ResolutionStatus, *, blocked_page: bool, blocked_target: bool) -> ValidationStatus:
        if status in {ResolutionStatus.NOT_IN_CORPUS, ResolutionStatus.UNPARSEABLE}:
            return ValidationStatus.UNRESOLVED
        if status in {ResolutionStatus.SELF_REFERENCE_REJECTED}:
            return ValidationStatus.REJECTED
        return self.validation_policy.reference_validation(
            candidate, status, blocked_page=blocked_page, blocked_target=blocked_target
        )

    def resolve(self, candidate: ReferenceCandidate) -> Resolution:
        blocked_page = self.quality_policy.page_blocks_auto_verify(candidate.source_document_id, candidate.source_page)
        if candidate.parser_status == ParserStatus.SELF_REFERENCE_REJECTED:
            return Resolution(
                ResolutionStatus.SELF_REFERENCE_REJECTED, None, "", "SOURCE_IDENTITY_MATCH",
                "Parser identified a source metadata/header self-reference", 0, "HIGH",
                ValidationStatus.REJECTED, blocked_by_page_quality=blocked_page,
            )
        number = candidate.normalized_document_number
        if not number:
            return Resolution(
                ResolutionStatus.UNPARSEABLE, None, "", "NO_IDENTIFIER",
                "No normalized identifier is available", 0, "LOW", ValidationStatus.UNRESOLVED,
                blocked_by_page_quality=blocked_page,
            )
        blocked_targets = [row for row in self.blocked_by_number.get(number, []) if int(row["id"]) != candidate.source_document_id]
        if blocked_targets:
            status = ResolutionStatus.NEEDS_REVIEW
            return Resolution(
                status, None, "", "TARGET_IDENTIFIER_QUALITY_BLOCK",
                "Identifier matches target metadata marked missing/suspicious by the KG quality policy",
                len(blocked_targets), "LOW", ValidationStatus.CANDIDATE,
                blocked_by_page_quality=blocked_page, blocked_by_target_metadata=True,
            )
        matches = [row for row in self.by_number.get(number, []) if int(row["id"]) != candidate.source_document_id]
        if not matches and any(int(row["id"]) == candidate.source_document_id for row in self.by_number.get(number, [])):
            return Resolution(
                ResolutionStatus.SELF_REFERENCE_REJECTED, None, "", "SOURCE_IDENTIFIER_ONLY",
                "Normalized identifier only matches the source document", 0, "HIGH",
                ValidationStatus.REJECTED, blocked_by_page_quality=blocked_page,
            )
        if len(matches) == 1:
            target = matches[0]
            target_date = str(target.get("gr_date") or "")
            reference_date = candidate.normalized_reference_date.isoformat() if candidate.normalized_reference_date else ""
            if reference_date and target_date and reference_date != target_date:
                return Resolution(
                    ResolutionStatus.AMBIGUOUS, None, "", "NUMBER_MATCH_DATE_CONFLICT",
                    f"Unique number match conflicts with stored target date: reference={reference_date}, target={target_date}",
                    1, "LOW", ValidationStatus.CANDIDATE, blocked_by_page_quality=blocked_page,
                )
            target_department = normalize_department_alias(str(target.get("department") or ""))
            if candidate.normalized_department and candidate.normalized_department != target_department:
                return Resolution(
                    ResolutionStatus.AMBIGUOUS, None, "", "NUMBER_MATCH_DEPARTMENT_CONFLICT",
                    "Unique number match conflicts with the explicitly extracted reference department",
                    1, "LOW", ValidationStatus.CANDIDATE, blocked_by_page_quality=blocked_page,
                )
            department_agrees = not candidate.normalized_department or candidate.normalized_department == target_department
            method = "EXACT_NORMALIZED_NUMBER_AND_DATE" if reference_date else "EXACT_NORMALIZED_NUMBER"
            status = ResolutionStatus.RESOLVED_EXACT
            validation = self._validation(candidate, status, blocked_page=blocked_page, blocked_target=False)
            return Resolution(
                status, int(target["id"]), str(target["file_name"]), method,
                f"Unique exact normalized identifier; date_agrees={not reference_date or reference_date == target_date}; department_agrees={department_agrees}",
                1, "HIGH", validation, blocked_by_page_quality=blocked_page,
            )
        if len(matches) > 1:
            reference_date = candidate.normalized_reference_date.isoformat() if candidate.normalized_reference_date else ""
            narrowed = [
                row for row in matches
                if (not reference_date or str(row.get("gr_date") or "") == reference_date)
                and (not candidate.normalized_department or normalize_department_alias(str(row.get("department") or "")) == candidate.normalized_department)
            ]
            if len(narrowed) == 1:
                target = narrowed[0]
                status = ResolutionStatus.RESOLVED_STRONG
                validation = self._validation(candidate, status, blocked_page=blocked_page, blocked_target=False)
                return Resolution(
                    status, int(target["id"]), str(target["file_name"]), "NUMBER_DATE_DEPARTMENT",
                    "Multiple identifier matches narrowed uniquely by reference date and department",
                    len(matches), "HIGH", validation, blocked_by_page_quality=blocked_page,
                )
            return Resolution(
                ResolutionStatus.AMBIGUOUS, None, "", "MULTIPLE_NUMBER_MATCHES",
                "Normalized identifier maps to multiple stored documents", len(matches), "LOW",
                ValidationStatus.CANDIDATE, blocked_by_page_quality=blocked_page,
            )
        status = ResolutionStatus.NEEDS_REVIEW if candidate.parser_status == ParserStatus.NEEDS_REVIEW else ResolutionStatus.NOT_IN_CORPUS
        validation = ValidationStatus.CANDIDATE if status == ResolutionStatus.NEEDS_REVIEW else ValidationStatus.UNRESOLVED
        return Resolution(
            status, None, "", "NO_NORMALIZED_NUMBER_MATCH",
            "No stored document has this normalized identifier", 0, "HIGH" if status == ResolutionStatus.NOT_IN_CORPUS else "LOW",
            validation, blocked_by_page_quality=blocked_page,
        )

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any, Iterable

from .models import ReferenceCandidate, ResolutionResult
from .normalization import normalize_document_number


class DiagnosticResolver:
    """Read-only deterministic resolver over already loaded document metadata."""

    def __init__(self, documents: Iterable[dict[str, Any]]):
        self.documents = [dict(document) for document in documents]
        self.by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for document in self.documents:
            normalized = normalize_document_number(str(document.get("gr_number") or ""))
            if normalized:
                self.by_number[normalized].append(document)

    def resolve(self, candidate: ReferenceCandidate) -> ResolutionResult:
        number = candidate.normalized_document_number
        if not number:
            return ResolutionResult(
                candidate.candidate_id, "UNPARSEABLE", None, "", "NO_IDENTIFIER",
                "No normalized document number was extracted", 0, "LOW"
            )
        matches = [
            document
            for document in self.by_number.get(number, [])
            if int(document["id"]) != candidate.source_document_id
        ]
        self_only = bool(self.by_number.get(number)) and not matches
        if len(matches) == 1:
            target = matches[0]
            target_date = str(target.get("gr_date") or "")
            date_agrees = not candidate.normalized_date or candidate.normalized_date == target_date
            department_agrees = candidate.source_department == target.get("department")
            if date_agrees:
                return ResolutionResult(
                    candidate.candidate_id,
                    "RESOLVED_EXACT",
                    int(target["id"]),
                    str(target["file_name"]),
                    "EXACT_NORMALIZED_NUMBER" + ("_AND_DATE" if candidate.normalized_date else ""),
                    f"unique normalized identifier; date_agrees={date_agrees}; department_agrees={department_agrees}",
                    1,
                    "HIGH",
                )
            return ResolutionResult(
                candidate.candidate_id,
                "AMBIGUOUS",
                None,
                "",
                "NUMBER_MATCH_DATE_CONFLICT",
                f"one identifier match but reference date {candidate.normalized_date} conflicts with stored date {target_date}",
                1,
                "LOW",
            )
        if len(matches) > 1:
            narrowed = [
                document
                for document in matches
                if (not candidate.normalized_date or str(document.get("gr_date") or "") == candidate.normalized_date)
                and document.get("department") == candidate.source_department
            ]
            if len(narrowed) == 1:
                target = narrowed[0]
                return ResolutionResult(
                    candidate.candidate_id,
                    "RESOLVED_STRONG",
                    int(target["id"]),
                    str(target["file_name"]),
                    "NUMBER_DATE_DEPARTMENT",
                    "multiple number matches narrowed uniquely by date and source department",
                    len(matches),
                    "HIGH",
                )
            return ResolutionResult(
                candidate.candidate_id,
                "AMBIGUOUS",
                None,
                "",
                "MULTIPLE_NUMBER_MATCHES",
                "normalized identifier maps to multiple stored documents",
                len(matches),
                "LOW",
            )
        return ResolutionResult(
            candidate.candidate_id,
            "NOT_IN_CORPUS",
            None,
            "",
            "SELF_IDENTIFIER_ONLY" if self_only else "NO_NORMALIZED_NUMBER_MATCH",
            "identifier only matches the source document and is not a target edge" if self_only else "no stored document has this normalized identifier",
            0,
            "HIGH" if not self_only else "MEDIUM",
        )

    def resolve_dict(self, candidate: ReferenceCandidate) -> dict[str, Any]:
        return {**asdict(candidate), **asdict(self.resolve(candidate))}


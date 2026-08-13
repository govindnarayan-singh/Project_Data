from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

from knowledge_engine.extraction.text_quality import normalize_marathi_text
from knowledge_engine.graph.normalization import normalize_document_number
from knowledge_engine.metadata.extractor import extract_metadata

from .inventory import resolve_inside_root
from .models import AcquisitionMatch, LocalPdf, MetadataProbe, UnresolvedGroup


def probe_pdf(root: Path, item: LocalPdf, *, max_pages: int = 2) -> MetadataProbe:
    path = resolve_inside_root(root, root / item.relative_path)
    try:
        reader = PdfReader(str(path), strict=False)
        texts: list[str] = []
        for page in list(reader.pages)[:max_pages]:
            texts.append(normalize_marathi_text(page.extract_text() or ""))
        text = "\n".join(texts)
        metadata = extract_metadata(text, item.department, item.file_name)
        raw = str(metadata.get("gr_number") or "")
        return MetadataProbe(
            relative_path=item.relative_path, department=item.department, file_name=item.file_name,
            sha256=item.sha256, page_count=item.page_count, pages_probed=min(len(reader.pages), max_pages),
            probe_method="DIGITAL_FIRST_PAGES", raw_identifier=raw,
            normalized_identifier=normalize_document_number(raw),
            document_date=metadata.get("gr_date") if metadata.get("gr_date_source") == "text" else None,
            document_type=str(metadata.get("document_type") or ""), subject=str(metadata.get("subject") or ""),
            text_characters=len(text), probe_error="" if text else "NO_DIGITAL_TEXT",
        )
    except Exception as exc:
        return MetadataProbe(
            item.relative_path, item.department, item.file_name, item.sha256, item.page_count, 0,
            "DIGITAL_FIRST_PAGES", "", "", item.filename_date, "", "", 0,
            f"{type(exc).__name__}:{exc}",
        )


def _priority(group: UnresolvedGroup, status: str) -> int:
    match = {"MATCH_EXACT": 400, "MATCH_STRONG": 250, "AMBIGUOUS": 50}.get(status, 0)
    signal = 120 if group.relation_signals else 0
    identifier = 80 if len(group.normalized_identifier) >= 8 else 20
    date = 30 if group.reference_dates else 0
    department = 20 if group.source_departments else 0
    quality = min(group.high_quality_mentions, 10) * 3
    return len(group.source_documents) * 100 + min(group.mentions, 25) * 10 + signal + identifier + date + department + quality + match


def resolve_groups(groups: list[UnresolvedGroup], probes: list[MetadataProbe]) -> list[AcquisitionMatch]:
    by_identifier: dict[str, list[MetadataProbe]] = defaultdict(list)
    for probe in probes:
        if probe.normalized_identifier:
            by_identifier[probe.normalized_identifier].append(probe)
    results: list[AcquisitionMatch] = []
    for group in groups:
        candidates = list(by_identifier.get(group.normalized_identifier, []))
        if not candidates:
            status = "BLOCKED_QUALITY" if group.blocked_mentions == group.mentions else "NOT_FOUND"
            results.append(AcquisitionMatch(group, status, priority_score=_priority(group, status),
                                            matching_evidence="No exact normalized identifier was extracted from a limited local-PDF probe"))
            continue
        # Duplicate local bytes represent one acquisition identity, not ambiguity.
        unique_by_sha = {candidate.sha256: candidate for candidate in candidates if candidate.sha256}
        candidates = sorted(unique_by_sha.values(), key=lambda item: item.relative_path.casefold())
        if len(candidates) == 1:
            candidate = candidates[0]
            date_conflict = bool(group.reference_dates and candidate.document_date and candidate.document_date not in group.reference_dates)
            department_conflict = bool(group.source_departments and candidate.department not in group.source_departments)
            if date_conflict:
                status, method, confidence = "AMBIGUOUS", "EXACT_NUMBER_DATE_CONFLICT", "LOW"
            elif candidate.probe_error:
                status, method, confidence = "BLOCKED_QUALITY", "EXACT_NUMBER_PROBE_QUALITY_BLOCK", "LOW"
            elif candidate.document_date in group.reference_dates or candidate.department in group.source_departments:
                status, method, confidence = "MATCH_EXACT", "EXACT_NUMBER_WITH_DATE_OR_DEPARTMENT", "HIGH"
            else:
                status, method, confidence = "MATCH_STRONG", "UNIQUE_EXACT_NORMALIZED_NUMBER", "HIGH"
            evidence = (
                f"normalized_number={group.normalized_identifier}; candidate_date={candidate.document_date or ''}; "
                f"reference_dates={','.join(str(value) for value in group.reference_dates)}; "
                f"candidate_department={candidate.department}; department_conflict={department_conflict}"
            )
        else:
            corroborated = [candidate for candidate in candidates if (
                (not group.reference_dates or candidate.document_date in group.reference_dates)
                and (not group.source_departments or candidate.department in group.source_departments)
            )]
            if len(corroborated) == 1:
                candidates = corroborated
                status, method, confidence = "MATCH_EXACT", "NUMBER_NARROWED_BY_DATE_AND_DEPARTMENT", "HIGH"
            else:
                status, method, confidence = "AMBIGUOUS", "MULTIPLE_LOCAL_NUMBER_MATCHES", "LOW"
            evidence = f"{len(unique_by_sha)} distinct local PDF identities share the normalized identifier"
        results.append(AcquisitionMatch(
            group, status, tuple(candidates), method, evidence, confidence, _priority(group, status)
        ))
    return sorted(results, key=lambda item: (-item.priority_score, item.group.group_id))

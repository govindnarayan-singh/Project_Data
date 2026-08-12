from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from knowledge_engine.extraction.text_quality import digital_text_classification
from knowledge_engine.graph.diagnostics.extractor import (
    REFERENCE_HEADING_RE,
    extract_reference_candidates,
)
from knowledge_engine.graph.diagnostics.resolution import DiagnosticResolver
from knowledge_engine.settings import settings


FLAGGED_CLASSES = {
    "EMPTY_OR_IMAGE_ONLY",
    "CORRUPTED_MARATHI",
    "SUSPICIOUS_GARBLED_LATIN",
    "UNCERTAIN",
}
EXPLICIT_RELATION_SIGNALS = (
    ("अधिक्रमित", "MARATHI", "SUPERSEDES", "HIGH_CONFIDENCE_EXPLICIT"),
    ("supersed", "ENGLISH", "SUPERSEDES", "HIGH_CONFIDENCE_EXPLICIT"),
    ("शुद्धीपत्रक", "MARATHI", "CORRECTS", "CONTEXT_DEPENDENT"),
    ("corrigendum", "ENGLISH", "CORRECTS", "CONTEXT_DEPENDENT"),
    ("मुदतवाढ", "MARATHI", "EXTENDS", "CONTEXT_DEPENDENT"),
    ("मुदत वाढ", "MARATHI", "EXTENDS", "CONTEXT_DEPENDENT"),
    ("सुधारणा", "MARATHI", "AMENDS", "CONTEXT_DEPENDENT"),
    ("दुरुस्ती", "MARATHI", "AMENDS", "CONTEXT_DEPENDENT"),
    ("amend", "ENGLISH", "AMENDS", "CONTEXT_DEPENDENT"),
    ("रद्द", "MARATHI", "CANCELS", "AMBIGUOUS"),
    ("निरस्त", "MARATHI", "CANCELS", "CONTEXT_DEPENDENT"),
    ("cancel", "ENGLISH", "CANCELS", "AMBIGUOUS"),
    ("संदर्भाधीन", "MARATHI", "REFERENCES", "HIGH_CONFIDENCE_EXPLICIT"),
    ("संदर्भीय", "MARATHI", "REFERENCES", "HIGH_CONFIDENCE_EXPLICIT"),
    ("अन्वये", "MARATHI", "REFERENCES", "CONTEXT_DEPENDENT"),
    ("reference", "ENGLISH", "REFERENCES", "CONTEXT_DEPENDENT"),
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _database_fingerprint(connection) -> dict[str, Any]:
    row = connection.execute(
        """SELECT (SELECT count(*) FROM documents) documents,
                  (SELECT count(*) FROM pages) pages,
                  (SELECT count(*) FROM chunks) chunks,
                  (SELECT count(*) FROM gr_relations) gr_relations"""
    ).fetchone()
    identities = connection.execute(
        "SELECT id,file_path,sha256,coalesce(gr_number,''),coalesce(gr_date::text,'') FROM documents ORDER BY id"
    ).fetchall()
    digest = hashlib.sha256()
    for identity in identities:
        digest.update("|".join(str(value) for value in identity.values()).encode("utf-8") + b"\n")
    return {**{key: int(value) for key, value in row.items()}, "document_identity_sha256": digest.hexdigest()}


def _gr_relations_schema(connection) -> dict[str, Any]:
    columns = [dict(row) for row in connection.execute(
        """SELECT column_name,data_type,is_nullable,column_default
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='gr_relations'
            ORDER BY ordinal_position"""
    )]
    constraints = [dict(row) for row in connection.execute(
        """SELECT conname AS name,
                  CASE contype WHEN 'p' THEN 'PRIMARY_KEY' WHEN 'f' THEN 'FOREIGN_KEY'
                               WHEN 'u' THEN 'UNIQUE' WHEN 'c' THEN 'CHECK' ELSE contype::text END AS type,
                  pg_get_constraintdef(oid) AS definition
             FROM pg_constraint
            WHERE conrelid='public.gr_relations'::regclass
            ORDER BY conname"""
    )]
    indexes = [dict(row) for row in connection.execute(
        """SELECT indexname AS name,indexdef AS definition
             FROM pg_indexes
            WHERE schemaname='public' AND tablename='gr_relations'
            ORDER BY indexname"""
    )]
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def _metadata_impact(issue: dict[str, str]) -> tuple[str, str]:
    field = issue["field"]
    status = issue["validation_status"]
    if field == "gr_number":
        return "HIGH", "Exact identifier-based incoming-reference resolution may fail or misresolve; preserve raw value and require verified canonical number before authoritative graph resolution."
    if field == "gr_date":
        return "MEDIUM", "Date cannot safely disambiguate identifier matches until verified from the source; use it only as corroborating evidence."
    if field == "subject":
        return "LOW", "Subject affects display and review context but is not a deterministic identity key; preserve raw subject and avoid semantic identity matching."
    return "NONE", "No direct effect on deterministic reference resolution."


def _page_review(page: dict[str, Any], document: dict[str, Any], reference_count: int) -> tuple[str, str, str, str]:
    text = " ".join((page["text"] or "").split())
    classification = digital_text_classification(page["text"] or "")
    meaningful = len(re.findall(r"[A-Za-z\u0900-\u097f]", text)) >= 40
    if classification == "EMPTY_OR_IMAGE_ONLY" and len(text) < 20:
        return "NO", "NO", "NONE", "IGNORABLE"
    if reference_count:
        return "YES", "YES", "HIGH", "IMPORTANT_BAD"
    if meaningful and any(marker in text.casefold() for marker in ("शासन", "government", "अधिनियम", "परिपत्रक", "आदेश", "नियम")):
        return "YES", "NO", "MEDIUM", "NON_CRITICAL_BAD"
    if meaningful:
        return "YES", "NO", "LOW", "NON_CRITICAL_BAD"
    return "NO", "NO", "NONE", "IGNORABLE"


def _pattern_catalogue(candidates) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.extraction_pattern_id].append(candidate)
    rows = []
    for pattern_id, values in sorted(grouped.items()):
        example = min(values, key=lambda value: (len(value.raw_reference_text), value.source_file, value.source_page))
        fields = []
        if example.reference_type != "OTHER_DOCUMENT":
            fields.append("document_type")
        if any(value.extracted_document_number for value in values):
            fields.append("document_number")
        if any(value.extracted_date for value in values):
            fields.append("date")
        if any(value.extracted_department for value in values):
            fields.append("department")
        completeness = sum(value.parser_status == "COMPLETE" for value in values) / len(values)
        difficulty = "EASY" if completeness >= 0.75 else ("MEDIUM" if completeness >= 0.25 else "HARD")
        rows.append(
            {
                "pattern_id": pattern_id,
                "reference_type": example.reference_type,
                "language": example.language,
                "structural_context": example.structural_context,
                "example_text": example.raw_reference_text[:500],
                "source_pdf": example.source_file,
                "source_page": example.source_page,
                "fields_apparently_available": ";".join(fields),
                "frequency": len(values),
                "document_frequency": len({value.source_document_id for value in values}),
                "parsing_difficulty": difficulty,
            }
        )
    return rows


def _relation_signals(pages: list[dict[str, Any]], documents: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], str, str, str]]] = defaultdict(list)
    for page in pages:
        for raw_line in (page["text"] or "").splitlines():
            line = " ".join(raw_line.split())
            if not line:
                continue
            folded = line.casefold()
            for phrase, language, relation_type, ambiguity in EXPLICIT_RELATION_SIGNALS:
                if phrase.casefold() in folded:
                    grouped[(phrase, relation_type)].append((page, line, language, ambiguity))
    rows = []
    for (phrase, relation_type), values in sorted(grouped.items()):
        page, line, language, ambiguity = min(values, key=lambda value: (len(value[1]), value[0]["document_id"], value[0]["page_number"]))
        rows.append(
            {
                "phrase": phrase,
                "language": language,
                "candidate_relation_type": relation_type,
                "example_source_document": documents[int(page["document_id"])]["file_name"],
                "example_page": page["page_number"],
                "short_evidence": line[:500],
                "frequency": len(values),
                "document_frequency": len({int(value[0]["document_id"]) for value in values}),
                "ambiguity_level": ambiguity,
            }
        )
    return rows


DETECTION_INCORRECT = {"REF-f6ad263f4334", "REF-bdd7ea6393b8"}
TYPE_INCORRECT = {"REF-686100148eda", "REF-a82a4023d106", "REF-9684f6408789"}
TYPE_UNCERTAIN = {"REF-e3d20d436337", "REF-46e80b48067a", "REF-384ab2cdecad"}
NUMBER_NOT_APPLICABLE = {
    "REF-b4a190c1eb49", "REF-a86a683dfdab", "REF-e11bbe040bf4", "REF-abb97de11365",
    "REF-b7da75d89a9b", "REF-d834190467b3", "REF-c97f9544dee7", "REF-87010b840d4a",
    "REF-bdd7ea6393b8", "REF-403b1b649688", "REF-b97ad9d43beb", "REF-384ab2cdecad",
}
NUMBER_INCORRECT = {
    "REF-e3d20d436337", "REF-a50346a72207", "REF-8d0cf824fd6f", "REF-686100148eda",
    "REF-46e80b48067a", "REF-f6ad263f4334", "REF-d64a2a21d08e", "REF-5ed8ebf8b15d",
    "REF-14fcc066028c", "REF-61f587e36da8", "REF-972762546776",
}
NUMBER_UNCERTAIN = {
    "REF-3d382e8756f9", "REF-0692208cb238", "REF-8b92d3800139", "REF-f2ad87f2be30",
    "REF-eedeb62886bf", "REF-62ad578b549e", "REF-c02622b6ba63", "REF-d312281e0b78",
}
DATE_NOT_APPLICABLE = {
    "REF-06a17e18d2d0", "REF-e3d20d436337", "REF-a50346a72207", "REF-884d5a11b48a",
    "REF-3d382e8756f9", "REF-dfdfe6c5166c", "REF-cb4214fc9769", "REF-d64a2a21d08e",
    "REF-bcfd5419fbe8", "REF-814f6a2c5a81", "REF-14fcc066028c", "REF-eedeb62886bf",
    "REF-62ad578b549e", "REF-61f587e36da8", "REF-71e2032400af", "REF-d312281e0b78",
    "REF-972762546776",
}
DATE_INCORRECT = {
    "REF-686100148eda", "REF-46e80b48067a", "REF-f6ad263f4334", "REF-5ed8ebf8b15d",
    "REF-38ffc0bdc588",
}
DATE_UNCERTAIN = {"REF-59f6c25cce47", "REF-0692208cb238", "REF-8b92d3800139", "REF-d834190467b3"}
REVIEW_NOTES = {
    "REF-e3d20d436337": "Act is 1986; parser captured the surrounding 2021 year as part of the identifier.",
    "REF-59f6c25cce47": "Identifier is clear, but the date is split/incomplete in the evidence window.",
    "REF-a50346a72207": "Parser captured the tail of the word notification rather than a document identifier.",
    "REF-8d0cf824fd6f": "Policy mention is genuine, but the identifier has an OCR/context prefix.",
    "REF-686100148eda": "Candidate conflates a Notification/web reference with a later GR/date on the page.",
    "REF-46e80b48067a": "Act mention is genuine, but the extracted identifier/date include a separate following GR reference.",
    "REF-f6ad263f4334": "This is the source document header, not an outgoing reference.",
    "REF-d64a2a21d08e": "Act title/year extraction is malformed by OCR and duplicates the year.",
    "REF-9684f6408789": "The evidence calls this a Government Resolution; RULE is a false type match from the identifier suffix.",
    "REF-bdd7ea6393b8": "This is a temporal project-eligibility clause, not a document reference.",
    "REF-5ed8ebf8b15d": "Parser retained only the parenthetical C.No fragment, not the full document number.",
    "REF-38ffc0bdc588": "English Dt. date was not extracted.",
    "REF-61f587e36da8": "Rule reference is genuine, but surrounding sentence text was captured as the identifier.",
    "REF-972762546776": "Duplicate rule mention with surrounding sentence text captured as the identifier.",
}


def _manual_sample(resolved_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    quotas = Counter()
    for row in sorted(
        resolved_rows,
        key=lambda row: (
            row["source_department"],
            row["resolution_status"],
            row["extraction_pattern_id"],
            row["source_file"],
            int(row["source_page"]),
        ),
    ):
        key = (row["source_department"], row["resolution_status"], row["reference_type"], row["language"])
        if quotas[key] >= 1:
            continue
        selected.append(row)
        quotas[key] += 1
        if len(selected) == 50:
            break
    if len(selected) < min(30, len(resolved_rows)):
        used = {row["candidate_id"] for row in selected}
        selected.extend(row for row in resolved_rows if row["candidate_id"] not in used)
        selected = selected[: min(50, len(resolved_rows))]

    sample = []
    for row in selected:
        candidate_id = row["candidate_id"]
        detection = "NO" if candidate_id in DETECTION_INCORRECT else "YES"
        if detection == "NO":
            type_status = "NOT_APPLICABLE"
        elif candidate_id in TYPE_INCORRECT:
            type_status = "NO"
        elif candidate_id in TYPE_UNCERTAIN:
            type_status = "UNCERTAIN"
        else:
            type_status = "YES"
        if candidate_id in NUMBER_NOT_APPLICABLE:
            number_status = "NOT_APPLICABLE"
        elif candidate_id in NUMBER_INCORRECT:
            number_status = "NO"
        elif candidate_id in NUMBER_UNCERTAIN:
            number_status = "UNCERTAIN"
        else:
            number_status = "YES"
        if candidate_id in DATE_NOT_APPLICABLE:
            date_status = "NOT_APPLICABLE"
        elif candidate_id in DATE_INCORRECT:
            date_status = "NO"
        elif candidate_id in DATE_UNCERTAIN:
            date_status = "UNCERTAIN"
        else:
            date_status = "YES"
        if detection == "NO" or type_status == "NO" or number_status == "NO" or date_status == "NO":
            review = "INCORRECT"
        elif "UNCERTAIN" in {type_status, number_status, date_status}:
            review = "UNCERTAIN"
        else:
            review = "CORRECT"
        expected = (
            f"Explicit {row['reference_type']} mention"
            + (f" with identifier {row['normalized_document_number']}" if row["normalized_document_number"] else "")
            + (f" dated {row['normalized_date']}" if row["normalized_date"] else "")
        )
        if row["resolution_status"].startswith("RESOLVED"):
            expected += f"; resolves to {row['target_file']} by deterministic identifier"
        elif row["resolution_status"] == "NOT_IN_CORPUS":
            expected += "; identifier is not represented in the 61-document corpus"
        if detection == "NO":
            expected = "Not an outgoing document reference"
        sample.append(
            {
                "candidate_id": candidate_id,
                "source_pdf": row["source_file"],
                "source_page": row["source_page"],
                "actual_reference_text": row["raw_reference_text"],
                "parser_reference_type": row["reference_type"],
                "parser_document_number": row["extracted_document_number"],
                "parser_normalized_number": row["normalized_document_number"],
                "parser_date": row["extracted_date"],
                "parser_normalized_date": row["normalized_date"],
                "resolution_status": row["resolution_status"],
                "target_file": row["target_file"],
                "expected_diagnostic_interpretation": expected,
                "detection_correct": detection,
                "type_correct": type_status,
                "document_number_correct": number_status,
                "date_correct": date_status,
                "resolution_correct": "YES" if row["resolution_status"].startswith("RESOLVED") and row["target_file"] else ("NOT_APPLICABLE" if not row["resolution_status"].startswith("RESOLVED") else "UNCERTAIN"),
                "review_result": review,
                "review_notes": REVIEW_NOTES.get(candidate_id, "Source text and parser fields reviewed directly; no independent human ground truth."),
                "review_scope": "CODEX_DIAGNOSTIC_REVIEW_NOT_INDEPENDENT_HUMAN_GROUND_TRUTH",
            }
        )
    return sample


def run(output_dir: Path, metadata_issues_path: Path, extraction_summary_path: Path) -> dict[str, Any]:
    import psycopg

    output_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        before = _database_fingerprint(connection)
        gr_relations_schema = _gr_relations_schema(connection)
        document_rows = [dict(row) for row in connection.execute(
            """SELECT id,file_name,file_path,department,document_type,gr_number,gr_date,subject,
                      extraction_method,digital_pages,ocr_pages,page_count
               FROM documents ORDER BY id"""
        )]
        pages = [dict(row) for row in connection.execute(
            """SELECT document_id,page_number,extraction_method,quality_score,text
               FROM pages ORDER BY document_id,page_number"""
        )]
    documents = {int(row["id"]): row for row in document_rows}
    pages_by_document: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        pages_by_document[int(page["document_id"])].append(page)

    candidates = []
    for document in document_rows:
        candidates.extend(extract_reference_candidates(document, pages_by_document[int(document["id"])]))
    resolver = DiagnosticResolver(document_rows)
    resolved_rows = [resolver.resolve_dict(candidate) for candidate in candidates]

    metadata_rows = []
    for issue in _read_csv(metadata_issues_path):
        impact, handling = _metadata_impact(issue)
        metadata_rows.append({**issue, "kg_impact": impact, "recommended_handling": handling})
    _write_csv(output_dir / "metadata_kg_impact.csv", list(metadata_rows[0]), metadata_rows)

    flagged_documents = {
        row["file_name"]: int(row["final_bad_or_uncertain_pages"])
        for row in _read_csv(extraction_summary_path)
        if int(row["final_bad_or_uncertain_pages"])
    }
    expected_flagged_pages = sum(flagged_documents.values())
    candidate_counts_by_page = Counter((candidate.source_document_id, candidate.source_page) for candidate in candidates)
    page_quality_rows = []
    for page in pages:
        classification = digital_text_classification(page["text"] or "")
        document = documents[int(page["document_id"])]
        if document["file_name"] not in flagged_documents or classification not in FLAGGED_CLASSES:
            continue
        ref_count = candidate_counts_by_page[(int(page["document_id"]), int(page["page_number"]))]
        meaningful, contains_references, impact, review = _page_review(page, document, ref_count)
        page_quality_rows.append(
            {
                "file": document["file_name"],
                "department": document["department"],
                "page": page["page_number"],
                "current_classification": classification,
                "final_source": page["extraction_method"].upper(),
                "short_text_preview": " ".join((page["text"] or "").split())[:300],
                "meaningful_government_content": meaningful,
                "contains_potential_document_references": contains_references,
                "kg_impact": impact,
                "review_classification": review,
                "notes": f"deterministic candidate references on page={ref_count}; quality_score={page['quality_score']}",
            }
        )
    if len(page_quality_rows) != expected_flagged_pages:
        raise RuntimeError(
            f"Expected {expected_flagged_pages} expansion flagged pages from extraction_summary.csv, "
            f"but found {len(page_quality_rows)} in stored page text"
        )
    _write_csv(output_dir / "page_quality_kg_impact.csv", list(page_quality_rows[0]), page_quality_rows)

    candidate_rows = [asdict(candidate) for candidate in candidates]
    _write_csv(output_dir / "reference_candidates.csv", list(candidate_rows[0]), candidate_rows)
    _write_csv(output_dir / "reference_resolution.csv", list(resolved_rows[0]), resolved_rows)
    unresolved = [row for row in resolved_rows if row["resolution_status"] in {"AMBIGUOUS", "NOT_IN_CORPUS", "UNPARSEABLE"}]
    unresolved_rows = [
        {
            "source_file": row["source_file"],
            "source_page": row["source_page"],
            "reference_type": row["reference_type"],
            "raw_reference": row["raw_reference_text"],
            "normalized_number": row["normalized_document_number"],
            "normalized_date": row["normalized_date"],
            "department": row["extracted_department"],
            "resolution_status": row["resolution_status"],
        }
        for row in unresolved
    ]
    _write_csv(output_dir / "unresolved_references.csv", list(unresolved_rows[0]), unresolved_rows)

    priority_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in resolved_rows:
        if row["resolution_status"] != "NOT_IN_CORPUS" or not row["normalized_document_number"]:
            continue
        priority_groups[(row["normalized_document_number"], row["reference_type"], row["normalized_date"])].append(row)
    priorities = []
    for (number, reference_type, reference_date), values in sorted(priority_groups.items()):
        source_files = sorted({value["source_file"] for value in values})
        department_counts = Counter(value["source_department"] for value in values)
        year_match = re.search(r"(?:19|20)\d{2}", number + reference_date)
        priorities.append(
            {
                "normalized_reference_number": number,
                "reference_type": reference_type,
                "reference_date": reference_date,
                "department": department_counts.most_common(1)[0][0],
                "year_if_known": year_match.group(0) if year_match else "",
                "number_of_source_documents_referring_to_it": len(source_files),
                "number_of_total_mentions": len(values),
                "example_source_files": ";".join(source_files[:5]),
                "priority_reason": f"explicit deterministic identifier cited by {len(source_files)} source document(s) in {len(values)} mention(s)",
            }
        )
    priorities.sort(key=lambda row: (-row["number_of_source_documents_referring_to_it"], -row["number_of_total_mentions"], row["normalized_reference_number"]))
    _write_csv(output_dir / "unresolved_reference_priority.csv", list(priorities[0]), priorities)

    patterns = _pattern_catalogue(candidates)
    _write_csv(output_dir / "reference_pattern_catalogue.csv", list(patterns[0]), patterns)
    signals = _relation_signals(pages, documents)
    _write_csv(output_dir / "relation_signal_catalogue.csv", list(signals[0]), signals)
    manual_sample = _manual_sample(resolved_rows)
    _write_csv(output_dir / "manual_validation_sample.csv", list(manual_sample[0]), manual_sample)

    explicit_documents = {candidate.source_document_id for candidate in candidates if candidate.structural_context == "REFERENCE_SECTION"}
    all_inline_documents = {candidate.source_document_id for candidate in candidates if candidate.structural_context == "INLINE"}
    inline_documents = all_inline_documents - explicit_documents
    no_reference_documents = set(documents) - explicit_documents - all_inline_documents
    reference_counts = Counter(candidate.source_document_id for candidate in candidates)
    resolution_counts = Counter(row["resolution_status"] for row in resolved_rows)
    review_counts = Counter(row["review_classification"] for row in page_quality_rows)
    metadata_impact_counts = Counter(row["kg_impact"] for row in metadata_rows)
    sample_detection = [row for row in manual_sample if row["detection_correct"] in {"YES", "NO"}]
    sample_numbers = [row for row in manual_sample if row["document_number_correct"] in {"YES", "NO"}]
    sample_dates = [row for row in manual_sample if row["date_correct"] in {"YES", "NO"}]
    sample_resolutions = [row for row in manual_sample if row["resolution_correct"] in {"YES", "NO"}]

    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        after = _database_fingerprint(connection)
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database_before": before,
        "database_after": after,
        "database_unchanged": before == after,
        "quality_review": {
            "metadata_diagnostic_count": len(metadata_rows),
            "metadata_kg_impact": dict(metadata_impact_counts),
            "flagged_page_count": len(page_quality_rows),
            "flagged_page_review": dict(review_counts),
        },
        "reference_discovery": {
            "documents_scanned": len(documents),
            "pages_scanned": len(pages),
            "documents_with_explicit_reference_sections": len(explicit_documents),
            "documents_with_inline_references": len(all_inline_documents),
            "documents_with_inline_references_but_no_explicit_section": len(inline_documents),
            "documents_with_no_detectable_references": len(no_reference_documents),
            "no_detectable_reference_files": [documents[document_id]["file_name"] for document_id in sorted(no_reference_documents)],
            "total_candidate_references": len(candidates),
            "references_per_document": {
                "minimum": min(reference_counts.values(), default=0),
                "maximum": max(reference_counts.values(), default=0),
                "mean": round(statistics.fmean(reference_counts.get(document_id, 0) for document_id in documents), 3),
                "median": statistics.median(reference_counts.get(document_id, 0) for document_id in documents),
            },
            "departments_by_candidate_mentions": dict(Counter(candidate.source_department for candidate in candidates).most_common()),
            "unique_pattern_families": len(patterns),
            "observed_reference_types": dict(Counter(candidate.reference_type for candidate in candidates).most_common()),
        },
        "reference_resolution": {
            **{status: resolution_counts[status] for status in ("RESOLVED_EXACT", "RESOLVED_STRONG", "AMBIGUOUS", "NOT_IN_CORPUS", "UNPARSEABLE")},
            "unique_unresolved_identifiers": len(priorities),
        },
        "relation_signals": {
            "phrase_families": dict(Counter(row["candidate_relation_type"] for row in signals)),
            "mentions": dict(sum((Counter({row["candidate_relation_type"]: int(row["frequency"])}) for row in signals), Counter())),
        },
        "diagnostic_validation": {
            "sample_size": len(manual_sample),
            "reference_detection_precision": round(sum(row["detection_correct"] == "YES" for row in sample_detection) / len(sample_detection), 4) if sample_detection else None,
            "reference_detection_denominator": len(sample_detection),
            "reference_type_accuracy": round(sum(row["type_correct"] == "YES" for row in manual_sample if row["type_correct"] in {"YES", "NO"}) / len([row for row in manual_sample if row["type_correct"] in {"YES", "NO"}]), 4) if any(row["type_correct"] in {"YES", "NO"} for row in manual_sample) else None,
            "reference_type_denominator": len([row for row in manual_sample if row["type_correct"] in {"YES", "NO"}]),
            "document_number_extraction_accuracy": round(sum(row["document_number_correct"] == "YES" for row in sample_numbers) / len(sample_numbers), 4) if sample_numbers else None,
            "document_number_denominator": len(sample_numbers),
            "date_extraction_accuracy": round(sum(row["date_correct"] == "YES" for row in sample_dates) / len(sample_dates), 4) if sample_dates else None,
            "date_denominator": len(sample_dates),
            "resolution_precision": round(sum(row["resolution_correct"] == "YES" for row in sample_resolutions) / len(sample_resolutions), 4) if sample_resolutions else None,
            "resolution_denominator": len(sample_resolutions),
            "recall": "NOT_YET_MEASURED",
            "scope": "Codex diagnostic source-text review; not independent human ground truth",
        },
        "gr_relations_assessment": {
            "recommendation": "REPLACE_WITH_MIGRATION",
            "reason": "The placeholder lacks raw/normalized identifier separation, reference date, extraction and resolution methods, validation state, explicit evidence provenance fields, unresolved-candidate lifecycle, and timestamps; float confidence is not an auditable category model.",
        },
        "gr_relations_schema": gr_relations_schema,
    }
    (output_dir / "kg_diagnostic_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run read-only deterministic KG diagnostic")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("metadata_issues", type=Path)
    parser.add_argument("extraction_summary", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir.resolve(), args.metadata_issues.resolve(), args.extraction_summary.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

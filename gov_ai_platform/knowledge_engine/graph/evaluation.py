from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .normalization import normalize_document_number, normalize_reference_date
from .parser import ReferenceParser
from .types import ParserStatus, ReferenceKind


def evaluate_parser(path: Path, parser: ReferenceParser | None = None) -> dict[str, Any]:
    parser = parser or ReferenceParser()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    predicted_positive = true_positive = labelled_positive = 0
    kind_correct = kind_total = number_correct = number_total = date_correct = date_total = 0
    details = []
    for index, row in enumerate(rows, start=1):
        document = {
            "id": 100000 + index, "file_name": row["source_file"], "department": row.get("department", "Regression"),
            "gr_number": row.get("source_gr_number", "SOURCE/REGRESSION/1"), "gr_date": row.get("source_gr_date", ""),
        }
        candidates = [
            candidate for candidate in parser.parse_document(document, [{"page_number": int(row["page"]), "text": row["raw_text"]}])
            if candidate.parser_status != ParserStatus.SELF_REFERENCE_REJECTED
        ]
        expected_reference = row["is_reference"].upper() == "YES"
        labelled_positive += int(expected_reference)
        predicted_positive += len(candidates)
        matched = None
        if expected_reference and candidates:
            expected_kind = row["expected_reference_kind"]
            expected_number = row["expected_document_number"]
            expected_date = normalize_reference_date(row["expected_date"]).value if row["expected_date"] else None
            matched = max(candidates, key=lambda candidate: (
                candidate.reference_kind.value == expected_kind,
                bool(expected_number and candidate.normalized_document_number),
                candidate.normalized_reference_date == expected_date,
            ))
            true_positive += 1
            if expected_kind:
                kind_total += 1
                kind_correct += int(matched.reference_kind.value == expected_kind)
            if expected_number:
                number_total += 1
                expected_normalized = expected_number if row.get("expected_number_is_canonical", "NO") == "YES" else normalize_document_number(expected_number)
                number_correct += int(matched.normalized_document_number == expected_normalized)
            if row["expected_date"]:
                date_total += 1
                date_correct += int(matched.normalized_reference_date == expected_date)
        details.append({
            "source_file": row["source_file"], "page": row["page"], "expected_reference": expected_reference,
            "predicted_count": len(candidates), "matched_kind": matched.reference_kind.value if matched else "",
            "matched_number": matched.normalized_document_number if matched else "",
            "matched_date": matched.normalized_reference_date.isoformat() if matched and matched.normalized_reference_date else "",
        })
    false_positives = predicted_positive - true_positive
    return {
        "sample_size": len(rows),
        "label_source": "CODEX_SOURCE_REVIEW",
        "reference_detection_precision": true_positive / predicted_positive if predicted_positive else 0.0,
        "reference_detection_recall": true_positive / labelled_positive if labelled_positive else 0.0,
        "false_positive_rate_per_example": false_positives / len(rows) if rows else 0.0,
        "reference_kind_accuracy": kind_correct / kind_total if kind_total else None,
        "reference_kind_denominator": kind_total,
        "document_number_extraction_accuracy": number_correct / number_total if number_total else None,
        "document_number_denominator": number_total,
        "date_extraction_accuracy": date_correct / date_total if date_total else None,
        "date_denominator": date_total,
        "quality_gate": {
            "detection_precision_pass": predicted_positive > 0 and true_positive / predicted_positive >= 0.95,
            "document_number_pass": number_total > 0 and number_correct / number_total >= 0.85,
            "date_pass": date_total > 0 and date_correct / date_total >= 0.90,
        },
        "details": details,
    }

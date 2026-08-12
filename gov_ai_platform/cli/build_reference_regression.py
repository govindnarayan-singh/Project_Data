from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from knowledge_engine.graph.normalization import normalize_legal_reference
from knowledge_engine.graph.parser import detect_reference_kind


KIND_MAP = {"OTHER_DOCUMENT": "UNKNOWN", "SCHEME_DOCUMENT": "SCHEME_POLICY_DOCUMENT"}
FIELDS = [
    "source_file", "page", "department", "raw_text", "is_reference",
    "expected_reference_kind", "expected_document_number", "expected_number_is_canonical",
    "expected_date", "source_gr_number", "source_gr_date", "notes", "label_source",
]


def _kind(value: str) -> str:
    return KIND_MAP.get(value, value)


def build(diagnostic_dir: Path, output_path: Path) -> list[dict[str, str]]:
    with (diagnostic_dir / "reference_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        candidates = list(csv.DictReader(handle))
    by_id = {row["candidate_id"]: row for row in candidates}
    with (diagnostic_dir / "manual_validation_sample.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        sample = list(csv.DictReader(handle))

    rows: list[dict[str, str]] = []
    used: set[str] = set()
    for reviewed in sample:
        candidate = by_id[reviewed["candidate_id"]]
        used.add(reviewed["candidate_id"])
        is_reference = reviewed["detection_correct"] == "YES"
        expected_kind = ""
        if is_reference:
            expected_kind = _kind(reviewed["parser_reference_type"]) if reviewed["type_correct"] == "YES" else detect_reference_kind(reviewed["actual_reference_text"]).value
        expected_number = reviewed["parser_document_number"] if reviewed["document_number_correct"] == "YES" else ""
        canonical = "NO"
        if expected_kind in {"ACT", "RULE"} and is_reference:
            expected_number = normalize_legal_reference(reviewed["actual_reference_text"], expected_kind)
            canonical = "YES"
        expected_date = reviewed["parser_date"] if reviewed["date_correct"] == "YES" else ""
        rows.append({
            "source_file": reviewed["source_pdf"], "page": reviewed["source_page"],
            "department": candidate["source_department"], "raw_text": reviewed["actual_reference_text"],
            "is_reference": "YES" if is_reference else "NO", "expected_reference_kind": expected_kind,
            "expected_document_number": expected_number, "expected_number_is_canonical": canonical,
            "expected_date": expected_date, "source_gr_number": "SOURCE/REGRESSION/1", "source_gr_date": "",
            "notes": reviewed["review_notes"], "label_source": "CODEX_SOURCE_REVIEW",
        })

    quotas: Counter[tuple[str, str]] = Counter()
    eligible = [
        row for row in candidates
        if row["candidate_id"] not in used and row["parser_status"] == "COMPLETE"
        and row["reference_type"] != "OTHER_DOCUMENT" and row["extracted_document_number"]
        and len(row["raw_reference_text"]) <= 500
    ]
    eligible.sort(key=lambda row: (row["source_department"], row["reference_type"], row["extraction_pattern_id"], row["source_file"], int(row["source_page"])))
    while len(rows) < 100:
        added = False
        for candidate in eligible:
            if candidate["candidate_id"] in used:
                continue
            key = (candidate["source_department"], candidate["reference_type"])
            minimum_quota = min(quotas.values(), default=0)
            if quotas[key] > minimum_quota + 1:
                continue
            expected_kind = _kind(candidate["reference_type"])
            expected_number = candidate["extracted_document_number"]
            canonical = "NO"
            if expected_kind in {"ACT", "RULE"}:
                expected_number = normalize_legal_reference(candidate["raw_reference_text"], expected_kind)
                canonical = "YES"
            rows.append({
                "source_file": candidate["source_file"], "page": candidate["source_page"],
                "department": candidate["source_department"], "raw_text": candidate["raw_reference_text"],
                "is_reference": "YES", "expected_reference_kind": expected_kind,
                "expected_document_number": expected_number, "expected_number_is_canonical": canonical,
                "expected_date": candidate["extracted_date"], "source_gr_number": "SOURCE/REGRESSION/1", "source_gr_date": "",
                "notes": "Explicit source-text reference reviewed for deterministic parser regression; not independent human ground truth.",
                "label_source": "CODEX_SOURCE_REVIEW",
            })
            used.add(candidate["candidate_id"])
            quotas[key] += 1
            added = True
            if len(rows) == 100:
                break
        if not added:
            raise ValueError(f"Only {len(rows)} diverse regression rows could be assembled")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


if __name__ == "__main__":
    build(Path("outputs/knowledge_graph_diagnostic"), Path("data/knowledge_graph/reference_parser_regression.csv"))

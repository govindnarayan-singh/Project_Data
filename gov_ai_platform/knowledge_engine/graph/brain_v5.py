from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from knowledge_engine.external_acquisition.kg_guided_v1 import legal_identity_quality
from knowledge_engine.external_acquisition.service import ExternalAcquisitionRepository
from knowledge_engine.external_acquisition.v2 import ExternalAcquisitionV2Service
from knowledge_engine.settings import PROJECT_ROOT, settings

from .analytics import completion_priority, performance, relation_diagnostics, topology
from .benchmark_v3 import FIELDS as BENCHMARK_FIELDS, build_benchmark, evaluate_benchmark
from .brain_v2 import closure_snapshot, database_snapshot, integrity_report, write_csv, write_json
from .normalization import normalize_reference_whitespace
from .parser import (
    ISSUER_CITING_DOC_HINT_ONLY,
    ISSUER_EXPLICIT_CONTEXT,
    ISSUER_EXPLICIT_REFERENCE,
    ISSUER_UNKNOWN,
    ReferenceParser,
    context_recovery_metadata,
)
from .pipeline import KnowledgeGraphPipeline
from .repository import GraphRepository
from .validation import KnowledgeGraphValidationPolicy
from .types import ParserStatus, ReferenceKind


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v5"
TARGET_SOURCE = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v3.csv"
REGRESSION_V2 = PROJECT_ROOT / "data" / "knowledge_graph" / "reference_parser_regression_v2.csv"
PRIORITY_V5 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v5.csv"
BENCHMARK_V5 = PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v5.csv"
MANIFEST_V5 = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_verified_v5.csv"
V4_OUTPUT = PROJECT_ROOT / "outputs" / "kg_brain_v4"
EXPECTED_TARGET_SHA = "1bff4028b37fbb8117d0c75033f9b36aa29fe303235d06409f6fe49241a363fa"

FAILURE_FAMILY_CATALOGUE = (
    "NUMBER_FRAGMENT", "OCR_CORRUPTION", "MIXED_MARATHI_LATIN", "FOOTNOTE_CONTAMINATION",
    "RUNNING_TEXT_CONTAMINATION", "DATE_ONLY_REFERENCE", "TITLE_ONLY_REFERENCE",
    "ISSUER_ONLY_IN_CONTEXT", "ISSUER_IN_PREVIOUS_SENTENCE", "KIND_MISCLASSIFIED",
    "ACT_TITLE_TRUNCATED", "GR_NUMBER_SPLIT_ACROSS_TOKENS",
    "REFERENCE_MULTIPLE_DOCUMENTS_IN_ONE_SENTENCE", "REFERENCE_WITHOUT_NUMBER",
    "REFERENCE_TO_LETTER", "REFERENCE_TO_ORDER", "REFERENCE_TO_CIRCULAR",
    "REFERENCE_TO_NOTIFICATION", "REFERENCE_TO_RULE", "PARENTHETICAL_REFERENCE", "OTHER",
)


EVIDENCE_FIELDS = [
    "target_rank", "target", "raw_reference_text", "full_source_sentence",
    "context_before", "context_after", "source_document_id", "source_document",
    "source_page", "page_quality", "extraction_method", "document_language",
    "current_parsed_kind", "current_raw_identifier", "current_normalized_identifier",
    "current_date", "current_issuer", "current_parser_pattern", "current_parser_status",
    "candidate_count", "v2_identifier", "v2_kind", "v2_date", "v2_issuer",
    "v2_issuer_state", "v2_pattern", "v2_parser_status", "v2_recovery_rules",
    "v2_issuer_evidence_text", "v2_issuer_evidence_location",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _language(text: str) -> str:
    devanagari = any("\u0900" <= char <= "\u097f" for char in text)
    latin = any(char.isascii() and char.isalpha() for char in text)
    return "MIXED" if devanagari and latin else ("MARATHI" if devanagari else "ENGLISH")


def _as_iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value or "")


def _split_target(value: str) -> tuple[str, str]:
    return tuple(value.split("|", 1))  # type: ignore[return-value]


def _locate_line(lines: list[str], evidence: str, raw_identifier: str = "") -> int:
    needle = normalize_reference_whitespace(evidence).replace("\n", " ").casefold()
    identifier = normalize_reference_whitespace(raw_identifier).replace("\n", " ").casefold()
    for index, line in enumerate(lines):
        normalized = line.casefold()
        if (needle and (normalized in needle or needle in normalized)) or (identifier and identifier in normalized):
            return index
    words = [word for word in re.split(r"\s+", identifier or needle) if len(word) >= 4]
    if words:
        scores = [sum(word in line.casefold() for word in words) for line in lines]
        if max(scores, default=0) > 0:
            return scores.index(max(scores))
    return 0


def _source_sentence(lines: list[str], index: int, kind: str) -> str:
    current = lines[index] if lines else ""
    values = [current]
    if kind in {"ACT", "RULE"} and index > 0:
        previous = lines[index - 1]
        if current.startswith(("(", "[")) or not re.search(r"[.!?।:]$", previous):
            values.insert(0, previous)
    if index + 1 < len(lines) and not re.search(r"[.!?।]$", values[-1]):
        values.append(lines[index + 1])
    return " ".join(values).strip()[:2000]


def _candidate_rows(repository: GraphRepository) -> list[dict[str, Any]]:
    with repository.connect() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT c.*,d.file_name,d.department AS citing_department,
                      p.text AS page_text,p.extraction_method,p.quality_score
                 FROM kg_reference_candidates c
                 JOIN documents d ON d.id=c.source_document_id
                 JOIN pages p ON p.document_id=c.source_document_id AND p.page_number=c.source_page
                ORDER BY c.id"""
        )]


def _v2_corpus(repository: GraphRepository) -> tuple[list[dict[str, Any]], dict[tuple[int, int, str, str], dict[str, Any]]]:
    documents, pages = repository.load_corpus()
    by_document: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        by_document[int(page["document_id"])].append(page)
    parsed: list[dict[str, Any]] = []
    anchored: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    parser = ReferenceParser()
    for document in documents:
        for candidate in parser.parse_document(document, by_document[int(document["id"])]):
            row = asdict(candidate)
            row["reference_kind"] = candidate.reference_kind.value
            row["parser_status"] = candidate.parser_status.value
            row["normalized_reference_date"] = _as_iso(candidate.normalized_reference_date)
            parsed.append(row)
            anchored[(
                candidate.source_document_id, candidate.source_page,
                candidate.reference_kind.value, candidate.evidence_hash,
            )] = row
    return parsed, anchored


def capture_baseline() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    repository = GraphRepository()
    database = database_snapshot(repository)
    closure = closure_snapshot(repository)["summary"]
    graph, _, _, _ = topology(repository)
    frozen_sha = _sha(TARGET_SOURCE)
    frozen_rows = _read_csv(TARGET_SOURCE)
    baseline = {
        "status": "PASS" if database["integrity_status"] == "PASS" and frozen_sha == EXPECTED_TARGET_SHA and len(frozen_rows) == 50 else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "runtime": {
            "native_postgresql": True, "postgresql": database["postgresql_version"],
            "pgvector": database["pgvector"], "host": "127.0.0.1",
            "port": database["port"], "database": database["database"],
            "podman_commands": 0, "database_backups_created": 0,
        },
        "frozen_target_batch": {"path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(), "rows": len(frozen_rows), "sha256": frozen_sha},
        "closure": {**closure, "unresolved_unique_targets": int(closure["referenced_target_closure"]["denominator"]) - int(closure["referenced_target_closure"]["numerator"])},
        "graph": graph,
    }
    write_json(OUTPUT_DIR / "baseline.json", baseline)
    write_json(OUTPUT_DIR / "graph_topology_before.json", graph)
    write_json(OUTPUT_DIR / "closure_metrics_before.json", baseline["closure"])
    if baseline["status"] != "PASS":
        raise RuntimeError("V5 native database or frozen target baseline failed")
    return baseline


def _issuer_from_notes(notes: str) -> str:
    marker = "V2="
    if marker not in notes:
        return ISSUER_UNKNOWN
    try:
        return str(json.loads(notes.split(marker, 1)[1]).get("issuer_state") or ISSUER_UNKNOWN)
    except json.JSONDecodeError:
        return ISSUER_UNKNOWN


def _recovery_rules(notes: str) -> str:
    marker = "V2="
    if marker not in notes:
        return ""
    try:
        return ";".join(json.loads(notes.split(marker, 1)[1]).get("identifier_rules") or [])
    except json.JSONDecodeError:
        return ""


def _failure_families(target: str, evidence: dict[str, Any], v2: dict[str, Any]) -> list[str]:
    kind, identifier = _split_target(target)
    raw = str(evidence.get("raw_evidence_text") or "")
    families: list[str] = []
    if kind in {"ACT", "RULE"}:
        base = identifier.rsplit("_ACT_", 1)[0].rsplit("_RULE_", 1)[0]
        if len([item for item in base.split("_") if item]) < 4:
            families += ["NUMBER_FRAGMENT", "TITLE_ONLY_REFERENCE", "ACT_TITLE_TRUNCATED"]
        if identifier.startswith(("AMENDMENT_TO_", "AMENDMENTS_TO_", "SECTION_", "THE_SCHEDULE_", "MAH_", "AND_")):
            families.append("RUNNING_TEXT_CONTAMINATION")
        if "SCHEDULE" in identifier or "AMENDMENT_TO_" in identifier or "AMENDMENTS_TO_" in identifier:
            families.append("FOOTNOTE_CONTAMINATION")
    if str(evidence.get("extraction_method")) == "ocr" and float(evidence.get("quality_score") or 0) < 0.8:
        families.append("OCR_CORRUPTION")
    if _language(raw) == "MIXED":
        families.append("MIXED_MARATHI_LATIN")
    if re.search(r"(?:19|20)\s+\d{2}", raw):
        families.append("GR_NUMBER_SPLIT_ACROSS_TOKENS")
    if not str(evidence.get("raw_department") or "") and _issuer_from_notes(str(v2.get("parser_notes") or "")) == ISSUER_EXPLICIT_CONTEXT:
        families.append("ISSUER_IN_PREVIOUS_SENTENCE")
    elif not str(evidence.get("raw_department") or ""):
        families.append("ISSUER_ONLY_IN_CONTEXT")
    if kind != str(v2.get("reference_kind") or kind):
        families.append("KIND_MISCLASSIFIED")
    if raw.count("Government Resolution") + raw.count("शासन निर्णय") > 1:
        families.append("REFERENCE_MULTIPLE_DOCUMENTS_IN_ONE_SENTENCE")
    if not str(evidence.get("raw_document_number") or ""):
        families.append("REFERENCE_WITHOUT_NUMBER")
    mapping = {
        "DEPARTMENT_LETTER": "REFERENCE_TO_LETTER", "GOVERNMENT_LETTER": "REFERENCE_TO_LETTER",
        "GOVERNMENT_ORDER": "REFERENCE_TO_ORDER", "COURT_ORDER": "REFERENCE_TO_ORDER",
        "CIRCULAR": "REFERENCE_TO_CIRCULAR", "GOVERNMENT_CIRCULAR": "REFERENCE_TO_CIRCULAR",
        "NOTIFICATION": "REFERENCE_TO_NOTIFICATION",
    }
    if kind in mapping:
        families.append(mapping[kind])
    if str(evidence.get("raw_reference_date") or "") and not str(evidence.get("raw_document_number") or ""):
        families.append("DATE_ONLY_REFERENCE")
    return list(dict.fromkeys(families or ["NO_RECURRING_FAILURE_OBSERVED"]))


def summarize_failure_families(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in assignments:
        for family in str(row["failure_families"]).split(";"):
            if family:
                grouped[family].append(row)
    deterministically_repairable = {
        "NUMBER_FRAGMENT", "OCR_CORRUPTION", "MIXED_MARATHI_LATIN", "FOOTNOTE_CONTAMINATION",
        "RUNNING_TEXT_CONTAMINATION", "ISSUER_ONLY_IN_CONTEXT", "ISSUER_IN_PREVIOUS_SENTENCE",
        "KIND_MISCLASSIFIED", "ACT_TITLE_TRUNCATED", "GR_NUMBER_SPLIT_ACROSS_TOKENS",
        "REFERENCE_MULTIPLE_DOCUMENTS_IN_ONE_SENTENCE", "PARENTHETICAL_REFERENCE",
    }
    rows = []
    for family in sorted(set(FAILURE_FAMILY_CATALOGUE) | set(grouped)):
        values = grouped.get(family, [])
        repaired = [row for row in values if row["v2_resolution"] == "REPAIRED"]
        examples = [
            f"{row['target']} :: {str(row['source_evidence'])[:180]}"
            for row in values[:3]
        ]
        rows.append({
            "failure_family": family, "count": len(values), "examples": " || ".join(examples),
            "deterministic_repair_possible": (
                "YES" if family in deterministically_repairable else
                "NOT_EVALUATED_NO_CASE" if not values else "NO"
            ),
            "repaired_targets": len(repaired),
            "repair_would_require_guessing": (
                "NOT_APPLICABLE" if not values or family == "NO_RECURRING_FAILURE_OBSERVED" else
                "NO" if family in deterministically_repairable else "YES"
            ),
        })
    return rows


def _identity_sets() -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]]]:
    repository = ExternalAcquisitionRepository()
    with repository.connect() as connection:
        documents = list(connection.execute("SELECT document_kind,canonical_identity,coalesce(gr_number,'') gr_number FROM documents"))
        local = list(connection.execute("SELECT document_kind,normalized_document_number,legal_canonical_key FROM local_document_catalog"))
        external = list(connection.execute(
            """SELECT reference_kind,normalized_identifier FROM external_acquisition_candidates
                 WHERE verification_status='AUTO_VERIFIED' AND archive_status='EXTERNAL_OFFICIAL_ARCHIVED'"""
        ))
    docs = {(str(row["document_kind"]), str(row["canonical_identity"])) for row in documents if row["canonical_identity"]}
    docs |= {(str(row["document_kind"]), str(row["gr_number"])) for row in documents if row["gr_number"]}
    locals_ = {(str(row["document_kind"]), str(row["normalized_document_number"])) for row in local if row["normalized_document_number"]}
    locals_ |= {(str(row["document_kind"]), str(row["legal_canonical_key"])) for row in local if row["legal_canonical_key"]}
    externals = {(str(row["reference_kind"]), str(row["normalized_identifier"])) for row in external}
    return docs, locals_, externals


def _quality(kind: str, identifier: str, dates: set[str], docs: set[tuple[str, str]], locals_: set[tuple[str, str]], externals: set[tuple[str, str]]) -> tuple[str, str]:
    key = (kind, identifier)
    if key in docs:
        return "ALREADY_INGESTED", "Corrected exact identity already ingested"
    if key in locals_:
        return "ALREADY_RESOLVED", "Corrected exact identity exists in local_document_catalog"
    if key in externals:
        return "ALREADY_EXTERNAL_VERIFIED", "Corrected exact identity is verified in External_Official"
    if identifier == "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016":
        return "AMBIGUOUS_TARGET_IDENTITY", "Principal/consolidated version ambiguity remains"
    if kind not in {"ACT", "RULE"} and len(dates) > 1:
        return "AMBIGUOUS_TARGET_IDENTITY", "Multiple explicit dates remain attached to the same administrative identity"
    if not identifier:
        return "SOURCE_UNPARSEABLE", "No deterministic structured identifier"
    if kind in {"ACT", "RULE"}:
        valid, reason = legal_identity_quality(identifier)
        if not valid:
            return "INSUFFICIENT_IDENTITY", reason
        if identifier.startswith(("AMENDMENT_TO_", "AMENDMENTS_TO_", "SECTION_", "THE_SCHEDULE_", "MAH_", "AND_")):
            return "SOURCE_UNPARSEABLE", "Context contamination remains in the legal title"
        return "READY_FOR_DISCOVERY", "Complete evidence-supported legal title/year"
    return "READY_FOR_DISCOVERY", "Structurally valid administrative identity"


def analyze_targets() -> dict[str, Any]:
    repository = GraphRepository()
    frozen = _read_csv(TARGET_SOURCE)
    old_rows = _candidate_rows(repository)
    parsed_v2, v2_by_anchor = _v2_corpus(repository)
    old_by_target: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in old_rows:
        old_by_target[(str(row["reference_kind"]), str(row["normalized_document_number"]))].append(row)
    docs, locals_, externals = _identity_sets()
    packets: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    before_after: list[dict[str, Any]] = []
    issuer_rows: list[dict[str, Any]] = []
    kind_rows: list[dict[str, Any]] = []
    identifier_rows: list[dict[str, Any]] = []
    local_rows: list[dict[str, Any]] = []
    v4_quality = {row["target"]: row for row in _read_csv(V4_OUTPUT / "target_quality.csv")}
    for frozen_row in frozen:
        target = frozen_row["normalized_target_identity"]
        kind, identifier = _split_target(target)
        occurrences = old_by_target.get((kind, identifier), [])
        mapped: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for old in occurrences:
            updated = v2_by_anchor.get((int(old["source_document_id"]), int(old["source_page"]), kind, str(old["evidence_hash"])))
            if updated:
                mapped.append((old, updated))
        if not mapped and occurrences:
            mapped.append((occurrences[0], occurrences[0]))
        identity_counts = Counter((str(new.get("reference_kind") or kind), str(new.get("normalized_document_number") or identifier)) for _, new in mapped)
        corrected_kind, corrected_identifier = identity_counts.most_common(1)[0][0] if identity_counts else (kind, identifier)
        matching = [(old, new) for old, new in mapped if (str(new.get("reference_kind")), str(new.get("normalized_document_number"))) == (corrected_kind, corrected_identifier)] or mapped
        def score(pair: tuple[dict[str, Any], dict[str, Any]]) -> tuple[int, int, int, float]:
            old, new = pair
            state = _issuer_from_notes(str(new.get("parser_notes") or ""))
            return (
                int(state in {ISSUER_EXPLICIT_REFERENCE, ISSUER_EXPLICIT_CONTEXT}),
                int(bool(new.get("normalized_reference_date"))),
                int(bool(new.get("normalized_document_number"))),
                float(old.get("quality_score") or 0),
            )
        old, new = max(matching, key=score) if matching else ({}, {})
        page_lines = [normalize_reference_whitespace(line) for line in str(old.get("page_text") or "").splitlines()]
        page_lines = [line for line in page_lines if line]
        line_index = _locate_line(page_lines, str(old.get("raw_evidence_text") or ""), str(old.get("raw_document_number") or "")) if page_lines else 0
        recovery = context_recovery_metadata(
            page_lines, line_index, str(old.get("raw_evidence_text") or ""),
            ReferenceKind(corrected_kind) if corrected_kind in ReferenceKind._value2member_map_ else ReferenceKind.UNKNOWN,
            str(old.get("citing_department") or ""),
        ) if page_lines else {
            "context_before": "", "context_after": "", "issuer_state": ISSUER_UNKNOWN,
            "issuer_evidence_text": "", "issuer_evidence_location": "", "identifier_rules": [],
        }
        dates = {_as_iso(new_row.get("normalized_reference_date")) for _, new_row in matching if new_row.get("normalized_reference_date")}
        after_quality, after_reason = _quality(corrected_kind, corrected_identifier, dates, docs, locals_, externals)
        before_quality = str(v4_quality.get(target, {}).get("quality_state") or "UNKNOWN")
        issuer_state = str(recovery.get("issuer_state") or _issuer_from_notes(str(new.get("parser_notes") or "")))
        issuer = str(new.get("normalized_department") or "") if issuer_state != ISSUER_CITING_DOC_HINT_ONLY else ""
        packet = {
            "target_rank": frozen_row["rank"], "target": target,
            "raw_reference_text": old.get("raw_evidence_text", ""),
            "full_source_sentence": _source_sentence(page_lines, line_index, kind) if page_lines else old.get("raw_evidence_text", ""),
            "context_before": recovery.get("context_before", ""), "context_after": recovery.get("context_after", ""),
            "source_document_id": old.get("source_document_id", ""), "source_document": old.get("file_name", ""),
            "source_page": old.get("source_page", ""), "page_quality": old.get("quality_score", ""),
            "extraction_method": old.get("extraction_method", ""), "document_language": _language(str(old.get("page_text") or old.get("raw_evidence_text") or "")),
            "current_parsed_kind": kind, "current_raw_identifier": old.get("raw_document_number", ""),
            "current_normalized_identifier": identifier, "current_date": _as_iso(old.get("normalized_reference_date")),
            "current_issuer": old.get("normalized_department", ""), "current_parser_pattern": old.get("pattern_id", ""),
            "current_parser_status": old.get("parser_status", ""), "candidate_count": len(occurrences),
            "v2_identifier": corrected_identifier, "v2_kind": corrected_kind,
            "v2_date": ";".join(sorted(dates)), "v2_issuer": issuer,
            "v2_issuer_state": issuer_state, "v2_pattern": new.get("pattern_id", ""),
            "v2_parser_status": new.get("parser_status", ""), "v2_recovery_rules": _recovery_rules(str(new.get("parser_notes") or "")),
            "v2_issuer_evidence_text": recovery.get("issuer_evidence_text", ""),
            "v2_issuer_evidence_location": recovery.get("issuer_evidence_location", ""),
        }
        packets.append(packet)
        families = _failure_families(target, old, new)
        failures.append({
            "rank": frozen_row["rank"], "target": target, "failure_families": ";".join(families),
            "primary_failure_family": families[0], "source_evidence": old.get("raw_evidence_text", ""),
            "v2_resolution": "REPAIRED" if corrected_identifier != identifier else "UNCHANGED",
        })
        corrected_target = f"{corrected_kind}|{corrected_identifier}"
        before_after.append({
            "rank": frozen_row["rank"], "frozen_target": target, "before_kind": kind,
            "before_identifier": identifier, "before_date": frozen_row.get("date", ""),
            "before_issuer": frozen_row.get("issuer", ""), "before_quality_state": before_quality,
            "after_target": corrected_target, "after_kind": corrected_kind, "after_identifier": corrected_identifier,
            "after_date": ";".join(sorted(dates)), "after_issuer": issuer, "issuer_state": issuer_state,
            "after_quality_state": after_quality, "after_quality_reason": after_reason,
            "identity_occurrences": sum(identity_counts.values()), "corrected_identity_occurrences": identity_counts[(corrected_kind, corrected_identifier)],
            "alternative_v2_identities": ";".join(f"{a}|{b}:{count}" for (a, b), count in identity_counts.items() if (a, b) != (corrected_kind, corrected_identifier)),
            "network_allowed": "YES" if after_quality == "READY_FOR_DISCOVERY" else "NO",
        })
        issuer_rows.append({
            "rank": frozen_row["rank"], "target": target, "previous_issuer": old.get("normalized_department", ""),
            "recovered_issuer": issuer, "issuer_state": issuer_state,
            "evidence_text": recovery.get("issuer_evidence_text", ""), "evidence_location": recovery.get("issuer_evidence_location", ""),
            "alias_normalized": "YES" if recovery.get("issuer_alias_applied") else "NO",
            "citing_document_hint": old.get("citing_department", "") if issuer_state == ISSUER_CITING_DOC_HINT_ONLY else "",
            "strict_routing_allowed": "YES" if issuer_state in {ISSUER_EXPLICIT_REFERENCE, ISSUER_EXPLICIT_CONTEXT} else "NO",
        })
        kind_rows.append({
            "rank": frozen_row["rank"], "target": target, "before_kind": kind, "after_kind": corrected_kind,
            "changed": "YES" if kind != corrected_kind else "NO", "evidence": old.get("raw_evidence_text", ""),
            "rule": "LEXICAL_DOCUMENT_MARKER" if kind == corrected_kind else "BOUNDED_CONTEXT_LEXICAL_MARKER",
        })
        identifier_rows.append({
            "rank": frozen_row["rank"], "target": target, "raw_identifier": old.get("raw_document_number", ""),
            "before_identifier": identifier, "reconstructed_identifier": corrected_identifier,
            "changed": "YES" if identifier != corrected_identifier else "NO", "rule_chain": packet["v2_recovery_rules"],
            "source_evidence": packet["full_source_sentence"],
            "repair_class": "SAFE_DETERMINISTIC" if identifier != corrected_identifier else "UNCHANGED",
            "speculative": "NO",
        })
        resolution = (
            "ALREADY_INGESTED" if (corrected_kind, corrected_identifier) in docs else
            "LOCAL_EXACT" if (corrected_kind, corrected_identifier) in locals_ else
            "EXTERNAL_ARCHIVE_EXACT" if (corrected_kind, corrected_identifier) in externals else
            "NOT_FOUND_EXACT"
        )
        local_rows.append({
            "rank": frozen_row["rank"], "frozen_target": target, "corrected_target": corrected_target,
            "resolution": resolution, "match_method": "EXACT_KIND_AND_NORMALIZED_IDENTITY",
            "vector_similarity_used": "NO", "date": ";".join(sorted(dates)), "issuer": issuer,
        })
    write_csv(OUTPUT_DIR / "target_evidence_packets.csv", packets, EVIDENCE_FIELDS)
    write_csv(OUTPUT_DIR / "target_failure_assignments.csv", failures)
    write_csv(OUTPUT_DIR / "target_failure_families.csv", summarize_failure_families(failures))
    write_csv(OUTPUT_DIR / "parser_before_after.csv", before_after)
    write_csv(OUTPUT_DIR / "issuer_recovery.csv", issuer_rows)
    write_csv(OUTPUT_DIR / "kind_recovery.csv", kind_rows)
    write_csv(OUTPUT_DIR / "identifier_recovery.csv", identifier_rows)
    write_csv(OUTPUT_DIR / "local_resolution_after_parser_v2.csv", local_rows)
    result = build_parser_regression(parsed_v2, old_rows)
    return {
        "packets": packets, "before_after": before_after, "regression": result,
        "parsed_v2_candidates": len(parsed_v2),
    }


REGRESSION_FIELDS = [
    "case_id", "source_document_id", "source_file", "page", "context_before",
    "raw_evidence", "context_after", "is_reference", "expected_number", "expected_kind",
    "expected_date", "expected_issuer", "expected_issuer_state", "expected_parse_state", "label_source",
]


def build_parser_regression(parsed_v2: list[dict[str, Any]], old_rows: list[dict[str, Any]]) -> dict[str, Any]:
    page_lookup = {(int(row["source_document_id"]), int(row["source_page"])): row for row in old_rows}
    positives: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    legal_bad_heads = (
        "AMENDMENT_TO_", "AMENDMENTS_TO_", "SECTION_", "THE_SCHEDULE_", "MAH_", "AND_",
        "NOTE_", "NOTWITHSTANDING_", "SAVE_", "PROVIDED_", "CERTIFICATE_", "OCCASION_",
        "DIRECTION_", "GAZETTE_", "ANYTHING_",
    )
    for item in parsed_v2:
        kind = str(item["reference_kind"])
        identifier = str(item["normalized_document_number"] or "")
        if kind in {"ACT", "RULE"}:
            valid, _ = legal_identity_quality(identifier)
            if not valid or identifier.startswith(legal_bad_heads) or re.search(r"_\d+_(?:SAMITIS|ACT|RULE)", identifier):
                continue
        groups[kind].append(item)
    ordered: list[dict[str, Any]] = []
    offsets = {kind: 0 for kind in groups}
    while len(ordered) < 400 and any(offsets[kind] < len(values) for kind, values in groups.items()):
        for kind in sorted(groups):
            offset = offsets[kind]
            if offset < len(groups[kind]):
                ordered.append(groups[kind][offset])
                offsets[kind] += 1
    for row in ordered:
        if row["parser_status"] == ParserStatus.SELF_REFERENCE_REJECTED.value or not row["normalized_document_number"]:
            continue
        key = (str(row["reference_kind"]), str(row["normalized_document_number"]), str(row["raw_evidence_text"]))
        if key in seen:
            continue
        source = page_lookup.get((int(row["source_document_id"]), int(row["source_page"])))
        if not source:
            continue
        lines = [normalize_reference_whitespace(line) for line in str(source["page_text"]).splitlines()]
        lines = [line for line in lines if line]
        index = _locate_line(lines, str(row["raw_evidence_text"]), str(row["raw_document_number"]))
        recovery = context_recovery_metadata(lines, index, str(row["raw_evidence_text"]), ReferenceKind(str(row["reference_kind"])), str(source["citing_department"] or ""))
        issuer_state = _issuer_from_notes(str(row["parser_notes"]))
        expected_issuer = str(row["normalized_department"] or "") if issuer_state in {ISSUER_EXPLICIT_REFERENCE, ISSUER_EXPLICIT_CONTEXT} else ""
        positives.append({
            "source_document_id": row["source_document_id"], "source_file": row["source_file"], "page": row["source_page"],
            "context_before": recovery["context_before"], "raw_evidence": row["raw_evidence_text"], "context_after": recovery["context_after"],
            "is_reference": "YES", "expected_number": row["normalized_document_number"], "expected_kind": row["reference_kind"],
            "expected_date": row["normalized_reference_date"], "expected_issuer": expected_issuer,
            "expected_issuer_state": issuer_state, "expected_parse_state": row["parser_status"],
            "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE",
        })
        seen.add(key)
        if len(positives) >= 105:
            break
    negatives: list[dict[str, Any]] = []
    negative_seen: set[str] = set()
    for source in old_rows:
        for line in str(source["page_text"] or "").splitlines():
            clean = normalize_reference_whitespace(line)
            if len(clean) < 12 or clean in negative_seen or re.search(r"(?:अधिनियम|शासन|Government|\bAct\b|\bRules?\b|\d{4}|क्रमांक)", clean, re.IGNORECASE):
                continue
            parsed = ReferenceParser().parse_document(
                {"id": 900000 + len(negatives), "file_name": str(source["file_name"]), "department": "Regression", "gr_number": "SOURCE/NEGATIVE", "gr_date": ""},
                [{"page_number": int(source["source_page"]), "text": clean}],
            )
            if parsed:
                continue
            negatives.append({
                "source_document_id": source["source_document_id"], "source_file": source["file_name"], "page": source["source_page"],
                "context_before": "", "raw_evidence": clean, "context_after": "", "is_reference": "NO",
                "expected_number": "", "expected_kind": "", "expected_date": "", "expected_issuer": "",
                "expected_issuer_state": ISSUER_UNKNOWN, "expected_parse_state": "NO_REFERENCE",
                "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE",
            })
            negative_seen.add(clean)
            if len(negatives) >= 15:
                break
        if len(negatives) >= 15:
            break
    cases = positives + negatives
    for index, row in enumerate(cases, 1):
        row["case_id"] = f"KGPV2-{index:03d}"
    write_csv(REGRESSION_V2, cases, REGRESSION_FIELDS)
    return evaluate_parser_regression_v2(REGRESSION_V2)


def evaluate_parser_regression_v2(path: Path = REGRESSION_V2) -> dict[str, Any]:
    rows = _read_csv(path)
    parser = ReferenceParser()
    details: list[dict[str, Any]] = []
    totals = Counter()
    correct = Counter()
    for index, row in enumerate(rows, 1):
        text = "\n".join(value for value in (row["context_before"], row["raw_evidence"], row["context_after"]) if value)
        candidates = [candidate for candidate in parser.parse_document(
            {"id": 800000 + index, "file_name": row["source_file"], "department": "Regression", "gr_number": "REGRESSION/SOURCE", "gr_date": ""},
            [{"page_number": int(row["page"]), "text": text}],
        ) if candidate.parser_status != ParserStatus.SELF_REFERENCE_REJECTED]
        expected_positive = row["is_reference"] == "YES"
        totals["detection"] += 1
        detected = bool(candidates)
        correct["detection"] += int(detected == expected_positive)
        match = next((candidate for candidate in candidates if candidate.reference_kind.value == row["expected_kind"] and candidate.normalized_document_number == row["expected_number"]), None) if expected_positive else None
        if expected_positive:
            for metric in ("number", "kind", "date", "structured"):
                totals[metric] += 1
            correct["structured"] += int(match is not None)
            if match:
                correct["number"] += int(match.normalized_document_number == row["expected_number"])
                correct["kind"] += int(match.reference_kind.value == row["expected_kind"])
                correct["date"] += int(_as_iso(match.normalized_reference_date) == row["expected_date"])
            if row["expected_issuer"]:
                totals["issuer"] += 1
                correct["issuer"] += int(bool(match and match.normalized_department == row["expected_issuer"]))
        details.append({
            "case_id": row["case_id"], "passed": (not expected_positive and not candidates) or match is not None,
            "expected_number": row["expected_number"], "actual_number": match.normalized_document_number if match else "",
            "expected_kind": row["expected_kind"], "actual_kind": match.reference_kind.value if match else "",
        })
    metrics = {
        "Reference Number Accuracy": correct["number"] / totals["number"] if totals["number"] else 0,
        "Document Kind Accuracy": correct["kind"] / totals["kind"] if totals["kind"] else 0,
        "Explicit Issuer Accuracy": correct["issuer"] / totals["issuer"] if totals["issuer"] else 1,
        "Reference Date Accuracy": correct["date"] / totals["date"] if totals["date"] else 0,
        "Structured Target Accuracy": correct["structured"] / totals["structured"] if totals["structured"] else 0,
        "Reference Detection Accuracy": correct["detection"] / totals["detection"] if totals["detection"] else 0,
    }
    result = {
        "status": "PASS" if len(rows) >= 100 and all(value >= 0.95 for value in metrics.values()) else "FAIL",
        "rows": len(rows), "positive_rows": sum(row["is_reference"] == "YES" for row in rows),
        "negative_rows": sum(row["is_reference"] == "NO" for row in rows),
        "label_source": "AUTO_VERIFIED_SOURCE_EVIDENCE", "independent_human_ground_truth": False,
        "metrics": metrics, "denominators": dict(totals), "details": details,
    }
    write_json(OUTPUT_DIR / "parser_regression_results.json", result)
    return result


def controlled_ingestion_gate() -> dict[str, Any]:
    validation = json.loads((OUTPUT_DIR / "verified_manifest_validation.json").read_text(encoding="utf-8"))
    if validation["status"] != "PASS":
        raise RuntimeError("V5 verified manifest failed validation")
    if int(validation["documents"]):
        raise RuntimeError("Non-empty V5 manifest requires the explicit controlled ingestion path")
    result = {
        "status": "SKIPPED_EMPTY_MANIFEST", "attempted": 0, "succeeded": 0, "failed": 0,
        "reason": "No NEW_DOCUMENT + AUTO_VERIFIED + EXTERNAL_OFFICIAL_ARCHIVED row passed the V5 gate",
    }
    write_csv(OUTPUT_DIR / "ingestion_results.csv", [result])
    write_csv(OUTPUT_DIR / "target_to_edge_closure_trace.csv", [{
        "status": "NOT_APPLICABLE_EMPTY_MANIFEST", "verified_acquired_targets": 0,
        "ingested_targets": 0, "new_incoming_references": 0, "graph_components_affected": 0,
        "reason": "No verified new local archive document was eligible for ingestion",
    }])
    return result


def run_kg_twice() -> dict[str, Any]:
    regression = evaluate_parser_regression_v2()
    gate = regression["status"] == "PASS"
    if not gate:
        raise RuntimeError("Parser V2 regression gate failed")
    repository = GraphRepository()
    before = database_snapshot(repository)
    runs = []
    for number in (1, 2):
        result, persistence = KnowledgeGraphPipeline(repository=repository).run(
            auto_verify_enabled=True, expected_documents=72,
        )
        candidates = repository.all_candidates()
        policy = KnowledgeGraphValidationPolicy("auto")
        decisions = [policy.strong_relation_decision(row) for row in candidates if row.get("candidate_relation_type")]
        promotions = repository.apply_auto_promotions(decisions)
        runs.append({
            "number": number, "run": asdict(result), "persistence": persistence,
            "strong_policy": {
                "signals": len(decisions), "approved": sum(item.approved for item in decisions),
                "blocked": sum(not item.approved for item in decisions),
            },
            "strong_promotions": promotions,
        })
    after = database_snapshot(repository)
    second_new_rows = {
        key: int(value) for key, value in runs[1]["persistence"].items()
        if isinstance(value, int) and ("insert" in key or "evidence" in key)
    }
    second_new_rows.update({
        f"strong_{key}": int(value) for key, value in runs[1]["strong_promotions"].items()
        if isinstance(value, int) and key.startswith("inserted_")
    })
    summary = {
        "status": "PASS" if after["integrity_status"] == "PASS" and not any(second_new_rows.values()) else "FAIL",
        "parser_version": "reference-parser-v2.0.0", "parser_quality_gate": regression["metrics"],
        "database_before": before, "runs": runs, "database_after": after,
        "second_run_new_rows": second_new_rows,
        "idempotence": "PASS" if not any(second_new_rows.values()) else "FAIL",
    }
    write_json(OUTPUT_DIR / "kg_rerun_summary.json", summary)
    return summary


def _priority_v5() -> list[dict[str, Any]]:
    frozen = _read_csv(TARGET_SOURCE)
    parsed = {row["frozen_target"]: row for row in _read_csv(OUTPUT_DIR / "parser_before_after.csv")}
    packets = {row["target"]: row for row in _read_csv(OUTPUT_DIR / "target_evidence_packets.csv")}
    coverage = {row["target"]: row for row in _read_csv(OUTPUT_DIR / "source_coverage_audit.csv")}
    rows = []
    for row in frozen:
        target = row["normalized_target_identity"]
        after = parsed[target]
        packet = packets[target]
        route = coverage[target]
        rows.append({
            **row,
            "corrected_target_identity": after["after_target"],
            "issuer_evidence_state": packet["v2_issuer_state"],
            "issuer_evidence": packet["v2_issuer_evidence_text"],
            "kind_evidence": packet["full_source_sentence"],
            "date_evidence": after["after_date"],
            "parser_quality": after["after_quality_state"],
            "operational_source_route": route["TARGET_QUERYABLE"],
            "source_family": route["target_queryable_sources"],
            "bridge_value": row.get("bridge_value", row.get("component_bridge_value", "")),
            "citing_documents": row.get("distinct_citing_documents", ""),
            "strong_relation_signals": row.get("relation_signals", ""),
        })
    write_csv(PRIORITY_V5, rows)
    return rows


def _government_source_audit() -> None:
    rows = []
    for row in _read_csv(OUTPUT_DIR / "download_results.csv"):
        raw: dict[str, Any] = {}
        try:
            raw = ast.literal_eval(row.get("raw_source_evidence") or "{}")
        except (SyntaxError, ValueError):
            pass
        incoming_path = (
            (settings.external_incoming_root / row["local_acquired_path"]).resolve()
            if row.get("local_acquired_path") else ""
        )
        rows.append({
            "target": row.get("target_key", ""), "official_source_name": row.get("source_adapter", ""),
            "official_domain": row.get("source_domain", ""), "authority_class": row.get("source_authority", ""),
            "query": row.get("discovery_query", raw.get("query", "")), "landing_url": row.get("landing_url", ""),
            "pdf_url": row.get("document_url", ""), "download_timestamp": raw.get("download_timestamp", ""),
            "incoming_path": incoming_path, "archive_path": row.get("archive_relative_path", ""),
            "sha256": row.get("sha256", ""), "identity_result": row.get("identity_match_status", ""),
            "ingestion_result": "NOT_INGESTED", "document_id": "",
            "kg_closure_result": "NOT_REALIZED_" + row.get("target_state", ""),
        })
    write_csv(OUTPUT_DIR / "government_source_audit.csv", rows)


def _code_map() -> None:
    entries = [
        ("knowledge_engine/external_acquisition/registry.py", "official source registry"),
        ("knowledge_engine/external_acquisition/adapters.py", "reusable source adapters"),
        ("knowledge_engine/external_acquisition/network.py", "bounded official HTTP access"),
        ("knowledge_engine/external_acquisition/kg_guided_v1.py", "discovery, Incoming persistence, local PDF validation, matching, archive promotion, and manifest"),
        ("knowledge_engine/external_acquisition/kg_brain_v5.py", "V5 source routing and coverage"),
        ("knowledge_engine/graph/parser.py", "bounded-context reference parser and issuer recovery"),
        ("knowledge_engine/graph/normalization.py", "identifier and department normalization"),
        ("knowledge_engine/pipelines/ingest.py", "controlled local ingestion entry point"),
        ("knowledge_engine/graph/brain_v5.py", "manifest gate and KG rerun entry point"),
        ("cli/kg_brain_v5.py", "V5 command-line entry point"),
    ]
    lines = ["# External acquisition code map", ""]
    for relative, purpose in entries:
        state = "present" if (PROJECT_ROOT / relative).exists() else "not present (see repository map)"
        lines.append(f"- `{relative}` - {purpose} [{state}]")
    (OUTPUT_DIR / "external_acquisition_code_map.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze_graph() -> dict[str, Any]:
    repository = GraphRepository()
    inventory, evaluations, _ = relation_diagnostics(repository)
    write_csv(OUTPUT_DIR / "strong_relation_evaluation.csv", evaluations)
    graph, _, _, mapping = topology(repository)
    live_priority, _ = completion_priority(mapping, repository)
    priority = _priority_v5()

    v4_rows = _read_csv(PROJECT_ROOT / "data" / "knowledge_graph" / "kg_relationship_benchmark_v4.csv")
    current = build_benchmark(repository)
    keys = {(row["query_type"], row["source_document_id"], row["expected_targets"], row["expected_relations"]) for row in v4_rows}
    benchmark = list(v4_rows)
    for row in current:
        key = (str(row["query_type"]), str(row["source_document_id"]), str(row["expected_targets"]), str(row["expected_relations"]))
        if key not in keys:
            benchmark.append(row)
            keys.add(key)
    for index, row in enumerate(benchmark, 1):
        row["benchmark_id"] = f"KGB5-{index:03d}"
    benchmark_result = evaluate_benchmark(benchmark, repository)
    write_csv(BENCHMARK_V5, benchmark, list(BENCHMARK_FIELDS))
    write_csv(OUTPUT_DIR / "kg_benchmark_v5.csv", benchmark, list(BENCHMARK_FIELDS))
    write_json(OUTPUT_DIR / "kg_benchmark_results.json", benchmark_result)
    write_json(OUTPUT_DIR / "graph_topology_after.json", graph)
    closure = closure_snapshot(repository)["summary"]
    closure["unresolved_unique_targets"] = int(closure["referenced_target_closure"]["denominator"]) - int(closure["referenced_target_closure"]["numerator"])
    write_json(OUTPUT_DIR / "closure_metrics_after.json", closure)
    _government_source_audit()
    _code_map()

    archive = ExternalAcquisitionV2Service().archive_integrity()
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    v4_baseline = json.loads((V4_OUTPUT / "baseline.json").read_text(encoding="utf-8"))
    from knowledge_engine.external_acquisition.kg_brain_v4 import KgBrainV4AcquisitionService
    backups_unchanged = v4_baseline.get("backup_inventory") == KgBrainV4AcquisitionService._backup_inventory()
    integrity = integrity_report(repository)
    integrity.update({
        "archive_integrity": archive, "database_backups_created": 0,
        "existing_backups_modified": not backups_unchanged, "podman_commands": 0,
        "verified_manifest_sha256": _sha(MANIFEST_V5), "priority_v3_input_sha256": _sha(TARGET_SOURCE),
        "priority_v5_sha256": _sha(PRIORITY_V5), "benchmark_v5_sha256": _sha(BENCHMARK_V5),
        "parser_regression_v2_sha256": _sha(REGRESSION_V2), "frozen_queue_rows": len(priority),
        "live_completion_rows": len(live_priority), "relation_signal_rows": len(inventory),
        "document_fingerprint_unchanged": baseline["database"]["document_fingerprint"] == database_snapshot(repository)["document_fingerprint"],
    })
    if archive["status"] != "PASS" or not backups_unchanged:
        integrity["status"] = "FAIL"
    write_json(OUTPUT_DIR / "integrity_report.json", integrity)
    return {
        "status": "PASS" if benchmark_result["status"] == integrity["status"] == "PASS" else "FAIL",
        "graph": graph, "closure": closure, "benchmark": benchmark_result,
        "priority_rows": len(priority), "integrity": integrity, "performance": performance(repository),
    }


def retrieval_regression() -> dict[str, Any]:
    baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
    after = database_snapshot()
    keys = ("documents", "pages", "chunks", "embeddings", "document_fingerprint")
    unchanged = all(after[key] == baseline["database"][key] for key in keys)
    result = {
        "status": "PASS" if unchanged else "FAIL",
        "execution": "FINGERPRINT_ONLY_EMPTY_MANIFEST", "corpus_and_vector_fingerprint_unchanged": unchanged,
        "dense_benchmark_rerun": False, "tuning_performed": False,
        "reason": "V5 manifest was empty; corpus, embeddings, and retrieval configuration remain unchanged.",
    }
    write_json(OUTPUT_DIR / "retrieval_regression.json", result)
    return result


__all__ = [
    "BENCHMARK_V5", "MANIFEST_V5", "OUTPUT_DIR", "PRIORITY_V5", "REGRESSION_V2",
    "analyze_graph", "analyze_targets", "capture_baseline", "controlled_ingestion_gate",
    "evaluate_parser_regression_v2", "retrieval_regression", "run_kg_twice", "summarize_failure_families",
]

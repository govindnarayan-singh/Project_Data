from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.acquisition.inventory import build_inventory
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.validation import SignalConfidence, classify_relation_signal
from knowledge_engine.settings import PROJECT_ROOT, settings

from .extraction import probe_pdf_identity
from .manifest import MANIFEST_FIELDS, build_manifest, validate_manifest
from .models import CATALOG_VERSION, IdentityRecord, LocalResolution
from .repository import CorpusIdentityRepository
from .resolver import resolve_local_targets


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "corpus_identity_v1"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "kg_resolved_local_targets_v1.csv"


def catalogue_action(prior: dict[str, Any] | None, *, sha256: str, resume: bool) -> str:
    if not prior:
        return "NEW"
    if (
        resume
        and str(prior.get("sha256")) == sha256
        and str(prior.get("catalog_version")) == CATALOG_VERSION
        and str(prior.get("identity_parse_status")) != "NEEDS_OCR"
    ):
        return "UNCHANGED"
    return "CHANGED"


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_value) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | tuple[str, ...] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0]) if rows else ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        value.isoformat()
                        if isinstance(value, (date, datetime))
                        else ";".join(str(item) for item in value)
                        if isinstance(value, (tuple, list, set))
                        else value
                    )
                    for key, value in row.items()
                }
            )


class CorpusIdentityService:
    def __init__(
        self,
        repository: CorpusIdentityRepository | None = None,
        graph_repository: GraphRepository | None = None,
        root: Path = settings.gr_root,
    ):
        self.repository = repository or CorpusIdentityRepository()
        self.graph_repository = graph_repository or GraphRepository()
        self.root = root.resolve()

    def _database_state(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = dict(
                connection.execute(
                    """
                    SELECT (SELECT count(*) FROM documents) documents,
                           (SELECT count(*) FROM pages) pages,
                           (SELECT count(*) FROM chunks) chunks,
                           (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                           (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
                           (SELECT count(*) FROM kg_relations) kg_relations,
                           (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed
                    """
                ).fetchone()
            )
            identities = connection.execute(
                "SELECT id,file_path,sha256,coalesce(gr_number,''),coalesce(gr_date::text,'') FROM documents ORDER BY id"
            ).fetchall()
        digest = hashlib.sha256()
        for identity in identities:
            digest.update(("|".join(str(value) for value in identity.values()) + "\n").encode("utf-8"))
        row["document_identity_sha256"] = digest.hexdigest()
        return {key: int(value) if isinstance(value, int) else value for key, value in row.items()}

    @staticmethod
    def _failed_record(item, error: str, ingested_document_id: int | None) -> IdentityRecord:
        return IdentityRecord(
            relative_path=item.relative_path,
            filename=item.file_name,
            department_folder=item.department,
            normalized_department="",
            sha256=item.sha256 or "0" * 64,
            file_size=item.file_size,
            file_mtime_ns=0,
            page_count=item.page_count,
            filename_date=item.filename_date,
            document_kind="UNKNOWN",
            raw_document_number="",
            normalized_document_number="",
            raw_date="",
            normalized_date=None,
            raw_department=item.department,
            raw_subject="",
            subject_preview="",
            legal_canonical_key="",
            identity_source_page=None,
            identity_source_method="NONE",
            identity_parse_status="EXTRACTION_FAILED",
            identity_quality="BLOCKED",
            digital_quality="",
            extraction_error=error,
            ingested_document_id=ingested_document_id,
        )

    def build_catalogue(self, *, resume: bool = True, workers: int = 8) -> dict[str, Any]:
        started = time.perf_counter()
        ingested_by_path, ingested_by_sha = self.repository.ingested_identities()
        inventory_started = time.perf_counter()
        inventory = build_inventory(
            self.root,
            ingested_paths=set(ingested_by_path),
            ingested_sha=set(ingested_by_sha),
            workers=workers,
        )
        inventory_seconds = time.perf_counter() - inventory_started
        existing = self.repository.existing_catalogue()
        counters = Counter()
        digital_pages = ocr_pages = 0
        timings: list[dict[str, Any]] = []

        with self.repository.connect() as connection:
            for index, item in enumerate(inventory, start=1):
                prior = existing.get(item.relative_path.casefold())
                action = catalogue_action(prior, sha256=item.sha256, resume=resume)
                if action == "UNCHANGED":
                    counters["unchanged"] += 1
                    continue
                ingested_document_id = ingested_by_path.get(item.relative_path.casefold()) or ingested_by_sha.get(item.sha256)
                if item.inventory_error:
                    record = self._failed_record(item, item.inventory_error, ingested_document_id)
                else:
                    record = probe_pdf_identity(self.root, item)
                    record = replace(record, ingested_document_id=ingested_document_id)
                with connection.transaction():
                    _, inserted = self.repository.upsert_record(record, connection=connection)
                counters["new" if inserted else "changed"] += 1
                digital_pages += len(record.probe_pages)
                ocr_pages += len(record.ocr_pages)
                timings.append(
                    {
                        "relative_path": record.relative_path,
                        "elapsed_seconds": record.elapsed_seconds,
                        "probe_pages": record.probe_pages,
                        "ocr_pages": record.ocr_pages,
                        "status": record.identity_parse_status,
                    }
                )
                if index % 100 == 0:
                    print(f"catalogued {index}/{len(inventory)} PDFs", flush=True)

        rows = self.repository.catalogue_rows()
        elapsed = time.perf_counter() - started
        quality_counts = Counter(str(row["identity_parse_status"]) for row in rows)
        summary = {
            "catalog_version": CATALOG_VERSION,
            "local_pdfs_inventoried": len(inventory),
            "catalogue_rows": len(rows),
            "new_files": counters["new"],
            "changed_files": counters["changed"],
            "unchanged_files": counters["unchanged"],
            "inventory_seconds": inventory_seconds,
            "identity_indexing_seconds": elapsed,
            "average_processed_pdf_seconds": (
                sum(item["elapsed_seconds"] for item in timings) / len(timings) if timings else 0.0
            ),
            "digital_probe_pages_this_run": digital_pages,
            "ocr_probe_pages_this_run": ocr_pages,
            "ocr_attempted_pdfs_this_run": sum(bool(item["ocr_pages"]) for item in timings),
            "quality_statuses": dict(quality_counts),
            "slowest_identity_probes": sorted(timings, key=lambda item: -item["elapsed_seconds"])[:20],
        }
        return {"summary": summary, "rows": rows, "inventory": inventory}

    @staticmethod
    def _resolution_row(resolution: LocalResolution, catalogue: dict[int, dict[str, Any]]) -> dict[str, Any]:
        target = catalogue.get(resolution.catalog_id or -1, {})
        return {
            **asdict(resolution),
            "target_relative_path": target.get("relative_path", ""),
            "target_sha256": target.get("sha256", ""),
            "target_document_kind": target.get("document_kind", ""),
            "target_document_number": target.get("normalized_document_number", ""),
            "target_document_date": target.get("normalized_date", ""),
            "target_department": target.get("department_folder", ""),
            "target_identity_quality": target.get("identity_quality", ""),
            "target_ingested_document_id": target.get("ingested_document_id", ""),
        }

    @staticmethod
    def _duplicate_identifier_rows(catalogue_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in catalogue_rows:
            if row.get("normalized_document_number"):
                groups[str(row["normalized_document_number"])].append(row)
        result = []
        for number, items in groups.items():
            if len(items) < 2:
                continue
            result.append(
                {
                    "normalized_document_number": number,
                    "catalogue_rows": len(items),
                    "distinct_sha256": len({row["sha256"] for row in items}),
                    "distinct_dates": len({str(row.get("normalized_date") or "") for row in items}),
                    "distinct_departments": len({row["normalized_department"] for row in items}),
                    "identifier_date_collisions": sum(
                        count - 1
                        for count in Counter(str(row.get("normalized_date") or "") for row in items).values()
                        if count > 1
                    ),
                    "identifier_department_collisions": sum(
                        count - 1
                        for count in Counter(row["normalized_department"] for row in items).values()
                        if count > 1
                    ),
                    "relative_paths": tuple(sorted(str(row["relative_path"]) for row in items)),
                }
            )
        return sorted(result, key=lambda row: (-row["catalogue_rows"], row["normalized_document_number"]))

    @staticmethod
    def _relation_signal_rows(
        candidate_rows: list[dict[str, Any]], catalogue: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = []
        for candidate in candidate_rows:
            relation_type = str(candidate.get("candidate_relation_type") or "")
            if not relation_type:
                continue
            assessment = classify_relation_signal(relation_type, str(candidate.get("raw_evidence_text") or ""))
            target = catalogue.get(int(candidate["resolved_target_catalog_id"])) if candidate.get("resolved_target_catalog_id") else None
            gates = {
                "explicit_phrase": assessment.confidence == SignalConfidence.HIGH_CONFIDENCE_EXPLICIT,
                "unique_local_target": candidate.get("local_resolution_status") in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"} and target is not None,
                "parser_valid": candidate.get("parser_status") == "VALID",
                "source_quality": not bool(candidate.get("blocked_by_page_quality")),
                "target_quality": bool(target and target.get("identity_quality") == "HIGH"),
            }
            approved = all(gates.values())
            rows.append(
                {
                    "candidate_id": candidate["id"],
                    "relation_type": relation_type,
                    "source_file": candidate.get("source_file", ""),
                    "source_page": candidate["source_page"],
                    "normalized_identifier": candidate["normalized_document_number"],
                    "local_resolution_status": candidate.get("local_resolution_status") or "",
                    "target_catalog_id": candidate.get("resolved_target_catalog_id") or "",
                    "target_relative_path": target.get("relative_path", "") if target else "",
                    "signal_confidence": assessment.confidence.value,
                    "signal_pattern": assessment.pattern_id,
                    "explicit_phrase_gate": gates["explicit_phrase"],
                    "unique_target_gate": gates["unique_local_target"],
                    "source_quality_gate": gates["source_quality"],
                    "target_quality_gate": gates["target_quality"],
                    "machine_state": "AUTO_VERIFIED_LOCAL_TARGET" if approved else "CANDIDATE",
                    "authoritative_edge_created": False,
                    "reason": (
                        "All local-target signal gates passed; target ingestion is required before an authoritative edge"
                        if approved
                        else assessment.reason
                    ),
                }
            )
        return rows

    @staticmethod
    def _not_found_reason(resolution: LocalResolution) -> str:
        identifier = resolution.normalized_identifier
        if resolution.reference_kind in {"ACT", "RULE"}:
            return "REFERENCE_IS_ACT_OR_RULE_NOT_STORED_LOCALLY"
        if resolution.reference_kind in {"DEPARTMENT_LETTER", "GOVERNMENT_LETTER", "COURT_ORDER"}:
            return "REFERENCE_IS_NON_GR_EXTERNAL_DOCUMENT"
        if len(identifier) < 8:
            return "REFERENCE_TOO_INCOMPLETE"
        if not any(character.isdigit() for character in identifier) or identifier.count("/") < 1:
            return "REFERENCE_IDENTIFIER_DAMAGED"
        return "REFERENCED_DOCUMENT_TRULY_NOT_IN_LOCAL_CORPUS"

    @staticmethod
    def _failure_analysis(
        resolutions: list[LocalResolution], prior_matches: dict[str, dict[str, str]], catalogue: dict[int, dict[str, Any]], limit: int = 100
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            resolutions,
            key=lambda item: (
                {"AMBIGUOUS": 0, "BLOCKED_QUALITY": 1, "NOT_FOUND": 2}.get(prior_matches.get(item.group_id, {}).get("status", ""), 3),
                item.group_id,
            ),
        )
        rows = []
        for resolution in ordered[: min(limit, len(ordered))]:
            prior = prior_matches.get(resolution.group_id, {})
            target = catalogue.get(resolution.catalog_id or -1, {})
            if prior.get("status") == "AMBIGUOUS" or resolution.status == "AMBIGUOUS_LOCAL":
                cause = "MULTIPLE_POSSIBLE_TARGETS"
            elif resolution.status in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}:
                if target.get("identity_source_method") == "OCR":
                    cause = "OCR_REQUIRED"
                elif int(target.get("identity_source_page") or 1) > 2:
                    cause = "IDENTIFIER_ON_PAGE_2_OR_LATER"
                elif "DATE" in resolution.method:
                    cause = "DATE_NORMALIZATION_FAILURE"
                else:
                    cause = "PDF_METADATA_NOT_EXTRACTED"
            elif resolution.status == "BLOCKED_QUALITY":
                cause = "REFERENCE_IDENTIFIER_MALFORMED"
            elif resolution.reference_kind in {"ACT", "RULE"}:
                cause = "ACT_OR_RULE_REFERENCE"
            elif len(resolution.normalized_identifier) < 8:
                cause = "REFERENCE_IDENTIFIER_MALFORMED"
            else:
                cause = "TARGET_NOT_PRESENT"
            rows.append(
                {
                    "group_id": resolution.group_id,
                    "kg_raw_reference": prior.get("raw_identifier_variants", ""),
                    "normalized_reference": resolution.normalized_identifier,
                    "reference_dates": resolution.reference_dates,
                    "reference_kind": resolution.reference_kind,
                    "reference_departments": resolution.reference_departments,
                    "previous_status": prior.get("status", ""),
                    "v2_status": resolution.status,
                    "diagnostic_root_cause": cause,
                    "target_relative_path": target.get("relative_path", ""),
                    "identity_source_page": target.get("identity_source_page", ""),
                    "identity_source_method": target.get("identity_source_method", ""),
                    "measured_evidence": resolution.evidence,
                }
            )
        return rows

    def run(self, *, resume: bool = True, workers: int = 8) -> dict[str, Any]:
        started = time.perf_counter()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        baseline = self._database_state()
        baseline["unresolved_consolidated_groups"] = 233
        baseline["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json(OUTPUT_DIR / "baseline.json", baseline)

        catalogue_result = self.build_catalogue(resume=resume, workers=workers)
        catalogue_rows = catalogue_result["rows"]
        catalogue = {int(row["id"]): row for row in catalogue_rows}
        catalogue_summary = catalogue_result["summary"]

        quality_counts = Counter(str(row["identity_parse_status"]) for row in catalogue_rows)
        exact_sha_duplicates = {
            sha: rows
            for sha, rows in (
                (sha, [row for row in catalogue_rows if str(row["sha256"]) == sha])
                for sha in {str(row["sha256"]) for row in catalogue_rows}
            )
            if len(rows) > 1
        }
        catalogue_summary.update(
            {
                "identity_statuses": dict(quality_counts),
                "digital_only_identities": sum(
                    row["identity_source_method"] == "DIGITAL" and row["identity_parse_status"] in {"IDENTIFIED_EXACT", "IDENTIFIED_PARTIAL"}
                    for row in catalogue_rows
                ),
                "ocr_assisted_identities": sum(
                    row["identity_source_method"] == "OCR" and row["identity_parse_status"] in {"IDENTIFIED_EXACT", "IDENTIFIED_PARTIAL"}
                    for row in catalogue_rows
                ),
                "document_numbers_extracted": sum(bool(row["normalized_document_number"]) for row in catalogue_rows),
                "dates_extracted_from_text": sum(bool(row["normalized_date"]) for row in catalogue_rows),
                "departments_determined": sum(bool(row["normalized_department"]) for row in catalogue_rows),
                "document_kinds_determined": sum(row["document_kind"] != "UNKNOWN" for row in catalogue_rows),
                "unique_normalized_identifiers": len({row["normalized_document_number"] for row in catalogue_rows if row["normalized_document_number"]}),
                "exact_duplicate_sha_groups": len(exact_sha_duplicates),
                "digital_probe_pages_total": sum(len(row["probe_pages"] or []) for row in catalogue_rows),
                "ocr_probe_pages_total": sum(len(row["ocr_pages"] or []) for row in catalogue_rows),
                "ocr_attempted_pdfs_total": sum(bool(row["ocr_used"]) for row in catalogue_rows),
            }
        )
        _write_json(OUTPUT_DIR / "catalogue_summary.json", catalogue_summary)
        _write_csv(
            OUTPUT_DIR / "catalogue_quality.csv",
            [{"identity_parse_status": status, "count": count} for status, count in sorted(quality_counts.items())],
        )
        duplicate_rows = self._duplicate_identifier_rows(catalogue_rows)
        _write_csv(OUTPUT_DIR / "duplicate_identifier_groups.csv", duplicate_rows)

        candidate_rows = self.graph_repository.all_candidates()
        resolution_started = time.perf_counter()
        resolutions = resolve_local_targets(candidate_rows, catalogue_rows)
        resolution_seconds = time.perf_counter() - resolution_started
        first_updates = sum(self.repository.persist_resolution(item) for item in resolutions)
        second_updates = sum(self.repository.persist_resolution(item) for item in resolutions)
        first_reconciliation = self.repository.reconcile_ingested_targets(resolutions, catalogue_rows)
        second_reconciliation = self.repository.reconcile_ingested_targets(resolutions, catalogue_rows)
        resolution_rows = [self._resolution_row(item, catalogue) for item in resolutions]
        fields = list(resolution_rows[0]) if resolution_rows else None
        _write_csv(OUTPUT_DIR / "resolution_v2.csv", resolution_rows, fields)
        _write_csv(
            OUTPUT_DIR / "resolved_local_targets.csv",
            [row for row in resolution_rows if row["status"] in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}],
            fields,
        )
        _write_csv(
            OUTPUT_DIR / "ambiguous_local_targets.csv",
            [row for row in resolution_rows if row["status"] == "AMBIGUOUS_LOCAL"],
            fields,
        )
        not_found = [item for item in resolutions if item.status == "NOT_FOUND_LOCAL"]
        not_found_rows = [self._resolution_row(item, catalogue) for item in not_found]
        _write_csv(OUTPUT_DIR / "not_found_local.csv", not_found_rows, fields)
        not_found_analysis = [
            {
                "group_id": item.group_id,
                "normalized_identifier": item.normalized_identifier,
                "reference_kind": item.reference_kind,
                "mentions": item.mentions,
                "classification": self._not_found_reason(item),
                "resolution_evidence": item.evidence,
            }
            for item in not_found
        ]
        _write_csv(OUTPUT_DIR / "not_found_analysis.csv", not_found_analysis)

        prior_path = PROJECT_ROOT / "outputs" / "graph_guided_expansion_v1" / "acquisition_matches.csv"
        with prior_path.open("r", encoding="utf-8-sig", newline="") as handle:
            prior_matches = {row["group_id"]: row for row in csv.DictReader(handle)}
        failure_rows = self._failure_analysis(resolutions, prior_matches, catalogue)
        _write_csv(OUTPUT_DIR / "failure_analysis.csv", failure_rows)
        _write_csv(OUTPUT_DIR / "acquisition_failure_analysis.csv", failure_rows)

        local_candidates = self.repository.local_candidate_rows()
        signal_rows = self._relation_signal_rows(local_candidates, catalogue)
        _write_csv(OUTPUT_DIR / "relation_signal_reresolution.csv", signal_rows)

        manifest_started = time.perf_counter()
        manifest = build_manifest(resolutions, catalogue_rows)
        ingested_by_path, ingested_by_sha = self.repository.ingested_identities()
        safety = validate_manifest(
            manifest,
            root=self.root,
            ingested_paths=set(ingested_by_path),
            ingested_sha=set(ingested_by_sha),
        )
        if safety["status"] != "PASS":
            raise RuntimeError(f"Graph-completion manifest failed safety validation: {safety}")
        _write_csv(MANIFEST_PATH, manifest, MANIFEST_FIELDS)
        manifest_sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
        manifest_seconds = time.perf_counter() - manifest_started

        statuses = Counter(item.status for item in resolutions)
        resolved = [item for item in resolutions if item.status in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}]
        resolved_mentions = sum(item.mentions for item in resolved)
        total_mentions = sum(item.mentions for item in resolutions)
        resolved_sources = {value for item in resolved for value in item.source_document_ids}
        all_sources = {value for item in resolutions for value in item.source_document_ids}
        unique_targets = {item.catalog_id for item in resolved if item.catalog_id is not None}
        current_edges = [edge for edge in self.graph_repository.all_edges() if edge["relation_type"] != "ISSUED_BY"]
        current_participant_ids = {
            int(value)
            for edge in current_edges
            for value in (edge["source_document_id"], edge.get("target_document_id"))
            if value is not None
        }
        already_ingested_target_ids = {
            int(catalogue[item.catalog_id]["ingested_document_id"])
            for item in resolved
            if item.catalog_id is not None and catalogue[item.catalog_id].get("ingested_document_id") is not None
        }
        ready_target_ids = {
            item.catalog_id for item in resolved
            if item.catalog_id is not None and item.ingestion_eligibility == "READY_FOR_INGESTION"
        }
        potential_existing_participants = current_participant_ids | resolved_sources | already_ingested_target_ids
        potential_denominator = baseline["documents"] + len(ready_target_ids)
        potential_participants = len(potential_existing_participants) + len(ready_target_ids)
        graph_potential = {
            "potential_new_references_after_ingestion": sum(
                item.ingestion_eligibility == "READY_FOR_INGESTION"
                for item in resolved
                for _source_id in item.source_document_ids
            ),
            "source_documents_gaining_references": len(resolved_sources),
            "resolved_local_targets": len(unique_targets),
            "strong_relation_candidates_with_local_targets": sum(
                row["machine_state"] == "AUTO_VERIFIED_LOCAL_TARGET" for row in signal_rows
            ),
            "potential_graph_document_coverage": min(potential_participants, potential_denominator) / potential_denominator if potential_denominator else 0,
            "potential_only": True,
            "caveat": "No local target is an authoritative document-to-document edge until controlled ingestion and KG re-resolution.",
        }
        _write_json(OUTPUT_DIR / "graph_completion_potential.json", graph_potential)

        resolution_metrics = {
            "groups_evaluated": len(resolutions),
            "statuses": dict(statuses),
            "automatic_local_resolution_rate": len(resolved) / len(resolutions) if resolutions else 0,
            "local_target_coverage": len(unique_targets) / len(resolutions) if resolutions else 0,
            "mention_coverage": resolved_mentions / total_mentions if total_mentions else 0,
            "distinct_citing_document_coverage": len(resolved_sources) / len(all_sources) if all_sources else 0,
            "resolution_lookup_seconds": resolution_seconds,
            "first_persistence_updates": first_updates,
            "second_persistence_updates": second_updates,
            "first_ingested_target_reconciliation": first_reconciliation,
            "second_ingested_target_reconciliation": second_reconciliation,
            "idempotent": second_updates == 0
            and second_reconciliation["updated_candidates"] == 0
            and second_reconciliation["inserted_reference_relations"] == 0
            and second_reconciliation["inserted_evidence_links"] == 0,
        }
        _write_json(OUTPUT_DIR / "resolution_metrics.json", resolution_metrics)

        integrity = self.repository.integrity()
        after = self._database_state()
        integrity.update(
            {
                "database_before": baseline,
                "database_after": after,
                "search_corpus_unchanged": all(
                    before == after[key]
                    for key, before in baseline.items()
                    if key in {"documents", "pages", "chunks", "embeddings", "kg_candidates", "document_identity_sha256"}
                ),
                "manifest_safety": safety,
                "resolver_idempotent": resolution_metrics["idempotent"],
            }
        )
        if not integrity["search_corpus_unchanged"] or safety["status"] != "PASS" or not resolution_metrics["idempotent"]:
            integrity["status"] = "FAIL"
        _write_json(OUTPUT_DIR / "integrity_report.json", integrity)

        performance = {
            **{key: catalogue_summary[key] for key in (
                "local_pdfs_inventoried",
                "inventory_seconds",
                "identity_indexing_seconds",
                "average_processed_pdf_seconds",
                "digital_probe_pages_this_run",
                "ocr_probe_pages_this_run",
                "ocr_attempted_pdfs_this_run",
                "digital_probe_pages_total",
                "ocr_probe_pages_total",
                "ocr_attempted_pdfs_total",
                "slowest_identity_probes",
            )},
            "catalogue_lookup_and_resolution_seconds": resolution_seconds,
            "manifest_generation_seconds": manifest_seconds,
            "total_wall_seconds": time.perf_counter() - started,
        }
        _write_json(OUTPUT_DIR / "performance.json", performance)

        manifest_info = {
            "path": MANIFEST_PATH.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": manifest_sha,
            "eligible_pdfs": len(manifest),
            "tiers": dict(Counter(row["priority_tier"] for row in manifest)),
            "safety": safety,
        }
        not_found_counts = Counter(row["classification"] for row in not_found_analysis)
        recommendation = (
            "A. AUTO-INGEST RESOLVED LOCAL TARGETS"
            if len(manifest) >= 25 and len(resolved) / len(resolutions) >= 0.15
            else "B. HARDEN IDENTITY EXTRACTION"
            if quality_counts["NO_IDENTIFIER"] + quality_counts["NEEDS_OCR"] + quality_counts["QUALITY_BLOCKED"] >= len(catalogue_rows) * 0.25
            else "C. EXPAND CORPUS FROM OTHER SOURCES"
        )
        result = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "recommendation": (
                "FAIL"
                if integrity["status"] != "PASS"
                else "CONDITIONAL PASS"
                if len(resolved) < 7
                else "PASS"
            ),
            "next_recommendation": recommendation,
            "catalogue": catalogue_summary,
            "identifier_coverage": {
                "document_numbers": catalogue_summary["document_numbers_extracted"],
                "dates": catalogue_summary["dates_extracted_from_text"],
                "departments": catalogue_summary["departments_determined"],
                "document_kinds": catalogue_summary["document_kinds_determined"],
                "unique_normalized_identifiers": catalogue_summary["unique_normalized_identifiers"],
                "duplicate_identifier_groups": len(duplicate_rows),
            },
            "resolution_v2": resolution_metrics,
            "improvement": {
                "previous_defensible_local_matches": 7,
                "new_defensible_local_matches": len(resolved),
                "absolute_improvement": len(resolved) - 7,
                "coverage_improvement": (len(resolved) - 7) / len(resolutions) if resolutions else 0,
            },
            "strong_relation_signals": {
                "counts": dict(Counter(row["relation_type"] for row in signal_rows)),
                "resolved_local_target": dict(Counter(row["relation_type"] for row in signal_rows if row["target_catalog_id"])),
                "auto_verified_local_target": sum(row["machine_state"] == "AUTO_VERIFIED_LOCAL_TARGET" for row in signal_rows),
                "human_confirmed": after["human_confirmed"],
            },
            "graph_completion": graph_potential,
            "manifest": manifest_info,
            "not_found": dict(not_found_counts),
            "database": after,
            "performance": performance,
            "integrity": integrity,
            "confirmations": {
                "all_local_pdfs_identity_indexed": len(catalogue_rows) == len(catalogue_result["inventory"]) == 2715,
                "full_corpus_embeddings_generated": False,
                "full_document_ingestion": False,
                "dense_retrieval_changed": False,
                "llm_used": False,
                "rag_added": False,
                "ui_work": False,
                "neo4j_added": False,
                "human_confirmed_generated": False,
                "machine_resolution_state": "AUTO_VERIFIED",
                "ambiguous_targets_guessed": False,
                "raw_identifiers_preserved": True,
            },
        }
        _write_json(OUTPUT_DIR / "final_validation.json", result)
        self._write_final_report(result)
        return result

    @staticmethod
    def _write_final_report(result: dict[str, Any]) -> None:
        catalogue = result["catalogue"]
        identifiers = result["identifier_coverage"]
        resolution = result["resolution_v2"]
        statuses = resolution["statuses"]
        improvement = result["improvement"]
        manifest = result["manifest"]
        graph = result["graph_completion"]
        database = result["database"]
        report = f"""# Local Corpus Identity V1 final report

## Result

**{result['recommendation']}**. PostgreSQL now contains a separate non-search identity catalogue; no PDF was ingested and no embedding was generated.

## Local corpus catalogue

- PDFs: {catalogue['local_pdfs_inventoried']}
- Catalogue rows: {catalogue['catalogue_rows']}
- IDENTIFIED_EXACT: {catalogue['identity_statuses'].get('IDENTIFIED_EXACT', 0)}
- IDENTIFIED_PARTIAL: {catalogue['identity_statuses'].get('IDENTIFIED_PARTIAL', 0)}
- NO_IDENTIFIER: {catalogue['identity_statuses'].get('NO_IDENTIFIER', 0)}
- AMBIGUOUS_METADATA: {catalogue['identity_statuses'].get('AMBIGUOUS_METADATA', 0)}
- EXTRACTION_FAILED: {catalogue['identity_statuses'].get('EXTRACTION_FAILED', 0)}
- NEEDS_OCR: {catalogue['identity_statuses'].get('NEEDS_OCR', 0)}
- QUALITY_BLOCKED: {catalogue['identity_statuses'].get('QUALITY_BLOCKED', 0)}
- Digital-only identities: {catalogue['digital_only_identities']}
- OCR-assisted identities: {catalogue['ocr_assisted_identities']}

## Identifier coverage

- Document numbers: {identifiers['document_numbers']}
- Text dates: {identifiers['dates']}
- Departments: {identifiers['departments']}
- Document kinds: {identifiers['document_kinds']}
- Unique normalized identifiers: {identifiers['unique_normalized_identifiers']}
- Duplicate identifier groups: {identifiers['duplicate_identifier_groups']}

## Resolution V2

- Groups evaluated: {resolution['groups_evaluated']}
- RESOLVED_LOCAL_EXACT: {statuses.get('RESOLVED_LOCAL_EXACT', 0)}
- RESOLVED_LOCAL_STRONG: {statuses.get('RESOLVED_LOCAL_STRONG', 0)}
- AMBIGUOUS_LOCAL: {statuses.get('AMBIGUOUS_LOCAL', 0)}
- NOT_FOUND_LOCAL: {statuses.get('NOT_FOUND_LOCAL', 0)}
- BLOCKED_QUALITY: {statuses.get('BLOCKED_QUALITY', 0)}
- SOURCE_UNPARSEABLE: {statuses.get('SOURCE_UNPARSEABLE', 0)}
- Automatic local resolution rate: {resolution['automatic_local_resolution_rate']:.4%}
- Resolver idempotent: {resolution['idempotent']}

## Improvement

- Previous defensible local matches: {improvement['previous_defensible_local_matches']}
- New defensible local matches: {improvement['new_defensible_local_matches']}
- Absolute improvement: {improvement['absolute_improvement']}

## Graph completion (potential only)

- Potential new REFERENCES after ingestion: {graph['potential_new_references_after_ingestion']}
- Source documents gaining references: {graph['source_documents_gaining_references']}
- Potential graph document coverage: {graph['potential_graph_document_coverage']:.4%}

## Next manifest

- Path: `{manifest['path']}`
- SHA-256: `{manifest['sha256']}`
- Eligible PDFs: {manifest['eligible_pdfs']}
- Tiers: {manifest['tiers']}
- Existing ingested PDFs: {manifest['safety']['already_ingested']}
- Duplicate paths / SHA: {manifest['safety']['duplicate_paths']} / {manifest['safety']['duplicate_sha']}

## Database

- Search documents: {database['documents']}
- Pages: {database['pages']}
- Chunks: {database['chunks']}
- Embeddings: {database['embeddings']}
- HUMAN_CONFIRMED: {database['human_confirmed']}

## Recommendation

**{result['next_recommendation']}**
"""
        (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")

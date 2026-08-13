from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.settings import PROJECT_ROOT, settings

from .groups import consolidate_unresolved
from .inventory import build_inventory
from .manifest import MANIFEST_FIELDS, select_manifest, validate_manifest
from .matcher import probe_pdf, resolve_groups


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "graph_guided_expansion_v1"
MANIFEST_PATH = PROJECT_ROOT / "data" / "manifests" / "graph_guided_expansion_v1.csv"


def _value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return ";".join(str(item) for item in value)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...] | list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: _value(value) for key, value in row.items()} for row in rows)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_value) + "\n", encoding="utf-8")


class GraphGuidedAcquisitionService:
    def __init__(self, repository: GraphRepository | None = None, root: Path = settings.gr_root):
        self.repository = repository or GraphRepository()
        self.root = root.resolve()

    def _database_state(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = dict(connection.execute(
                """SELECT (SELECT count(*) FROM documents) documents,
                          (SELECT count(*) FROM pages) pages,
                          (SELECT count(*) FROM chunks) chunks,
                          (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                          (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
                          (SELECT count(*) FROM kg_relations) kg_relations,
                          (SELECT count(*) FROM kg_relations WHERE relation_type='REFERENCES') references,
                          (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by,
                          (SELECT count(*) FROM chunks WHERE token_count>700) chunks_over_700,
                          (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) invalid_vectors,
                          (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed"""
            ).fetchone())
            row["resolution_states"] = {item["resolution_status"]: int(item["n"]) for item in connection.execute(
                "SELECT resolution_status,count(*) n FROM kg_reference_candidates GROUP BY resolution_status ORDER BY resolution_status")}
            row["validation_states"] = {item["validation_status"]: int(item["n"]) for item in connection.execute(
                "SELECT validation_status,count(*) n FROM kg_reference_candidates GROUP BY validation_status ORDER BY validation_status")}
            row["relation_types"] = {item["relation_type"]: int(item["n"]) for item in connection.execute(
                "SELECT relation_type,count(*) n FROM kg_relations GROUP BY relation_type ORDER BY relation_type")}
            identities = list(connection.execute(
                "SELECT id,file_path,sha256,coalesce(gr_number,''),coalesce(gr_date::text,'') FROM documents ORDER BY id"))
        digest = hashlib.sha256()
        for item in identities:
            digest.update(("|".join(str(value) for value in item.values()) + "\n").encode("utf-8"))
        row["document_identity_sha256"] = digest.hexdigest()
        return row

    def _ingested_identities(self) -> tuple[set[str], set[str]]:
        with self.repository.connect() as connection:
            rows = list(connection.execute("SELECT file_path,sha256 FROM documents"))
        return {str(row["file_path"]) for row in rows}, {str(row["sha256"]) for row in rows}

    def _probe_inventory(self, inventory, groups):
        # First-page metadata is the cheapest reliable local identity index. It
        # is limited to non-ingested PDFs and never invokes OCR, chunks, or vectors.
        probes = []
        pages = 0
        for item in inventory:
            if item.already_ingested or item.inventory_error:
                continue
            probe = probe_pdf(self.root, item, max_pages=2)
            probes.append(probe)
            pages += probe.pages_probed
        return probes, pages

    @staticmethod
    def _group_row(group) -> dict[str, Any]:
        return asdict(group)

    @staticmethod
    def _match_row(match) -> dict[str, Any]:
        candidate_paths = tuple(item.relative_path for item in match.candidates)
        candidate_departments = tuple(item.department for item in match.candidates)
        candidate_dates = tuple(str(item.document_date or "") for item in match.candidates)
        return {
            "group_id": match.group.group_id,
            "raw_identifier_variants": match.group.raw_identifiers,
            "normalized_identifier": match.group.normalized_identifier,
            "reference_kind": match.group.reference_kind,
            "reference_dates": match.group.reference_dates,
            "departments": match.group.source_departments,
            "source_documents": match.group.source_files,
            "source_pages": match.group.source_pages,
            "mentions": match.group.mentions,
            "relation_signals": match.group.relation_signals,
            "candidate_paths": candidate_paths,
            "candidate_departments": candidate_departments,
            "candidate_dates": candidate_dates,
            "matching_evidence": match.matching_evidence,
            "match_method": match.match_method,
            "status": match.status,
            "confidence_category": match.confidence_category,
            "priority_score": match.priority_score,
        }

    def run(self, *, manifest_target: int = 120) -> dict[str, Any]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        baseline = self._database_state()
        baseline["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json(OUTPUT_DIR / "baseline.json", baseline)
        _write_json(OUTPUT_DIR / "graph_metrics_before.json", self.graph_metrics())

        candidates = self.repository.all_candidates()
        policy = KnowledgeGraphValidationPolicy.from_env()
        dry = policy.dry_run(candidates)
        _write_json(OUTPUT_DIR / "auto_policy_dry_run.json", dry)
        decisions = [policy.strong_relation_decision(row) for row in candidates if row.get("candidate_relation_type")]
        first = self.repository.apply_auto_promotions(decisions)
        second = self.repository.apply_auto_promotions(decisions)
        auto_result = {"first_apply": first, "second_apply": second, "idempotent": second["inserted_relations"] == 0,
                       "human_confirmed_created": 0}
        _write_json(OUTPUT_DIR / "auto_policy_result.json", auto_result)

        started_inventory = time.perf_counter()
        ingested_paths, ingested_sha = self._ingested_identities()
        inventory = build_inventory(self.root, ingested_paths=ingested_paths, ingested_sha=ingested_sha)
        inventory_seconds = time.perf_counter() - started_inventory
        inventory_rows = [asdict(item) for item in inventory]
        _write_csv(OUTPUT_DIR / "local_pdf_inventory.csv", inventory_rows, list(inventory_rows[0]))

        groups = consolidate_unresolved(candidates)
        group_rows = [self._group_row(group) for group in groups]
        _write_csv(OUTPUT_DIR / "unresolved_group_consolidation.csv", group_rows, list(group_rows[0]))

        started_probe = time.perf_counter()
        probes, pages_probed = self._probe_inventory(inventory, groups)
        probe_seconds = time.perf_counter() - started_probe
        probe_rows = [asdict(probe) for probe in probes]
        _write_csv(OUTPUT_DIR / "metadata_probes.csv", probe_rows, list(probe_rows[0]))

        started_match = time.perf_counter()
        matches = resolve_groups(groups, probes)
        matching_seconds = time.perf_counter() - started_match
        match_rows = [self._match_row(match) for match in matches]
        match_fields = list(match_rows[0])
        _write_csv(OUTPUT_DIR / "acquisition_matches.csv", match_rows, match_fields)
        _write_csv(OUTPUT_DIR / "ambiguous_acquisition_matches.csv", [row for row in match_rows if row["status"] == "AMBIGUOUS"], match_fields)
        _write_csv(OUTPUT_DIR / "not_found_references.csv", [row for row in match_rows if row["status"] in {"NOT_FOUND", "BLOCKED_QUALITY"}], match_fields)
        _write_csv(OUTPUT_DIR / "graph_guided_priority.csv", match_rows, match_fields)

        started_manifest = time.perf_counter()
        manifest = select_manifest(matches, target=manifest_target)
        safety = validate_manifest(manifest, root=self.root, ingested_paths=ingested_paths, ingested_sha=ingested_sha)
        if safety["status"] != "PASS":
            raise RuntimeError(f"Manifest safety failed: {safety}")
        _write_csv(MANIFEST_PATH, manifest, list(MANIFEST_FIELDS))
        manifest_sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
        manifest_seconds = time.perf_counter() - started_manifest

        selected_paths = {row["file_path"] for row in manifest}
        selected_matches = [match for match in matches if match.candidates and match.candidates[0].relative_path in selected_paths]
        impact = {
            "unresolved_groups_potentially_covered": len(selected_matches),
            "unresolved_mentions_potentially_covered": sum(item.group.mentions for item in selected_matches),
            "distinct_citing_documents_represented": len({value for item in selected_matches for value in item.group.source_documents}),
            "selected_targets_with_relation_signals": dict(Counter(signal for item in selected_matches for signal in item.group.relation_signals)),
            "caveat": "Potential only: ingestion and post-ingestion identity resolution are required before graph edges can change.",
        }
        _write_json(OUTPUT_DIR / "expected_graph_impact.json", impact)
        graph_after = self.graph_metrics(local_matches=matches)
        _write_json(OUTPUT_DIR / "graph_metrics_after.json", graph_after)

        performance = {
            "local_pdfs_inventoried": len(inventory), "inventory_seconds": inventory_seconds,
            "unresolved_groups_processed": len(groups), "matching_seconds": matching_seconds,
            "pdfs_requiring_limited_metadata_probe": len(probes), "pages_probed": pages_probed,
            "ocr_probes": 0, "metadata_probe_seconds": probe_seconds,
            "manifest_generation_seconds": manifest_seconds,
        }
        _write_json(OUTPUT_DIR / "performance.json", performance)
        statuses = Counter(match.status for match in matches)
        result = {
            "baseline": baseline, "auto_policy": auto_result, "inventory": performance,
            "match_statuses": dict(statuses), "manifest": {"path": MANIFEST_PATH.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": manifest_sha, "selected": len(manifest), "tiers": dict(Counter(row["selection_tier"] for row in manifest)),
                "safety": safety}, "expected_graph_impact": impact, "graph_metrics_after": graph_after,
            "database_after": self._database_state(),
        }
        _write_json(OUTPUT_DIR / "run_summary.json", result)
        return result

    def graph_metrics(self, *, local_matches=None) -> dict[str, Any]:
        candidates = self.repository.all_candidates()
        edges = self.repository.all_edges()
        with self.repository.connect() as connection:
            documents = int(connection.execute("SELECT count(*) n FROM documents").fetchone()["n"])
        document_edges = [edge for edge in edges if edge["relation_type"] != "ISSUED_BY"]
        participating = {int(edge["source_document_id"]) for edge in document_edges} | {
            int(edge["target_document_id"]) for edge in document_edges if edge.get("target_document_id")}
        denominator_statuses = {"RESOLVED_EXACT", "RESOLVED_STRONG", "AMBIGUOUS", "NOT_IN_CORPUS", "NEEDS_REVIEW"}
        denominator = sum(row["resolution_status"] in denominator_statuses for row in candidates)
        resolved = sum(row["resolution_status"] in {"RESOLVED_EXACT", "RESOLVED_STRONG"} for row in candidates)
        unresolved_groups = consolidate_unresolved(candidates)
        metrics = {
            "reference_candidates_total": len(candidates),
            "parseable_reference_candidates": sum(row["resolution_status"] != "UNPARSEABLE" for row in candidates),
            "resolved_reference_candidates": resolved,
            "unresolved_reference_candidates": sum(row["resolution_status"] in {"AMBIGUOUS", "NOT_IN_CORPUS", "NEEDS_REVIEW"} for row in candidates),
            "resolution_rate": resolved / denominator if denominator else 0,
            "resolution_rate_formula": "resolved / (resolved + ambiguous + not_in_corpus + needs_review); SELF_REFERENCE_REJECTED and UNPARSEABLE excluded",
            "ambiguous_rate": sum(row["resolution_status"] == "AMBIGUOUS" for row in candidates) / denominator if denominator else 0,
            "unparseable_rate": sum(row["resolution_status"] == "UNPARSEABLE" for row in candidates) / len(candidates) if candidates else 0,
            "document_to_document_relations": len(document_edges),
            "auto_verified_strong_relations": sum(edge["relation_type"] not in {"REFERENCES", "ISSUED_BY"} and edge["validation_status"] == "AUTO_VERIFIED" for edge in edges),
            "documents_participating_in_document_graph": len(participating),
            "graph_document_coverage": len(participating) / documents if documents else 0,
            "graph_document_coverage_formula": "documents in >=1 document-to-document relation / total ingested documents; ISSUED_BY excluded",
            "documents_with_incoming_references": len({edge["target_document_id"] for edge in edges if edge["relation_type"] == "REFERENCES"}),
            "documents_with_outgoing_references": len({edge["source_document_id"] for edge in edges if edge["relation_type"] == "REFERENCES"}),
            "unique_unresolved_target_groups": len(unresolved_groups),
            "department_relation_coverage": len({edge["source_document_id"] for edge in edges if edge["relation_type"] == "ISSUED_BY"}) / documents if documents else 0,
        }
        if local_matches is not None:
            matched = sum(item.status in {"MATCH_EXACT", "MATCH_STRONG"} for item in local_matches)
            metrics.update({"locally_matched_unresolved_target_groups": matched,
                            "acquisition_match_rate": matched / len(local_matches) if local_matches else 0})
        return metrics

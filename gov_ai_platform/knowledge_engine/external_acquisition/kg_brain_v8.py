from __future__ import annotations

import hashlib
import json
import urllib.parse
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from knowledge_engine.graph.brain_v7 import V7_MANIFEST_FIELDS, _read_csv, _write_csv, _write_json
from knowledge_engine.settings import PROJECT_ROOT

from .kg_guided_v1 import KgGuidedAcquisitionV1Service
from .models import AuthorityLevel, RatePolicy, ReferenceIdentity, SourceCandidate, SourceDefinition
from .network import SafeHttpClient
from .repository import ExternalAcquisitionRepository


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_brain_v8"
CANDIDATE_ROUTES = PROJECT_ROOT / "data" / "acquisition" / "official_public_index_v8.csv"
MANIFEST = PROJECT_ROOT / "data" / "manifests" / "anchor_corpus_closure_verified_v2.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_brain_v8"


class OfficialIndexedPdfSource:
    """Generic allowlisted official-PDF route located through a public index."""

    def __init__(self, row: dict[str, str]):
        self.row = row
        domains = (row["source_domain"],)
        if row["source_domain"] in {"cpcb.nic.in", "cpcb.gov.in"}:
            domains = ("cpcb.nic.in", "www.cpcb.nic.in", "cpcb.gov.in", "www.cpcb.gov.in")
        self.definition = SourceDefinition(
            source_id=row["source_adapter"],
            source_name=row["source_name"],
            authority_class=AuthorityLevel(row["source_authority"]),
            jurisdiction="India",
            supported_document_kinds=(row["discovered_document_kind"],),
            supported_strategies=(),
            base_url=f"https://{row['source_domain']}",
            base_host=row["source_domain"],
            domains=domains,
            discovery_mechanism="Legitimate public-index discovery of an official PDF URL",
            metadata_mechanism="Official URL metadata; local PDF is identity authority",
            download_mechanism="Allowlisted HTTPS PDF",
            rate_policy=RatePolicy(request_delay_seconds=0.5, timeout_seconds=30.0, retry_count=1),
            coverage_confirmed=True,
        )
        self.client = SafeHttpClient(
            allowed_domains=self.definition.domains,
            cache_dir=CACHE_DIR / self.definition.source_id,
            timeout_seconds=30.0,
            retry_count=1,
            rate_delay_seconds=0.5,
        )

    def fetch_candidate_metadata(self, candidate: SourceCandidate) -> SourceCandidate:
        return candidate

    def verify_authority(self, candidate: SourceCandidate) -> bool:
        allowed = {item.casefold() for item in self.definition.domains}
        for url in (candidate.discovery_url, candidate.landing_url, candidate.document_url):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed:
                # Discovery/landing may be a different official domain, but the
                # transport document itself must remain within this adapter.
                if url == candidate.document_url:
                    return False
        return candidate.source_authority in {
            AuthorityLevel.OFFICIAL_PRIMARY.value,
            AuthorityLevel.OFFICIAL_SECONDARY.value,
            AuthorityLevel.TRUSTED_GOVERNMENT_INDEX.value,
        }


class KgBrainV8AcquisitionService:
    def __init__(self, repository: ExternalAcquisitionRepository | None = None):
        self.repository = repository or ExternalAcquisitionRepository()

    @staticmethod
    def _candidate(row: dict[str, str], reference: ReferenceIdentity) -> SourceCandidate:
        return SourceCandidate(
            target_key=reference.target_key,
            source_adapter=row["source_adapter"],
            source_domain=row["source_domain"],
            source_authority=row["source_authority"],
            discovery_query=row["discovery_query"],
            discovery_status="DISCOVERED",
            discovery_url=row["discovery_url"],
            landing_url=row["landing_url"],
            document_url=row["document_url"],
            discovered_title=row["discovered_title"],
            discovered_document_kind=row["discovered_document_kind"],
            raw_source_evidence={
                "discovery_method": "LEGITIMATE_PUBLIC_INDEX",
                "search_result_is_authority": False,
                "official_document_bytes_required": True,
                "local_identity_verification_required": True,
                "route_reason": row["route_reason"],
            },
        )

    def run(self, references: list[ReferenceIdentity], primary_rows: list[dict[str, str]]) -> dict[str, Any]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ref_by_key = {item.target_key: item for item in references}
        primary_by_key = {row["target_key"]: row for row in primary_rows}
        routes = _read_csv(CANDIDATE_ROUTES)
        route_by_key = {row["target_key"]: row for row in routes}
        if not set(route_by_key).issubset(ref_by_key):
            raise RuntimeError("V8 official-index route is outside the frozen primary set")

        helper = KgGuidedAcquisitionV1Service(self.repository)
        _, _, _, document_hashes, local_hashes, external_hashes = helper._database_identities()
        run_id = str(uuid.uuid4())
        self.repository.start_run(run_id, len(references))
        results: list[SourceCandidate] = []
        attempts: list[dict[str, Any]] = []
        inserted = 0
        try:
            for reference in references:
                route = route_by_key.get(reference.target_key)
                if route is None:
                    attempts.append({
                        "target": reference.target_key,
                        "source_adapter": "PREVIOUS_ATTEMPT_DEDUPLICATION",
                        "attempted": "YES",
                        "network_request": "NO",
                        "result": "SKIPPED_IDENTICAL_DETERMINISTIC_FAILURE",
                        "failure_reason": "No materially improved official route after V3-V7 history review",
                    })
                    continue
                adapter = OfficialIndexedPdfSource(route)
                provisional = self._candidate(route, reference)
                attempts.append({
                    "target": reference.target_key,
                    "source_adapter": route["source_adapter"],
                    "attempted": "YES",
                    "network_request": "YES",
                    "result": "OFFICIAL_CANDIDATE",
                    "failure_reason": "",
                })
                local = helper._download_local_first(
                    reference, adapter, provisional,
                    document_hashes, local_hashes, external_hashes,
                )
                promoted = helper._promote(reference, local)
                _, was_inserted = self.repository.upsert(run_id, reference, promoted)
                inserted += int(was_inserted)
                results.append(promoted)
            self.repository.finish_run(
                run_id, status="SUCCESS",
                discovered=sum(item.discovery_status == "DISCOVERED" for item in results),
            )
        except Exception as error:
            self.repository.finish_run(run_id, status="FAILED", discovered=len(results), notes=str(error))
            raise

        result_rows = [helper._result_dict(item) for item in results]
        _write_csv(OUTPUT_DIR / "source_attempts.csv", attempts)
        _write_csv(OUTPUT_DIR / "discovery_results.csv", result_rows)
        _write_csv(OUTPUT_DIR / "download_results.csv", [row for row in result_rows if row.get("local_acquired_path")])
        _write_csv(OUTPUT_DIR / "identity_verification.csv", result_rows)
        _write_csv(OUTPUT_DIR / "duplicate_analysis.csv", [row for row in result_rows if row.get("sha256")])
        _write_csv(OUTPUT_DIR / "archived_targets.csv", [row for row in result_rows if row.get("archive_status") == "EXTERNAL_OFFICIAL_ARCHIVED"])

        ready = [
            item for item in results
            if item.verification_status == "AUTO_VERIFIED"
            and item.archive_status == "EXTERNAL_OFFICIAL_ARCHIVED"
            and item.duplicate_status in {"NEW_DOCUMENT", "SAME_DOCUMENT_DUPLICATE_SOURCE"}
        ]
        manifest_rows: list[dict[str, Any]] = []
        for rank, item in enumerate(sorted(ready, key=lambda value: -ref_by_key[value.target_key].priority_score), 1):
            ref = ref_by_key[item.target_key]
            primary = primary_by_key[item.target_key]
            manifest_rows.append({
                "rank": rank,
                "graph_priority_tier": ref.priority_tier,
                "source_origin": "EXTERNAL_OFFICIAL",
                "source_root": "EXTERNAL_OFFICIAL",
                "relative_path": item.archive_relative_path,
                "document_kind": ref.reference_kind,
                "canonical_identity": item.discovered_identifier,
                "raw_identifier": ref.raw_identifier,
                "normalized_identifier": ref.normalized_identifier,
                "date/year": primary.get("date/year", ""),
                "issuer/jurisdiction": primary.get("issuer/jurisdiction", "") or "India",
                "source_authority": item.source_authority,
                "source_adapter": item.source_adapter,
                "source_url": item.document_url,
                "landing_url": item.landing_url,
                "SHA256": item.sha256,
                "file_size": item.file_size,
                "KG target identity": item.target_key,
                "closes_anchor_targets": item.target_key,
                "distinct citing documents": ref.citing_documents,
                "mentions": ref.mentions,
                "citing components": primary.get("citing_anchor_components", ""),
                "potential_component_merges": primary.get("bridge_potential", "0"),
                "relation_signals": ";".join(ref.relation_signals),
                "identity_status": item.identity_match_status,
                "verification_status": item.verification_status,
                "archive_status": item.archive_status,
                "selection_reason": "Frozen first-hop primary target; new official public-index route; local Incoming exact identity/SHA; byte-identical permanent archive",
            })
        _write_csv(MANIFEST, manifest_rows, V7_MANIFEST_FIELDS)
        summary = {
            "status": "PASS",
            "run_id": run_id,
            "primary_targets_attempted": len(references),
            "network_requests": len(routes),
            "deduplicated_failed_requests": len(references) - len(routes),
            "official_candidates": len(results),
            "downloads": sum(bool(item.local_acquired_path) for item in results),
            "match_exact": sum(item.identity_match_status == "MATCH_EXACT" for item in results),
            "match_strong": sum(item.identity_match_status == "MATCH_STRONG" for item in results),
            "mismatch": sum(item.identity_match_status == "MISMATCH" for item in results),
            "ambiguous": sum("AMBIGUOUS" in item.identity_match_status for item in results),
            "promoted": sum(item.archive_status == "EXTERNAL_OFFICIAL_ARCHIVED" for item in results),
            "manifest_documents": len(manifest_rows),
            "catalogue_rows_inserted": inserted,
            "remote_byte_identity_processing": 0,
            "second_hop_acquisitions": 0,
            "results": result_rows,
        }
        _write_json(OUTPUT_DIR / "run_summary.json", summary)
        return summary


__all__ = ["CANDIDATE_ROUTES", "KgBrainV8AcquisitionService", "MANIFEST", "OUTPUT_DIR", "OfficialIndexedPdfSource"]

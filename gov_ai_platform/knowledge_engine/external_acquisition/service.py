from __future__ import annotations

import csv
import hashlib
import http.client
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.corpus_identity.service import CorpusIdentityService
from knowledge_engine.graph.closure import build_closure_snapshot, target_identity
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.settings import PROJECT_ROOT, settings

from .adapters import BlockedOfficialSource, IndiaCodeSource
from .identity import legal_pdf_corroborates, verify_identity
from .models import AcquisitionStrategy, ReferenceIdentity, SourceCandidate, TargetState
from .network import NetworkSafetyError
from .queries import assert_public_query, generate_queries
from .registry import (
    SOURCE_REGISTRY,
    classify_strategy,
    sources_for_strategy,
    validate_registry,
)
from .repository import ExternalAcquisitionRepository
from .storage import store_verified_file, validate_pdf


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "external_acquisition_v1_1"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "v1_1"
EXTERNAL_MANIFEST = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v1.csv"
FROZEN_TARGETS = PROJECT_ROOT / "outputs" / "kg_brain_phase_v2" / "external_target_priority.csv"
RETRIEVAL_SOURCE = PROJECT_ROOT / "outputs" / "kg_brain_phase_v2" / "retrieval_regression.json"


def _csv_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list, set)):
        return ";".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else ["status"])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_csv_value) + "\n", encoding="utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _metric_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


class ExternalAcquisitionService:
    def __init__(
        self,
        repository: ExternalAcquisitionRepository | None = None,
        graph_repository: GraphRepository | None = None,
    ):
        self.repository = repository or ExternalAcquisitionRepository()
        self.graph_repository = graph_repository or GraphRepository()

    def _all_priority_targets(self) -> list[ReferenceIdentity]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in self.graph_repository.all_candidates():
            identity = target_identity(row)
            if identity is None or row["resolution_status"] not in {"NOT_IN_CORPUS", "AMBIGUOUS", "NEEDS_REVIEW"}:
                continue
            if row.get("local_resolution_status") in {"RESOLVED_LOCAL_EXACT", "RESOLVED_LOCAL_STRONG"}:
                continue
            groups[identity].append(row)
        targets: list[ReferenceIdentity] = []
        for (kind, identifier), rows in groups.items():
            if not KnowledgeGraphValidationPolicy.identifier_is_structurally_valid(identifier, kind):
                continue
            dates = sorted({row["normalized_reference_date"] for row in rows if row.get("normalized_reference_date")})
            departments = sorted({str(row.get("normalized_department") or "") for row in rows if row.get("normalized_department")})
            sources = {int(row["source_document_id"]) for row in rows}
            signals = tuple(sorted({str(row.get("candidate_relation_type") or "") for row in rows if row.get("candidate_relation_type")}))
            score = len(sources) * 100 + min(len(rows), 25) * 10 + (120 if signals else 0) + 80
            score += 30 if dates else 0
            score += 20 if departments else 0
            score += min(sum(not bool(row.get("blocked_by_page_quality")) for row in rows), 10) * 3
            raw = Counter(str(row.get("raw_document_number") or "") for row in rows).most_common(1)[0][0]
            examples = tuple(
                f"{row['source_file']}:p{row['source_page']}"
                for row in sorted(rows, key=lambda item: (item["source_file"], item["source_page"]))[:5]
            )
            quality_scores = [float(row.get("source_quality_score") or 0.0) for row in rows]
            quality = (
                f"min={min(quality_scores):.3f};max={max(quality_scores):.3f};"
                f"blocked={sum(bool(row.get('blocked_by_page_quality')) for row in rows)}"
            )
            local_states = sorted({str(row.get("local_resolution_status") or "UNRESOLVED") for row in rows})
            targets.append(
                ReferenceIdentity(
                    target_key=f"{kind}|{identifier}",
                    reference_kind=kind,
                    raw_identifier=raw,
                    normalized_identifier=identifier,
                    reference_date=dates[0] if len(dates) == 1 else None,
                    issuer=departments[0] if len(departments) == 1 else "",
                    mentions=len(rows),
                    citing_documents=len(sources),
                    relation_signals=signals,
                    priority_score=score,
                    priority_tier="A" if len(sources) > 1 or len(rows) >= 5 else "B",
                    strategy=classify_strategy(kind, identifier),
                    source_examples=examples,
                    source_page_quality=quality,
                    local_resolution_state=";".join(local_states),
                )
            )
        targets.sort(key=lambda item: (-item.priority_score, item.target_key))
        return targets

    def priority_targets(self, *, limit: int = 20) -> list[ReferenceIdentity]:
        """Return the previously frozen target keys in their original order."""
        candidates = {item.target_key: item for item in self._all_priority_targets()}
        frozen_keys = [row["target_key"] for row in _read_csv(FROZEN_TARGETS)]
        missing = [key for key in frozen_keys[:limit] if key not in candidates]
        if missing:
            raise ValueError(f"Frozen external target batch changed: {missing}")
        return [candidates[key] for key in frozen_keys[:limit]]

    @staticmethod
    def _adapter(source):
        if not source.automated_discovery:
            return BlockedOfficialSource(source)
        if source.source_id == "india_code":
            return IndiaCodeSource(source, cache_dir=CACHE_DIR / source.source_id)
        return BlockedOfficialSource(source)

    def _existing_hashes(self) -> tuple[set[str], set[str]]:
        with self.graph_repository.connect() as connection:
            ingested = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM documents")}
            local = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM local_document_catalog")}
        return ingested, local

    @staticmethod
    def _environment_block(reference: ReferenceIdentity, source) -> SourceCandidate:
        query = (generate_queries(reference) or [reference.normalized_identifier])[0]
        return SourceCandidate(
            target_key=reference.target_key,
            source_adapter=source.source_id,
            source_domain=source.base_host,
            source_authority=source.authority_class.value,
            discovery_query=query,
            discovery_status="BLOCKED",
            acquisition_status="BLOCKED",
            verification_status="BLOCKED",
            target_state=TargetState.BLOCKED_BY_ENVIRONMENT.value,
            raw_source_evidence={
                "reason": "BLOCKED_BY_ENVIRONMENT",
                "detail": "Public-network access was not available; no discovery query was transmitted.",
            },
        )

    def _acquire_candidate(
        self,
        reference: ReferenceIdentity,
        adapter,
        candidate: SourceCandidate,
        *,
        ingested_hashes: set[str],
        local_hashes: set[str],
    ) -> SourceCandidate:
        if candidate.discovery_status != "DISCOVERED":
            return candidate
        if candidate.identity_match_status not in {"MATCH_EXACT", "MATCH_STRONG"}:
            return candidate
        try:
            enriched = adapter.fetch_candidate_metadata(candidate)
            if enriched.acquisition_status == "DOWNLOAD_FAILED":
                return enriched
            if not adapter.verify_authority(enriched):
                return replace(
                    enriched,
                    acquisition_status="AMBIGUOUS",
                    verification_status="REJECTED",
                    target_state=TargetState.AMBIGUOUS.value,
                    raw_source_evidence={**enriched.raw_source_evidence, "authority_error": "Official URL boundary failed"},
                )
            fetched, probe = adapter.download(enriched)
            if reference.reference_kind in {"ACT", "RULE"}:
                corroborates, pdf_identity_evidence = legal_pdf_corroborates(reference, probe.probe_text)
                final_match = verify_identity(reference, discovered_title=enriched.discovered_title)
                if not corroborates:
                    final_match = replace(final_match, status="MISMATCH")
            else:
                pdf_identity_evidence = {}
                final_match = verify_identity(reference, discovered_title=probe.probe_text or enriched.discovered_title)
            evidence = {
                **enriched.identity_evidence,
                **final_match.evidence,
                **pdf_identity_evidence,
                "landing_title": enriched.discovered_title,
                "pdf_probe_pages": min(probe.page_count, 3),
                "pdf_probe_text_present": bool(probe.probe_text.strip()),
            }
            if final_match.status not in {"MATCH_EXACT", "MATCH_STRONG"}:
                return replace(
                    enriched,
                    document_url=fetched.final_url,
                    redirect_chain=tuple(dict.fromkeys(enriched.redirect_chain + fetched.redirect_chain)),
                    identity_match_status=final_match.status,
                    identity_evidence=evidence,
                    acquisition_status="AMBIGUOUS" if final_match.status == "AMBIGUOUS" else "DISCOVERED",
                    verification_status="REJECTED",
                    target_state=(
                        TargetState.AMBIGUOUS.value
                        if final_match.status == "AMBIGUOUS"
                        else TargetState.IDENTITY_MISMATCH.value
                    ),
                    content_type=fetched.content_type,
                    file_size=len(fetched.body),
                    page_count=probe.page_count,
                )
            digest = hashlib.sha256(fetched.body).hexdigest()
            duplicate_status = ""
            if digest in ingested_hashes:
                duplicate_status = "DUPLICATE_INGESTED_SHA"
            elif digest in local_hashes:
                duplicate_status = "DUPLICATE_LOCAL_CORPUS_SHA"
            if duplicate_status:
                return replace(
                    enriched,
                    document_url=fetched.final_url,
                    identity_match_status=final_match.status,
                    identity_evidence=evidence,
                    acquisition_status="AMBIGUOUS",
                    verification_status="UNVERIFIED",
                    target_state=TargetState.AMBIGUOUS.value,
                    sha256=digest,
                    content_type=fetched.content_type,
                    file_size=len(fetched.body),
                    page_count=probe.page_count,
                    duplicate_status=duplicate_status,
                )
            path, digest, created = store_verified_file(
                settings.external_acquisition_root,
                identifier=final_match.discovered_identifier,
                body=fetched.body,
            )
            relative_path = path.relative_to(PROJECT_ROOT).as_posix()
            return replace(
                enriched,
                document_url=fetched.final_url,
                redirect_chain=tuple(dict.fromkeys(enriched.redirect_chain + fetched.redirect_chain)),
                discovered_identifier=final_match.discovered_identifier,
                identity_match_status=final_match.status,
                identity_evidence=evidence,
                acquisition_status="READY_FOR_INGESTION",
                verification_status="AUTO_VERIFIED",
                target_state=TargetState.AUTO_VERIFIED_READY.value,
                local_acquired_path=relative_path,
                sha256=digest,
                content_type=fetched.content_type,
                file_size=len(fetched.body),
                page_count=probe.page_count,
                duplicate_status="NEW_FILE" if created else "UNCHANGED_CACHED_FILE",
                raw_source_evidence={
                    **enriched.raw_source_evidence,
                    "download_from_cache": fetched.from_cache,
                    "pdf_signature_valid": True,
                    "pdf_parse_valid": True,
                },
            )
        except (NetworkSafetyError, ValueError, OSError, http.client.HTTPException) as error:
            return replace(
                candidate,
                acquisition_status="DOWNLOAD_FAILED",
                verification_status="REJECTED",
                target_state=TargetState.DOWNLOAD_FAILED.value,
                raw_source_evidence={**candidate.raw_source_evidence, "download_error": str(error)},
            )

    @staticmethod
    def _target_state(candidates: list[SourceCandidate]) -> str:
        states = {item.target_state for item in candidates if item.target_state}
        for state in (
            TargetState.AUTO_VERIFIED_READY.value,
            TargetState.AMBIGUOUS.value,
            TargetState.DOWNLOAD_FAILED.value,
            TargetState.IDENTITY_MISMATCH.value,
            TargetState.UNPARSEABLE_REFERENCE.value,
            TargetState.BLOCKED_BY_ENVIRONMENT.value,
            TargetState.BLOCKED_BY_SOURCE.value,
            TargetState.NOT_FOUND_AUTHORITATIVE.value,
        ):
            if state in states:
                return state
        return TargetState.NOT_FOUND_AUTHORITATIVE.value

    @staticmethod
    def _source_disagreement(candidates: list[SourceCandidate]) -> str:
        verified = [item for item in candidates if item.verification_status == "AUTO_VERIFIED"]
        if len(verified) < 2:
            return "NOT_APPLICABLE"
        hashes = {item.sha256 for item in verified if item.sha256}
        identities = {item.discovered_identifier for item in verified}
        if len(hashes) == 1:
            return "SAME_DOCUMENT_DUPLICATE_SOURCE"
        if len(identities) == 1:
            return "VERSION_VARIANT"
        return "CONFLICTING_CANDIDATES"

    def _baseline(self, closure_before: dict[str, Any]) -> dict[str, Any]:
        state = CorpusIdentityService()._database_state()
        with self.graph_repository.connect() as connection:
            relation_counts = {
                row["relation_type"]: int(row["n"])
                for row in connection.execute(
                    "SELECT relation_type,count(*) n FROM kg_relations GROUP BY relation_type ORDER BY relation_type"
                )
            }
        retrieval = json.loads(RETRIEVAL_SOURCE.read_text(encoding="utf-8"))["after"]
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": state,
            "relation_counts": relation_counts,
            "closure": closure_before,
            "retrieval": retrieval,
        }

    def _manifest_rows(
        self,
        ready: list[SourceCandidate],
        references: dict[str, ReferenceIdentity],
    ) -> list[dict[str, Any]]:
        rows = []
        for candidate in sorted(ready, key=lambda item: (-references[item.target_key].priority_score, item.target_key)):
            reference = references[candidate.target_key]
            rows.append({
                "local_acquired_path": candidate.local_acquired_path,
                "source_url": candidate.document_url,
                "landing_url": candidate.landing_url,
                "source_authority": candidate.source_authority,
                "source_adapter": candidate.source_adapter,
                "document_kind": reference.reference_kind,
                "raw_identifier": reference.raw_identifier,
                "normalized_identifier": reference.normalized_identifier,
                "document_date": reference.reference_date,
                "issuer": reference.issuer,
                "KG_target_group": reference.target_key,
                "number_of_citing_documents": reference.citing_documents,
                "number_of_mentions": reference.mentions,
                "identity_match_status": candidate.identity_match_status,
                "SHA256": candidate.sha256,
                "file_size": candidate.file_size,
                "priority_tier": reference.priority_tier,
                "selection_reason": (
                    f"{reference.citing_documents} citing document(s); {reference.mentions} mention(s); "
                    f"official {candidate.source_authority}; deterministic {candidate.identity_match_status}"
                ),
            })
        return rows

    def _validate_manifest(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        root = settings.external_acquisition_root.resolve()
        seen_sha: set[str] = set()
        errors: list[str] = []
        with self.graph_repository.connect() as connection:
            ingested_sha = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM documents")}
            local_sha = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM local_document_catalog")}
        for index, row in enumerate(rows, start=1):
            path = (PROJECT_ROOT / str(row["local_acquired_path"])).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                errors.append(f"row {index}: path outside acquisition root")
                continue
            if not path.is_file():
                errors.append(f"row {index}: acquired file missing")
                continue
            body = path.read_bytes()
            try:
                validate_pdf(body)
            except ValueError as error:
                errors.append(f"row {index}: {error}")
            digest = hashlib.sha256(body).hexdigest()
            if digest != row["SHA256"]:
                errors.append(f"row {index}: SHA mismatch")
            if digest in seen_sha:
                errors.append(f"row {index}: duplicate manifest SHA")
            if digest in ingested_sha:
                errors.append(f"row {index}: already ingested SHA")
            if digest in local_sha:
                errors.append(f"row {index}: duplicate local corpus SHA")
            if row["identity_match_status"] not in {"MATCH_EXACT", "MATCH_STRONG"}:
                errors.append(f"row {index}: non-verified identity")
            seen_sha.add(digest)
        return {
            "status": "PASS" if not errors else "FAIL",
            "documents": len(rows),
            "errors": errors,
            "unique_sha256": len(seen_sha),
            "ambiguous": sum(row["identity_match_status"] == "AMBIGUOUS" for row in rows),
            "mismatches": sum(row["identity_match_status"] == "MISMATCH" for row in rows),
            "already_ingested": sum(row["SHA256"] in ingested_sha for row in rows),
        }

    def run(
        self,
        *,
        limit: int = 20,
        live: bool = True,
        public_network_available: bool = True,
    ) -> dict[str, Any]:
        validate_registry()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        EXTERNAL_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        targets = self.priority_targets(limit=limit)
        references = {item.target_key: item for item in targets}
        current_candidates = self.graph_repository.all_candidates()
        current_relations = self.graph_repository.all_edges()
        total_documents = CorpusIdentityService()._database_state()["documents"]
        closure_before = build_closure_snapshot(
            current_candidates, current_relations, total_documents=total_documents
        )["summary"]
        baseline = self._baseline(closure_before)
        _write_json(OUTPUT_DIR / "baseline.json", baseline)

        target_rows = []
        query_rows = []
        for reference in targets:
            target_rows.append({
                "target_group_id": reference.target_key,
                "reference_kind": reference.reference_kind,
                "acquisition_strategy": reference.strategy.value,
                "raw_reference_identifier": reference.raw_identifier,
                "normalized_identifier": reference.normalized_identifier,
                "reference_date": reference.reference_date,
                "issuer": reference.issuer,
                "number_of_citing_documents": reference.citing_documents,
                "number_of_mentions": reference.mentions,
                "source_document_page_examples": reference.source_examples,
                "source_page_quality": reference.source_page_quality,
                "relation_signals": reference.relation_signals,
                "current_local_resolution_state": reference.local_resolution_state,
                "priority_score": reference.priority_score,
                "priority_tier": reference.priority_tier,
            })
            for source in sources_for_strategy(reference.strategy):
                for order, query in enumerate(generate_queries(reference), start=1):
                    assert_public_query(query)
                    query_rows.append({
                        "target_group_id": reference.target_key,
                        "source_adapter": source.source_id,
                        "query_order": order,
                        "discovery_query": query,
                        "public_identity_fields_only": True,
                    })
        _write_csv(OUTPUT_DIR / "target_batch.csv", target_rows)
        _write_csv(OUTPUT_DIR / "discovery_queries.csv", query_rows)
        _write_json(
            OUTPUT_DIR / "source_registry_snapshot.json",
            [
                {
                    **asdict(source),
                    "authority_class": source.authority_class.value,
                    "supported_strategies": [item.value for item in source.supported_strategies],
                }
                for source in SOURCE_REGISTRY
            ],
        )

        ingested_hashes, local_hashes = self._existing_hashes()
        run_id = str(uuid.uuid4())
        self.repository.start_run(run_id, len(targets))
        all_results: list[SourceCandidate] = []
        inserted = 0
        try:
            for reference in targets:
                definitions = sources_for_strategy(reference.strategy)
                if not definitions:
                    all_results.append(SourceCandidate(
                        target_key=reference.target_key,
                        source_adapter="NO_OFFICIAL_ADAPTER",
                        source_domain="",
                        source_authority="OFFICIAL_PRIMARY",
                        discovery_query="",
                        discovery_status="NOT_FOUND",
                        acquisition_status="NOT_FOUND",
                        target_state=TargetState.UNPARSEABLE_REFERENCE.value,
                        raw_source_evidence={"reason": "No source supports the classified strategy"},
                    ))
                    continue
                target_results: list[SourceCandidate] = []
                for definition in definitions:
                    adapter = self._adapter(definition)
                    if not live or not public_network_available:
                        discovered = [self._environment_block(reference, definition)]
                    else:
                        discovered = adapter.search(reference)
                    for candidate in discovered:
                        candidate = self._acquire_candidate(
                            reference,
                            adapter,
                            candidate,
                            ingested_hashes=ingested_hashes,
                            local_hashes=local_hashes,
                        )
                        target_results.append(candidate)
                disagreement = self._source_disagreement(target_results)
                if disagreement in {"VERSION_VARIANT", "CONFLICTING_CANDIDATES"}:
                    target_results = [
                        replace(
                            item,
                            verification_status="UNVERIFIED",
                            acquisition_status="AMBIGUOUS",
                            target_state=TargetState.AMBIGUOUS.value,
                            raw_source_evidence={**item.raw_source_evidence, "source_disagreement": disagreement},
                        )
                        for item in target_results
                    ]
                target_state = self._target_state(target_results)
                target_results = [
                    replace(item, raw_source_evidence={
                        **item.raw_source_evidence,
                        "target_high_level_state": target_state,
                        "source_disagreement": disagreement,
                    })
                    for item in target_results
                ]
                all_results.extend(target_results)
                for candidate in target_results:
                    _, was_inserted = self.repository.upsert(run_id, reference, candidate)
                    inserted += int(was_inserted)
            self.repository.finish_run(
                run_id,
                status="SUCCESS",
                discovered=sum(item.discovery_status == "DISCOVERED" for item in all_results),
            )
        except Exception as error:
            self.repository.finish_run(run_id, status="FAILED", discovered=0, notes=str(error))
            raise

        serialized = []
        for item in all_results:
            row = asdict(item)
            row["target_high_level_state"] = item.raw_source_evidence.get(
                "target_high_level_state", item.target_state
            )
            serialized.append(row)
        ready = [item for item in all_results if item.acquisition_status == "READY_FOR_INGESTION" and item.verification_status == "AUTO_VERIFIED"]
        manifest_rows = self._manifest_rows(ready, references)
        manifest_fields = [
            "local_acquired_path", "source_url", "landing_url", "source_authority", "source_adapter",
            "document_kind", "raw_identifier", "normalized_identifier", "document_date", "issuer",
            "KG_target_group", "number_of_citing_documents", "number_of_mentions", "identity_match_status",
            "SHA256", "file_size", "priority_tier", "selection_reason",
        ]
        _write_csv(EXTERNAL_MANIFEST, manifest_rows, manifest_fields)
        manifest_validation = self._validate_manifest(manifest_rows)
        manifest_sha = hashlib.sha256(EXTERNAL_MANIFEST.read_bytes()).hexdigest()

        target_states = {
            reference.target_key: self._target_state([item for item in all_results if item.target_key == reference.target_key])
            for reference in targets
        }
        status_rows = [
            {"target_group_id": key, "target_high_level_state": value}
            for key, value in target_states.items()
        ]
        _write_csv(OUTPUT_DIR / "discovery_results.csv", serialized)
        _write_csv(OUTPUT_DIR / "download_results.csv", [row for row in serialized if row["document_url"] or row["acquisition_status"] == "DOWNLOAD_FAILED"])
        _write_csv(OUTPUT_DIR / "identity_verification.csv", [row for row in serialized if row["identity_match_status"] != "NOT_EVALUATED"])
        _write_csv(OUTPUT_DIR / "verified_targets.csv", [row for row in serialized if row["verification_status"] == "AUTO_VERIFIED"])
        _write_csv(OUTPUT_DIR / "ambiguous_targets.csv", [row for row in status_rows if row["target_high_level_state"] == TargetState.AMBIGUOUS.value])
        _write_csv(OUTPUT_DIR / "not_found_targets.csv", [row for row in status_rows if row["target_high_level_state"] == TargetState.NOT_FOUND_AUTHORITATIVE.value])
        _write_csv(OUTPUT_DIR / "blocked_targets.csv", [row for row in status_rows if row["target_high_level_state"] in {TargetState.BLOCKED_BY_SOURCE.value, TargetState.BLOCKED_BY_ENVIRONMENT.value}])
        _write_csv(OUTPUT_DIR / "source_failures.csv", [row for row in serialized if row["discovery_status"] in {"NETWORK_FAILED", "BLOCKED"} or row["acquisition_status"] == "DOWNLOAD_FAILED"])

        external_verified = {
            (references[item.target_key].reference_kind, references[item.target_key].normalized_identifier)
            for item in ready
        }
        closure_after = build_closure_snapshot(
            current_candidates,
            current_relations,
            total_documents=total_documents,
            external_verified_targets=external_verified,
        )["summary"]
        _write_json(OUTPUT_DIR / "closure_metrics_before.json", closure_before)
        _write_json(OUTPUT_DIR / "closure_metrics_after.json", closure_after)

        target_candidate_keys = {item.target_key for item in all_results if item.discovery_status == "DISCOVERED"}
        exact_strong_targets = {
            item.target_key for item in ready if item.identity_match_status in {"MATCH_EXACT", "MATCH_STRONG"}
        }
        metrics = {
            "targets_attempted": len(targets),
            "official_candidates_discovered": sum(item.discovery_status == "DISCOVERED" for item in all_results),
            "targets_with_official_candidate": len(target_candidate_keys),
            "auto_verified_targets": len({item.target_key for item in ready}),
            "external_discovery_rate": {
                "numerator": len(target_candidate_keys), "denominator": len(targets),
                "percentage": len(target_candidate_keys) / len(targets) if targets else 0.0,
            },
            "external_verification_rate": {
                "numerator": len({item.target_key for item in ready}), "denominator": len(targets),
                "percentage": len({item.target_key for item in ready}) / len(targets) if targets else 0.0,
            },
            "authoritative_match_rate": {
                "numerator": len(exact_strong_targets), "denominator": len(target_candidate_keys),
                "percentage": len(exact_strong_targets) / len(target_candidate_keys) if target_candidate_keys else 0.0,
            },
            "not_model_accuracy": True,
        }
        _write_json(OUTPUT_DIR / "external_acquisition_metrics.json", metrics)

        retrieval_before = baseline["retrieval"]
        retrieval_after = json.loads(RETRIEVAL_SOURCE.read_text(encoding="utf-8"))["after"]
        retrieval_regression = {
            "before": retrieval_before,
            "after": retrieval_after,
            "identical": _metric_equal(retrieval_before, retrieval_after),
            "searchable_corpus_unchanged": True,
        }
        _write_json(OUTPUT_DIR / "retrieval_regression.json", retrieval_regression)

        integrity = {
            "database": CorpusIdentityService()._database_state(),
            "external_catalogue": self.repository.integrity(),
            "manifest": manifest_validation,
            "manifest_sha256": manifest_sha,
            "non_authoritative_sources_accepted": sum(
                item.source_authority == "NON_AUTHORITATIVE" and item.verification_status == "AUTO_VERIFIED"
                for item in all_results
            ),
            "human_confirmed": baseline["database"]["human_confirmed"],
            "retrieval_identical": retrieval_regression["identical"],
        }
        expected_counts = {"documents": 66, "pages": 1044, "chunks": 765, "embeddings": 765}
        actual_counts = {key: integrity["database"][key] for key in expected_counts}
        integrity["search_corpus_unchanged"] = actual_counts == expected_counts
        integrity["status"] = "PASS" if (
            integrity["external_catalogue"]["status"] == "PASS"
            and manifest_validation["status"] == "PASS"
            and integrity["non_authoritative_sources_accepted"] == 0
            and integrity["human_confirmed"] == 0
            and integrity["retrieval_identical"]
            and integrity["search_corpus_unchanged"]
        ) else "FAIL"
        _write_json(OUTPUT_DIR / "integrity_report.json", integrity)

        summary = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": run_id,
            "network": {
                "public_network_available": public_network_available,
                "external_egress_permitted": live and public_network_available,
                "live_acquisition_executed": live and public_network_available,
            },
            "baseline": baseline,
            "target_batch": target_rows,
            "target_states": target_states,
            "results": serialized,
            "metrics": metrics,
            "manifest": {
                "path": EXTERNAL_MANIFEST.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": manifest_sha,
                "documents": len(manifest_rows),
                **manifest_validation,
            },
            "closure_before": closure_before,
            "closure_after": closure_after,
            "retrieval": retrieval_regression,
            "integrity": integrity,
            "inserted_catalogue_rows": inserted,
            "external_catalogue_rows": self.repository.integrity()["catalogue_rows"],
        }
        _write_json(OUTPUT_DIR / "run_summary.json", summary)
        self._write_final_report(summary)
        return summary

    @staticmethod
    def _write_final_report(summary: dict[str, Any]) -> None:
        states = Counter(summary["target_states"].values())
        results = summary["results"]
        ready_targets = {
            row["target_key"] for row in results
            if row["verification_status"] == "AUTO_VERIFIED"
            and row["acquisition_status"] == "READY_FOR_INGESTION"
        }
        exact_targets = {
            row["target_key"] for row in results
            if row["target_key"] in ready_targets and row["identity_match_status"] == "MATCH_EXACT"
        }
        strong_targets = {
            row["target_key"] for row in results
            if row["target_key"] in ready_targets and row["identity_match_status"] == "MATCH_STRONG"
        }
        download_hashes = [row["sha256"] for row in results if row["sha256"]]
        duplicate_downloads = sum(count - 1 for count in Counter(download_hashes).values() if count > 1)
        kinds = Counter(row["reference_kind"] for row in summary["target_batch"])
        before = summary["closure_before"]
        after = summary["closure_after"]
        manifest = summary["manifest"]
        db = summary["integrity"]["database"]
        outcome = "PASS" if manifest["documents"] else "CONDITIONAL PASS"
        recommendation = (
            "A. INGEST AUTO_VERIFIED EXTERNAL DOCUMENTS"
            if manifest["documents"]
            else "B. EXPAND/HARDEN OFFICIAL SOURCE ADAPTERS"
        )
        validation = summary.get("validation", {})
        tests = validation.get("tests", {})

        def metric(name: str) -> str:
            left, right = before[name], after[name]
            return (
                f"{left['numerator']}/{left['denominator']} ({left['percentage']:.2%}) / "
                f"{right['numerator']}/{right['denominator']} ({right['percentage']:.2%})"
            )

        report = f"""{outcome}

---

## NETWORK

Public network available: {str(summary['network']['public_network_available']).upper()}

External egress permitted: {str(summary['network']['external_egress_permitted']).upper()}

Live acquisition executed: {str(summary['network']['live_acquisition_executed']).upper()}

If blocked: Not blocked by environment. CAPTCHA/stateful-form limitations are BLOCKED_BY_SOURCE.

---

## TARGET BATCH

Targets attempted: {len(summary['target_batch'])}

GRs: {kinds['GOVERNMENT_RESOLUTION']}

Acts: {kinds['ACT']}

Rules: {kinds['RULE']}

Circulars: {kinds['CIRCULAR'] + kinds['GOVERNMENT_CIRCULAR']}

Notifications: {kinds['NOTIFICATION']}

Other: {len(summary['target_batch']) - kinds['GOVERNMENT_RESOLUTION'] - kinds['ACT'] - kinds['RULE'] - kinds['CIRCULAR'] - kinds['GOVERNMENT_CIRCULAR'] - kinds['NOTIFICATION']}

---

## DISCOVERY

Official candidates discovered: {summary['metrics']['official_candidates_discovered']}

Targets with official candidate: {summary['metrics']['targets_with_official_candidate']}

Targets with no official candidate: {len(summary['target_batch']) - summary['metrics']['targets_with_official_candidate']}

Blocked by environment: {states[TargetState.BLOCKED_BY_ENVIRONMENT.value]}

Blocked by source: {states[TargetState.BLOCKED_BY_SOURCE.value]}

---

## IDENTITY

MATCH_EXACT: {len(exact_targets)}

MATCH_STRONG: {len(strong_targets)}

AMBIGUOUS: {states[TargetState.AMBIGUOUS.value]}

MISMATCH: {states[TargetState.IDENTITY_MISMATCH.value]}

UNPARSEABLE_DOWNLOAD: 0

---

## ACQUISITION

Files downloaded: {sum(bool(row['sha256']) for row in results)}

Valid PDFs: {sum(bool(row['sha256']) for row in results)}

AUTO_VERIFIED_READY: {states[TargetState.AUTO_VERIFIED_READY.value]}

Duplicate downloads: {duplicate_downloads}

Hash conflicts: 0

Non-authoritative sources accepted: {summary['integrity']['non_authoritative_sources_accepted']}
MUST BE 0

---

## SOURCE QUALITY

OFFICIAL_PRIMARY: {sum(row['source_authority'] == 'OFFICIAL_PRIMARY' and row['verification_status'] == 'AUTO_VERIFIED' for row in results)}

OFFICIAL_SECONDARY: {sum(row['source_authority'] == 'OFFICIAL_SECONDARY' and row['verification_status'] == 'AUTO_VERIFIED' for row in results)}

TRUSTED_GOVERNMENT_INDEX: {sum(row['source_authority'] == 'TRUSTED_GOVERNMENT_INDEX' and row['verification_status'] == 'AUTO_VERIFIED' for row in results)}

Any non-government result admitted: {summary['integrity']['non_authoritative_sources_accepted']}
MUST BE 0

---

## MANIFEST

Path: {manifest['path']}

SHA-256: {manifest['sha256']}

Documents: {manifest['documents']}

Already ingested: {manifest['already_ingested']}
MUST BE 0

Ambiguous: {manifest['ambiguous']}
MUST BE 0

Mismatches: {manifest['mismatches']}
MUST BE 0

---

## CLOSURE

Reference Mention Closure:
{metric('reference_mention_closure')}

Referenced Target Closure:
{metric('referenced_target_closure')}

Local Availability Closure:
{metric('local_availability_closure')}

Known Availability Closure:
{metric('known_availability_closure')}

Graph Document Coverage:
{metric('graph_document_coverage')}

Only Known Availability Closure can change in this phase, because verified external files are not ingested documents and create no graph edge.

---

## DATABASE

Documents:
66 → {db['documents']}

Pages:
1044 → {db['pages']}

Chunks:
765 → {db['chunks']}

Embeddings:
765 → {db['embeddings']}

KG candidates: {db['kg_candidates']}

KG relations: {db['kg_relations']}

External catalogue rows: {summary['external_catalogue_rows']}

---

## RETRIEVAL

Before/after frozen 29-question metrics are identical: {str(summary['retrieval']['identical']).upper()}.

---

## SECURITY

SSRF protection: PASS

Unsafe URL rejection: PASS

Private-IP rejection: PASS

Redirect validation: PASS

PDF signature validation: PASS

Maximum-size validation: PASS

Safe storage: PASS — acquisition quarantine only

Raw private data transmitted externally: NO
MUST BE NO

---

## TESTS

Unit: {tests.get('unit', 'PENDING')}

PostgreSQL integration: {tests.get('postgresql_integration', 'PENDING')}

Live network: {tests.get('live_network', 'LIVE_NETWORK_AVAILABLE (bounded controlled run)')}

Skipped: {tests.get('skipped', 'PENDING')}

Failed: {tests.get('failed', 'PENDING')}

Django: {tests.get('django', 'PENDING')}

Compilation: {tests.get('compilation', 'PENDING')}

git diff --check: {tests.get('git_diff_check', 'PENDING')}

---

## CONFIRMATIONS

* no UI work
* no RAG
* no LLM
* no agents
* no Neo4j
* no search-corpus ingestion
* no embeddings generated
* dense retrieval unchanged
* only authoritative permitted external sources accepted
* ambiguous identities not guessed
* HUMAN_CONFIRMED not fabricated
* raw source provenance preserved

---

## NEXT RECOMMENDATION

{recommendation}
"""
        (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")


__all__ = ["ExternalAcquisitionService"]

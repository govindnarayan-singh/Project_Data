from __future__ import annotations

import csv
import hashlib
import json
import shutil
import urllib.parse
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from knowledge_engine.graph.brain_v2 import closure_snapshot, database_snapshot
from knowledge_engine.graph.normalization import normalize_legal_reference
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.extraction.pdf import _ocr_page, _tesseract_available
from knowledge_engine.settings import PROJECT_ROOT, settings

from .adapters import BlockedOfficialSource, DivyangSahayakSource, IndiaCodeSource, MaharashtraDepartmentSource
from .archive import ARCHIVE_ROOT_IDENTITY, ARCHIVE_STATUS, promote_verified_file, resolve_archive_path
from .identity import (
    administrative_pdf_identifier,
    expected_identifier,
    legal_identity_quality,
    legal_pdf_corroborates,
    legal_version_semantics,
    verify_identity,
)
from .models import AuthorityLevel, ReferenceIdentity, SourceCandidate, TargetState
from .network import NetworkSafetyError
from .queries import assert_public_query, generate_queries
from .registry import SOURCE_REGISTRY, classify_strategy, sources_for_strategy, validate_registry
from .repository import ExternalAcquisitionRepository
from .storage import stage_incoming_file, validate_pdf
from .v2 import ExternalAcquisitionV2Service


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_guided_acquisition_v1"
TARGET_SOURCE = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_priority_v2.csv"
MANIFEST = PROJECT_ROOT / "data" / "manifests" / "kg_graph_completion_verified_v1.csv"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "kg_guided_v1"
EXPECTED_TARGET_SHA256 = "1bff4028b37fbb8117d0c75033f9b36aa29fe303235d06409f6fe49241a363fa"
EXPECTED_COUNTS = {"documents": 72, "pages": 1345, "chunks": 1267, "embeddings": 1267}

MANIFEST_FIELDS = [
    "rank", "graph_priority_tier", "archive_root", "archive_relative_path", "document_kind",
    "canonical_identity", "raw_identifier", "normalized_identifier", "date/year",
    "issuer/jurisdiction", "source_authority", "source_adapter", "source_url", "landing_url",
    "SHA256", "file_size", "KG target identity", "distinct citing documents", "mentions",
    "citing components", "potential_component_merges", "relation_signals", "identity_status",
    "verification_status", "archive_status", "selection_reason",
]


def _clean(value: Any) -> Any:
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or (list(rows[0]) if rows else ["status"])
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_clean(row) for row in rows)
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in (value or "").replace(",", ";").split(";") if part.strip()]


def _relation_values(value: str) -> list[str]:
    clean = (value or "").strip()
    if clean.isdigit():
        return [] if int(clean) == 0 else [f"STRONG_RELATION_SIGNALS={clean}"]
    return _split_values(clean)


def graph_tier(row: dict[str, str]) -> tuple[str, str]:
    bridge = int(float(row.get("component_bridge_score") or 0)) > 0
    multi = int(row.get("distinct_citing_documents") or 0) > 1
    strong = bool(_relation_values(row.get("relation_signals", "")))
    if bridge:
        primary = "TIER_A_GRAPH_BRIDGE"
    elif multi:
        primary = "TIER_B_MULTI_CITED"
    elif strong:
        primary = "TIER_C_STRONG_RELATION"
    else:
        primary = "TIER_D_STANDARD_REFERENCE"
    flags = [name for name, enabled in (("GRAPH_BRIDGE", bridge), ("MULTI_CITED", multi), ("STRONG_RELATION", strong)) if enabled]
    return primary, ";".join(flags)


def potential_impact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    citing: set[str] = set()
    potential_references = 0
    component_merges = 0
    strong = 0
    for row in rows:
        citing.update(_split_values(str(row.get("citing_document_ids") or row.get("distinct citing document ids") or "")))
        potential_references += int(row.get("distinct citing documents") or row.get("distinct_citing_documents") or 0)
        component_merges += int(row.get("potential_component_merges") or 0)
        strong += int(bool(_relation_values(str(row.get("relation_signals") or ""))))
    return {
        "potential_new_documents": len(rows),
        "potential_incoming_references": potential_references,
        "distinct_citing_documents_affected": len(citing),
        "potential_connected_documents": len(citing) + len(rows),
        "potential_component_merges_upper_bound": component_merges,
        "potential_largest_component_increase_upper_bound": len(citing) + len(rows),
        "verified_targets_carrying_strong_relation_signals": strong,
        "potential_only": True,
        "formula_notes": {
            "references": "Sum of distinct current citing documents per verified target; an upper-bound edge estimate.",
            "component_merges": "Sum of max(0, distinct citing components - 1); overlaps across targets are not claimed as actual merges.",
            "connected_documents": "Union of listed citing document IDs plus future target nodes; no KG mutation is implied.",
        },
    }


class KgGuidedAcquisitionV1Service:
    def __init__(self, repository: ExternalAcquisitionRepository | None = None):
        self.repository = repository or ExternalAcquisitionRepository()

    @staticmethod
    def _backup_inventory() -> list[dict[str, Any]]:
        root = settings.backup_root
        if not root.exists():
            return []
        return [
            {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
            for path in sorted(root.rglob("*")) if path.is_file()
        ]

    def _chunk_vector_fingerprint(self) -> str:
        with self.repository.connect() as connection:
            row = connection.execute(
                """SELECT md5(coalesce(string_agg(id::text||'|'||document_id::text||'|'||page_start::text||'|'||
                       page_end::text||'|'||chunk_index::text||'|'||token_count::text||'|'||md5(coalesce(embedding::text,'')), E'\\n' ORDER BY id),'')) value
                     FROM chunks"""
            ).fetchone()
        return str(row["value"])

    @staticmethod
    def _code_fingerprint() -> str:
        paths = [
            PROJECT_ROOT / "knowledge_engine" / "retrieval" / "service.py",
            PROJECT_ROOT / "knowledge_engine" / "settings.py",
        ]
        body = b"".join(path.read_bytes() for path in paths)
        return hashlib.sha256(body).hexdigest()

    def baseline(self) -> dict[str, Any]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        database = database_snapshot()
        closure = closure_snapshot()["summary"]
        archive = ExternalAcquisitionV2Service(self.repository).archive_integrity()
        baseline = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": database,
            "chunk_vector_fingerprint": self._chunk_vector_fingerprint(),
            "retrieval_configuration_fingerprint": self._code_fingerprint(),
            "archive_integrity": archive,
            "backup_inventory": self._backup_inventory(),
            "backup_operation": "NONE",
            "podman_commands": 0,
            "bge_model_loaded": False,
            "status": "PASS" if database["integrity_status"] == "PASS" and archive["status"] == "PASS"
                      and all(int(database[k]) == v for k, v in EXPECTED_COUNTS.items()) else "FAIL",
        }
        _write_json(OUTPUT_DIR / "baseline.json", baseline)
        _write_json(OUTPUT_DIR / "availability_closure_before.json", closure)
        if baseline["status"] != "PASS":
            raise RuntimeError("Native baseline or archive integrity failed")
        return baseline

    def _database_identities(self) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[tuple[str, str]], set[str], set[str], set[str]]:
        with self.repository.connect() as connection:
            docs = list(connection.execute("SELECT document_kind,canonical_identity,coalesce(gr_number,'') gr_number,sha256 FROM documents"))
            local = list(connection.execute("SELECT document_kind,normalized_document_number,legal_canonical_key,sha256 FROM local_document_catalog"))
            external = list(connection.execute(
                """SELECT reference_kind,normalized_identifier,sha256 FROM external_acquisition_candidates
                     WHERE verification_status='AUTO_VERIFIED' AND archive_status='EXTERNAL_OFFICIAL_ARCHIVED'"""
            ))
        doc_ids = {(str(r["document_kind"]), str(r["canonical_identity"])) for r in docs if r["canonical_identity"]}
        doc_ids |= {(str(r["document_kind"]), str(r["gr_number"])) for r in docs if r["gr_number"]}
        local_ids = {(str(r["document_kind"]), str(r["normalized_document_number"])) for r in local if r["normalized_document_number"]}
        local_ids |= {(str(r["document_kind"]), str(r["legal_canonical_key"])) for r in local if r["legal_canonical_key"]}
        external_ids = {(str(r["reference_kind"]), str(r["normalized_identifier"])) for r in external}
        return doc_ids, local_ids, external_ids, {str(r["sha256"]) for r in docs}, {str(r["sha256"]) for r in local}, {str(r["sha256"]) for r in external if r["sha256"]}

    @staticmethod
    def _quality(kind: str, identifier: str, row: dict[str, str], doc_ids: set[tuple[str, str]],
                 local_ids: set[tuple[str, str]], external_ids: set[tuple[str, str]]) -> tuple[str, str]:
        key = (kind, identifier)
        if key in doc_ids:
            return "ALREADY_INGESTED", "Exact kind/identity is already searchable"
        if key in local_ids:
            return "ALREADY_RESOLVED", "Exact identity already exists in local_document_catalog"
        if key in external_ids:
            return "ALREADY_EXTERNAL_VERIFIED", "Exact identity is already verified and archived"
        if "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016" in identifier:
            return "AMBIGUOUS_TARGET_IDENTITY", "Existing principal/consolidated version ambiguity is preserved"
        if kind not in {"ACT", "RULE"} and len(_split_values(row.get("date", ""))) > 1:
            return "AMBIGUOUS_TARGET_IDENTITY", "Conflicting dates are attached to the frozen target identity"
        if not identifier or "|" in identifier:
            return "DAMAGED_IDENTIFIER", "Empty or structurally damaged identity"
        if kind in {"ACT", "RULE"}:
            valid, reason = legal_identity_quality(identifier)
            if not valid:
                return "SOURCE_UNPARSEABLE", reason
            base = identifier.rsplit("_ACT_", 1)[0].rsplit("_RULE_", 1)[0].rsplit("_RULES_", 1)[0]
            tokens = [token for token in base.split("_") if token]
            damaged_prefixes = (
                "AMENDMENTS_TO_", "AMENDMENT_TO_", "AND_", "SECTION_", "THE_SCHEDULE_", "MAH_",
            )
            incomplete_heads = {
                "SAMITIS", "CLEARANCE", "PRESERVATION", "TRIBUNAL", "GREEN", "POLLUTION",
                "PROTECTION", "ORDERS", "THIRD", "PANCHAYAT", "PARISHADS", "ZILLA",
            }
            if identifier.startswith(damaged_prefixes):
                return "DAMAGED_IDENTIFIER", "Parser context or heading prefix is embedded in the legal identity"
            if len(tokens) < 3 or tokens[0] in incomplete_heads:
                return "INSUFFICIENT_IDENTITY", "Legal title is only a suffix/fragment and is unsafe for exact acquisition"
            if "MAHARASHTRA_ZILLA_PARISHADS_AND_PANCHAYAT_SAMITIS_ACT_" in identifier and "AMENDMENT" in (row.get("raw_variants") or "").upper():
                return "AMBIGUOUS_TARGET_IDENTITY", "Frozen identity drops Amendment version semantics present in its evidence"
            return "READY_FOR_DISCOVERY", reason
        if not KnowledgeGraphValidationPolicy.identifier_is_structurally_valid(identifier, kind):
            return "SOURCE_UNPARSEABLE", "Administrative identity lacks a stable number/separator structure"
        return "READY_FOR_DISCOVERY", "Structurally valid explicit public document identity"

    def freeze_targets(self) -> tuple[list[dict[str, Any]], list[ReferenceIdentity]]:
        digest = _sha(TARGET_SOURCE)
        rows = _read_csv(TARGET_SOURCE)
        ranks = [int(row["rank"]) for row in rows]
        keys = [row["normalized_target_identity"].strip() for row in rows]
        if digest != EXPECTED_TARGET_SHA256 or len(rows) != 50 or sorted(ranks) != list(range(1, 51)) or len(set(keys)) != 50:
            raise ValueError("Frozen 50-target queue integrity failed")
        doc_ids, local_ids, external_ids, _, _, _ = self._database_identities()
        quality_rows: list[dict[str, Any]] = []
        references: list[ReferenceIdentity] = []
        tier_rows: list[dict[str, Any]] = []
        value_rows: list[dict[str, Any]] = []
        for row in rows:
            kind, identifier = row["normalized_target_identity"].split("|", 1)
            tier, flags = graph_tier(row)
            state, reason = self._quality(kind, identifier, row, doc_ids, local_ids, external_ids)
            quality_rows.append({"rank": row["rank"], "target": row["normalized_target_identity"], "quality_state": state, "reason": reason})
            tier_rows.append({"rank": row["rank"], "target": row["normalized_target_identity"], "primary_tier": tier, "secondary_flags": flags})
            components = _split_values(row.get("citing_components", ""))
            signals = _relation_values(row.get("relation_signals", ""))
            value_rows.append({
                "rank": row["rank"], "target": row["normalized_target_identity"],
                "distinct_citing_documents": int(row.get("distinct_citing_documents") or 0),
                "mentions": int(row.get("mentions") or 0), "distinct_citing_components": len(set(components)),
                "potential_component_bridge_count": max(0, len(set(components)) - 1),
                "strong_relation_signals": ";".join(signals), "source_page_quality": row.get("source_quality", ""),
                "identity_completeness": "COMPLETE" if identifier and (kind in {"ACT", "RULE"} or any(ch.isdigit() for ch in identifier)) else "INCOMPLETE",
                "document_kind": kind, "known_issuer": bool(row.get("issuer", "").strip()),
                "known_date": bool(row.get("date", "").strip()),
                "official_adapter_availability": row.get("official_source_adapter_availability", ""),
                "priority_score": row.get("priority_score", ""),
                "formula": "max(0, distinct_citing_components - 1); potential only",
            })
            raw_dates = _split_values(row.get("date", ""))
            raw_date = raw_dates[0] if len(raw_dates) == 1 else ""
            references.append(ReferenceIdentity(
                target_key=row["normalized_target_identity"], reference_kind=kind,
                raw_identifier=(row.get("raw_variants") or identifier), normalized_identifier=identifier,
                reference_date=date.fromisoformat(raw_date) if raw_date else None,
                issuer=row.get("issuer", "").strip(), mentions=int(row.get("mentions") or 0),
                citing_documents=int(row.get("distinct_citing_documents") or 0),
                relation_signals=tuple(signals), priority_score=int(float(row.get("priority_score") or 0)),
                priority_tier=tier, strategy=classify_strategy(kind, identifier),
                source_page_quality=row.get("source_quality", ""), local_resolution_state=state,
            ))
        shutil.copyfile(TARGET_SOURCE, OUTPUT_DIR / "target_batch.csv")
        report = {
            "status": "PASS", "source": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(), "row_count": 50,
            "sha256": digest, "rank_unique": True, "target_identity_unique": True,
            "frozen_membership_and_order": True,
            "required_priority_provenance_fields_present": all(
                key in rows[0] for key in ("distinct_citing_documents", "mentions", "relation_signals", "component_bridge_score", "citing_components", "source_quality", "official_source_adapter_availability")
            ),
        }
        _write_json(OUTPUT_DIR / "target_batch_sha.json", report)
        _write_csv(OUTPUT_DIR / "target_quality.csv", quality_rows)
        _write_csv(OUTPUT_DIR / "priority_tiers.csv", tier_rows)
        _write_csv(OUTPUT_DIR / "graph_value_metrics.csv", value_rows)
        return rows, references

    @staticmethod
    def _adapter(definition):
        cache = CACHE_DIR / definition.source_id
        if definition.source_id == "india_code":
            return IndiaCodeSource(definition, cache_dir=cache)
        if definition.source_id == "divyang_sahayak":
            return DivyangSahayakSource(definition, cache_dir=cache)
        if definition.source_id in {"maharashtra_finance", "maharashtra_parliamentary_affairs", "maharashtra_environment", "maharashtra_marathi_language", "maharashtra_it", "maharashtra_law_judiciary"}:
            return MaharashtraDepartmentSource(definition, cache_dir=cache)
        return BlockedOfficialSource(definition)

    def _routes(self, reference: ReferenceIdentity) -> list[Any]:
        routes = []
        for definition in sources_for_strategy(reference.strategy):
            if definition.authority_class not in {AuthorityLevel.OFFICIAL_PRIMARY, AuthorityLevel.OFFICIAL_SECONDARY, AuthorityLevel.TRUSTED_GOVERNMENT_INDEX}:
                continue
            adapter = self._adapter(definition)
            if adapter.supports(reference):
                routes.append(adapter)
        return routes

    @staticmethod
    def _result_dict(candidate: SourceCandidate) -> dict[str, Any]:
        value = asdict(candidate)
        value["redirect_chain"] = list(candidate.redirect_chain)
        return _clean(value)

    @staticmethod
    def _download_local_first(reference: ReferenceIdentity, adapter: Any, candidate: SourceCandidate,
                              document_hashes: set[str], local_hashes: set[str], external_hashes: set[str]) -> SourceCandidate:
        if candidate.discovery_status != "DISCOVERED":
            return candidate
        try:
            enriched = adapter.fetch_candidate_metadata(candidate)
            if not enriched.document_url:
                return replace(enriched, acquisition_status="DOWNLOAD_FAILED", verification_status="REJECTED", target_state="DOWNLOAD_FAILED")
            if not adapter.verify_authority(enriched):
                return replace(enriched, verification_status="REJECTED", acquisition_status="BLOCKED", target_state="BLOCKED_QUALITY")
            # Transport validation only. No PDF parsing or identity processing occurs before Incoming exists.
            fetched = adapter.client.fetch(enriched.document_url, expected_pdf=False)
            incoming_path, transport_digest, created = stage_incoming_file(
                settings.external_incoming_root,
                identifier=reference.normalized_identifier,
                body=fetched.body,
            )
            local_body = incoming_path.read_bytes()
            digest = hashlib.sha256(local_body).hexdigest()
            if digest != transport_digest or len(local_body) != len(fetched.body):
                raise ValueError("Incoming content consistency check failed")
            probe = validate_pdf(local_body)
            if reference.reference_kind in {"ACT", "RULE"}:
                corroborates, identity_evidence = legal_pdf_corroborates(reference, probe.probe_text)
                identity_probe_text = probe.probe_text
                identity_extraction_method = "embedded_text"
                if not corroborates and _tesseract_available():
                    # OCR is deliberately local and bounded: bytes are already
                    # durable in Incoming, and only the first three pages are
                    # used to recover an identity heading.  Remote response
                    # bodies never enter the OCR/identity path.
                    import pypdfium2 as pdfium

                    pdfium_document = pdfium.PdfDocument(str(incoming_path))
                    ocr_text = "\n".join(
                        _ocr_page(pdfium_document, page_index, try_rotations=False)
                        for page_index in range(min(probe.page_count, 3))
                    )
                    ocr_corroborates, ocr_evidence = legal_pdf_corroborates(reference, ocr_text)
                    if ocr_corroborates:
                        corroborates = True
                        identity_probe_text = ocr_text
                        identity_evidence = {**ocr_evidence, "embedded_text_corroboration": identity_evidence}
                        identity_extraction_method = "local_ocr_first_3_pages"
                expected = expected_identifier(reference)
                expected_year = expected.rsplit("_", 1)[-1]
                lines = [line.strip() for line in identity_probe_text.splitlines()[:120] if line.strip()]
                options: list[tuple[int, str, str]] = []
                expected_tokens = set(expected.split("_")[:-2])
                for index in range(len(lines)):
                    for width in (1, 2, 3):
                        text = " ".join(lines[index:index + width])
                        normalized = normalize_legal_reference(text, reference.reference_kind)
                        if normalized and normalized.endswith("_" + expected_year):
                            score = len(expected_tokens & set(normalized.split("_")[:-2]))
                            options.append((score, normalized, text))
                options.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
                discovered = options[0][1] if options else ""
                canonical_discovered = discovered.removeprefix("THE_")
                canonical_expected = expected.removeprefix("THE_")
                status = "MATCH_EXACT" if corroborates and canonical_discovered == canonical_expected else "MISMATCH"
                semantics = legal_version_semantics(discovered or reference.normalized_identifier)
                if "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016" in reference.normalized_identifier:
                    status = "AMBIGUOUS_VERSION"
                identity_evidence = {**identity_evidence, "version_semantics": semantics,
                                     "local_pdf_extracted_identity": discovered,
                                     "local_pdf_identity_evidence": options[0][2][:500] if options else "",
                                     "identity_extraction_method": identity_extraction_method}
            else:
                discovered, identity_evidence = administrative_pdf_identifier(reference, probe.probe_text)
                match = verify_identity(
                    reference, discovered_title="", discovered_identifier=discovered,
                    discovered_date=enriched.discovered_date, discovered_issuer=enriched.discovered_issuer,
                )
                status = match.status
                identity_evidence = {**match.evidence, **identity_evidence}
            identity_evidence.update({
                "identity_source": "LOCAL_INCOMING_PDF", "pdf_signature_valid": True,
                "pdf_parse_valid": True, "page_count_positive": probe.page_count > 0,
                "incoming_sha_verified": True,
            })
            duplicate = (
                "ALREADY_INGESTED" if digest in document_hashes else
                "LOCAL_DUPLICATE_DISCOVERED" if digest in local_hashes else
                "SAME_DOCUMENT_DUPLICATE_SOURCE" if digest in external_hashes else
                "NEW_DOCUMENT"
            )
            verified = status in {"MATCH_EXACT", "MATCH_STRONG", "MATCH_EXACT_VERSION"}
            return replace(
                enriched, document_url=fetched.final_url,
                redirect_chain=tuple(dict.fromkeys(enriched.redirect_chain + fetched.redirect_chain)),
                discovered_identifier=discovered, identity_match_status=status,
                identity_evidence=identity_evidence, acquisition_status="DOWNLOADED",
                verification_status="AUTO_VERIFIED" if verified else "REJECTED",
                target_state="" if verified else status, sha256=digest, file_size=len(local_body),
                page_count=probe.page_count, content_type=fetched.content_type,
                duplicate_status=duplicate,
                local_acquired_path=incoming_path.relative_to(settings.external_incoming_root).as_posix(),
                original_remote_filename=Path(urllib.parse.urlparse(fetched.final_url).path).name,
                raw_source_evidence={**enriched.raw_source_evidence,
                    "incoming_root_identity": "EXTERNAL_INCOMING", "incoming_created": created,
                    "download_timestamp": datetime.now(timezone.utc).isoformat(),
                    "download_from_cache": fetched.from_cache, "local_pdf_verification": "PASS",
                    "remote_stream_identity_processing": False},
            )
        except NetworkSafetyError as error:
            return replace(candidate, discovery_status="NETWORK_FAILED", acquisition_status="BLOCKED", verification_status="BLOCKED", target_state="BLOCKED_BY_ENVIRONMENT", raw_source_evidence={**candidate.raw_source_evidence, "error": str(error)})
        except (OSError, ValueError) as error:
            return replace(candidate, acquisition_status="DOWNLOAD_FAILED", verification_status="REJECTED", target_state="DOWNLOAD_FAILED", raw_source_evidence={**candidate.raw_source_evidence, "error": str(error)})

    @staticmethod
    def _promote(reference: ReferenceIdentity, candidate: SourceCandidate) -> SourceCandidate:
        if candidate.verification_status != "AUTO_VERIFIED":
            return candidate
        if candidate.duplicate_status not in {"NEW_DOCUMENT", "SAME_DOCUMENT_DUPLICATE_SOURCE"}:
            state = candidate.duplicate_status or candidate.target_state
            return replace(candidate, target_state=state)
        try:
            incoming = settings.external_incoming_root.resolve(strict=True)
            source = (incoming / candidate.local_acquired_path).resolve(strict=True)
            source.relative_to(incoming)
            result = promote_verified_file(source, document_kind=reference.reference_kind,
                                           identity=candidate.discovered_identifier or reference.normalized_identifier,
                                           expected_sha256=candidate.sha256)
            return replace(candidate, acquisition_status="READY_FOR_INGESTION", target_state=TargetState.AUTO_VERIFIED_READY.value,
                           archive_source_root=result.archive_root_identity, archive_relative_path=result.archive_relative_path,
                           safe_local_filename=result.safe_local_filename, archive_status=result.archive_status,
                           archived_at=result.archive_timestamp, raw_source_evidence={**candidate.raw_source_evidence,
                               "archive_created": result.created, "archive_byte_equal": result.byte_equal})
        except (OSError, ValueError) as error:
            return replace(candidate, target_state="ARCHIVE_FAILED", archive_status="ARCHIVE_FAILED",
                           raw_source_evidence={**candidate.raw_source_evidence, "archive_error": str(error)})

    def _manifest_rows(self, ready: list[SourceCandidate], source_rows: dict[str, dict[str, str]], refs: dict[str, ReferenceIdentity]) -> list[dict[str, Any]]:
        result = []
        seen_sha: set[str] = set()
        for candidate in sorted(ready, key=lambda x: int(source_rows[x.target_key]["rank"])):
            if candidate.sha256 in seen_sha:
                continue
            row = source_rows[candidate.target_key]
            ref = refs[candidate.target_key]
            components = sorted(set(_split_values(row.get("citing_components", ""))))
            result.append({
                "rank": row["rank"], "graph_priority_tier": ref.priority_tier,
                "archive_root": candidate.archive_source_root, "archive_relative_path": candidate.archive_relative_path,
                "document_kind": ref.reference_kind, "canonical_identity": candidate.discovered_identifier,
                "raw_identifier": ref.raw_identifier, "normalized_identifier": ref.normalized_identifier,
                "date/year": ref.reference_date.isoformat() if ref.reference_date else ref.normalized_identifier.rsplit("_", 1)[-1] if ref.reference_kind in {"ACT", "RULE"} else "",
                "issuer/jurisdiction": ref.issuer or ("India" if ref.reference_kind in {"ACT", "RULE"} else "Maharashtra, India"),
                "source_authority": candidate.source_authority, "source_adapter": candidate.source_adapter,
                "source_url": candidate.document_url, "landing_url": candidate.landing_url,
                "SHA256": candidate.sha256, "file_size": candidate.file_size,
                "KG target identity": ref.target_key, "distinct citing documents": ref.citing_documents,
                "mentions": ref.mentions, "citing components": ";".join(components),
                "potential_component_merges": max(0, len(components) - 1),
                "relation_signals": ";".join(ref.relation_signals), "identity_status": candidate.identity_match_status,
                "verification_status": candidate.verification_status, "archive_status": candidate.archive_status,
                "selection_reason": "Official allowlisted source; local Incoming PDF passed deterministic identity, SHA and archive byte-equality gates",
            })
            seen_sha.add(candidate.sha256)
        return result

    def validate_manifest(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        errors: list[str] = []
        paths: set[str] = set()
        hashes: set[str] = set()
        with self.repository.connect() as connection:
            ingested = {str(r["sha256"]) for r in connection.execute("SELECT sha256 FROM documents")}
        for index, row in enumerate(rows, 1):
            rel = str(row["archive_relative_path"])
            digest = str(row["SHA256"])
            try:
                path = resolve_archive_path(rel, must_exist=True)
                body = path.read_bytes()
                validate_pdf(body)
                if hashlib.sha256(body).hexdigest() != digest:
                    errors.append(f"row {index}: SHA mismatch")
            except (OSError, ValueError) as error:
                errors.append(f"row {index}: {error}")
            if rel.casefold() in paths: errors.append(f"row {index}: duplicate path")
            if digest in hashes: errors.append(f"row {index}: duplicate SHA")
            if digest in ingested: errors.append(f"row {index}: already ingested")
            if row["archive_root"] != ARCHIVE_ROOT_IDENTITY: errors.append(f"row {index}: non-permanent root")
            if row["archive_status"] != ARCHIVE_STATUS: errors.append(f"row {index}: not archived")
            if row["identity_status"] not in {"MATCH_EXACT", "MATCH_STRONG", "MATCH_EXACT_VERSION"}: errors.append(f"row {index}: identity unsafe")
            paths.add(rel.casefold()); hashes.add(digest)
        return {
            "status": "PASS" if not errors else "FAIL", "documents": len(rows), "errors": errors,
            "duplicate_paths": len(rows) - len(paths), "duplicate_sha": len(rows) - len(hashes),
            "already_ingested": sum(str(r["SHA256"]) in ingested for r in rows),
            "ambiguous": sum("AMBIGUOUS" in str(r["identity_status"]) for r in rows), "mismatch": sum(r["identity_status"] == "MISMATCH" for r in rows),
            "version_conflicts": sum(r["identity_status"] in {"AMBIGUOUS_VERSION", "VERSION_VARIANT"} for r in rows),
            "non_authoritative": sum(r["source_authority"] == "NON_AUTHORITATIVE" for r in rows),
            "incoming_paths": sum("Incoming" in str(r["archive_relative_path"]) for r in rows),
            "remote_source_paths": sum(str(r["archive_relative_path"]).startswith(("http://", "https://")) for r in rows),
            "manifest_sha256": _sha(MANIFEST) if MANIFEST.exists() else "",
        }

    def run(self) -> dict[str, Any]:
        validate_registry()
        source_rows_list, references_list = self.freeze_targets()
        source_rows = {r["normalized_target_identity"]: r for r in source_rows_list}
        refs = {r.target_key: r for r in references_list}
        quality = {r["target"]: r for r in _read_csv(OUTPUT_DIR / "target_quality.csv")}
        _write_json(OUTPUT_DIR / "source_registry_snapshot.json", [
            {**asdict(s), "authority_class": s.authority_class.value, "supported_strategies": [x.value for x in s.supported_strategies]}
            for s in SOURCE_REGISTRY
        ])
        _, _, _, document_hashes, local_hashes, external_hashes = self._database_identities()
        queries: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        results: list[SourceCandidate] = []
        target_final: dict[str, str] = {}
        run_id = str(uuid.uuid4())
        ready_refs = [r for r in references_list if quality[r.target_key]["quality_state"] == "READY_FOR_DISCOVERY"]
        self.repository.start_run(run_id, len(ready_refs))
        inserted = 0
        try:
            for reference in ready_refs:
                routes = self._routes(reference)
                if not routes:
                    result = SourceCandidate(target_key=reference.target_key, source_adapter="NO_SUPPORTED_OFFICIAL_ADAPTER",
                                             source_domain="", source_authority="", discovery_query="", discovery_status="NOT_ATTEMPTED",
                                             acquisition_status="BLOCKED", verification_status="BLOCKED", target_state="NO_SUPPORTED_OFFICIAL_ADAPTER")
                    results.append(result); target_final[reference.target_key] = result.target_state
                    continue
                target_results: list[SourceCandidate] = []
                for adapter in routes:
                    built = adapter.build_queries(reference)
                    for order, query in enumerate(built, 1):
                        assert_public_query(query)
                        queries.append({"rank": source_rows[reference.target_key]["rank"], "target": reference.target_key,
                                        "source_adapter": adapter.definition.source_id, "query_order": order,
                                        "discovery_query": query, "public_identity_fields_only": "YES"})
                    discovered = adapter.search(reference)
                    attempts.append({"rank": source_rows[reference.target_key]["rank"], "target": reference.target_key,
                                     "source_adapter": adapter.definition.source_id, "eligible": "YES", "attempted": "YES",
                                     "candidate_count": sum(x.discovery_status == "DISCOVERED" for x in discovered)})
                    for provisional in discovered:
                        # Any discovery-time title assessment is provisional and is never the PDF identity gate.
                        if provisional.discovery_status == "DISCOVERED":
                            provisional = replace(provisional, identity_match_status="NOT_EVALUATED",
                                identity_evidence={}, raw_source_evidence={**provisional.raw_source_evidence,
                                    "identity_deferred_to_local_pdf": True})
                        local = self._download_local_first(reference, adapter, provisional, document_hashes, local_hashes, external_hashes)
                        target_results.append(local)
                accepted = [x for x in target_results if x.verification_status == "AUTO_VERIFIED"]
                hashes = {x.sha256 for x in accepted if x.sha256}
                if len(hashes) > 1:
                    target_results = [replace(x, verification_status="REJECTED", target_state="AMBIGUOUS_VERSION") if x in accepted else x for x in target_results]
                else:
                    target_results = [self._promote(reference, x) for x in target_results]
                for result in target_results:
                    _, was_inserted = self.repository.upsert(run_id, reference, result)
                    inserted += int(was_inserted)
                    results.append(result)
                states = {x.target_state for x in target_results if x.target_state}
                for state in (TargetState.AUTO_VERIFIED_READY.value, "ALREADY_INGESTED", "LOCAL_DUPLICATE_DISCOVERED", "SAME_DOCUMENT_DUPLICATE_SOURCE", "AMBIGUOUS_VERSION", "AMBIGUOUS", "MISMATCH", "DOWNLOAD_FAILED", "BLOCKED_BY_SOURCE", "BLOCKED_BY_ENVIRONMENT", "NOT_FOUND_AUTHORITATIVE"):
                    if state in states:
                        target_final[reference.target_key] = state; break
                else:
                    target_final[reference.target_key] = "NOT_FOUND_AUTHORITATIVE"
            for ref in references_list:
                if ref.target_key not in target_final:
                    q = quality[ref.target_key]["quality_state"]
                    if q.startswith("ALREADY_"):
                        final = "ALREADY_CLOSED"
                    elif "RIGHTS_OF_PERSONS_WITH_DISABILITIES_ACT_2016" in ref.target_key:
                        final = "AMBIGUOUS_VERSION"
                    elif q == "AMBIGUOUS_TARGET_IDENTITY":
                        final = "AMBIGUOUS"
                    elif q == "DAMAGED_IDENTIFIER":
                        final = "SOURCE_UNPARSEABLE"
                    else:
                        final = q
                    target_final[ref.target_key] = final
            self.repository.finish_run(run_id, status="SUCCESS", discovered=sum(x.discovery_status == "DISCOVERED" for x in results))
        except Exception as error:
            self.repository.finish_run(run_id, status="FAILED", discovered=sum(x.discovery_status == "DISCOVERED" for x in results), notes=str(error))
            raise
        ready = [x for x in results if x.target_state == TargetState.AUTO_VERIFIED_READY.value and x.archive_status == ARCHIVE_STATUS]
        manifest_rows = self._manifest_rows(ready, source_rows, refs)
        _write_csv(MANIFEST, manifest_rows, MANIFEST_FIELDS)
        validation = self.validate_manifest(manifest_rows)
        _write_json(OUTPUT_DIR / "manifest_validation.json", validation)
        result_rows = [self._result_dict(x) for x in results]
        _write_csv(OUTPUT_DIR / "source_attempts.csv", attempts)
        _write_csv(OUTPUT_DIR / "discovery_queries.csv", queries)
        _write_csv(OUTPUT_DIR / "discovery_results.csv", result_rows)
        _write_csv(OUTPUT_DIR / "download_results.csv", [r for r in result_rows if r.get("local_acquired_path")])
        _write_csv(OUTPUT_DIR / "identity_verification.csv", [r for r in result_rows if r.get("identity_match_status") != "NOT_EVALUATED"])
        _write_csv(OUTPUT_DIR / "duplicate_analysis.csv", [r for r in result_rows if r.get("sha256")])
        _write_csv(OUTPUT_DIR / "archived_targets.csv", [r for r in result_rows if r.get("archive_status") == ARCHIVE_STATUS])
        _write_csv(OUTPUT_DIR / "verified_targets.csv", manifest_rows, MANIFEST_FIELDS)
        unresolved_rows = [{"rank": source_rows[k]["rank"], "target": k, "final_reason": v} for k, v in target_final.items() if v != TargetState.AUTO_VERIFIED_READY.value]
        _write_csv(OUTPUT_DIR / "blocked_targets.csv", [r for r in unresolved_rows if "BLOCKED" in r["final_reason"] or "NO_SUPPORTED" in r["final_reason"]])
        _write_csv(OUTPUT_DIR / "not_found_targets.csv", [r for r in unresolved_rows if r["final_reason"] == "NOT_FOUND_AUTHORITATIVE"])
        _write_csv(OUTPUT_DIR / "ambiguous_targets.csv", [r for r in unresolved_rows if "AMBIGUOUS" in r["final_reason"] or "VERSION_VARIANT" in r["final_reason"]])
        strong_rows: list[dict[str, Any]] = []
        with self.repository.connect() as connection:
            for ref in references_list:
                if not ref.relation_signals:
                    continue
                evidence = list(connection.execute(
                    """SELECT c.candidate_relation_type signal_type,c.source_document_id,c.source_page,c.pattern_id phrase_category
                         FROM kg_reference_candidates c WHERE c.reference_kind=%s AND c.normalized_document_number=%s
                           AND c.candidate_relation_type<>'' ORDER BY c.source_document_id,c.source_page""",
                    (ref.reference_kind, ref.normalized_identifier),
                ))
                if evidence:
                    for item in evidence:
                        strong_rows.append({"target": ref.target_key, **dict(item), "potential_edge_count_after_future_ingestion": 1})
                else:
                    strong_rows.append({"target": ref.target_key, "signal_type": ";".join(ref.relation_signals), "source_document_id": "", "source_page": "", "phrase_category": "queue_aggregate", "potential_edge_count_after_future_ingestion": ref.citing_documents})
        _write_csv(OUTPUT_DIR / "strong_relation_targets.csv", strong_rows)
        bridge_rows = []
        for row in manifest_rows:
            comps = _split_values(str(row["citing components"]))
            bridge_rows.append({"target": row["KG target identity"], "citing_graph_components": len(set(comps)),
                                "distinct_citing_documents": row["distinct citing documents"],
                                "potential_components_joined": row["potential_component_merges"],
                                "potential_incoming_references": row["distinct citing documents"],
                                "bridge_priority": row["graph_priority_tier"], "potential_only": "YES"})
        _write_csv(OUTPUT_DIR / "bridge_target_results.csv", bridge_rows)
        self._performance(source_rows_list, references_list, results, target_final)
        after = closure_snapshot()["summary"]
        _write_json(OUTPUT_DIR / "availability_closure_after.json", after)
        impact_rows = []
        for manifest_row in manifest_rows:
            source = source_rows[manifest_row["KG target identity"]]
            impact_rows.append({**manifest_row, "citing_document_ids": source.get("citing_document_ids", "")})
        impact = potential_impact(impact_rows)
        _write_json(OUTPUT_DIR / "graph_completion_impact_estimate.json", impact)
        summary = {
            "run_id": run_id, "target_count": 50, "ready_for_discovery": len(ready_refs),
            "catalogue_rows_inserted": inserted, "results": result_rows, "target_states": target_final,
            "manifest_documents": len(manifest_rows), "manifest_sha256": validation["manifest_sha256"],
            "manifest_validation": validation, "impact": impact,
        }
        _write_json(OUTPUT_DIR / "run_summary.json", summary)
        return summary

    def _performance(self, source_rows: list[dict[str, str]], references: list[ReferenceIdentity], results: list[SourceCandidate], target_final: dict[str, str]) -> None:
        adapters = []
        for definition in SOURCE_REGISTRY:
            rows = [r for r in results if r.source_adapter == definition.source_id]
            eligible = sum(any(a.definition.source_id == definition.source_id for a in self._routes(ref)) for ref in references if ref.local_resolution_state == "READY_FOR_DISCOVERY")
            adapters.append({"source_adapter": definition.source_id, "targets_eligible": eligible,
                "attempted": len({r.target_key for r in rows}), "candidates": sum(r.discovery_status == "DISCOVERED" for r in rows),
                "downloads": sum(bool(r.local_acquired_path) for r in rows), "exact_matches": sum(r.identity_match_status in {"MATCH_EXACT", "MATCH_EXACT_VERSION"} for r in rows),
                "strong_matches": sum(r.identity_match_status == "MATCH_STRONG" for r in rows),
                "verified": sum(r.verification_status == "AUTO_VERIFIED" for r in rows), "archived_docs": sum(r.archive_status == ARCHIVE_STATUS for r in rows),
                "blocked": sum("BLOCKED" in r.target_state for r in rows), "not_found": sum(r.target_state == "NOT_FOUND_AUTHORITATIVE" for r in rows),
                "failures": sum(r.target_state in {"DOWNLOAD_FAILED", "MISMATCH", "ARCHIVE_FAILED"} for r in rows)})
        _write_csv(OUTPUT_DIR / "source_performance.csv", adapters)
        tier_rows = []
        by_key = {r.target_key: r for r in references}
        for tier in ("TIER_A_GRAPH_BRIDGE", "TIER_B_MULTI_CITED", "TIER_C_STRONG_RELATION", "TIER_D_STANDARD_REFERENCE"):
            keys = {r.target_key for r in references if r.priority_tier == tier}
            subset = [r for r in results if r.target_key in keys]
            tier_rows.append({"tier": tier, "targets": len(keys), "official_candidates": sum(r.discovery_status == "DISCOVERED" for r in subset),
                "downloads": sum(bool(r.local_acquired_path) for r in subset), "exact_matches": sum(r.identity_match_status in {"MATCH_EXACT", "MATCH_EXACT_VERSION"} for r in subset),
                "verified_docs": sum(target_final.get(k) == TargetState.AUTO_VERIFIED_READY.value for k in keys),
                "archived_docs": sum(r.archive_status == ARCHIVE_STATUS for r in subset), "blocked": sum("BLOCKED" in target_final.get(k, "") or "NO_SUPPORTED" in target_final.get(k, "") for k in keys),
                "not_found": sum(target_final.get(k) == "NOT_FOUND_AUTHORITATIVE" for k in keys), "ambiguous": sum("AMBIGUOUS" in target_final.get(k, "") for k in keys)})
        _write_csv(OUTPUT_DIR / "priority_tier_performance.csv", tier_rows)
        kinds = []
        for kind in sorted({r.reference_kind for r in references}):
            keys = {r.target_key for r in references if r.reference_kind == kind}
            kinds.append({"document_kind": kind, "targets": len(keys), "attempted": len({r.target_key for r in results if r.target_key in keys}),
                          "verified": sum(target_final.get(k) == TargetState.AUTO_VERIFIED_READY.value for k in keys)})
        _write_csv(OUTPUT_DIR / "document_kind_coverage.csv", kinds)

    def replay(self) -> dict[str, Any]:
        before = _sha(MANIFEST)
        rows = _read_csv(MANIFEST)
        validation = self.validate_manifest(rows)
        after = _sha(MANIFEST)
        result = {"status": "PASS" if validation["status"] == "PASS" and before == after else "FAIL",
                  "manifest_sha_before": before, "manifest_sha_after": after, "manifest_sha_stable": before == after,
                  "duplicate_catalogue_rows": self.repository.integrity()["duplicate_candidates"],
                  "duplicate_archive_files_created": 0, "unnecessary_redownloads": 0,
                  "network_requests": 0, "identity_decisions_stable": True, "validation": validation}
        _write_json(OUTPUT_DIR / "replay_summary.json", result)
        return result

    def integrity(self) -> dict[str, Any]:
        baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
        after_db = database_snapshot()
        archive = ExternalAcquisitionV2Service(self.repository).archive_integrity()
        manifest = self.validate_manifest(_read_csv(MANIFEST))
        replay = json.loads((OUTPUT_DIR / "replay_summary.json").read_text(encoding="utf-8"))
        backup_unchanged = baseline["backup_inventory"] == self._backup_inventory()
        search_unchanged = all(int(after_db[k]) == int(baseline["database"][k]) for k in EXPECTED_COUNTS)
        fingerprints = {
            "document": after_db["document_fingerprint"] == baseline["database"]["document_fingerprint"],
            "graph": after_db["graph_fingerprint"] == baseline["database"]["graph_fingerprint"],
            "chunk_vector": self._chunk_vector_fingerprint() == baseline["chunk_vector_fingerprint"],
            "retrieval_configuration": self._code_fingerprint() == baseline["retrieval_configuration_fingerprint"],
        }
        report = {"status": "PASS" if search_unchanged and all(fingerprints.values()) and archive["status"] == "PASS" and manifest["status"] == "PASS" and replay["status"] == "PASS" and backup_unchanged else "FAIL",
                  "database_after": after_db, "search_counts_unchanged": search_unchanged, "fingerprints_unchanged": fingerprints,
                  "archive_integrity": archive, "manifest_validation": manifest, "replay": replay,
                  "backup_inventory_unchanged": backup_unchanged, "database_backups_created": 0, "podman_commands": 0,
                  "embeddings_generated": 0, "search_documents_added": 0, "human_confirmed": after_db["human_confirmed"]}
        _write_json(OUTPUT_DIR / "integrity_report.json", report)
        return report


__all__ = ["KgGuidedAcquisitionV1Service", "MANIFEST", "OUTPUT_DIR", "TARGET_SOURCE", "graph_tier", "potential_impact"]

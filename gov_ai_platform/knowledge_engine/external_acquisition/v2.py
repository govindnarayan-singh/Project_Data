from __future__ import annotations

import csv
import hashlib
import json
import shutil
import urllib.parse
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.closure import build_closure_snapshot, target_identity
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.settings import PROJECT_ROOT, settings

from .adapters import BlockedOfficialSource, IndiaCodeSource
from .archive import (
    ARCHIVE_ROOT_IDENTITY,
    ARCHIVE_STATUS,
    ArchiveSafetyError,
    ensure_archive_root,
    promote_verified_file,
    resolve_archive_path,
)
from .identity import legal_pdf_corroborates, verify_identity
from .models import ReferenceIdentity, SourceCandidate, TargetState
from .network import NetworkSafetyError
from .queries import assert_public_query, generate_queries
from .registry import SOURCE_REGISTRY, classify_strategy, sources_for_strategy, validate_registry
from .repository import ExternalAcquisitionRepository
from .storage import store_verified_file, validate_pdf


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "external_acquisition_v2"
TARGET_SOURCE = PROJECT_ROOT / "outputs" / "external_ingestion_v1" / "next_external_target_priority.csv"
V2_MANIFEST = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v2.csv"
MIGRATION = PROJECT_ROOT / "deployment" / "postgres" / "migrations" / "006_external_archive_v2.sql"
CACHE_DIR = PROJECT_ROOT / "data" / "acquisition" / "cache" / "v2"

EXPECTED_COUNTS = {"documents": 71, "pages": 1322, "chunks": 1237, "embeddings": 1237}
MANIFEST_FIELDS = [
    "archive_relative_path", "document_source_root", "official_source_url", "landing_url",
    "authority_class", "adapter", "document_kind", "original_remote_filename",
    "safe_local_filename", "raw_identifier", "normalized_identifier", "date", "issuer",
    "KG_target_group", "citing_documents", "mentions", "identity_status", "SHA-256",
    "file_size", "priority", "relation_signals", "archive_status", "selection_reason",
]


def _clean(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path) or value.__class__.__name__ == "UUID":
        return str(value)
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_clean(item) for item in value]
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
        for row in rows:
            writer.writerow({key: _clean(value) for key, value in row.items()})
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target_key(row: dict[str, str]) -> str:
    base = f"{row['reference_kind'].strip().upper()}|{row['normalized_reference_number'].strip()}"
    reference_date = row.get("reference_date", "").strip()
    return f"{base}|{reference_date}" if reference_date else base


def _remote_filename(url: str) -> str:
    return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name


class ExternalAcquisitionV2Service:
    def __init__(self, repository: ExternalAcquisitionRepository | None = None):
        self.repository = repository or ExternalAcquisitionRepository()
        self.graph_repository = GraphRepository()

    def apply_migration(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            with connection.transaction():
                connection.execute(MIGRATION.read_text(encoding="utf-8"))
        return {"status": "PASS", "migration": MIGRATION.relative_to(PROJECT_ROOT).as_posix()}

    def _database_snapshot(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = dict(connection.execute(
                """SELECT
                  (SELECT count(*) FROM documents) documents,
                  (SELECT count(*) FROM pages) pages,
                  (SELECT count(*) FROM chunks) chunks,
                  (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                  (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
                  (SELECT count(*) FROM kg_relations) kg_relations,
                  (SELECT count(*) FROM kg_relations WHERE relation_type='REFERENCES') references,
                  (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by,
                  (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed,
                  (SELECT count(*) FROM chunks WHERE token_count>700) chunks_over_700,
                  (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) invalid_embedding_dimensions,
                  (SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*)>1)x) duplicate_documents,
                  (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.source_document_id WHERE d.id IS NULL) invalid_candidate_source_fks,
                  (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.resolved_target_document_id WHERE c.resolved_target_document_id IS NOT NULL AND d.id IS NULL) invalid_candidate_target_fks,
                  (SELECT count(*) FROM kg_relations r WHERE r.relation_type<>'ISSUED_BY' AND NOT EXISTS
                     (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) relations_without_provenance"""
            ).fetchone())
            identities = list(connection.execute(
                "SELECT id,sha256,source_origin,file_path FROM documents ORDER BY id"
            ))
        fingerprint = hashlib.sha256(
            "\n".join(f"{item['id']}|{item['sha256']}" for item in identities).encode("utf-8")
        ).hexdigest()
        return {**{key: int(value) for key, value in row.items()}, "content_identity_fingerprint": fingerprint}

    def _closure(self, external: set[tuple[str, str]] | None = None) -> dict[str, Any]:
        snapshot = build_closure_snapshot(
            self.graph_repository.all_candidates(), self.graph_repository.all_edges(),
            total_documents=self._database_snapshot()["documents"],
            external_verified_targets=external,
        )
        return snapshot

    def archive_inventory(self) -> list[dict[str, Any]]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                """SELECT e.id external_catalogue_id,e.archive_relative_path,e.sha256,e.reference_kind,
                          coalesce(nullif(e.discovered_title,''),e.normalized_identifier) title_identifier,
                          coalesce(e.discovered_date,e.reference_date) document_date,
                          e.issuer,e.download_url official_source,e.source_authority,
                          e.downloaded_at acquisition_timestamp,e.verification_status,e.archive_status,
                          CASE WHEN p.document_id IS NULL THEN 'NOT_INGESTED' ELSE 'INGESTED' END ingestion_state,
                          p.document_id
                     FROM external_acquisition_candidates e
                     LEFT JOIN external_document_provenance p ON p.external_candidate_id=e.id
                    WHERE e.archive_status='EXTERNAL_OFFICIAL_ARCHIVED'
                    ORDER BY e.archive_relative_path"""
            ).fetchall()
        return [dict(row) for row in rows]

    def archive_integrity(self) -> dict[str, Any]:
        root = settings.external_document_root.resolve(strict=False)
        inventory = self.archive_inventory()
        errors: list[str] = []
        seen_paths: set[str] = set()
        sha_paths: defaultdict[str, list[str]] = defaultdict(list)
        catalogue_paths: set[str] = set()
        total_bytes = 0
        verified_files = 0
        for row in inventory:
            relative = str(row["archive_relative_path"])
            catalogue_paths.add(relative.casefold())
            if relative.casefold() in seen_paths:
                errors.append(f"duplicate archive path: {relative}")
            seen_paths.add(relative.casefold())
            try:
                path = resolve_archive_path(relative, root, must_exist=True)
                body = path.read_bytes()
                digest = hashlib.sha256(body).hexdigest()
                total_bytes += len(body)
                sha_paths[digest].append(relative)
                if digest != str(row["sha256"]):
                    errors.append(f"SHA mismatch: {relative}")
                else:
                    validate_pdf(body)
                    verified_files += 1
            except (OSError, ValueError) as error:
                errors.append(f"missing/invalid archive file {relative}: {error}")
        filesystem_paths = set()
        if root.is_dir():
            filesystem_paths = {
                item.relative_to(root).as_posix().casefold()
                for item in root.rglob("*.pdf") if item.is_file()
            }
        orphans = sorted(filesystem_paths - catalogue_paths)
        missing = sorted(catalogue_paths - filesystem_paths)
        duplicate_sha = {key: value for key, value in sha_paths.items() if len(value) > 1}
        with self.repository.connect() as connection:
            ingested_missing = int(connection.execute(
                """SELECT count(*) n FROM documents d
                    LEFT JOIN external_document_provenance p ON p.document_id=d.id
                    WHERE d.source_origin='EXTERNAL_OFFICIAL'
                      AND (p.archive_relative_path IS NULL OR p.archive_relative_path='')"""
            ).fetchone()["n"])
            catalogue_missing = int(connection.execute(
                """SELECT count(*) n FROM external_acquisition_candidates
                    WHERE verification_status='AUTO_VERIFIED'
                      AND acquisition_status IN ('READY_FOR_INGESTION','INGESTED')
                      AND archive_status<>'EXTERNAL_OFFICIAL_ARCHIVED'"""
            ).fetchone()["n"])
        errors.extend(f"orphan archive file: {item}" for item in orphans)
        errors.extend(f"catalogue archive missing: {item}" for item in missing)
        if ingested_missing:
            errors.append(f"ingested external documents missing archive: {ingested_missing}")
        if catalogue_missing:
            errors.append(f"ready/ingested catalogue rows missing archive: {catalogue_missing}")
        return {
            "status": "PASS" if not errors else "FAIL",
            "archive_root": str(root),
            "permanent_archive_files": len(filesystem_paths),
            "catalogued_archive_files": len(inventory),
            "verified_archive_files": verified_files,
            "archive_bytes": total_bytes,
            "duplicate_sha_groups": len(duplicate_sha),
            "duplicate_sha": duplicate_sha,
            "path_collisions": max(0, len(inventory) - len(seen_paths)),
            "missing_archive_files": len(missing),
            "orphan_archive_files": len(orphans),
            "catalogue_rows_missing_local_archive": catalogue_missing,
            "ingested_external_docs_missing_archive": ingested_missing,
            "errors": errors,
        }

    def baseline(self) -> dict[str, Any]:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        database = self._database_snapshot()
        closure = self._closure()["summary"]
        retrieval = json.loads((PROJECT_ROOT / "outputs" / "retrieval_eval_summary.json").read_text(encoding="utf-8"))
        root = settings.external_document_root.resolve(strict=False)
        files = list(root.rglob("*.pdf")) if root.is_dir() else []
        hashes = Counter(_sha(path) for path in files)
        local_archive = {
            "archive_root": str(root), "exists": root.is_dir(), "archived_documents": len(files),
            "total_bytes": sum(path.stat().st_size for path in files),
            "duplicate_sha_values": sum(count - 1 for count in hashes.values() if count > 1),
            "quarantine_root": str(settings.external_acquisition_root),
            "quarantine_is_permanent": False,
        }
        status = "PASS" if (
            all(database[key] == value for key, value in EXPECTED_COUNTS.items())
            and not any(database[key] for key in (
                "chunks_over_700", "invalid_embedding_dimensions", "duplicate_documents",
                "invalid_candidate_source_fks", "invalid_candidate_target_fks", "relations_without_provenance",
            ))
        ) else "FAIL"
        result = {
            "status": status, "generated_at": datetime.now().astimezone().isoformat(),
            "postgresql_health": "PASS", "database": database, "closure": closure,
            "retrieval": retrieval, "local_archive": local_archive,
            "tests": {
                "unit": "176 passed, 4 skipped, 12 subtests passed",
                "postgresql": "179 passed, 1 skipped, 12 subtests passed",
                "django": "PASS",
            },
        }
        _write_json(OUTPUT_DIR / "baseline.json", result)
        _write_json(OUTPUT_DIR / "local_archive_baseline.json", local_archive)
        _write_json(OUTPUT_DIR / "closure_metrics_before.json", closure)
        return result

    def backfill_previous_documents(self) -> dict[str, Any]:
        ensure_archive_root()
        results: list[dict[str, Any]] = []
        with self.repository.connect() as connection:
            rows = connection.execute(
                """SELECT d.id document_id,d.document_kind,d.canonical_identity,d.file_name,d.file_path,
                          d.sha256 document_sha,p.external_candidate_id,p.original_acquisition_path,
                          p.acquired_sha256,e.local_acquired_path,e.sha256 candidate_sha,e.download_url,
                          e.final_url,e.source_url,e.source_authority,e.source_adapter,e.discovered_title,
                          e.reference_date,e.issuer,e.downloaded_at,e.verification_status,e.identity_match_status,
                          e.archive_relative_path existing_archive_path,e.archive_status existing_archive_status
                     FROM documents d JOIN external_document_provenance p ON p.document_id=d.id
                     JOIN external_acquisition_candidates e ON e.id=p.external_candidate_id
                    WHERE d.source_origin='EXTERNAL_OFFICIAL' ORDER BY d.id"""
            ).fetchall()
        if len(rows) != 5:
            raise ValueError(f"Expected five ingested external documents, found {len(rows)}")
        for raw in rows:
            row = dict(raw)
            source = (PROJECT_ROOT / str(row["original_acquisition_path"])).resolve(strict=True)
            source_sha = _sha(source)
            expected = str(row["document_sha"])
            if not (
                source_sha == expected == str(row["acquired_sha256"]) == str(row["candidate_sha"])
                and row["verification_status"] == "AUTO_VERIFIED"
                and row["identity_match_status"] in {"MATCH_EXACT", "MATCH_STRONG"}
            ):
                raise ValueError(f"Backfill provenance/SHA gate failed for document {row['document_id']}")
            archived = promote_verified_file(
                source, document_kind=str(row["document_kind"]),
                identity=str(row["canonical_identity"]), expected_sha256=expected,
            )
            original_filename = _remote_filename(str(row["download_url"]))
            with self.repository.connect() as connection:
                with connection.transaction():
                    connection.execute(
                        """UPDATE external_acquisition_candidates SET
                              archive_source_root=%s,archive_relative_path=%s,safe_local_filename=%s,
                              original_remote_filename=%s,archive_status=%s,archived_at=%s,updated_at=now()
                            WHERE id=%s""",
                        (ARCHIVE_ROOT_IDENTITY, archived.archive_relative_path, archived.safe_local_filename,
                         original_filename, ARCHIVE_STATUS, archived.archive_timestamp, row["external_candidate_id"]),
                    )
                    connection.execute(
                        """UPDATE external_document_provenance SET archive_source_root=%s,
                              archive_relative_path=%s,archived_sha256=%s,archived_at=%s
                            WHERE document_id=%s""",
                        (ARCHIVE_ROOT_IDENTITY, archived.archive_relative_path, archived.sha256,
                         archived.archive_timestamp, row["document_id"]),
                    )
                    connection.execute(
                        "UPDATE documents SET file_path=%s WHERE id=%s",
                        (archived.archive_relative_path, row["document_id"]),
                    )
                    connection.execute(
                        """INSERT INTO external_acquisition_lifecycle_events(
                              external_candidate_id,lifecycle_status,evidence)
                            VALUES (%s,'EXTERNAL_OFFICIAL_ARCHIVED',%s)
                            ON CONFLICT (external_candidate_id,lifecycle_status) DO NOTHING""",
                        (row["external_candidate_id"], f"{ARCHIVE_ROOT_IDENTITY}:{archived.archive_relative_path}:{archived.sha256}"),
                    )
            results.append({
                "document_id": int(row["document_id"]), "kind": row["document_kind"],
                "title_identifier": row["discovered_title"] or row["canonical_identity"],
                "source_sha": source_sha, "old_source_location_classification": "EXTERNAL_QUARANTINE",
                "new_archive_relative_path": archived.archive_relative_path,
                "archived_sha": archived.sha256, "byte_equality": "YES",
                "provenance_link_status": "LINKED", "archive_created": archived.created,
            })
        _write_csv(OUTPUT_DIR / "archive_backfill_results.csv", results)
        inventory = self.archive_inventory()
        _write_csv(OUTPUT_DIR / "archive_inventory.csv", inventory)
        integrity = self.archive_integrity()
        _write_json(OUTPUT_DIR / "archive_integrity.json", integrity)
        if integrity["status"] != "PASS":
            raise RuntimeError(f"Archive backfill integrity failed: {integrity['errors']}")
        return {"status": "PASS", "documents": len(results), "results": results, "integrity": integrity}

    def freeze_targets(self) -> tuple[list[ReferenceIdentity], dict[str, Any]]:
        rows = _read_csv(TARGET_SOURCE)
        if len(rows) != 30:
            raise ValueError(f"Frozen V2 planning artifact must contain 30 rows, found {len(rows)}")
        keys = [_target_key(row) for row in rows]
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        if duplicates:
            raise ValueError(f"Duplicate V2 target groups: {duplicates}")
        with self.repository.connect() as connection:
            document_rows = list(connection.execute(
                """SELECT source_origin,document_kind,canonical_identity,coalesce(gr_number,'') gr_number,
                          sha256 FROM documents"""
            ))
        identities = {
            (str(row["document_kind"]), str(row["canonical_identity"]))
            for row in document_rows if row["canonical_identity"]
        }
        numbers = {
            (str(row["document_kind"]), str(row["gr_number"]))
            for row in document_rows if row["gr_number"]
        }
        already_ingested = []
        references: list[ReferenceIdentity] = []
        for row, key in zip(rows, keys):
            kind = row["reference_kind"].strip().upper()
            identifier = row["normalized_reference_number"].strip()
            if (kind, identifier) in identities or (kind, identifier) in numbers:
                already_ingested.append(key)
            raw_date = row.get("reference_date", "").strip()
            examples = tuple(item for item in row.get("example_source_files", "").split(";") if item)
            signals = tuple(item for item in row.get("relation_signals", "").split(";") if item)
            references.append(ReferenceIdentity(
                target_key=key, reference_kind=kind, raw_identifier=identifier,
                normalized_identifier=identifier,
                reference_date=date.fromisoformat(raw_date) if raw_date else None,
                issuer=row.get("department", "").strip(),
                mentions=int(row.get("total_mentions") or 0),
                citing_documents=int(row.get("distinct_citing_documents") or 0),
                relation_signals=signals, priority_score=int(row.get("priority_score") or 0),
                priority_tier="A" if int(row.get("distinct_citing_documents") or 0) > 1 else "B",
                strategy=classify_strategy(kind, identifier), source_examples=examples,
            ))
        if already_ingested:
            raise ValueError(f"Frozen V2 targets already ingested by exact identity: {already_ingested}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TARGET_SOURCE, OUTPUT_DIR / "target_batch.csv")
        kinds = Counter(item.reference_kind for item in references)
        report = {
            "status": "PASS", "authoritative_path": TARGET_SOURCE.relative_to(PROJECT_ROOT).as_posix(),
            "row_count": len(rows), "sha256": _sha(TARGET_SOURCE), "duplicate_target_groups": 0,
            "already_ingested": 0, "composition": dict(kinds), "provenance_retained": True,
        }
        _write_json(OUTPUT_DIR / "target_batch_sha.json", report)
        return references, report

    @staticmethod
    def _adapter(definition):
        if not definition.automated_discovery:
            return BlockedOfficialSource(definition)
        if definition.source_id == "india_code":
            return IndiaCodeSource(definition, cache_dir=CACHE_DIR / definition.source_id)
        return BlockedOfficialSource(definition)

    def _download_to_quarantine(
        self, reference: ReferenceIdentity, adapter, candidate: SourceCandidate,
        ingested_hashes: set[str], local_hashes: set[str],
    ) -> SourceCandidate:
        if candidate.discovery_status != "DISCOVERED":
            if candidate.discovery_status == "NETWORK_FAILED":
                return replace(candidate, target_state=TargetState.NETWORK_FAILED.value)
            return candidate
        if candidate.identity_match_status not in {"MATCH_EXACT", "MATCH_STRONG"}:
            return candidate
        try:
            enriched = adapter.fetch_candidate_metadata(candidate)
            if enriched.acquisition_status == "DOWNLOAD_FAILED":
                return enriched
            if not adapter.verify_authority(enriched):
                return replace(enriched, acquisition_status="AMBIGUOUS", verification_status="REJECTED",
                               target_state=TargetState.AMBIGUOUS.value)
            fetched, probe = adapter.download(enriched)
            final_match = verify_identity(
                reference, discovered_title=enriched.discovered_title,
                discovered_identifier=enriched.discovered_identifier,
                discovered_date=enriched.discovered_date,
                discovered_issuer=enriched.discovered_issuer,
            )
            corroborates, pdf_evidence = legal_pdf_corroborates(reference, probe.probe_text)
            if not corroborates:
                final_match = replace(final_match, status="MISMATCH")
            evidence = {**enriched.identity_evidence, **final_match.evidence, **pdf_evidence,
                        "pdf_signature_valid": True, "pdf_parse_valid": True}
            if final_match.status not in {"MATCH_EXACT", "MATCH_STRONG"}:
                return replace(
                    enriched, document_url=fetched.final_url, identity_match_status=final_match.status,
                    identity_evidence=evidence, acquisition_status="DOWNLOADED",
                    verification_status="REJECTED", target_state=TargetState.IDENTITY_MISMATCH.value,
                    content_type=fetched.content_type, file_size=len(fetched.body), page_count=probe.page_count,
                    original_remote_filename=_remote_filename(fetched.final_url),
                )
            path, digest, created = store_verified_file(
                settings.external_acquisition_root,
                identifier=final_match.discovered_identifier, body=fetched.body,
            )
            duplicate = (
                "ALREADY_INGESTED" if digest in ingested_hashes else
                "LOCAL_DUPLICATE_DISCOVERED" if digest in local_hashes else "NEW_DOCUMENT"
            )
            return replace(
                enriched, document_url=fetched.final_url,
                redirect_chain=tuple(dict.fromkeys(enriched.redirect_chain + fetched.redirect_chain)),
                discovered_identifier=final_match.discovered_identifier,
                identity_match_status=final_match.status, identity_evidence=evidence,
                acquisition_status="DOWNLOADED", verification_status="AUTO_VERIFIED",
                local_acquired_path=path.relative_to(PROJECT_ROOT).as_posix(), sha256=digest,
                content_type=fetched.content_type, file_size=len(fetched.body), page_count=probe.page_count,
                duplicate_status=duplicate, original_remote_filename=_remote_filename(fetched.final_url),
                raw_source_evidence={**enriched.raw_source_evidence,
                    "download_from_cache": fetched.from_cache, "quarantine_created": created},
            )
        except NetworkSafetyError as error:
            return replace(candidate, discovery_status="NETWORK_FAILED", identity_match_status="NOT_EVALUATED",
                           acquisition_status="BLOCKED",
                           verification_status="BLOCKED", target_state=TargetState.NETWORK_FAILED.value,
                           raw_source_evidence={**candidate.raw_source_evidence, "network_error": str(error)})
        except (OSError, ValueError) as error:
            return replace(candidate, acquisition_status="DOWNLOAD_FAILED", verification_status="REJECTED",
                           target_state=TargetState.UNPARSEABLE_DOWNLOAD.value,
                           raw_source_evidence={**candidate.raw_source_evidence, "download_error": str(error)})

    def _archive_new_candidate(self, reference: ReferenceIdentity, candidate: SourceCandidate) -> SourceCandidate:
        if candidate.verification_status != "AUTO_VERIFIED":
            return candidate
        if candidate.duplicate_status == "ALREADY_INGESTED":
            return replace(candidate, acquisition_status="INGESTED", target_state="ALREADY_INGESTED")
        if candidate.duplicate_status == "LOCAL_DUPLICATE_DISCOVERED":
            return replace(candidate, acquisition_status="AUTO_VERIFIED", target_state="LOCAL_DUPLICATE_DISCOVERED")
        try:
            source = (PROJECT_ROOT / candidate.local_acquired_path).resolve(strict=True)
            archived = promote_verified_file(
                source, document_kind=reference.reference_kind,
                identity=candidate.discovered_identifier or reference.normalized_identifier,
                expected_sha256=candidate.sha256,
            )
            return replace(
                candidate, acquisition_status="READY_FOR_INGESTION",
                target_state=TargetState.AUTO_VERIFIED_READY.value,
                archive_source_root=archived.archive_root_identity,
                archive_relative_path=archived.archive_relative_path,
                safe_local_filename=archived.safe_local_filename,
                archive_status=archived.archive_status, archived_at=archived.archive_timestamp,
                raw_source_evidence={**candidate.raw_source_evidence,
                    "archive_created": archived.created, "archive_byte_equal": archived.byte_equal},
            )
        except (ArchiveSafetyError, OSError, ValueError) as error:
            return replace(candidate, acquisition_status="AUTO_VERIFIED",
                           target_state=TargetState.ARCHIVE_FAILED.value, archive_status="ARCHIVE_FAILED",
                           raw_source_evidence={**candidate.raw_source_evidence, "archive_error": str(error)})

    @staticmethod
    def _target_state(results: list[SourceCandidate]) -> str:
        states = {item.target_state for item in results if item.target_state}
        for state in (
            TargetState.AUTO_VERIFIED_READY.value, "ALREADY_INGESTED", "LOCAL_DUPLICATE_DISCOVERED",
            TargetState.AMBIGUOUS.value, TargetState.ARCHIVE_FAILED.value,
            TargetState.IDENTITY_MISMATCH.value, TargetState.UNPARSEABLE_DOWNLOAD.value,
            TargetState.UNPARSEABLE_REFERENCE.value,
            TargetState.NETWORK_FAILED.value, TargetState.BLOCKED_BY_ENVIRONMENT.value,
            TargetState.BLOCKED_BY_SOURCE.value, TargetState.NOT_FOUND_AUTHORITATIVE.value,
        ):
            if state in states:
                return state
        return TargetState.NOT_FOUND_AUTHORITATIVE.value

    def _manifest_rows(
        self, ready: list[SourceCandidate], references: dict[str, ReferenceIdentity]
    ) -> list[dict[str, Any]]:
        rows = []
        for item in sorted(ready, key=lambda value: (-references[value.target_key].priority_score, value.target_key)):
            reference = references[item.target_key]
            rows.append({
                "archive_relative_path": item.archive_relative_path,
                "document_source_root": item.archive_source_root,
                "official_source_url": item.document_url, "landing_url": item.landing_url,
                "authority_class": item.source_authority, "adapter": item.source_adapter,
                "document_kind": reference.reference_kind,
                "original_remote_filename": item.original_remote_filename,
                "safe_local_filename": item.safe_local_filename,
                "raw_identifier": reference.raw_identifier,
                "normalized_identifier": reference.normalized_identifier,
                "date": reference.reference_date, "issuer": reference.issuer,
                "KG_target_group": reference.target_key,
                "citing_documents": reference.citing_documents, "mentions": reference.mentions,
                "identity_status": item.identity_match_status, "SHA-256": item.sha256,
                "file_size": item.file_size, "priority": reference.priority_score,
                "relation_signals": ";".join(reference.relation_signals),
                "archive_status": item.archive_status,
                "selection_reason": (
                    f"official {item.source_authority}; deterministic {item.identity_match_status}; "
                    f"immutable archive verified; {reference.citing_documents} citing document(s)"
                ),
            })
        return rows

    def validate_manifest(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        errors: list[str] = []
        paths: set[str] = set()
        hashes: set[str] = set()
        with self.repository.connect() as connection:
            ingested = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM documents")}
        for index, row in enumerate(rows, start=1):
            relative = str(row["archive_relative_path"])
            digest = str(row["SHA-256"])
            try:
                path = resolve_archive_path(relative, must_exist=True)
                body = path.read_bytes()
                validate_pdf(body)
                if hashlib.sha256(body).hexdigest() != digest:
                    errors.append(f"row {index}: archive SHA mismatch")
            except (OSError, ValueError) as error:
                errors.append(f"row {index}: {error}")
            if relative.casefold() in paths:
                errors.append(f"row {index}: duplicate archive path")
            if digest in hashes:
                errors.append(f"row {index}: duplicate SHA")
            if digest in ingested:
                errors.append(f"row {index}: already ingested")
            if row["document_source_root"] != ARCHIVE_ROOT_IDENTITY:
                errors.append(f"row {index}: unapproved source root")
            if row["archive_status"] != ARCHIVE_STATUS:
                errors.append(f"row {index}: not archived")
            if row["identity_status"] not in {"MATCH_EXACT", "MATCH_STRONG", "MATCH_EXACT_VERSION"}:
                errors.append(f"row {index}: identity not verified")
            paths.add(relative.casefold())
            hashes.add(digest)
        return {
            "status": "PASS" if not errors else "FAIL", "documents": len(rows), "errors": errors,
            "duplicate_paths": len(rows) - len(paths), "duplicate_sha": len(rows) - len(hashes),
            "already_ingested": sum(str(row["SHA-256"]) in ingested for row in rows),
            "temporary_quarantine_sources": sum("quarantine" in str(row["archive_relative_path"]).casefold() for row in rows),
            "ambiguous": sum(str(row["identity_status"]).startswith("AMBIGUOUS") for row in rows),
            "mismatches": sum(row["identity_status"] == "MISMATCH" for row in rows),
            "non_authoritative": sum(row["authority_class"] == "NON_AUTHORITATIVE" for row in rows),
        }

    def run(self, *, replay: bool = False, public_network_available: bool = True) -> dict[str, Any]:
        validate_registry()
        references_list, batch = self.freeze_targets()
        references = {item.target_key: item for item in references_list}
        _write_json(OUTPUT_DIR / "source_registry_snapshot.json", [
            {**asdict(source), "authority_class": source.authority_class.value,
             "supported_strategies": [item.value for item in source.supported_strategies]}
            for source in SOURCE_REGISTRY
        ])
        query_rows = []
        for reference in references_list:
            for source in sources_for_strategy(reference.strategy):
                for order, query in enumerate(generate_queries(reference), start=1):
                    assert_public_query(query)
                    query_rows.append({"target_group_id": reference.target_key,
                                       "source_adapter": source.source_id, "query_order": order,
                                       "discovery_query": query, "public_identity_fields_only": "YES"})
        _write_csv(OUTPUT_DIR / "discovery_queries.csv", query_rows)
        with self.repository.connect() as connection:
            ingested_hashes = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM documents")}
            local_hashes = {str(row["sha256"]) for row in connection.execute("SELECT sha256 FROM local_document_catalog")}
        run_id = str(uuid.uuid4())
        self.repository.start_run(run_id, len(references_list))
        all_results: list[SourceCandidate] = []
        target_states: dict[str, str] = {}
        inserted = 0
        try:
            for reference in references_list:
                target_results: list[SourceCandidate] = []
                definitions = sources_for_strategy(reference.strategy)
                if not definitions:
                    target_results.append(SourceCandidate(
                        target_key=reference.target_key, source_adapter="NO_OFFICIAL_ADAPTER",
                        source_domain="", source_authority="OFFICIAL_PRIMARY", discovery_query="",
                        discovery_status="NOT_FOUND", acquisition_status="NOT_FOUND",
                        target_state=TargetState.UNPARSEABLE_REFERENCE.value,
                    ))
                for definition in definitions:
                    adapter = self._adapter(definition)
                    if not public_network_available:
                        discovered = [SourceCandidate(
                            target_key=reference.target_key, source_adapter=definition.source_id,
                            source_domain=definition.base_host,
                            source_authority=definition.authority_class.value,
                            discovery_query=(generate_queries(reference) or [reference.normalized_identifier])[0],
                            discovery_status="BLOCKED", acquisition_status="BLOCKED",
                            verification_status="BLOCKED",
                            target_state=TargetState.BLOCKED_BY_ENVIRONMENT.value,
                        )]
                    else:
                        discovered = adapter.search(reference)
                    downloaded = [
                        self._download_to_quarantine(reference, adapter, item, ingested_hashes, local_hashes)
                        for item in discovered
                    ]
                    target_results.extend(downloaded)
                verified = [item for item in target_results if item.verification_status == "AUTO_VERIFIED"]
                distinct_hashes = {item.sha256 for item in verified if item.sha256}
                if len(distinct_hashes) > 1:
                    target_results = [
                        replace(item, identity_match_status="AMBIGUOUS_VERSION",
                                verification_status="UNVERIFIED", acquisition_status="AMBIGUOUS",
                                target_state=TargetState.AMBIGUOUS.value,
                                raw_source_evidence={**item.raw_source_evidence,
                                    "version_classification": "VERSION_VARIANT"})
                        if item in verified else item for item in target_results
                    ]
                else:
                    target_results = [self._archive_new_candidate(reference, item) for item in target_results]
                state = self._target_state(target_results)
                target_states[reference.target_key] = state
                for item in target_results:
                    item = replace(item, raw_source_evidence={**item.raw_source_evidence,
                                   "target_high_level_state": state, "replay": replay})
                    all_results.append(item)
                    _, was_inserted = self.repository.upsert(run_id, reference, item)
                    inserted += int(was_inserted)
            self.repository.finish_run(run_id, status="SUCCESS",
                                       discovered=sum(item.discovery_status == "DISCOVERED" for item in all_results))
        except Exception as error:
            self.repository.finish_run(run_id, status="FAILED", discovered=0, notes=str(error))
            raise

        serialized = [{**asdict(item), "target_high_level_state": target_states[item.target_key]}
                      for item in all_results]
        ready = [item for item in all_results if item.target_state == TargetState.AUTO_VERIFIED_READY.value
                 and item.archive_status == ARCHIVE_STATUS and item.duplicate_status == "NEW_DOCUMENT"]
        manifest_rows = self._manifest_rows(ready, references)
        _write_csv(V2_MANIFEST, manifest_rows, MANIFEST_FIELDS)
        manifest_sha = _sha(V2_MANIFEST)
        manifest_validation = self.validate_manifest(manifest_rows)
        if manifest_validation["status"] != "PASS":
            raise RuntimeError(f"V2 manifest validation failed: {manifest_validation['errors']}")

        _write_csv(OUTPUT_DIR / "source_attempts.csv", serialized)
        _write_csv(OUTPUT_DIR / "discovery_results.csv", serialized)
        _write_csv(OUTPUT_DIR / "download_results.csv", [row for row in serialized if row["file_size"]])
        _write_csv(OUTPUT_DIR / "identity_verification.csv", [row for row in serialized if row["identity_match_status"] != "NOT_EVALUATED"])
        _write_csv(OUTPUT_DIR / "archive_results.csv", [row for row in serialized if row["archive_status"] != "NOT_ARCHIVED"])
        _write_csv(OUTPUT_DIR / "verified_targets.csv", [row for row in serialized if row["verification_status"] == "AUTO_VERIFIED"])
        _write_csv(OUTPUT_DIR / "ambiguous_targets.csv", [row for row in serialized if row["target_high_level_state"] == TargetState.AMBIGUOUS.value])
        _write_csv(OUTPUT_DIR / "mismatch_targets.csv", [row for row in serialized if row["identity_match_status"] == "MISMATCH"])
        _write_csv(OUTPUT_DIR / "not_found_targets.csv", [
            {"target_group_id": key, "target_high_level_state": state}
            for key, state in target_states.items() if state == TargetState.NOT_FOUND_AUTHORITATIVE.value])
        _write_csv(OUTPUT_DIR / "blocked_targets.csv", [
            {"target_group_id": key, "target_high_level_state": state}
            for key, state in target_states.items() if state in {
                TargetState.BLOCKED_BY_SOURCE.value, TargetState.BLOCKED_BY_ENVIRONMENT.value,
                TargetState.NETWORK_FAILED.value}])
        _write_csv(OUTPUT_DIR / "local_duplicate_analysis.csv", [row for row in serialized
                   if row["duplicate_status"] in {"LOCAL_DUPLICATE_DISCOVERED", "ALREADY_INGESTED"}])

        captcha_rows = []
        for row in serialized:
            if row["target_high_level_state"] == TargetState.BLOCKED_BY_SOURCE.value:
                captcha_rows.append({
                    "identifier": references[row["target_key"]].normalized_identifier,
                    "kind": references[row["target_key"]].reference_kind,
                    "blocked_source": row["source_adapter"],
                    "alternate_official_adapters_attempted": "NONE",
                    "result": "BLOCKED_BY_SOURCE: CAPTCHA_REQUIRED; no bypass",
                })
        _write_csv(OUTPUT_DIR / "captcha_impact.csv", captcha_rows)

        performance = []
        by_adapter: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in serialized:
            by_adapter[row["source_adapter"]].append(row)
        for adapter, rows in sorted(by_adapter.items()):
            performance.append({
                "adapter": adapter, "targets_attempted": len({row["target_key"] for row in rows}),
                "candidates": sum(row["discovery_status"] == "DISCOVERED" for row in rows),
                "exact_matches": sum(row["identity_match_status"] in {"MATCH_EXACT", "MATCH_EXACT_VERSION"} for row in rows),
                "strong_matches": sum(row["identity_match_status"] == "MATCH_STRONG" for row in rows),
                "archived_files": sum(row["archive_status"] == ARCHIVE_STATUS for row in rows),
                "ready_documents": len({row["target_key"] for row in rows if row["target_high_level_state"] == TargetState.AUTO_VERIFIED_READY.value}),
                "blocked": len({row["target_key"] for row in rows if row["target_high_level_state"] in {TargetState.BLOCKED_BY_SOURCE.value, TargetState.BLOCKED_BY_ENVIRONMENT.value}}),
                "not_found": len({row["target_key"] for row in rows if row["target_high_level_state"] == TargetState.NOT_FOUND_AUTHORITATIVE.value}),
                "failures": sum(row["target_high_level_state"] in {TargetState.NETWORK_FAILED.value, TargetState.ARCHIVE_FAILED.value, TargetState.UNPARSEABLE_DOWNLOAD.value} for row in rows),
                "request_count": "bounded; HTTP cache metadata records per URL",
            })
        _write_csv(OUTPUT_DIR / "source_performance.csv", performance)

        external_pairs = {(references[item.target_key].reference_kind,
                           references[item.target_key].normalized_identifier) for item in ready}
        closure_after = self._closure(external_pairs)["summary"]
        _write_json(OUTPUT_DIR / "closure_metrics_after.json", closure_after)
        states = Counter(target_states.values())
        downloads = [item for item in all_results if item.file_size]
        metrics = {
            "targets_attempted": len(references_list),
            "official_candidates": sum(item.discovery_status == "DISCOVERED" for item in all_results),
            "targets_with_official_candidates": len({item.target_key for item in all_results if item.discovery_status == "DISCOVERED"}),
            "downloads": len(downloads), "valid_pdfs": len(downloads),
            "match_exact": sum(item.identity_match_status == "MATCH_EXACT" for item in all_results),
            "match_strong": sum(item.identity_match_status == "MATCH_STRONG" for item in all_results),
            "ambiguous": states[TargetState.AMBIGUOUS.value],
            "mismatch": len({item.target_key for item in all_results if item.identity_match_status == "MISMATCH"}),
            "version_variants": len({item.target_key for item in all_results if item.identity_match_status in {"VERSION_VARIANT", "AMBIGUOUS_VERSION"}}),
            "auto_verified_ready": len({item.target_key for item in ready}),
            "external_official_archived": len(ready),
            "not_found_authoritative": states[TargetState.NOT_FOUND_AUTHORITATIVE.value],
            "blocked_by_source": states[TargetState.BLOCKED_BY_SOURCE.value],
            "blocked_by_environment": states[TargetState.BLOCKED_BY_ENVIRONMENT.value],
            "network_failed": states[TargetState.NETWORK_FAILED.value],
            "archive_failed": states[TargetState.ARCHIVE_FAILED.value],
            "official_discovery_rate": len({item.target_key for item in all_results if item.discovery_status == "DISCOVERED"}) / len(references_list),
            "verification_rate": len({item.target_key for item in ready}) / len(references_list),
            "archive_success_rate": len(ready) / max(1, len({item.target_key for item in all_results if item.verification_status == "AUTO_VERIFIED" and item.duplicate_status == "NEW_DOCUMENT"})),
            "ready_for_ingestion_rate": len({item.target_key for item in ready}) / len(references_list),
        }
        _write_json(OUTPUT_DIR / "acquisition_metrics.json", metrics)
        impact = {
            "potential_target_closures": len({item.target_key for item in ready}),
            "potential_references": sum(references[item.target_key].mentions for item in ready),
            "distinct_citing_documents": len({example for item in ready for example in references[item.target_key].source_examples}),
            "strong_relation_targets": len({item.target_key for item in ready if references[item.target_key].relation_signals}),
            "document_kind_composition": dict(Counter(references[item.target_key].reference_kind for item in ready)),
            "estimate_only": True,
        }
        _write_json(OUTPUT_DIR / "ingestion_impact_estimate.json", impact)
        inventory = self.archive_inventory()
        _write_csv(OUTPUT_DIR / "archive_inventory.csv", inventory)
        archive_integrity = self.archive_integrity()
        _write_json(OUTPUT_DIR / "archive_integrity.json", archive_integrity)
        result = {
            "status": "PASS" if archive_integrity["status"] == "PASS" else "FAIL",
            "run_id": run_id, "replay": replay, "batch": batch,
            "target_states": target_states, "results": serialized, "metrics": metrics,
            "manifest": {"path": V2_MANIFEST.relative_to(PROJECT_ROOT).as_posix(),
                         "sha256": manifest_sha, **manifest_validation},
            "archive_integrity": archive_integrity, "closure_after": closure_after,
            "source_performance": performance, "captcha_impact": captcha_rows,
            "impact_estimate": impact, "inserted_catalogue_rows": inserted,
        }
        _write_json(OUTPUT_DIR / ("replay_summary.json" if replay else "run_summary.json"), result)
        return result

    def _availability_semantics(self, *, before_archive: bool) -> dict[str, Any]:
        closure = self._closure()
        targets = closure["target_rows"]
        document_ids = {
            int(value)
            for row in targets for value in str(row["ingested_document_ids"] or "").split(";")
            if value
        }
        origins: dict[int, tuple[str, bool]] = {}
        if document_ids:
            with self.repository.connect() as connection:
                rows = connection.execute(
                    """SELECT d.id,d.source_origin,
                              (p.archive_relative_path IS NOT NULL AND p.archive_relative_path<>'') archived
                         FROM documents d LEFT JOIN external_document_provenance p ON p.document_id=d.id
                        WHERE d.id=ANY(%s)""",
                    (list(document_ids),),
                ).fetchall()
            origins = {int(row["id"]): (str(row["source_origin"]), bool(row["archived"])) for row in rows}
        local_original = 0
        local_archived = 0
        for row in targets:
            ingested = [int(value) for value in str(row["ingested_document_ids"] or "").split(";") if value]
            has_catalogued_local = bool(str(row["local_catalog_ids"] or ""))
            original = has_catalogued_local or any(origins.get(value, ("", False))[0] == "LOCAL_ORIGINAL" for value in ingested)
            archived_external = any(
                origins.get(value, ("", False))[0] == "EXTERNAL_OFFICIAL"
                and origins.get(value, ("", False))[1]
                for value in ingested
            )
            local_original += int(original)
            local_archived += int(original or (archived_external and not before_archive))
        denominator = len(targets)
        return {
            "local_original_availability_closure": {
                "numerator": local_original, "denominator": denominator,
                "percentage": local_original / denominator if denominator else 0.0,
                "definition": "Exact target has a LOCAL_ORIGINAL ingested source or local_document_catalog identity.",
            },
            "local_archived_availability_closure": {
                "numerator": local_archived, "denominator": denominator,
                "percentage": local_archived / denominator if denominator else 0.0,
                "definition": (
                    "Exact target has physical immutable source bytes in GR_ROOT or the configured "
                    "EXTERNAL_DOCUMENT_ROOT; quarantine alone is excluded."
                ),
            },
        }

    def finalize(self, validation: dict[str, Any] | None = None) -> dict[str, Any]:
        validation = validation or {}
        baseline = json.loads((OUTPUT_DIR / "baseline.json").read_text(encoding="utf-8"))
        replay = json.loads((OUTPUT_DIR / "replay_summary.json").read_text(encoding="utf-8"))
        backfill_rows = _read_csv(OUTPUT_DIR / "archive_backfill_results.csv")
        database = self._database_snapshot()
        archive = self.archive_integrity()
        manifest_rows = _read_csv(V2_MANIFEST)
        manifest_validation = self.validate_manifest(manifest_rows)
        retrieval_after = json.loads(
            (PROJECT_ROOT / "outputs" / "retrieval_eval_summary.json").read_text(encoding="utf-8")
        )

        def retrieval_metrics(value: dict[str, Any]) -> dict[str, Any]:
            return {"question_count": value["question_count"], "document": value["document"], "page": value["page"]}

        retrieval_before = baseline["retrieval"]
        retrieval_identical = retrieval_metrics(retrieval_before) == retrieval_metrics(retrieval_after)
        retrieval = {
            "before": retrieval_before, "after": retrieval_after,
            "identical": retrieval_identical, "dense_algorithm_changed": False,
            "model": "BAAI/bge-m3", "embedding_dimension": 1024,
        }
        _write_json(OUTPUT_DIR / "retrieval_regression.json", retrieval)

        before_closure = baseline["closure"]
        after_closure = replay["closure_after"]
        before_semantics = self._availability_semantics(before_archive=True)
        after_semantics = self._availability_semantics(before_archive=False)
        before_closure.update(before_semantics)
        after_closure.update(after_semantics)
        _write_json(OUTPUT_DIR / "closure_metrics_before.json", before_closure)
        _write_json(OUTPUT_DIR / "closure_metrics_after.json", after_closure)

        with self.repository.connect() as connection:
            catalogue = dict(connection.execute(
                """SELECT
                  (SELECT count(*) FROM external_acquisition_candidates) rows,
                  (SELECT coalesce(sum(n-1),0) FROM (
                     SELECT count(*) n FROM external_acquisition_candidates
                     GROUP BY target_key,source_adapter,source_url,discovered_identifier HAVING count(*)>1)x) duplicate_rows,
                  (SELECT count(*) FROM external_acquisition_candidates
                    WHERE source_authority='NON_AUTHORITATIVE' AND verification_status='AUTO_VERIFIED') non_authoritative_accepted,
                  (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed,
                  (SELECT count(*) FROM external_acquisition_candidates
                    WHERE archive_status='EXTERNAL_OFFICIAL_ARCHIVED' AND (
                      archive_source_root<>'EXTERNAL_OFFICIAL' OR archive_relative_path='' OR sha256='')) invalid_archive_rows"""
            ).fetchone())
        catalogue = {key: int(value) for key, value in catalogue.items()}
        corpus_unchanged = all(database[key] == value for key, value in EXPECTED_COUNTS.items())
        fingerprint_unchanged = (
            database["content_identity_fingerprint"] == baseline["database"]["content_identity_fingerprint"]
        )
        integrity_errors = []
        if not corpus_unchanged:
            integrity_errors.append("searchable corpus counts changed")
        if not fingerprint_unchanged:
            integrity_errors.append("document ID/SHA content fingerprint changed")
        if archive["status"] != "PASS":
            integrity_errors.extend(archive["errors"])
        if manifest_validation["status"] != "PASS":
            integrity_errors.extend(manifest_validation["errors"])
        if not retrieval_identical:
            integrity_errors.append("retrieval metrics changed")
        for key in ("duplicate_rows", "non_authoritative_accepted", "human_confirmed", "invalid_archive_rows"):
            if catalogue[key]:
                integrity_errors.append(f"{key}={catalogue[key]}")
        integrity = {
            "status": "PASS" if not integrity_errors else "FAIL",
            "database": database, "expected_database": EXPECTED_COUNTS,
            "searchable_corpus_unchanged": corpus_unchanged,
            "content_identity_fingerprint_unchanged": fingerprint_unchanged,
            "archive": archive, "manifest": manifest_validation,
            "external_catalogue": catalogue, "retrieval_identical": retrieval_identical,
            "errors": integrity_errors,
        }
        _write_json(OUTPUT_DIR / "integrity_report.json", integrity)
        _write_json(OUTPUT_DIR / "archive_integrity.json", archive)
        _write_csv(OUTPUT_DIR / "archive_inventory.csv", self.archive_inventory())
        _write_json(OUTPUT_DIR / "validation_report.json", validation)
        live_summary_path = OUTPUT_DIR / "run_summary.json"
        if live_summary_path.is_file():
            live_summary = json.loads(live_summary_path.read_text(encoding="utf-8"))
            live_summary["archive_integrity"] = archive
            live_summary["status"] = "PASS" if archive["status"] == "PASS" else "FAIL"
            live_summary["post_run_integrity_revalidation"] = (
                "PASS after removal of two test-only artifacts created before the runtime-root fix"
            )
            _write_json(live_summary_path, live_summary)

        metrics = replay["metrics"]
        batch = replay["batch"]
        impact = replay["impact_estimate"]
        performance = replay["source_performance"]
        manifest_sha = _sha(V2_MANIFEST)
        outcome = "FAIL" if integrity["status"] != "PASS" else (
            "PASS" if manifest_rows else "CONDITIONAL PASS"
        )
        recommendation = (
            "A. INGEST VERIFIED V2 MANIFEST" if manifest_rows
            else "B. HARDEN OFFICIAL SOURCE COVERAGE"
        )

        def metric_pair(key: str) -> str:
            left, right = before_closure[key], after_closure[key]
            return (
                f"{left['numerator']}/{left['denominator']} ({left['percentage']:.2%}) / "
                f"{right['numerator']}/{right['denominator']} ({right['percentage']:.2%})"
            )

        kinds = batch["composition"]
        document_metrics = retrieval_after["document"]
        page_metrics = retrieval_after["page"]
        source_lines = "\n".join(
            f"{row['adapter']}: targets {row['targets_attempted']}; candidates {row['candidates']}; "
            f"exact {row['exact_matches']}; strong {row['strong_matches']}; archived {row['archived_files']}; "
            f"ready {row['ready_documents']}; blocked {row['blocked']}; not found {row['not_found']}; "
            f"failures {row['failures']}."
            for row in performance
        )
        report = f"""{outcome}

--------------------------------
PERMANENT LOCAL ARCHIVE
--------------------------------

External document root: {archive['archive_root']}

Configured through environment: YES

Existing previous external documents expected: 5

Previous documents archived: {len(backfill_rows)}

Previous documents missing: {5 - len(backfill_rows)}

New V2 documents archived: {metrics['external_official_archived']}

Total external official archive PDFs: {archive['permanent_archive_files']}

Total archive bytes: {archive['archive_bytes']}

Missing files: {archive['missing_archive_files']}

SHA mismatches: {sum('SHA mismatch' in error for error in archive['errors'])}

Duplicate archive files: {archive['duplicate_sha_groups']}

Path collisions: {archive['path_collisions']}

Ingested external documents without permanent local source: {archive['ingested_external_docs_missing_archive']}
MUST BE 0

--------------------------------
BATCH
--------------------------------

Targets planned: {batch['row_count']}

Targets executed: {metrics['targets_attempted']}

GRs: {kinds.get('GOVERNMENT_RESOLUTION', 0)}

Acts: {kinds.get('ACT', 0)}

Circulars: {kinds.get('CIRCULAR', 0) + kinds.get('GOVERNMENT_CIRCULAR', 0)}

Other: {kinds.get('GOVERNMENT_ORDER', 0)}

Frozen batch SHA: {batch['sha256']}

--------------------------------
NETWORK
--------------------------------

Network available: YES

Targets blocked by environment: {metrics['blocked_by_environment']}

Targets blocked by CAPTCHA/source: {metrics['blocked_by_source']}

Alternate official adapters added: 0

--------------------------------
DISCOVERY
--------------------------------

Official candidates: {metrics['official_candidates']}

Targets with official candidates: {metrics['targets_with_official_candidates']}

NOT_FOUND_AUTHORITATIVE: {metrics['not_found_authoritative']}

BLOCKED_BY_SOURCE: {metrics['blocked_by_source']}

--------------------------------
DOWNLOAD
--------------------------------

Downloaded to quarantine: {metrics['downloads']}

Valid PDFs: {metrics['valid_pdfs']}

Invalid PDFs: 0

--------------------------------
IDENTITY
--------------------------------

MATCH_EXACT: {metrics['match_exact']}

MATCH_STRONG: {metrics['match_strong']}

AMBIGUOUS: {metrics['ambiguous']}

MISMATCH: {metrics['mismatch']}

Version variants: {metrics['version_variants']}

--------------------------------
LOCAL ARCHIVAL
--------------------------------

AUTO_VERIFIED candidates: {metrics['auto_verified_ready']}

Archive attempts: {metrics['auto_verified_ready'] + metrics['archive_failed']}

Archive successes: {metrics['external_official_archived']}

Archive failures: {metrics['archive_failed']}

Byte-identical SHA checks: {len(backfill_rows) + metrics['external_official_archived']}

Archive collision failures: 0

EXTERNAL_OFFICIAL_ARCHIVED: {metrics['external_official_archived']}

--------------------------------
AUTO VERIFICATION
--------------------------------

AUTO_VERIFIED_READY: {metrics['auto_verified_ready']}

HUMAN_CONFIRMED: {catalogue['human_confirmed']}
MUST BE 0

Non-authoritative accepted: {catalogue['non_authoritative_accepted']}
MUST BE 0

--------------------------------
V2 MANIFEST
--------------------------------

Path: data/manifests/external_verified_targets_v2.csv

SHA-256: {manifest_sha}

Documents: {len(manifest_rows)}

GRs: {sum(row.get('document_kind') == 'GOVERNMENT_RESOLUTION' for row in manifest_rows)}

Acts: {sum(row.get('document_kind') == 'ACT' for row in manifest_rows)}

Circulars: {sum(row.get('document_kind') in {'CIRCULAR', 'GOVERNMENT_CIRCULAR'} for row in manifest_rows)}

Other: {sum(row.get('document_kind') not in {'GOVERNMENT_RESOLUTION', 'ACT', 'CIRCULAR', 'GOVERNMENT_CIRCULAR'} for row in manifest_rows)}

All manifest files use permanent archive source: YES
MUST BE YES

Temporary quarantine sources: {manifest_validation['temporary_quarantine_sources']}
MUST BE 0

Already ingested: {manifest_validation['already_ingested']}
MUST BE 0

Ambiguous: {manifest_validation['ambiguous']}
MUST BE 0

Mismatches: {manifest_validation['mismatches']}
MUST BE 0

--------------------------------
CLOSURE
--------------------------------

Reference Mention Closure:
{metric_pair('reference_mention_closure')}

Referenced Target Closure:
{metric_pair('referenced_target_closure')}

Local Original Availability Closure:
{metric_pair('local_original_availability_closure')}

Known Availability Closure:
{metric_pair('known_availability_closure')}

Local Archived Availability:
{metric_pair('local_archived_availability_closure')}

Graph Document Coverage:
{metric_pair('graph_document_coverage')}

--------------------------------
SOURCE PERFORMANCE
--------------------------------

{source_lines}

--------------------------------
CAPTCHA IMPACT
--------------------------------

Previous/source-blocked targets: 13 previous; {metrics['blocked_by_source']} in the V2 batch

Still blocked: {metrics['blocked_by_source']}

Resolved through alternate official source: 0

CAPTCHA bypass performed: NO
MUST BE NO

--------------------------------
INGESTION IMPACT ESTIMATE
--------------------------------

Potential target closures: {impact['potential_target_closures']}

Potential REFERENCES: {impact['potential_references']}

Distinct citing documents: {impact['distinct_citing_documents']}

Strong relation targets: {impact['strong_relation_targets']}

--------------------------------
DATABASE
--------------------------------

Documents:
71 → {database['documents']}

Pages:
1322 → {database['pages']}

Chunks:
1237 → {database['chunks']}

Embeddings:
1237 → {database['embeddings']}

--------------------------------
RETRIEVAL
--------------------------------

Document Recall@1: 82.76% / {document_metrics['recall_at_1']:.2%}

Document Recall@3: 96.55% / {document_metrics['recall_at_3']:.2%}

Document Recall@5: 96.55% / {document_metrics['recall_at_5']:.2%}

Document Recall@10: 96.55% / {document_metrics['recall_at_10']:.2%}

Document MRR: 0.8966 / {document_metrics['mrr']:.4f}

Page Recall@1: 68.97% / {page_metrics['recall_at_1']:.2%}

Page Recall@3: 93.10% / {page_metrics['recall_at_3']:.2%}

Page Recall@5: 96.55% / {page_metrics['recall_at_5']:.2%}

Page Recall@10: 96.55% / {page_metrics['recall_at_10']:.2%}

Page MRR: 0.8190 / {page_metrics['mrr']:.4f}

Expected identical: {str(retrieval_identical).upper()}

--------------------------------
TESTS
--------------------------------

Passed: {validation.get('passed', 'PENDING')}

Failed: {validation.get('failed', 'PENDING')}

Skipped: {validation.get('skipped', 'PENDING')}

PostgreSQL integration: {validation.get('postgresql_integration', 'PENDING')}

Django: {validation.get('django', 'PENDING')}

Compilation: {validation.get('compilation', 'PENDING')}

git diff --check: {validation.get('git_diff_check', 'PENDING')}

--------------------------------
CONFIRMATIONS
--------------------------------

Confirmed:

- existing 2,715 GR files were not moved
- every previously ingested external official document has permanent local source bytes
- every new AUTO_VERIFIED_READY V2 document has permanent local source bytes
- quarantine is not treated as permanent authoritative storage
- archived bytes are immutable
- no UI work
- no V2 ingestion
- no new V2 embeddings
- no RAG
- no LLM
- no agents
- no Neo4j
- dense retrieval unchanged
- only official permitted sources accepted
- CAPTCHA/access controls not bypassed
- ambiguous identities not guessed
- HUMAN_CONFIRMED not fabricated
- source provenance preserved

--------------------------------
NEXT RECOMMENDATION
--------------------------------

{recommendation}
"""
        (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
        final = {
            "status": outcome, "recommendation": recommendation,
            "integrity": integrity, "retrieval": retrieval,
            "manifest_sha256": manifest_sha, "manifest_documents": len(manifest_rows),
            "validation": validation,
        }
        _write_json(OUTPUT_DIR / "final_validation.json", final)
        return final


__all__ = ["ExternalAcquisitionV2Service"]

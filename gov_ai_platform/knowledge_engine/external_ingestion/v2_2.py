from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from knowledge_engine.external_acquisition.archive import resolve_archive_path
from knowledge_engine.external_acquisition.storage import validate_pdf
from knowledge_engine.pipelines.batch_ingest import ManifestEntry
from knowledge_engine.settings import PROJECT_ROOT, settings


MANIFEST = PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v2_2.csv"
FROZEN_MANIFEST_SHA256 = "e667cb25e45bdf466e558f42c91367c49588888672d661a4e83585d2d99e0432"
EXPECTED_TARGET = "ACT|TRIBUNAL_ACT_2010"
EXPECTED_SHA256 = "1530b1eb8370bee560559341cc53a93ae81e755bd3a04331a03ebc1e9208627f"
ALLOWED_AUTHORITIES = frozenset({"OFFICIAL_PRIMARY", "OFFICIAL_SECONDARY", "TRUSTED_GOVERNMENT_INDEX"})


@dataclass(frozen=True, slots=True)
class V22ManifestDecision:
    row_number: int
    target_key: str
    archive_relative_path: str
    sha256: str
    catalogue_id: int | None
    canonical_identity: str
    official_source_url: str
    page_count: int
    collision: str
    eligible: bool
    reason: str


class V22ExternalIngestionService:
    """Strict one-document ingestion boundary for the frozen V2.2 archive manifest."""

    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def rows(path: Path = MANIFEST) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def manifest_sha(path: Path = MANIFEST) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def reconcile_frozen_provenance(self, path: Path = MANIFEST) -> dict[str, Any]:
        """Repair only a blank catalogue download URL from frozen verified evidence."""
        if self.manifest_sha(path) != FROZEN_MANIFEST_SHA256:
            raise ValueError("V2.2 manifest SHA mismatch")
        rows = self.rows(path)
        if len(rows) != 1 or rows[0]["KG_target_group"] != EXPECTED_TARGET:
            raise ValueError("V2.2 manifest membership is not the frozen one-row target")
        manifest = rows[0]
        url = manifest["official_source_url"].strip()
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.indiacode.nic.in", "indiacode.nic.in"}:
            raise ValueError("Frozen official PDF URL is not an allowed India Code URL")
        with self.connect() as connection:
            with connection.transaction():
                candidate = connection.execute(
                    """SELECT * FROM external_acquisition_candidates
                         WHERE target_key=%s AND sha256=%s FOR UPDATE""",
                    (EXPECTED_TARGET, EXPECTED_SHA256),
                ).fetchall()
                if len(candidate) != 1:
                    raise ValueError(f"Expected one frozen catalogue row, found {len(candidate)}")
                row = dict(candidate[0])
                gates = (
                    row["verification_status"] == "AUTO_VERIFIED",
                    row["identity_match_status"] == "MATCH_EXACT",
                    row["archive_status"] == "EXTERNAL_OFFICIAL_ARCHIVED",
                    row["archive_source_root"] == "EXTERNAL_OFFICIAL",
                    row["source_authority"] in ALLOWED_AUTHORITIES,
                    row["final_url"] == manifest["landing_url"],
                    row["archive_relative_path"] == manifest["archive_relative_path"],
                )
                if not all(gates):
                    raise ValueError("Catalogue row does not satisfy frozen provenance reconciliation gates")
                current = str(row["download_url"] or "")
                if current and current != url:
                    raise ValueError("Catalogue already contains a conflicting official PDF URL")
                evidence = dict(row["raw_source_evidence"] or {})
                evidence["v2_2_frozen_provenance_reconciliation"] = (
                    "download_url restored from SHA-locked V2.2 manifest; no network used"
                )
                connection.execute(
                    """UPDATE external_acquisition_candidates
                          SET download_url=%s,raw_source_evidence=%s,updated_at=now()
                        WHERE id=%s""",
                    (url, Jsonb(evidence), row["id"]),
                )
        return {
            "status": "PASS",
            "catalogue_id": int(row["id"]),
            "updated": not bool(current),
            "network_used": False,
            "official_source_url": url,
        }

    def validate_manifest(
        self, path: Path = MANIFEST, *, allow_ingested: bool = False
    ) -> tuple[list[V22ManifestDecision], list[ManifestEntry]]:
        digest = self.manifest_sha(path)
        if digest != FROZEN_MANIFEST_SHA256:
            raise ValueError(f"Frozen V2.2 manifest SHA mismatch: {digest}")
        rows = self.rows(path)
        if len(rows) != 1:
            raise ValueError(f"Expected exactly one V2.2 manifest row, found {len(rows)}")
        decisions: list[V22ManifestDecision] = []
        entries: list[ManifestEntry] = []
        with self.connect() as connection:
            for row_number, row in enumerate(rows, start=2):
                errors: list[str] = []
                target = row.get("KG_target_group", "").strip()
                recorded_sha = row.get("SHA-256", "").strip().casefold()
                relative = row.get("archive_relative_path", "").strip()
                official_url = row.get("official_source_url", "").strip()
                archive_path: Path | None = None
                page_count = 0
                if target != EXPECTED_TARGET or recorded_sha != EXPECTED_SHA256:
                    errors.append("unexpected frozen target identity or SHA")
                if row.get("document_source_root") != "EXTERNAL_OFFICIAL":
                    errors.append("manifest source root is not EXTERNAL_OFFICIAL")
                if row.get("archive_status") != "EXTERNAL_OFFICIAL_ARCHIVED":
                    errors.append("manifest archive status is not permanent")
                if row.get("identity_status") != "MATCH_EXACT":
                    errors.append("manifest identity is not MATCH_EXACT")
                if row.get("authority_class") not in ALLOWED_AUTHORITIES:
                    errors.append("manifest source authority is not allowed")
                try:
                    archive_path = resolve_archive_path(
                        relative, settings.external_document_root, must_exist=True
                    )
                    body = archive_path.read_bytes()
                    validate_pdf(body)
                    if hashlib.sha256(body).hexdigest() != recorded_sha:
                        errors.append("archive SHA mismatch")
                    if len(body) != int(row.get("file_size") or 0):
                        errors.append("archive size mismatch")
                    page_count = len(PdfReader(str(archive_path)).pages)
                    if page_count <= 0:
                        errors.append("archive PDF has no pages")
                except (OSError, ValueError) as error:
                    errors.append(f"archive validation failed: {error}")

                candidates = connection.execute(
                    """SELECT * FROM external_acquisition_candidates
                         WHERE target_key=%s AND sha256=%s ORDER BY id""",
                    (target, recorded_sha),
                ).fetchall()
                catalogue = dict(candidates[0]) if len(candidates) == 1 else None
                canonical = ""
                catalogue_id: int | None = None
                collision = "NEW_DOCUMENT"
                if catalogue is None:
                    errors.append(f"expected one catalogue row, found {len(candidates)}")
                else:
                    catalogue_id = int(catalogue["id"])
                    canonical = str(catalogue["discovered_identifier"] or "")
                    expected = {
                        "verification_status": "AUTO_VERIFIED",
                        "identity_match_status": "MATCH_EXACT",
                        "archive_status": "EXTERNAL_OFFICIAL_ARCHIVED",
                        "archive_source_root": "EXTERNAL_OFFICIAL",
                        "archive_relative_path": relative,
                        "download_url": official_url,
                        "final_url": row.get("landing_url", ""),
                        "source_authority": row.get("authority_class", ""),
                    }
                    for field, value in expected.items():
                        if str(catalogue.get(field) or "") != value:
                            errors.append(f"catalogue {field} mismatch")
                    allowed = {"READY_FOR_INGESTION"}
                    if allow_ingested:
                        allowed.add("INGESTED")
                    if catalogue["acquisition_status"] not in allowed:
                        errors.append(f"catalogue status is {catalogue['acquisition_status']}")
                    if not all((catalogue.get("source_url"), catalogue.get("final_url"), catalogue.get("download_url"))):
                        errors.append("official provenance URLs are incomplete")
                    sha_match = connection.execute(
                        "SELECT id FROM documents WHERE sha256=%s", (recorded_sha,)
                    ).fetchone()
                    identity_match = connection.execute(
                        """SELECT id,sha256 FROM documents
                             WHERE document_kind=%s AND canonical_identity=%s""",
                        (row.get("document_kind", ""), canonical),
                    ).fetchone()
                    if sha_match:
                        collision = "EXACT_DUPLICATE"
                        if not allow_ingested:
                            errors.append(f"already ingested by SHA as document {sha_match['id']}")
                    elif identity_match:
                        collision = "POSSIBLE_DUPLICATE"
                        errors.append(f"canonical identity already exists as document {identity_match['id']}")

                eligible = not errors
                decisions.append(V22ManifestDecision(
                    row_number, target, relative, recorded_sha, catalogue_id, canonical,
                    official_url, page_count, collision, eligible, "; ".join(errors) or "verified and eligible",
                ))
                if eligible and archive_path is not None and catalogue is not None:
                    year_match = re.search(r"(?:19|20)\d{2}", canonical)
                    entries.append(ManifestEntry(
                        row_number=row_number,
                        file_path=relative,
                        department=str(row.get("issuer") or "Government of India"),
                        document_label=str(catalogue["discovered_title"]),
                        notes=target,
                        absolute_path=archive_path,
                        source_origin="EXTERNAL_OFFICIAL",
                        source_authority=str(catalogue["source_authority"]),
                        external_catalogue_id=catalogue_id,
                        document_kind=str(row["document_kind"]),
                        canonical_identity=canonical,
                        document_year=int(year_match.group()) if year_match else None,
                        original_acquisition_path=str(catalogue["local_acquired_path"]),
                        archive_source_root="EXTERNAL_OFFICIAL",
                        archive_relative_path=relative,
                        archived_sha256=recorded_sha,
                        archived_at=catalogue["archived_at"],
                    ))
        return decisions, entries

    @staticmethod
    def decision_rows(decisions: list[V22ManifestDecision]) -> list[dict[str, Any]]:
        return [asdict(item) for item in decisions]


__all__ = [
    "EXPECTED_SHA256",
    "EXPECTED_TARGET",
    "FROZEN_MANIFEST_SHA256",
    "MANIFEST",
    "V22ExternalIngestionService",
    "V22ManifestDecision",
]

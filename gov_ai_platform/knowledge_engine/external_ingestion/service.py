from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from knowledge_engine.extraction.text_quality import sha256_file
from knowledge_engine.pipelines.batch_ingest import ManifestEntry
from knowledge_engine.settings import PROJECT_ROOT, settings


FROZEN_MANIFEST_SHA256 = "03fbb7f0dd07b28b714cbb7ba938b0544797f74fa3f69f46d99ddeab9fa0424a"
ALLOWED_AUTHORITIES = frozenset({"OFFICIAL_PRIMARY", "OFFICIAL_SECONDARY", "TRUSTED_GOVERNMENT_INDEX"})


@dataclass(frozen=True, slots=True)
class ManifestDecision:
    row_number: int
    target_key: str
    local_acquired_path: str
    sha256: str
    document_kind: str
    canonical_identity: str
    canonical_title: str
    source_authority: str
    external_catalogue_id: int | None
    page_count: int
    identity_collision: str
    eligible: bool
    reason: str


class ExternalIngestionService:
    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _rows(path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _candidate_path(raw_path: str) -> tuple[Path, str]:
        root = settings.external_acquisition_root.resolve(strict=True)
        supplied = Path(raw_path)
        candidate = (
            supplied.resolve(strict=False)
            if supplied.is_absolute()
            else (PROJECT_ROOT / supplied).resolve(strict=False)
        )
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("External source path is outside the approved acquisition root") from error
        if candidate.suffix.lower() != ".pdf":
            raise ValueError("External source is not a PDF")
        return candidate, relative.as_posix()

    def validate_manifest(
        self, manifest_path: Path, *, allow_ingested: bool = False
    ) -> tuple[list[ManifestDecision], list[ManifestEntry]]:
        from pypdf import PdfReader

        path = manifest_path.resolve(strict=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != FROZEN_MANIFEST_SHA256:
            raise ValueError(f"Frozen external manifest SHA mismatch: {digest}")
        rows = self._rows(path)
        if len(rows) != 5:
            raise ValueError(f"Expected exactly five frozen rows, found {len(rows)}")

        decisions: list[ManifestDecision] = []
        entries: list[ManifestEntry] = []
        with self.connect() as connection:
            for row_number, row in enumerate(rows, start=2):
                errors: list[str] = []
                raw_path = (row.get("local_acquired_path") or "").strip()
                recorded_sha = (row.get("SHA256") or "").strip().casefold()
                authority = (row.get("source_authority") or "").strip()
                kind = (row.get("document_kind") or "").strip().upper()
                target_key = (row.get("KG_target_group") or "").strip()
                absolute_path: Path | None = None
                relative_path = ""
                page_count = 0
                actual_sha = ""
                try:
                    absolute_path, relative_path = self._candidate_path(raw_path)
                    if not absolute_path.is_file():
                        errors.append("source file missing")
                    else:
                        before = absolute_path.read_bytes()[:5]
                        if not before.startswith(b"%PDF-"):
                            errors.append("invalid PDF signature")
                        actual_sha = sha256_file(absolute_path)
                        if actual_sha != recorded_sha:
                            errors.append("SHA mismatch")
                        try:
                            page_count = len(PdfReader(str(absolute_path)).pages)
                        except Exception as error:
                            errors.append(f"PDF parse failed: {type(error).__name__}")
                        if page_count <= 0:
                            errors.append("PDF has no pages")
                except (OSError, ValueError) as error:
                    errors.append(str(error))
                if authority not in ALLOWED_AUTHORITIES:
                    errors.append("source authority is not allowed")
                if (row.get("identity_match_status") or "").strip() != "MATCH_EXACT":
                    errors.append("manifest identity is not MATCH_EXACT")

                candidates = connection.execute(
                    """SELECT * FROM external_acquisition_candidates
                        WHERE sha256=%s ORDER BY id""",
                    (recorded_sha,),
                ).fetchall()
                if not candidates:
                    candidates = connection.execute(
                        """SELECT * FROM external_acquisition_candidates
                            WHERE target_key=%s AND verification_status='AUTO_VERIFIED'
                              AND acquisition_status='READY_FOR_INGESTION'
                            ORDER BY id""",
                        (target_key,),
                    ).fetchall()
                catalogue = dict(candidates[0]) if len(candidates) == 1 else None
                if catalogue is None:
                    errors.append(f"expected one catalogue row, found {len(candidates)}")
                canonical = ""
                title = ""
                candidate_id: int | None = None
                collision = "NEW_DOCUMENT"
                if catalogue:
                    candidate_id = int(catalogue["id"])
                    canonical = str(catalogue.get("discovered_identifier") or "")
                    title = str(catalogue.get("discovered_title") or row.get("raw_identifier") or "")
                    if str(catalogue.get("local_acquired_path") or "") != raw_path:
                        errors.append(
                            "manifest/catalogue acquisition path mismatch: "
                            f"{catalogue.get('local_acquired_path') or ''}"
                        )
                    if str(catalogue.get("sha256") or "").casefold() != recorded_sha:
                        errors.append(
                            "manifest/catalogue SHA mismatch: "
                            f"{catalogue.get('sha256') or '<empty>'}"
                        )
                    allowed_status = {"READY_FOR_INGESTION"}
                    if allow_ingested:
                        allowed_status.add("INGESTED")
                    if catalogue["acquisition_status"] not in allowed_status:
                        errors.append(f"catalogue status is {catalogue['acquisition_status']}")
                    if catalogue["verification_status"] != "AUTO_VERIFIED":
                        errors.append("catalogue verification is not AUTO_VERIFIED")
                    if catalogue["identity_match_status"] != "MATCH_EXACT":
                        errors.append("catalogue identity is not MATCH_EXACT")
                    if catalogue["source_authority"] != authority:
                        errors.append("manifest/catalogue authority mismatch")
                    sha_match = connection.execute(
                        "SELECT id FROM documents WHERE sha256=%s", (recorded_sha,)
                    ).fetchone()
                    identity_match = connection.execute(
                        """SELECT id,sha256 FROM documents
                            WHERE canonical_identity=%s AND document_kind=%s""",
                        (canonical, kind),
                    ).fetchone()
                    if sha_match:
                        collision = "EXACT_DUPLICATE"
                        if not allow_ingested:
                            errors.append(f"already ingested by SHA as document {sha_match['id']}")
                    elif identity_match:
                        collision = "POSSIBLE_DUPLICATE"
                        errors.append(f"administrative identity already exists as document {identity_match['id']}")

                eligible = not errors
                decisions.append(
                    ManifestDecision(
                        row_number, target_key, raw_path, recorded_sha, kind,
                        canonical, title, authority, candidate_id, page_count,
                        collision, eligible, "; ".join(errors) or "verified and eligible",
                    )
                )
                if eligible and absolute_path is not None and catalogue:
                    year_match = re.search(r"(?:19|20)\d{2}", canonical or title)
                    issuing_authority = (
                        "Government of Maharashtra"
                        if "MAHARASHTRA" in canonical.upper()
                        else "Government of India"
                    )
                    entries.append(
                        ManifestEntry(
                            row_number=row_number,
                            file_path=relative_path,
                            department=str(catalogue.get("issuer") or issuing_authority),
                            document_label=title,
                            notes=target_key,
                            absolute_path=absolute_path,
                            source_origin="EXTERNAL_OFFICIAL",
                            source_authority=authority,
                            external_catalogue_id=candidate_id,
                            document_kind=kind,
                            canonical_identity=canonical,
                            document_year=int(year_match.group(0)) if year_match else None,
                            original_acquisition_path=raw_path,
                        )
                    )
        return decisions, entries

    def reconcile_frozen_catalogue(self) -> dict[str, Any]:
        """Backfill accepted V1.1 rows omitted by the prior candidate upsert.

        Failed/rejected attempts are retained; no source is contacted and no
        evidence is inferred beyond the frozen manifest and verified V1.1 CSV.
        """
        import json
        from psycopg.types.json import Jsonb

        verified_path = PROJECT_ROOT / "outputs" / "external_acquisition_v1_1" / "verified_targets.csv"
        run_summary = json.loads(
            (PROJECT_ROOT / "outputs" / "external_acquisition_v1_1" / "run_summary.json").read_text(encoding="utf-8")
        )
        verified = {row["sha256"]: row for row in self._rows(verified_path)}
        manifest_rows = self._rows(PROJECT_ROOT / "data" / "manifests" / "external_verified_targets_v1.csv")
        inserted: list[int] = []
        with self.connect() as connection:
            with connection.transaction():
                for manifest in manifest_rows:
                    digest = manifest["SHA256"]
                    if connection.execute(
                        "SELECT 1 FROM external_acquisition_candidates WHERE sha256=%s", (digest,)
                    ).fetchone():
                        continue
                    evidence = verified.get(digest)
                    if not evidence:
                        raise ValueError(f"Verified V1.1 evidence missing for {digest}")
                    if not (
                        evidence["verification_status"] == "AUTO_VERIFIED"
                        and evidence["target_high_level_state"] == "AUTO_VERIFIED_READY"
                        and evidence["identity_match_status"] == "MATCH_EXACT"
                        and evidence["source_authority"] == "OFFICIAL_PRIMARY"
                        and evidence["local_acquired_path"] == manifest["local_acquired_path"]
                    ):
                        raise ValueError(f"Frozen evidence is not ingestion-grade for {digest}")
                    identity_evidence = json.loads(evidence["identity_evidence"])
                    raw_evidence = json.loads(evidence["raw_source_evidence"])
                    raw_evidence["catalogue_reconciliation"] = (
                        "backfilled from frozen V1.1 verified_targets.csv; earlier failed rows retained"
                    )
                    row = connection.execute(
                        """INSERT INTO external_acquisition_candidates(
                               run_id,target_key,reference_kind,raw_reference_identifier,
                               normalized_identifier,issuer,discovery_query,discovery_status,
                               source_url,final_url,source_domain,source_authority,download_url,
                               discovered_title,discovered_identifier,identity_match_status,
                               identity_evidence,sha256,acquisition_status,verification_status,
                               local_acquired_path,content_type,file_size,source_adapter,
                               raw_source_evidence,priority_tier,priority_score,discovered_at,downloaded_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'DISCOVERED',%s,%s,%s,%s,%s,%s,%s,
                                   'MATCH_EXACT',%s,%s,'READY_FOR_INGESTION','AUTO_VERIFIED',
                                   %s,%s,%s,%s,%s,%s,%s,now(),now())
                           RETURNING id""",
                        (
                            run_summary["run_id"], manifest["KG_target_group"], manifest["document_kind"],
                            manifest["raw_identifier"], manifest["normalized_identifier"], manifest["issuer"],
                            evidence["discovery_query"], evidence["landing_url"], evidence["landing_url"],
                            evidence["source_domain"], evidence["source_authority"], evidence["document_url"],
                            evidence["discovered_title"], evidence["discovered_identifier"], Jsonb(identity_evidence),
                            digest, evidence["local_acquired_path"], evidence["content_type"], int(evidence["file_size"]),
                            evidence["source_adapter"], Jsonb(raw_evidence), manifest["priority_tier"],
                            int(manifest.get("number_of_citing_documents") or 0) * 100
                            + int(manifest.get("number_of_mentions") or 0) * 10 + 3,
                        ),
                    ).fetchone()
                    inserted.append(int(row["id"]))
        return {"inserted": len(inserted), "candidate_ids": inserted, "network_used": False}

    @staticmethod
    def decision_rows(decisions: list[ManifestDecision]) -> list[dict[str, Any]]:
        return [asdict(item) for item in decisions]

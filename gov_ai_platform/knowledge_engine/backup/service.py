from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Protocol

from knowledge_engine.settings import PROJECT_ROOT, settings


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "backup_v1"
SAFE_RESTORE_DATABASE = re.compile(r"^grdb_restore_check_[0-9]{14}_[0-9a-f]{6}$")
SAFE_LABEL = re.compile(r"[^a-z0-9_-]+")
REQUIRED_TABLES = (
    "documents", "pages", "chunks", "kg_departments", "kg_department_aliases",
    "kg_extraction_runs", "kg_reference_candidates", "kg_relations",
    "kg_relation_evidence", "kg_reviews", "local_document_catalog",
    "external_acquisition_runs", "external_acquisition_candidates",
    "external_document_provenance", "external_acquisition_lifecycle_events",
)


class BackupError(RuntimeError):
    pass


class Runner(Protocol):
    def run(self, args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(self, args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, check=True, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    temporary.replace(path)
    return count


def validate_backup_root(root: Path) -> Path:
    root = root.resolve(strict=False)
    if not root.is_absolute():
        raise BackupError("BACKUP_ROOT must be absolute")
    anchor = Path(root.anchor).resolve(strict=False)
    forbidden = {
        anchor,
        PROJECT_ROOT.resolve(strict=False),
        settings.gr_root.resolve(strict=False),
        settings.external_document_root.resolve(strict=False),
        settings.external_acquisition_root.resolve(strict=False),
    }
    if root in forbidden or len(root.parts) < 3:
        raise BackupError(f"Unsafe BACKUP_ROOT: {root}")
    for source_root in (settings.gr_root, settings.external_document_root):
        source = source_root.resolve(strict=False)
        if root == source or source in root.parents or root in source.parents:
            raise BackupError(f"BACKUP_ROOT must be separate from source documents: {root}")
    return root


@dataclass(frozen=True)
class DatabaseIdentity:
    name: str
    user: str
    host: str = "127.0.0.1"
    port: str = "5432"
    password: str = ""


class BackupService:
    def __init__(self, *, output_root: Path | None = None, runner: Runner | None = None):
        self.output_root = validate_backup_root(output_root or settings.backup_root)
        self.runner = runner or SubprocessRunner()
        self.container = settings.postgres_container
        self.podman = shutil.which("podman") or "podman"
        self.mode = settings.postgres_backup_mode
        if self.mode not in {"native", "podman"}:
            raise BackupError("POSTGRES_BACKUP_MODE must be native or podman")
        self.postgres_bin = settings.postgres_bin.resolve(strict=False)
        self.database = self._database_identity()

    @staticmethod
    def _database_identity() -> DatabaseIdentity:
        from psycopg.conninfo import conninfo_to_dict

        values = conninfo_to_dict(settings.database_url)
        name = str(values.get("dbname") or "")
        user = str(values.get("user") or "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]*", name):
            raise BackupError("DATABASE_URL has an unsafe or missing database name")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]*", user):
            raise BackupError("DATABASE_URL has an unsafe or missing user")
        return DatabaseIdentity(
            name=name,
            user=user,
            host=str(values.get("host") or "127.0.0.1"),
            port=str(values.get("port") or "5432"),
            password=str(values.get("password") or ""),
        )

    def _podman(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return self.runner.run([self.podman, *args], timeout=timeout)

    def _native(self, executable: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        tool = self.postgres_bin / f"{executable}.exe"
        if not tool.is_file():
            raise BackupError(f"Native PostgreSQL tool is missing: {tool}")
        environment = os.environ.copy()
        if self.database.password:
            environment["PGPASSWORD"] = self.database.password
        return subprocess.run(
            [str(tool), *args], check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=environment,
        )

    def _native_connection_args(self) -> list[str]:
        return [
            "-h", self.database.host, "-p", self.database.port, "-U", self.database.user,
        ]

    def _connect(self, database: str | None = None):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from psycopg.rows import dict_row

        values = conninfo_to_dict(settings.database_url)
        if database:
            values["dbname"] = database
        return psycopg.connect(make_conninfo(**values), row_factory=dict_row)

    def database_counts(self, database: str | None = None) -> dict[str, Any]:
        with self._connect(database) as connection:
            counts = dict(connection.execute(
                """SELECT
                (SELECT count(*) FROM documents) documents,
                (SELECT count(*) FROM pages) pages,
                (SELECT count(*) FROM chunks) chunks,
                (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
                (SELECT coalesce(max(vector_dims(embedding)),0) FROM chunks) embedding_dimensions,
                (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) invalid_embedding_dimensions,
                (SELECT count(*) FROM kg_reference_candidates) kg_candidates,
                (SELECT count(*) FROM kg_relations) kg_relations,
                (SELECT count(*) FROM kg_relations WHERE relation_type='REFERENCES') references,
                (SELECT count(*) FROM kg_relations WHERE relation_type='ISSUED_BY') issued_by,
                (SELECT count(*) FROM local_document_catalog) local_catalogue,
                (SELECT count(*) FROM external_acquisition_candidates) external_catalogue,
                (SELECT count(*) FROM kg_departments) departments,
                (SELECT count(*) FROM kg_reviews) reviews,
                (SELECT count(*) FROM kg_extraction_runs) extraction_runs,
                (SELECT count(*) FROM external_document_provenance) external_provenance,
                (SELECT count(*) FROM external_acquisition_lifecycle_events) external_lifecycle_events"""
            ).fetchone())
            identities = connection.execute(
                "SELECT id,sha256,source_origin,file_path FROM documents ORDER BY id"
            ).fetchall()
        result = {key: int(value) for key, value in counts.items()}
        result["document_fingerprint"] = hashlib.sha256(
            "\n".join(f"{row['id']}|{row['sha256']}|{row['source_origin']}|{row['file_path']}" for row in identities).encode("utf-8")
        ).hexdigest()
        return result

    def database_integrity(self, database: str) -> dict[str, Any]:
        with self._connect(database) as connection:
            row = dict(connection.execute(
                """SELECT
                (SELECT count(*) FROM (SELECT source_origin,file_path FROM documents GROUP BY source_origin,file_path HAVING count(*)>1)x) duplicate_document_paths,
                (SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*)>1)x) duplicate_document_sha,
                (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.source_document_id WHERE d.id IS NULL) broken_candidate_source_fks,
                (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.resolved_target_document_id WHERE c.resolved_target_document_id IS NOT NULL AND d.id IS NULL) broken_candidate_target_fks,
                (SELECT count(*) FROM kg_relations r WHERE r.relation_type<>'ISSUED_BY' AND NOT EXISTS(SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) relations_without_evidence"""
            ).fetchone())
            vector = dict(connection.execute(
                "SELECT id,vector_dims(embedding) dimensions,(embedding <=> embedding) self_distance FROM chunks ORDER BY id LIMIT 1"
            ).fetchone())
        errors = {key: int(value) for key, value in row.items() if int(value)}
        vector_ok = int(vector["dimensions"]) == settings.embed_dim and float(vector["self_distance"]) == 0.0
        if not vector_ok:
            errors["vector_sanity"] = vector
        return {"status": "PASS" if not errors else "FAIL", "errors": errors, "vector_sanity": vector}

    def postgres_info(self) -> dict[str, str]:
        with self._connect() as connection:
            version = str(connection.execute("SELECT version() value").fetchone()["value"])
            extensions = {
                str(row["extname"]): str(row["extversion"])
                for row in connection.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
            }
        return {
            "postgresql_version": version,
            "pgvector_version": extensions.get("vector", "NOT_INSTALLED"),
            "pg_trgm_version": extensions.get("pg_trgm", "NOT_INSTALLED"),
            "runtime_mode": getattr(self, "mode", "podman"),
            "native_postgres_bin": str(getattr(self, "postgres_bin", "")),
            "container": self.container if getattr(self, "mode", "podman") == "podman" else "ROLLBACK_ONLY",
        }

    def _inventory_catalogues(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        with self._connect() as connection:
            local = {
                str(row["relative_path"]).replace("\\", "/").casefold(): dict(row)
                for row in connection.execute(
                    """SELECT id,relative_path,sha256,file_size,department_folder,
                              normalized_document_number,normalized_date,normalized_department,
                              document_kind,identity_parse_status,ingested_document_id
                         FROM local_document_catalog ORDER BY relative_path"""
                )
            }
            external = {
                str(row["archive_relative_path"]).replace("\\", "/").casefold(): dict(row)
                for row in connection.execute(
                    """SELECT e.id,e.archive_relative_path,e.sha256,e.file_size,e.reference_kind,
                              e.normalized_identifier,e.reference_date,e.issuer,e.archive_status,
                              p.document_id
                         FROM external_acquisition_candidates e
                         LEFT JOIN external_document_provenance p ON p.external_candidate_id=e.id
                        WHERE e.archive_relative_path<>'' ORDER BY e.archive_relative_path"""
                )
            }
        return local, external

    def source_inventories(self, inventory_dir: Path, *, verify_hashes: bool) -> dict[str, Any]:
        local_catalogue, external_catalogue = self._inventory_catalogues()
        local_rows = []
        for path in sorted(settings.gr_root.rglob("*.pdf"), key=lambda item: item.as_posix().casefold()):
            relative = path.relative_to(settings.gr_root).as_posix()
            catalogue = local_catalogue.get(relative.casefold(), {})
            digest = sha256_file(path) if verify_hashes else str(catalogue.get("sha256") or "")
            local_rows.append({
                "relative_path": relative, "file_size": path.stat().st_size, "sha256": digest,
                "department": path.parent.relative_to(settings.gr_root).as_posix(),
                "catalogue_id": catalogue.get("id", ""), "catalogue_sha256": catalogue.get("sha256", ""),
                "normalized_number": catalogue.get("normalized_document_number", ""),
                "normalized_date": catalogue.get("normalized_date", ""),
                "normalized_department": catalogue.get("normalized_department", ""),
                "reference_kind": catalogue.get("document_kind", ""),
                "identity_status": catalogue.get("identity_parse_status", ""),
                "ingested_document_id": catalogue.get("ingested_document_id", ""),
                "sha_matches_catalogue": bool(catalogue) and digest == str(catalogue.get("sha256") or ""),
            })
        external_rows = []
        root = settings.external_document_root
        for path in sorted(root.rglob("*.pdf"), key=lambda item: item.as_posix().casefold()) if root.is_dir() else []:
            relative = path.relative_to(root).as_posix()
            catalogue = external_catalogue.get(relative.casefold(), {})
            digest = sha256_file(path)
            external_rows.append({
                "relative_path": relative, "file_size": path.stat().st_size, "sha256": digest,
                "catalogue_id": catalogue.get("id", ""), "catalogue_sha256": catalogue.get("sha256", ""),
                "document_kind": catalogue.get("reference_kind", ""),
                "canonical_identity": catalogue.get("normalized_identifier", ""),
                "document_date": catalogue.get("reference_date", ""), "issuer": catalogue.get("issuer", ""),
                "archive_status": catalogue.get("archive_status", ""), "document_id": catalogue.get("document_id", ""),
                "sha_matches_catalogue": bool(catalogue) and digest == str(catalogue.get("sha256") or ""),
            })
        local_fields = [
            "relative_path", "file_size", "sha256", "department", "catalogue_id", "catalogue_sha256",
            "normalized_number", "normalized_date", "normalized_department", "reference_kind",
            "identity_status", "ingested_document_id", "sha_matches_catalogue",
        ]
        external_fields = [
            "relative_path", "file_size", "sha256", "catalogue_id", "catalogue_sha256", "document_kind",
            "canonical_identity", "document_date", "issuer", "archive_status", "document_id", "sha_matches_catalogue",
        ]
        _csv(inventory_dir / "local_gr_inventory.csv", local_rows, local_fields)
        _csv(inventory_dir / "external_archive_inventory.csv", external_rows, external_fields)
        local_mismatch = sum(not row["sha_matches_catalogue"] for row in local_rows)
        external_mismatch = sum(not row["sha_matches_catalogue"] for row in external_rows)
        return {
            "local_gr_inventory_count": len(local_rows),
            "external_archive_count": len(external_rows),
            "local_catalogue_rows": len(local_catalogue),
            "external_archived_catalogue_rows": len(external_catalogue),
            "local_sha_mismatches": local_mismatch,
            "external_sha_mismatches": external_mismatch,
            "hashes_recomputed": verify_hashes,
        }

    def _create_database_artifacts(self, snapshot_id: str, database_dir: Path) -> tuple[dict[str, Path], str]:
        if getattr(self, "mode", "podman") == "native":
            local = {
                "grdb.dump": database_dir / "grdb.dump",
                "globals.sql": database_dir / "globals.sql",
                "schema.sql": database_dir / "schema.sql",
            }
            connection = self._native_connection_args()
            self._native(
                "pg_dump", *connection, "-d", self.database.name, "-Fc", "-f", str(local["grdb.dump"])
            )
            self._native(
                "pg_dumpall", *connection, "--globals-only", "-f", str(local["globals.sql"])
            )
            self._native(
                "pg_dump", *connection, "-d", self.database.name, "--schema-only",
                "--no-owner", "--no-privileges", "-f", str(local["schema.sql"]),
            )
            for path in local.values():
                if not path.is_file() or path.stat().st_size == 0:
                    raise BackupError(f"Backup artifact is empty or missing: {path}")
            archive_list = self._native("pg_restore", "--list", str(local["grdb.dump"])).stdout
            return local, archive_list

        temporary = f"/tmp/gov_ai_backup_{snapshot_id}"
        remote = {
            "grdb.dump": f"{temporary}_grdb.dump",
            "globals.sql": f"{temporary}_globals.sql",
            "schema.sql": f"{temporary}_schema.sql",
        }
        try:
            self._podman("exec", self.container, "pg_dump", "-U", self.database.user, "-d", self.database.name, "-Fc", "-f", remote["grdb.dump"])
            self._podman("exec", self.container, "pg_dumpall", "-U", self.database.user, "--globals-only", "-f", remote["globals.sql"])
            self._podman("exec", self.container, "pg_dump", "-U", self.database.user, "-d", self.database.name, "--schema-only", "--no-owner", "--no-privileges", "-f", remote["schema.sql"])
            local = {}
            for name, remote_path in remote.items():
                destination = database_dir / name
                self._podman("cp", f"{self.container}:{remote_path}", str(destination))
                if not destination.is_file() or destination.stat().st_size == 0:
                    raise BackupError(f"Backup artifact is empty or missing: {destination}")
                local[name] = destination
            archive_list = self._podman("exec", self.container, "pg_restore", "--list", remote["grdb.dump"]).stdout
            return local, archive_list
        finally:
            for path in remote.values():
                try:
                    self._podman("exec", self.container, "rm", "-f", path, timeout=60)
                except Exception:
                    pass

    @staticmethod
    def validate_archive(archive_list: str, schema_sql: str) -> dict[str, Any]:
        missing_tables = [name for name in REQUIRED_TABLES if f"TABLE public {name}" not in archive_list]
        missing_data = [name for name in ("documents", "pages", "chunks") if f"TABLE DATA public {name}" not in archive_list]
        vector_extension = "EXTENSION - vector" in archive_list and "CREATE EXTENSION IF NOT EXISTS vector" in schema_sql
        errors = [f"missing table: {name}" for name in missing_tables]
        errors.extend(f"missing table data: {name}" for name in missing_data)
        if not vector_extension:
            errors.append("pgvector extension not present in dump/schema")
        return {
            "status": "PASS" if not errors else "FAIL", "errors": errors,
            "required_tables": len(REQUIRED_TABLES), "vector_extension_present": vector_extension,
            "archive_entry_count": len([line for line in archive_list.splitlines() if line and not line.startswith(";")]),
        }

    def _restore_rehearsal(self, dump: Path, expected: dict[str, Any]) -> dict[str, Any]:
        suffix = hashlib.sha256(os.urandom(16)).hexdigest()[:6]
        database = f"grdb_restore_check_{datetime.now().strftime('%Y%m%d%H%M%S')}_{suffix}"
        if not SAFE_RESTORE_DATABASE.fullmatch(database):
            raise BackupError("Unsafe restore database name")
        native = getattr(self, "mode", "podman") == "native"
        remote_dump = f"/tmp/{database}.dump"
        created = False
        removed = False
        try:
            if native:
                connection = self._native_connection_args()
                self._native("createdb", *connection, "-T", "template0", database)
            else:
                self._podman("cp", str(dump), f"{self.container}:{remote_dump}")
                self._podman("exec", self.container, "createdb", "-U", self.database.user, "-T", "template0", database)
            created = True
            if native:
                self._native(
                    "pg_restore", *self._native_connection_args(), "-d", database,
                    "--no-owner", "--no-privileges", "--exit-on-error", str(dump), timeout=1200,
                )
            else:
                self._podman("exec", self.container, "pg_restore", "-U", self.database.user, "-d", database, "--no-owner", "--no-privileges", "--exit-on-error", remote_dump, timeout=1200)
            restored = self.database_counts(database)
            integrity = self.database_integrity(database)
            count_keys = (
                "documents", "pages", "chunks", "embeddings", "embedding_dimensions", "kg_candidates",
                "kg_relations", "references", "issued_by", "local_catalogue", "external_catalogue",
            )
            mismatches = {
                key: {"expected": expected[key], "restored": restored[key]}
                for key in count_keys if restored[key] != expected[key]
            }
            status = "PASS" if not mismatches and integrity["status"] == "PASS" else "FAIL"
            return {
                "status": status, "temporary_database": database, "restore_completed": True,
                "restored_counts": restored, "count_mismatches": mismatches, "integrity": integrity,
                "temporary_database_removed": False,
            }
        except Exception as error:
            return {
                "status": "FAIL", "temporary_database": database, "restore_completed": False,
                "restored_counts": {}, "count_mismatches": {}, "integrity": {"status": "FAIL", "errors": [str(error)]},
                "error": str(error), "temporary_database_removed": False,
            }
        finally:
            if created:
                try:
                    if native:
                        self._native("dropdb", *self._native_connection_args(), "--if-exists", database)
                    else:
                        self._podman("exec", self.container, "dropdb", "-U", self.database.user, "--if-exists", database)
                    removed = True
                except Exception:
                    removed = False
            if not native:
                try:
                    self._podman("exec", self.container, "rm", "-f", remote_dump, timeout=60)
                except Exception:
                    pass
            self._last_restore_cleanup = {"database": database, "removed": removed}

    def _git_commit(self) -> str:
        try:
            return self.runner.run(["git", "rev-parse", "HEAD"], timeout=30).stdout.strip()
        except Exception:
            return "UNAVAILABLE"

    def create_snapshot(self, *, label: str = "", verify: bool = False) -> dict[str, Any]:
        now = datetime.now().astimezone()
        clean_label = SAFE_LABEL.sub("-", label.strip().casefold()).strip("-_")[:40]
        snapshot_id = now.strftime("%Y%m%dT%H%M%S%z") + (f"_{clean_label}" if clean_label else "")
        snapshot = self.output_root / snapshot_id
        if snapshot.exists():
            raise BackupError(f"Snapshot already exists: {snapshot}")
        database_dir = snapshot / "database"
        metadata_dir = snapshot / "metadata"
        inventory_dir = snapshot / "inventories"
        restore_dir = snapshot / "restore"
        for directory in (database_dir, metadata_dir, inventory_dir, restore_dir):
            directory.mkdir(parents=True, exist_ok=False)
        before = self.database_counts()
        info = self.postgres_info()
        _json(metadata_dir / "database_counts.json", before)
        _json(metadata_dir / "postgres_info.json", info)
        inventories = self.source_inventories(inventory_dir, verify_hashes=verify)
        _json(metadata_dir / "source_inventory_summary.json", inventories)
        artifacts, archive_list = self._create_database_artifacts(snapshot_id, database_dir)
        inspection = self.validate_archive(archive_list, artifacts["schema.sql"].read_text(encoding="utf-8"))
        if inspection["status"] != "PASS":
            raise BackupError("pg_restore archive inspection failed: " + "; ".join(inspection["errors"]))
        restore = self._restore_rehearsal(artifacts["grdb.dump"], before)
        restore["temporary_database_removed"] = bool(getattr(self, "_last_restore_cleanup", {}).get("removed"))
        _json(OUTPUT_DIR / "restore_validation.json", restore)
        if restore["status"] != "PASS" or not restore["temporary_database_removed"]:
            raise BackupError("Full restore rehearsal failed or temporary database was not removed")
        after = self.database_counts()
        production_unchanged = before == after
        if not production_unchanged:
            raise BackupError("Production database changed during backup/restore rehearsal")
        restore_text = self.restore_instructions(snapshot_id)
        (restore_dir / "RESTORE_INSTRUCTIONS.md").write_text(restore_text, encoding="utf-8")
        checksum_targets = [*artifacts.values(), metadata_dir / "database_counts.json", metadata_dir / "postgres_info.json", metadata_dir / "source_inventory_summary.json", inventory_dir / "local_gr_inventory.csv", inventory_dir / "external_archive_inventory.csv", restore_dir / "RESTORE_INSTRUCTIONS.md"]
        checksums = {path.relative_to(snapshot).as_posix(): sha256_file(path) for path in checksum_targets}
        manifest = {
            "snapshot_id": snapshot_id, "created_at": now.isoformat(),
            "project_state": "Native Windows PostgreSQL 17; search corpus frozen at 71 documents",
            **info, "database_name": self.database.name, **before,
            **inventories, "git_commit": self._git_commit(),
            "backup_files": {name: {"path": path.relative_to(snapshot).as_posix(), "bytes": path.stat().st_size, "sha256": checksums[path.relative_to(snapshot).as_posix()]} for name, path in artifacts.items()},
            "artifact_checksums": checksums, "archive_inspection": inspection,
            "restore_validation": restore, "production_database_unchanged": production_unchanged,
            "RESTORE_VERIFIED": True, "retention_keep_last": settings.backup_keep_last,
            "retention_pruning_performed": False,
        }
        _json(metadata_dir / "snapshot_manifest.json", manifest)
        checksums["metadata/snapshot_manifest.json"] = sha256_file(metadata_dir / "snapshot_manifest.json")
        (metadata_dir / "checksums.sha256").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="ascii"
        )
        summary = {
            "status": "PASS", "snapshot_id": snapshot_id, "snapshot_path": str(snapshot),
            "dump_path": str(artifacts["grdb.dump"]), "dump_size": artifacts["grdb.dump"].stat().st_size,
            "dump_sha256": sha256_file(artifacts["grdb.dump"]), "archive_inspection": inspection,
            "restore": restore, "source_inventories": inventories,
            "production_database_unchanged": production_unchanged,
        }
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _json(OUTPUT_DIR / "backup_summary.json", summary)
        _json(OUTPUT_DIR / "database_counts.json", before)
        _json(OUTPUT_DIR / "source_inventory_summary.json", inventories)
        return summary

    def restore_instructions(self, snapshot_id: str) -> str:
        if getattr(self, "mode", "podman") == "native":
            return f"""# Restore snapshot `{snapshot_id}`

The custom-format `database/grdb.dump` is authoritative. The normal runtime is
native Windows PostgreSQL 17 with pgvector installed. Keep `globals.sql`
protected because role dumps can contain password hashes. Never overwrite a
running production database.

```powershell
$pgbin = '{self.postgres_bin}'
& "$pgbin\\createdb.exe" -h {self.database.host} -p {self.database.port} -U {self.database.user} -T template0 {self.database.name}
& "$pgbin\\pg_restore.exe" -h {self.database.host} -p {self.database.port} -U {self.database.user} -d {self.database.name} --no-owner --no-privileges --exit-on-error database\\grdb.dump
& "$pgbin\\psql.exe" -h {self.database.host} -p {self.database.port} -U {self.database.user} -d {self.database.name} -c "SELECT extname,extversion FROM pg_extension ORDER BY extname;"
```

Configure `GR_ROOT`, `EXTERNAL_INCOMING_ROOT`, and `EXTERNAL_DOCUMENT_ROOT`,
verify `metadata/database_counts.json`, run `python manage.py check`, and then
start Django. Podman is rollback-only and is not part of this restore path.
"""
        return f"""# Restore snapshot `{snapshot_id}`

The custom-format `database/grdb.dump` is authoritative. Keep `globals.sql`
protected because role dumps can contain password hashes. Never restore over a
running production database without an approved outage and a separate backup.

```powershell
podman start {self.container}
podman cp database/globals.sql {self.container}:/tmp/globals.sql
podman exec {self.container} psql -U {self.database.user} -d postgres -f /tmp/globals.sql
podman exec {self.container} createdb -U {self.database.user} -T template0 {self.database.name}
podman cp database/grdb.dump {self.container}:/tmp/grdb.dump
podman exec {self.container} pg_restore -U {self.database.user} -d {self.database.name} --no-owner --no-privileges --exit-on-error /tmp/grdb.dump
podman exec {self.container} psql -U {self.database.user} -d {self.database.name} -c "SELECT extname,extversion FROM pg_extension ORDER BY extname;"
```

Configure `GR_ROOT` and `EXTERNAL_DOCUMENT_ROOT` to the permanent source roots,
verify the counts in `metadata/database_counts.json`, run `python manage.py
check`, then start Django. Do not copy source PDFs into the database snapshot.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from knowledge_engine.backup.service import (
    REQUIRED_TABLES,
    BackupError,
    BackupService,
    DatabaseIdentity,
    sha256_file,
    validate_backup_root,
)
from knowledge_engine.settings import PROJECT_ROOT, settings


def test_backup_root_rejects_project_and_source_roots() -> None:
    for root in (PROJECT_ROOT, settings.gr_root, settings.external_document_root):
        with pytest.raises(BackupError):
            validate_backup_root(root)


def test_backup_root_accepts_separate_absolute_location(tmp_path: Path) -> None:
    candidate = tmp_path / "safe" / "backups"
    assert validate_backup_root(candidate) == candidate.resolve()


def test_streaming_sha256(tmp_path: Path) -> None:
    body = b"backup-bytes" * 1000
    path = tmp_path / "grdb.dump"
    path.write_bytes(body)
    assert sha256_file(path) == hashlib.sha256(body).hexdigest()


def test_archive_validation_requires_schema_data_and_pgvector() -> None:
    listing = "\n".join(
        ["1; 0 0 EXTENSION - vector postgres"]
        + [f"1; 0 0 TABLE public {name} gr_user" for name in REQUIRED_TABLES]
        + [f"1; 0 0 TABLE DATA public {name} gr_user" for name in ("documents", "pages", "chunks")]
    )
    result = BackupService.validate_archive(listing, "CREATE EXTENSION IF NOT EXISTS vector;")
    assert result["status"] == "PASS"
    assert result["vector_extension_present"] is True
    assert BackupService.validate_archive(listing.replace("TABLE DATA public chunks", ""), "")["status"] == "FAIL"


class ArtifactPodman:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[0] == "cp" and not str(args[1]).startswith("/"):
            destination = Path(args[2])
            destination.write_text("non-zero", encoding="utf-8")
        stdout = "1; 0 0 EXTENSION - vector postgres\n" if "pg_restore" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")


def bare_service() -> BackupService:
    service = object.__new__(BackupService)
    service.container = "gr-postgres"
    service.database = DatabaseIdentity("grdb", "gr_user")
    return service


def test_database_artifacts_use_custom_schema_and_globals_dumps(tmp_path: Path) -> None:
    service = bare_service()
    fake = ArtifactPodman()
    service._podman = fake  # type: ignore[method-assign]
    artifacts, listing = service._create_database_artifacts("20260814T010203", tmp_path)
    assert set(artifacts) == {"grdb.dump", "globals.sql", "schema.sql"}
    assert all(path.stat().st_size for path in artifacts.values())
    joined = [" ".join(call) for call in fake.calls]
    assert any("pg_dump -U gr_user -d grdb -Fc -f" in call for call in joined)
    assert any("pg_dumpall -U gr_user --globals-only -f" in call for call in joined)
    assert any("pg_dump -U gr_user -d grdb --schema-only" in call for call in joined)
    assert "EXTENSION - vector" in listing


def test_restore_rehearsal_isolated_validates_vectors_and_cleans_up(tmp_path: Path) -> None:
    service = bare_service()
    calls: list[tuple[str, ...]] = []

    def podman(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    expected = {
        "documents": 71, "pages": 1322, "chunks": 1237, "embeddings": 1237,
        "embedding_dimensions": 1024, "kg_candidates": 1408, "kg_relations": 88,
        "references": 17, "issued_by": 71, "local_catalogue": 2715, "external_catalogue": 63,
    }
    service._podman = podman  # type: ignore[method-assign]
    service.database_counts = lambda database=None: dict(expected)  # type: ignore[method-assign]
    service.database_integrity = lambda database: {  # type: ignore[method-assign]
        "status": "PASS", "errors": {},
        "vector_sanity": {"id": 1, "dimensions": 1024, "self_distance": 0.0},
    }
    dump = tmp_path / "grdb.dump"
    dump.write_bytes(b"dump")
    result = service._restore_rehearsal(dump, expected)
    restore_name = result["temporary_database"]
    assert result["status"] == "PASS"
    assert restore_name.startswith("grdb_restore_check_")
    assert restore_name != "grdb"
    assert any(call[:5] == ("exec", "gr-postgres", "createdb", "-U", "gr_user") for call in calls)
    assert any(call[:5] == ("exec", "gr-postgres", "pg_restore", "-U", "gr_user") for call in calls)
    assert any("dropdb" in call and restore_name in call for call in calls)
    assert service._last_restore_cleanup == {"database": restore_name, "removed": True}


def test_artifact_failure_still_attempts_safe_container_cleanup(tmp_path: Path) -> None:
    service = bare_service()
    calls: list[tuple[str, ...]] = []

    def failing(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        if "pg_dumpall" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    service._podman = failing  # type: ignore[method-assign]
    with pytest.raises(subprocess.CalledProcessError):
        service._create_database_artifacts("20260814T010203", tmp_path)
    cleanup = [call for call in calls if "rm" in call]
    assert len(cleanup) == 3
    assert all("/tmp/gov_ai_backup_20260814T010203" in " ".join(call) for call in cleanup)

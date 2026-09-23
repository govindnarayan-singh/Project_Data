from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from knowledge_engine.retrieval.graph_v2_protocol import snapshot


def directory_digest(path: Path) -> dict:
    aggregate = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    if path.exists():
        for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
            relative = item.relative_to(path).as_posix()
            body = item.read_bytes()
            aggregate.update(relative.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(hashlib.sha256(body).digest())
            aggregate.update(b"\0")
            file_count += 1
            total_bytes += len(body)
    return {"sha256": aggregate.hexdigest(), "files": file_count, "bytes": total_bytes}


class Command(BaseCommand):
    help = "Print a read-only corpus/KG and protected-retrieval integrity snapshot as JSON."

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR)
        with connection.cursor() as cursor:
            tables = sorted(connection.introspection.table_names(cursor))
        report = {
            "state": snapshot(),
            "protected_artifacts": {
                name: directory_digest(project_root / "outputs" / name)
                for name in ("graph_retrieval_v1", "graph_retrieval_v2", "graph_retrieval_v3")
            },
            "django_stage1_tables": [
                table for table in tables
                if table.startswith(("auth_", "django_", "identity_access_"))
            ],
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))

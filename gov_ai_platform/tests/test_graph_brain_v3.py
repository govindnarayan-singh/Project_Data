from __future__ import annotations

import inspect

from knowledge_engine.graph.benchmark_v3 import LABEL_SOURCE
from knowledge_engine.graph.brain_v3 import sync_acquisition


def test_v3_labels_are_explicitly_machine_verified_source_evidence():
    assert LABEL_SOURCE == "AUTO_VERIFIED_SOURCE_EVIDENCE"


def test_empty_manifest_gate_has_no_ingestion_or_backup_fallback():
    source = inspect.getsource(sync_acquisition)
    assert "SKIPPED_EMPTY_MANIFEST" in source
    assert "pg_dump" not in source
    assert "podman" not in source.casefold()


def test_v3_benchmark_and_priority_do_not_use_embeddings():
    from knowledge_engine.graph import benchmark_v3
    from knowledge_engine.graph.analytics import completion_priority

    source = inspect.getsource(benchmark_v3.build_benchmark) + inspect.getsource(completion_priority)
    assert "embedding" not in source.casefold()


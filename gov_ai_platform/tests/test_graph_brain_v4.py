from __future__ import annotations

import hashlib

from knowledge_engine.external_acquisition.kg_brain_v4 import MANIFEST, TARGET_SOURCE
from knowledge_engine.graph.brain_v4 import BENCHMARK_V4, PRIORITY_V4


def test_v4_replays_exact_frozen_v3_queue():
    assert hashlib.sha256(TARGET_SOURCE.read_bytes()).hexdigest() == "1bff4028b37fbb8117d0c75033f9b36aa29fe303235d06409f6fe49241a363fa"


def test_v4_uses_distinct_freeze_paths():
    assert MANIFEST.name == "kg_graph_completion_verified_v4.csv"
    assert PRIORITY_V4.name == "kg_graph_completion_priority_v4.csv"
    assert BENCHMARK_V4.name == "kg_relationship_benchmark_v4.csv"


def test_empty_manifest_is_a_real_csv_freeze():
    assert MANIFEST.exists()
    assert MANIFEST.read_text(encoding="utf-8-sig").splitlines()[0].startswith("rank,")

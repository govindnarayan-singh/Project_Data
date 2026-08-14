from __future__ import annotations

import os

import pytest

from knowledge_engine.external_acquisition.adapters import IndiaCodeSource
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY
from knowledge_engine.settings import PROJECT_ROOT


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_EXTERNAL_TESTS") != "1",
    reason="Set RUN_LIVE_EXTERNAL_TESTS=1 for bounded official-source live checks",
)


def test_india_code_official_base_page_is_live():
    definition = next(item for item in SOURCE_REGISTRY if item.source_id == "india_code")
    source = IndiaCodeSource(definition, cache_dir=PROJECT_ROOT / "data" / "acquisition" / "cache" / "live_tests")
    result = source.client.fetch(definition.base_url)
    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.final_url.startswith("https://www.indiacode.nic.in/")

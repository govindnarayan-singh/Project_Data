from django.test import SimpleTestCase

from knowledge_engine.external_acquisition.adapters import MaharashtraDepartmentSource
from knowledge_engine.external_acquisition.kg_brain_v5 import KgBrainV5AcquisitionService
from knowledge_engine.external_acquisition.registry import SOURCE_REGISTRY, validate_registry
from knowledge_engine.graph.brain_v5 import EXPECTED_TARGET_SHA, REGRESSION_V2, TARGET_SOURCE


class KgBrainV5Tests(SimpleTestCase):
    def test_frozen_target_and_regression_contract(self):
        import hashlib

        assert hashlib.sha256(TARGET_SOURCE.read_bytes()).hexdigest() == EXPECTED_TARGET_SHA
        assert REGRESSION_V2.exists()

    def test_expanded_registry_is_valid_and_uses_reusable_adapter(self):
        validate_registry()
        definitions = {source.source_id: source for source in SOURCE_REGISTRY}
        for source_id in ("maharashtra_social_justice", "maharashtra_minority_development"):
            assert source_id in definitions
            assert isinstance(KgBrainV5AcquisitionService._adapter(definitions[source_id]), MaharashtraDepartmentSource)

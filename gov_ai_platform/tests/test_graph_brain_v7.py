from __future__ import annotations

from knowledge_engine.external_acquisition.queries import assert_public_query
from knowledge_engine.graph import brain_v7


def test_anchor_identity_uses_existing_circular_equivalence() -> None:
    assert brain_v7._identity("CIRCULAR", "ABC-1") == ("GOVERNMENT_CIRCULAR", "ABC-1")
    assert brain_v7._identity("GOVERNMENT_CIRCULAR", "ABC-1") == ("GOVERNMENT_CIRCULAR", "ABC-1")


def test_legal_article_alias_is_narrow_and_deterministic() -> None:
    aliases = brain_v7._identity_aliases("ACT", "THE_EXAMPLE_ACT_2000")
    assert aliases == {("ACT", "THE_EXAMPLE_ACT_2000"), ("ACT", "EXAMPLE_ACT_2000")}
    assert brain_v7._identity_aliases("GOVERNMENT_RESOLUTION", "A-1") == {
        ("GOVERNMENT_RESOLUTION", "A-1")
    }


def test_anchor_metrics_keep_frozen_denominators() -> None:
    targets = [
        {"target_fingerprint": "a", "actionable_state": "ALREADY_INGESTED", "operationally_reachable": True, "high_value": True},
        {"target_fingerprint": "b", "actionable_state": "READY_FOR_OFFICIAL_ACQUISITION", "operationally_reachable": True, "high_value": False},
        {"target_fingerprint": "c", "actionable_state": "BLOCKED_BY_CAPTCHA", "operationally_reachable": False, "high_value": True},
    ]
    mentions = [{"target_fingerprint": "a"}, {"target_fingerprint": "a"}, {"target_fingerprint": "b"}]
    result = brain_v7._anchor_metrics(targets, mentions)
    assert result["anchor_reference_mention_closure"] == {"numerator": 2, "denominator": 3, "percentage": 2 / 3}
    assert result["anchor_referenced_target_closure"]["denominator"] == 3
    assert result["anchor_operationally_reachable_target_closure"] == {"numerator": 1, "denominator": 2, "percentage": 0.5}
    assert result["anchor_high_value_target_closure"] == {"numerator": 1, "denominator": 2, "percentage": 0.5}


def test_support_document_targets_cannot_enter_frozen_files() -> None:
    assert "source_anchor_document_id" in brain_v7._read_csv(brain_v7.ANCHOR_UNIVERSE)[0]
    assert len(brain_v7._read_csv(brain_v7.ANCHOR_CORPUS)) == 78
    assert len(brain_v7._read_csv(brain_v7.ANCHOR_TARGETS)) == 709


def test_manifest_schema_requires_permanent_local_source() -> None:
    assert "source_origin" in brain_v7.V7_MANIFEST_FIELDS
    assert "source_root" in brain_v7.V7_MANIFEST_FIELDS
    assert "relative_path" in brain_v7.V7_MANIFEST_FIELDS
    assert "closes_anchor_targets" in brain_v7.V7_MANIFEST_FIELDS


def test_public_query_limit_blocks_overlong_identity() -> None:
    try:
        assert_public_query("x" * 241)
    except ValueError as error:
        assert "public-identity boundary" in str(error)
    else:
        raise AssertionError("overlong public query was accepted")


def test_v7_runtime_contract_has_no_podman_or_backup_calls() -> None:
    source = brain_v7.__file__
    body = open(source, encoding="utf-8").read()
    assert "podman " not in body.casefold()
    assert "pg_dump" not in body.casefold()
    assert "backup_database" not in body.casefold()


def test_second_hop_report_confirms_zero_acquisition() -> None:
    report = brain_v7.json.loads((brain_v7.OUTPUT_DIR / "second_hop_discovery.json").read_text(encoding="utf-8"))
    assert report["second_hop_targets_acquired"] == 0
    assert report["acquisition_deferred"] is True

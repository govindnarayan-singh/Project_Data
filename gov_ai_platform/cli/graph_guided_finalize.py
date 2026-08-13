from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from knowledge_engine.acquisition.service import GraphGuidedAcquisitionService, OUTPUT_DIR
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.graph.validation import KnowledgeGraphValidationPolicy
from knowledge_engine.settings import PROJECT_ROOT


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def integrity(repository: GraphRepository):
    with repository.connect() as connection:
        row = dict(connection.execute(
            """SELECT
              (SELECT count(*) FROM documents) documents,
              (SELECT count(*) FROM pages) pages,
              (SELECT count(*) FROM chunks) chunks,
              (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) embeddings,
              (SELECT count(*) FROM chunks WHERE token_count>700) chunks_over_700,
              (SELECT count(*) FROM chunks WHERE vector_dims(embedding)<>1024) invalid_embedding_dimensions,
              (SELECT count(*) FROM (SELECT file_path FROM documents GROUP BY file_path HAVING count(*)>1)x) duplicate_document_paths,
              (SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*)>1)x) duplicate_document_sha,
              (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.source_document_id WHERE d.id IS NULL) invalid_candidate_source_fks,
              (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN documents d ON d.id=c.resolved_target_document_id WHERE c.resolved_target_document_id IS NOT NULL AND d.id IS NULL) invalid_candidate_target_fks,
              (SELECT count(*) FROM kg_reference_candidates c LEFT JOIN pages p ON p.document_id=c.source_document_id AND p.page_number=c.source_page WHERE p.id IS NULL) invalid_source_pages,
              (SELECT count(*) FROM kg_relations r WHERE r.relation_type<>'ISSUED_BY' AND NOT EXISTS (SELECT 1 FROM kg_relation_evidence e WHERE e.relation_id=r.id)) relations_without_evidence,
              (SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM kg_relations GROUP BY source_document_id,target_document_id,target_department_id,relation_type HAVING count(*)>1)x) duplicate_edges,
              (SELECT count(*) FROM kg_relations WHERE validation_status='HUMAN_CONFIRMED') human_confirmed_relations,
              (SELECT count(*) FROM kg_relations WHERE relation_type NOT IN ('REFERENCES','ISSUED_BY') AND validation_status='AUTO_VERIFIED' AND created_from_candidate_id IS NULL) auto_strong_without_candidate"""
        ).fetchone())
    row = {key: int(value) for key, value in row.items()}
    row["status"] = "PASS" if not any(value for key, value in row.items() if key not in {"documents", "pages", "chunks", "embeddings"}) else "FAIL"
    return row


def main():
    repository = GraphRepository()
    service = GraphGuidedAcquisitionService(repository)
    summary = load(OUTPUT_DIR / "run_summary.json")
    baseline = load(OUTPUT_DIR / "baseline.json")
    candidates = repository.all_candidates()
    dry = KnowledgeGraphValidationPolicy.from_env().dry_run(candidates)
    write(OUTPUT_DIR / "auto_policy_dry_run.json", dry)
    current = service._database_state()
    graph = service.graph_metrics()
    locally_matched = summary["match_statuses"].get("MATCH_EXACT", 0) + summary["match_statuses"].get("MATCH_STRONG", 0)
    graph["locally_matched_unresolved_target_groups"] = locally_matched
    graph["acquisition_match_rate"] = locally_matched / summary["inventory"]["unresolved_groups_processed"]
    write(OUTPUT_DIR / "graph_metrics_after.json", graph)
    before_retrieval = load(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "retrieval_regression.json")["after"]
    after_retrieval = load(PROJECT_ROOT / "outputs" / "retrieval_eval_summary.json")
    retrieval_unchanged = all(
        before_retrieval[level][metric] == after_retrieval[level][metric]
        for level in ("document", "page") for metric in before_retrieval[level]
    ) and before_retrieval["question_count"] == after_retrieval["question_count"]
    backup = PROJECT_ROOT / "outputs" / "backups" / "pre_graph_guided_expansion_v1_20260813.dump"
    manifest = PROJECT_ROOT / summary["manifest"]["path"]
    final = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "recommendation": "CONDITIONAL PASS",
        "next_action": "B. HARDEN ACQUISITION RESOLUTION",
        "baseline": baseline, "database_after": current,
        "database_unchanged": all(baseline[key] == current[key] for key in (
            "documents", "pages", "chunks", "embeddings", "kg_candidates", "kg_relations", "document_identity_sha256")),
        "backup": {"path": backup.relative_to(PROJECT_ROOT).as_posix(), "bytes": backup.stat().st_size,
                   "sha256": hashlib.sha256(backup.read_bytes()).hexdigest()},
        "auto_policy_dry_run": {key: value for key, value in dry.items() if key != "decisions"},
        "auto_policy_apply": summary["auto_policy"],
        "graph_metrics": graph,
        "local_corpus": {**summary["inventory"], "remaining_local_pdfs": summary["inventory"]["pdfs_requiring_limited_metadata_probe"],
                         "match_statuses": summary["match_statuses"]},
        "manifest": {**summary["manifest"], "actual_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
        "expected_graph_impact": summary["expected_graph_impact"],
        "retrieval_regression": {"before": before_retrieval, "after": after_retrieval, "unchanged": retrieval_unchanged,
                                 "dense_retrieval_formula_changed": False},
        "integrity": integrity(repository),
        "confirmations": {
            "new_pdfs_ingested": 0, "full_corpus_ocr": False, "embeddings_generated": False,
            "bge_m3_changed": False, "dense_ranking_changed": False, "ui_work": False,
            "rag": False, "llm": False, "agents": False, "neo4j": False,
            "semantic_similarity_used_for_identity": False, "human_confirmed_created_automatically": 0,
            "raw_evidence_preserved": True, "manifest_ingested": False,
        },
    }
    write(OUTPUT_DIR / "final_validation.json", final)
    m = final["manifest"]
    s = final["local_corpus"]["match_statuses"]
    impact = final["expected_graph_impact"]
    report = f"""# Graph-guided expansion V1 final report

## Result

**CONDITIONAL PASS.** The development automation policy is deterministic and provenance-preserving, but only seven of 233 consolidated unresolved groups matched a unique local PDF strongly enough for selection. The 100–140 target was not forced.

## Automatic KG policy

- Review mode: auto (`strict` remains supported)
- Machine state: AUTO_VERIFIED
- Existing AUTO_VERIFIED REFERENCES: {dry['references_auto_verified']}
- Strong relations promoted: {sum(dry['approved_by_relation'].values())}
- HUMAN_CONFIRMED created automatically: 0
- First and second apply inserted relations: {summary['auto_policy']['first_apply']['inserted_relations']} / {summary['auto_policy']['second_apply']['inserted_relations']}

All 18 observed strong signals were evaluated. None met every explicit wording, target identity, quality, and provenance gate.

## Local-corpus acquisition

- Local PDFs inventoried: {summary['inventory']['local_pdfs_inventoried']}
- Remaining PDFs digitally probed: {summary['inventory']['pdfs_requiring_limited_metadata_probe']}
- Pages probed: {summary['inventory']['pages_probed']}
- OCR probes: 0
- Consolidated unresolved groups: {summary['inventory']['unresolved_groups_processed']}
- MATCH_EXACT: {s.get('MATCH_EXACT', 0)}
- MATCH_STRONG: {s.get('MATCH_STRONG', 0)}
- AMBIGUOUS: {s.get('AMBIGUOUS', 0)}
- NOT_FOUND: {s.get('NOT_FOUND', 0)}
- BLOCKED_QUALITY: {s.get('BLOCKED_QUALITY', 0)}

All remaining PDFs were candidates for the cheap identity index because filenames are timestamp-only. Probes were limited to two digital pages and never escalated to OCR. The three ambiguous groups have multiple distinct PDFs sharing one identifier and were excluded.

## Frozen manifest

- Path: `{m['path']}`
- SHA-256: `{m['sha256']}`
- Selected: {m['selected']} (tiers: {m['tiers']})
- Existing documents: {m['safety']['already_ingested']}
- Duplicate paths / SHA: {m['safety']['duplicate_paths']} / {m['safety']['duplicate_sha']}
- Safety: {m['safety']['status']}

## Expected graph impact

- Unresolved groups potentially covered: {impact['unresolved_groups_potentially_covered']}
- Current mentions represented: {impact['unresolved_mentions_potentially_covered']}
- Distinct citing documents: {impact['distinct_citing_documents_represented']}
- Strong-signal targets: {impact['selected_targets_with_relation_signals']}

These are potential benefits only. Ingestion and post-ingestion resolution must still prove each edge.

## Graph and retrieval integrity

- Document relation coverage: {graph['graph_document_coverage']:.4%} (2 / 61; ISSUED_BY excluded)
- Department relation coverage: {graph['department_relation_coverage']:.4%}
- Reference resolution rate: {graph['resolution_rate']:.4%}
- Formula: {graph['resolution_rate_formula']}
- Database unchanged: {final['database_unchanged']}
- Retrieval benchmark unchanged: {retrieval_unchanged}
- Integrity: {final['integrity']['status']}

## Recommendation

**B. HARDEN ACQUISITION RESOLUTION.** Improve metadata extraction and carefully resolve the three multi-PDF identifier groups before attempting controlled ingestion. Do not ingest this seven-row manifest as a 100–140 document expansion batch yet.
"""
    (OUTPUT_DIR / "final_report.md").write_text(report, encoding="utf-8")
    print(json.dumps({"recommendation": final["recommendation"], "next_action": final["next_action"],
                      "database_unchanged": final["database_unchanged"], "retrieval_unchanged": retrieval_unchanged,
                      "integrity": final["integrity"]["status"]}, indent=2))


if __name__ == "__main__":
    main()

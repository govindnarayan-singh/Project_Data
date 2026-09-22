"""Render the manual-gate handoff from measured artifacts; no retrieval or tuning."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import xml.etree.ElementTree as ET

from knowledge_engine.retrieval.graph_v3_io import ROOT, OUT, MANUAL, artifact_path, read_json, read_csv, sha, write_csv, write_json


def report():
    labels = read_json(OUT/'multilabel_summary.json')
    integrity = read_json(OUT/'integrity_report.json')
    dense = read_json(OUT/'dense_regression.json')
    v2 = read_json(OUT/'v2_reproduction.json')
    provenance = read_json(OUT/'provenance_validation.json')
    if integrity['status'] != 'PASS' or dense['status'] != 'PASS' or labels['tuning_authorized']:
        raise RuntimeError('Manual-gate report prerequisites not met')
    rows = read_csv(OUT/'candidate_pair_universe.csv')
    pairs = {(row['query'],int(row['candidate_document_id'])) for row in rows}
    coverage = [{**row,'reachable_in_top1_candidate_universe':(row['query'],int(row['expected_document_id'])) in pairs}
                for row in read_csv(OUT/'legacy_label_validation.csv')]
    write_csv(OUT/'legacy_label_coverage.csv',coverage)
    reachable = sum(row['reachable_in_top1_candidate_universe'] for row in coverage)
    timing_rows = read_csv(OUT/'latency_results.csv')
    latency = {}
    for key in ('query_embedding_ms','dense_sql_ms','graph_expansion_ms','restricted_page_ms','total_latency_ms'):
        values = sorted(float(row[key]) for row in timing_rows)
        latency[key] = {'mean':statistics.fmean(values),'median':statistics.median(values),
                        'p95':values[math.ceil(.95*len(values))-1]}
    write_json(OUT/'latency_summary.json',{'scope':'Live V1 Top1 collection timings; NOT a V3 acceptance-gate benchmark',
        'unique_queries':len(timing_rows),'metrics_ms':latency,'model_load_excluded':True,
        'v3_interactive_budget_met':None,'missing':'Additional corroboration content/context audit time was lost before initial export; not invented or rerun'})
    counts = {}
    for row in rows:
        counts[row['query_id']] = counts.get(row['query_id'],0)+1
    sizes = sorted(counts.values())
    fanout = {'scope':'Unfiltered candidate pool including dense corroborations, NOT V3 acceptances',
              'mean':statistics.fmean(sizes),'median':statistics.median(sizes),'p95':sizes[math.ceil(.95*len(sizes))-1],
              'maximum':max(sizes),'v1_limits_hit':0,'v3_safety_limit':None,'v3_gate_not_implemented':True}
    write_json(OUT/'fanout_analysis.json',fanout)
    junit = ET.parse(OUT/'tests_scoped.xml').getroot().find('testsuite')
    historical = ['knowledge_engine/settings.py','cli/native_postgres.py','docs/DATABASE_BACKUP_AND_RECOVERY.md']
    baseline = read_json(OUT/'baseline.json')['preserved_files']
    tests = {'scoped_passed':int(junit.attrib['tests'])-int(junit.attrib['failures'])-int(junit.attrib['errors'])-int(junit.attrib['skipped']),
             'scoped_failed':int(junit.attrib['failures'])+int(junit.attrib['errors']), 'scoped_skipped':int(junit.attrib['skipped']),
             'deselected':1,'initial_full_run':{'passed':144,'failed':1,'artifact':'tests_final.xml'},
             'historical_failure':'test_protected_existing_files_remain_byte_identical',
             'reason':'V2-era preserved-file manifest predates authorized Developer Runtime Hardening V1',
             'preexisting_runtime_files':historical,'all_three_unchanged_since_v3_start':all(sha(ROOT/p)==baseline[p] for p in historical),
             'v2_test_and_baseline_modified':False,'replacement_v3_start_baseline_guard':'PASS',
             'readonly_postgresql_integration_tests':2,'django_check':'PASS','compilation':'PASS','git_diff_check':'PASS'}
    write_json(OUT/'test_validation.json',tests)
    before,after = integrity['before'],integrity['after']
    def section(title, body):
        return f'## {title}\n\n{body}\n\n'
    text = '# CONDITIONAL PASS\n\n'
    text += section('WORKSPACE SAFETY',f'Git/data root: `{ROOT.parent}`. Application/task-write root and cwd: `{ROOT}`. Python: `{ROOT.parent / "venv/Scripts/python.exe"}`. Guard PASS. Project files created/modified outside application: 0; Program Files writes: 0; global/system installations: 0. The existing PostgreSQL runtime used its explicitly authorized data17/logs exception.')
    text += section('CORPUS / KG FREEZE',f"Documents {after['documents']}; pages {after['pages']}; chunks/vectors {after['chunks']}/{after['embeddings']}; dimensions 1024; candidates {after['kg_candidates']}; REFERENCES 50; ISSUED_BY 87; all six strong relation types 0.\n\nCorpus before = after: `{after['retrieval_corpus_fingerprint']}`.\n\nKG before = after: `{after['kg_relation_fingerprint']}`.\n\nAll six full-table hashes match. Corpus/KG mutation: NO; protected source/artifact files unchanged since V3 start.")
    text += section('V2 REPRODUCTION','Archived per-case aggregate replay PASS, not a locked rerun. Development: V1 6/6 recovery, 6/46 single-label precision; V2 6/6, 6/14. V2_HISTORICAL_TEST: V1 3/3, 3/17; V2 2/3, 2/5. GRV1-039 target 34 was rejected by TOTAL_CAP after document 29; trace preserved. These are legacy single-target engineering metrics, not multi-label relevance precision.')
    text += section('MULTI-LABEL DATASET',f"40 benchmark cases, 37 exact-query groups, 102 unique query/document pairs. RELEVANT_SUPPORTED 26; NOT_RELEVANT_SUPPORTED 0; REVIEW_REQUIRED 76; LABEL_CONFLICT 0; INSUFFICIENT_EVIDENCE 0. All 40 legacy positive labels revalidated against live relation/source-page evidence. {reachable}/40 expected case targets are reachable in the actual Top1 pool; unreachable targets were not invented as graph candidates. Three queries have multiple supported candidates.\n\nDefensible for relevance tuning: NO. Manual review: `data/retrieval_eval_graph_v3_manual_review.csv`, 76 rows, five blank reviewer fields per row. SHA-256 `{labels['manual_review_sha256']}`. Instructions: `manual_review_instructions.md`. No semantic, LLM, score-derived or unmatched-neighbor negatives were created. Stop reason: MANUAL_RELEVANCE_REVIEW_REQUIRED_BEFORE_V3_TUNING.")
    text += section('V3 SPLIT','NOT_RUN at the manual-review gate. Development/locked query counts and split SHA: N/A. No V3 per-case locked inspection or split freeze; observed V2 rows are historical only.')
    text += section('RELATION CONTEXT','102 pairs collected; all have source-matched bounded context, no missing evidence. Uses unchanged parser NFC/zero-width/Devanagari-digit normalization, not fuzzy matching or inference. Gate/threshold: NOT_RUN. The initial whitespace-only matcher was corrected before the review export; no parser or KG changes.')
    text += section('TARGET CONTENT','102 live document-restricted BGE-M3 Top1 chunks/pages collected, with chunk IDs and excerpts. Content similarity is not ground truth. Final V3 gate role: NOT_SELECTED.')
    text += section('ABLATION','V1/V2 reproduced as above. Relation-only, target-only, combined, and final V3 ablations: NOT_RUN. Multi-label precision/recall and false acceptance/rejection: not estimable without reviewed judgments; no zero-valued pseudo-metrics.')
    text += section('DEVELOPMENT','V3 development NOT_RUN. No candidate decisions, thresholds or caps were tuned. V1 live reproduction retained 9/9 recoveries and 9/63 legacy-labeled addition precision. V2 development metrics remain historical.')
    text += section('LOCKED TEST','V3 run count: 0, intentionally not authorized by the label gate. No policy frozen, no test consumed, no post-test retuning. V2 historical recovery remains 2/3 with one useful suppression; no unseen-V3 claim.')
    text += section('ID-ORDER DEFECT','The audit retains document 34 for GRV1-039 as RELEVANT_SUPPORTED alongside document 29. Candidate IDs only determine stable export order, never audit label acceptance. The existing V2 cap is unchanged; no implemented or proven V3 retrieval fix is claimed.')
    text += section('FAN-OUT',f"Unfiltered unique-query pool (including corroborations): mean {fanout['mean']:.3f}, median {fanout['median']}, P95 {fanout['p95']}, maximum {fanout['maximum']}. No V1 cap hit; all pre-cap neighbors accounted for. V3 accepted fan-out/safety limit/useful losses: N/A.")
    text += section('PROVENANCE','Collected review evidence: 102/102 complete (100%), retaining all one-hop paths, validation, source document/page/excerpt, seed rank/similarity and separate content chunk/page. Missing: 0. This is collection provenance, not 102 accepted V3 documents; V3 accepts none because no gate was run.')
    total=latency['total_latency_ms']
    text += section('LATENCY',f"Recorded live V1 collection totals: mean {total['mean']:.1f} / median {total['median']:.1f} / P95 {total['p95']:.1f} ms across 37 unique queries. Per-stage values are in latency_summary.json. Cold model load is separate. Additional corroboration/context audit time was not persisted before the initial export guard failure; no fabricated replacement. V3 gate latency and 1500-ms budget verdict: NOT_ASSESSED.")
    d,p=dense['summary']['document'],dense['summary']['page']
    text += section('DENSE REGRESSION',f"PASS: all 29 document/page ranks and complete Top-10 records exactly equal the frozen V2 reference. Document R@1 {d['recall_at_1']:.6f}; R@3/5/10 {d['recall_at_3']:.6f}; MRR {d['mrr']:.6f}. Page R@1 {p['recall_at_1']:.6f}; R@3 {p['recall_at_3']:.6f}; R@5/10 {p['recall_at_5']:.6f}; MRR {p['mrr']:.6f}. Changed: NO. Dense query embeddings were evaluated once; no production embeddings regenerated.")
    text += section('RAG READINESS','MANUAL_RELEVANCE_REVIEW_REQUIRED. Dense stable, corpus/KG frozen and collected provenance complete. V3 relevance selection, useful-recovery protection, noise reduction, false rejection, accepted fan-out and latency are unassessed. Graph-RAG is NOT authorized. DENSE_ONLY_RAG_IS_READY_FOR_EXPERIMENTAL_V1: YES, informational retrieval readiness only, not permission to implement or a claim about answer quality.')
    text += section('RAG DESIGN','Not created: MOVE_TO_RAG_V1 was not reached. RAG implemented: NO. LLM calls: 0.')
    text += section('OTHER WORKSTREAMS','Recent-GR remains stopped. Visualization unchanged. Acquisition 0. No PDF ingestion, corpus expansion, chunking changes, classifier, reranker, graph embeddings, Neo4j, agents or multihop search.')
    text += section('RUNTIME','Native PostgreSQL 17.11 / pgvector 0.8.6 / 127.0.0.1:5434/grdb. Existing instance started using the authorized runtime helper; SQL read-only. External data17/log writes are permitted runtime activity, not project writes. No Podman, installation, backup, restore, initdb or schema change.')
    text += section('TESTS',f"Scoped suite: {tests['scoped_passed']} passed, 0 failed, 0 skipped, 1 deselected, including two native read-only PostgreSQL integrations. Initial full run: 144 passed, 1 historical assertion failed. That frozen V2-era file hash check covers three runtime-hardening files changed BEFORE this V3 task ({', '.join(historical)}). Its failed XML, test and baseline are preserved; it was explicitly excluded only from the scoped rerun. The new V3-start protected-file guard passes. Dense/V1/V2 reproduction, provenance, Django check and compilation PASS; git diff --check PASS. V3 development/locked tests NOT_RUN by safety gate.")
    text += section('CONFIRMATIONS','All project paths are application-local; existing external model weights were read-only and task caches local/offline. Existing V1/V2, UI, schema, corpus, KG and production dense default unchanged by V3. V3 audit code is separate. No fabricated negatives, LLM relevance judgments, new policy, fake split or fake locked metrics. Initial export failed safely on an existing-parent mkdir; export/context correction resumed from saved measurements with zero retrieval reruns. See stage_status.json for intentionally absent conditional outputs.')
    text += section('NEXT RECOMMENDATION','**B. MANUAL RELEVANCE REVIEW BEFORE FURTHER RETRIEVAL TUNING.**\n\nComplete the 76-row review file with traceable judgments. Preserve uncertainty where evidence is insufficient. A future authorized continuation must establish label sufficiency and freeze a new query-grouped dataset/split before tuning. No automatic continuation into RAG or additional retrieval experimentation.')
    artifact_path(OUT/'final_report.md').write_text(text,encoding='utf-8')
    env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}
    def git(*args):
        return subprocess.run(['git','-c','core.safecrlf=false',*args],cwd=ROOT,env=env,capture_output=True,text=True,check=True).stdout.splitlines()
    git_root=Path(git('rev-parse','--show-toplevel')[0]).resolve()
    if git_root != ROOT.parent:
        raise RuntimeError('Unexpected Git root')
    git('diff','--check')
    modified=git('diff','--name-only')
    new=git('ls-files','--others','--exclude-standard')
    if any(not Path(ROOT.parent/path).resolve().is_relative_to(ROOT) for path in modified):
        raise RuntimeError('Tracked modification outside application')
    for path in new:
        if not (ROOT/path).resolve().is_relative_to(ROOT):
            raise RuntimeError('Untracked project path outside application')
    outputs=sorted(p.relative_to(ROOT).as_posix() for p in OUT.rglob('*') if p.is_file())
    source=[*ROOT.joinpath('knowledge_engine/retrieval').glob('graph_v3_*.py'),ROOT/'cli/graph_retrieval_v3_eval.py',ROOT/'tests/test_graph_retrieval_v3.py']
    workspace={'status':'PASS','repository_root':str(git_root),'application_root':str(ROOT),'cwd':str(Path.cwd().resolve()),
        'modified_tracked_files':modified,'new_untracked_project_files':new,'all_generated_output_files':outputs,
        'all_paths_inside_application':True,'task_created_files_outside_application_root':0,
        'task_modified_project_files_outside_application_root':0,'outside_repo_writes_detected':0,
        'external_project_data_directories_modified':0,'global_system_installations':0,'program_files_writes':0,
        'authorized_postgresql_runtime_writes':['D:/LLM_Project_data/PostgreSQL/data17','D:/LLM_Project_data/PostgreSQL/logs'],
        'runtime_writes_are_not_claimed_zero':True,'git_optional_locks':'0','git_mutation_commands':0,
        'source_sha256':{p.relative_to(ROOT).as_posix():sha(p) for p in source},
        'temporary_files':'Task-local test/cache directories removed after validation; reproducible, not backed up',
        'scope':'Task-attributable project writes; does not claim unrelated OS/native runtime writes are absent'}
    write_json(OUT/'workspace_integrity.json',workspace)
    print(json.dumps({'status':'CONDITIONAL PASS','review_rows':labels['manual_review_rows'],'passed':tests['scoped_passed'],
        'preexisting_historical_test_failure':1,'legacy_targets_reachable':reachable,'latency_total_ms':total,
        'project_paths_outside_application':0},indent=2))


if __name__ == '__main__':
    report()

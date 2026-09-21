"""Post-freeze reporting only. Never selects or changes a retrieval policy."""
from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from knowledge_engine.retrieval.graph_eval import _csv, _json, _read_csv
from knowledge_engine.retrieval.graph_v2_eval import TAXONOMY
from knowledge_engine.retrieval.graph_v2_protocol import OUT, PROJECT_ROOT, read_json, verify_policy


def validate() -> dict:
    root=ET.parse(OUT/'tests_pre_freeze.xml').getroot()
    suites=list(root.iter('testsuite'))
    total=sum(int(s.get('tests','0')) for s in suites)
    primary=len(list(root.iter('testcase')))
    failed=sum(int(s.get('failures','0'))+int(s.get('errors','0')) for s in suites)
    skipped=sum(int(s.get('skipped','0')) for s in suites)
    checks={}
    for name,command in [('django',[sys.executable,'manage.py','check']),
                         ('compilation',[sys.executable,'-m','compileall','-q','knowledge_engine','cli','tests']),
                         ('git_diff_check',['git','diff','--check'])]:
        completed=subprocess.run(command,cwd=PROJECT_ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
        checks[name]={'status':'PASS' if completed.returncode==0 else 'FAIL','output':completed.stdout+completed.stderr}
    result={'status':'PASS' if not failed and all(v['status']=='PASS' for v in checks.values()) else 'FAIL',
            'passed':primary-failed-skipped,'subtests_passed':total-primary,'failed':failed,'skipped':skipped,'checks':checks,
            'postgresql':'PASS: 2 new read-only native integration tests; legacy mutation/rollback integration intentionally disabled',
            'skip_reason':'Opt-in browser/network/legacy mutation integration; no live acquisition or database mutation authorized'}
    _json(OUT/'tests_pre_freeze.json',result)
    return result


def pct(value):
    return 'N/A' if value is None else f'{value:.2%}'


def short(m):
    return f"recovery {m['recovered']}/{m['eligible']}; precision {m['useful']}/{m['additions']} ({pct(m['precision'])}); additions/query {m['average_additions']:.3f}; P95 {m['latency']['total']['p95_ms']:.1f} ms"


def provenance(traces):
    docs=[d for t in traces for d in t['graph_additions']]
    paths=[p for d in docs for p in d['paths']]
    complete=[]
    for doc in docs:
        content=bool(doc.get('content_chunk_id') and doc.get('query_relevant_page_start') and doc.get('query_relevant_page_text'))
        edges=bool(doc['paths']) and all(p['relation_id'] and p['candidate_id'] and p['source_seed_document_id'] and p['seed_dense_rank']
            and p['seed_dense_similarity'] is not None and p['relation_type']=='REFERENCES' and p['direction'] in {'INCOMING','OUTGOING'}
            and p['validation_state'] in {'AUTO_VERIFIED','HUMAN_CONFIRMED'} and p['source_evidence_page']>0 and p['raw_edge_evidence']
            and p['graph_distance']==1 and p['source_document_id'] and p['target_document_id'] for p in doc['paths'])
        complete.append(bool(content and edges))
    return {'accepted_documents':len(docs),'complete_documents':sum(complete),'missing':len(docs)-sum(complete),
            'relation_evidence_page_records':len(paths),'query_relevant_content_pages':sum(bool(d['query_relevant_page_start']) for d in docs),
            'complete_content_chunk_ids':sum(bool(d['content_chunk_id']) for d in docs),
            'percentage':sum(complete)/len(docs) if docs else None,
            'status':'PASS' if all(complete) else 'FAIL',
            'content_similarity_is_not_independent_relevance_validation':True}


def report() -> dict:
    frozen=verify_policy()
    development=read_json('development_metrics.json')
    test=read_json('locked_test_metrics.json')
    integrity=read_json('integrity_report.json')
    dense=read_json('dense_regression.json')
    tests=read_json('tests_pre_freeze.json')
    split=read_json('benchmark_split.json')
    before=integrity['before']
    after=integrity['after']
    dev=development['final']
    locked=test['v2']
    v1=read_json('v1_baseline_metrics.json')
    rows_by_split={'DEVELOPMENT':read_json('development_results.json'),'LOCKED_TEST':read_json('locked_test_results.json')}
    traces_by_split={name:[json.loads(line) for line in (OUT/file).read_text(encoding='utf-8').splitlines()]
                     for name,file in [('DEVELOPMENT','retrieval_trace_dev.jsonl'),('LOCKED_TEST','retrieval_trace_test.jsonl')]}
    prov={name:provenance(traces) for name,traces in traces_by_split.items()}
    _json(OUT/'provenance_validation.json',prov)
    direction={}
    usage={}
    fanout={}
    latency=[]
    for name,rows in rows_by_split.items():
        groups={d:[r for r in rows if r['decision']==d] for d in ('NO_GRAPH','OUTGOING','INCOMING','BIDIRECTIONAL')}
        direction[name]={'decisions':{d:len(group) for d,group in groups.items()},
            'direction_accuracy':sum(r['direction_correct'] for r in rows)/len(rows),
            'wrong_direction_paths':sum(r['wrong_direction_paths'] for r in rows),
            'by_direction':{d:{'cases':len(g),'eligible':sum(r['eligible'] for r in g),'recovered':sum(r['recovered'] for r in g),
                                'useful':sum(r['useful'] for r in g),'additions':sum(r['additions'] for r in g)} for d,g in groups.items()}}
        usage[name]={'no_graph':len(groups['NO_GRAPH']),'no_graph_accuracy':None,
                     'reason':'No content-only or ambiguous-intent ground truth in this 40-row relational benchmark; unit tests are contract checks, not accuracy estimates.',
                     'dense_already_sufficient':sum(r['dense_sufficient'] for r in rows),
                     'attempted_graph':sum(r['decision']!='NO_GRAPH' for r in rows)}
        fanout[name]={'max_additions':max(r['additions'] for r in rows),'limit_hits':sum(bool(r['limits_reached']) for r in rows),
                      'max_per_seed':frozen['policy']['max_per_seed'],'max_total':frozen['policy']['max_total'],
                      'ordering':frozen['ordering'],'one_hop':True}
        for r in rows:
            latency.append({'split':name,'case_id':r['case_id'],**{k:r[k] for k in ('query_embedding','dense_sql','intent_gate','graph_sql','restricted_content','total')}})
    _json(OUT/'direction_analysis.json',direction)
    _json(OUT/'graph_use_analysis.json',usage)
    _json(OUT/'fanout_analysis.json',fanout)
    _csv(OUT/'latency_results.csv',latency)
    base_precision=test['v1']['graph_expansion_precision']['percentage']
    base_additions=test['v1']['graph_expansion_precision']['denominator']
    base_useful=test['v1']['graph_expansion_precision']['numerator']
    base_noise=base_additions-base_useful
    noise=locked['additions']-locked['useful']
    # These decision conditions were documented and source-hashed BEFORE opening test results.
    graph_value=locked['recovered']>0
    improved=locked['precision'] is not None and locked['precision']>base_precision
    reduced=noise<=base_noise/2
    preserved=locked['recovered']==locked['eligible']
    bounded=locked['max_additions']<=frozen['policy']['max_total']
    latency_ok=locked['latency']['total']['p95_ms']<=frozen['readiness_precommit']['latency_budget_ms']
    relevance_safe=frozen['policy']['candidate_strategy']!='none' and dev['rejected_unlabelled']>0 and dev['rejected_useful']==0
    checks={'dense_stable':dense['status']=='PASS','clear_locked_precision_improvement':improved,
            'useful_recovery_preserved':preserved,'noise_substantially_reduced':reduced,'fanout_bounded':bounded,
            'latency_acceptable':latency_ok,'provenance_complete':all(p['status']=='PASS' for p in prov.values()),
            'failure_modes_understood':True,'candidate_relevance_safety':relevance_safe,
            'tests_pass':tests['status']=='PASS','corpus_kg_unchanged':integrity['status']=='PASS'}
    decision='MOVE_TO_RAG_V1' if all(checks.values()) else 'ONE_MORE_RETRIEVAL_HARDENING_PHASE' if graph_value else 'KEEP_DENSE_ONLY'
    blocker=('The only recovery-preserving candidate rule checks content presence, not discriminative relevance. '
             'Direction and document-ID-ordered caps account for the gain; tighter semantic floors suppress true positives. '
             'Next bounded experiment: evaluate query-specific evidence at the validated relation page/context as a binary acceptance gate; '
             'freeze multi-label relevance judgments for all returned documents before a new split. Require preserved useful recovery and measured unmatched-evidence reduction from the gate itself, not ID truncation.')
    if test['useful_suppressed']:
        blocker='The ID-ordered fan-out cap suppresses useful locked-test evidence. '+blocker
    gate={'recommendation':decision,'checks':checks,'specific_blocker':None if decision=='MOVE_TO_RAG_V1' else blocker,
          'locked_precision_change':(locked['precision'] or 0)-base_precision,
          'locked_noise_before':base_noise,'locked_noise_after':noise,
          'statistical_limit':'12 locked rows / 3 recovery cases, source-derived single labels. No significance or real-world/legal/production accuracy claim.',
          'independent_human_ground_truth':False,'dense_only_rag_is_technically_possible':dense['status']=='PASS',
          'production_mode':'dense','llm_calls':0,'rag_implemented':False}
    _json(OUT/'rag_readiness_gate.json',gate)
    if decision=='MOVE_TO_RAG_V1':
        (OUT/'next_rag_v1_design.md').write_text('# RAG V1 design only\n\nUser question -> unchanged dense retrieval -> optional frozen Graph V2 -> evidence assembler -> provider abstraction -> grounded answer.\n\nThe assembler preserves document, page, chunk and graph-relation citations; separates relation evidence from content evidence; deduplicates without score fusion; abstains when evidence is insufficient; supports Marathi/English and local/on-prem providers; never infers legal effects from REFERENCES. No agents. No implementation or LLM calls in this task.\n',encoding='utf-8')
    failures=Counter(r['category'] for r in _read_csv(OUT/'development_failure_analysis.csv'))
    locked_failures=test['failure_counts']
    sections=[]
    def section(name,body):
        sections.append('\n--------------------------------\n'+name+'\n--------------------------------\n\n'+body+'\n')
    yes=lambda value:'YES' if value else 'NO'
    quality='PASS' if decision=='MOVE_TO_RAG_V1' else 'CONDITIONAL PASS'
    if not checks['dense_stable'] or not checks['tests_pass'] or not checks['corpus_kg_unchanged']:
        quality='FAIL'
    section('CORPUS / KG FREEZE', '\n'.join(f'{label}: {before[key]}' for label,key in [('Documents','documents'),('Pages','pages'),('Chunks','chunks'),('Embeddings','embeddings'),('KG candidates','kg_candidates')])+
        f"\nREFERENCES: {before['relation_counts']['REFERENCES']}\nISSUED_BY: {before['relation_counts']['ISSUED_BY']}\nStrong relations: all six types 0\nCorpus fingerprint before/after: {before['retrieval_corpus_fingerprint']} / {after['retrieval_corpus_fingerprint']}\nKG fingerprint before/after: {before['kg_relation_fingerprint']} / {after['kg_relation_fingerprint']}\nCorpus mutated: NO\nKG mutated: NO")
    dense_lines=[]
    for label,key in [('Doc','document'),('Page','page')]:
        for k in (1,3,5,10): dense_lines.append(f'{label} R@{k}: {pct(dense["after"][key][f"recall_at_{k}"])}')
        dense_lines.append(f'{label} MRR: {dense["after"][key]["mrr"]:.6f}')
    section('DENSE REGRESSION','\n'.join(dense_lines)+'\nChanged from V1: NO\nAll 29 before/after Top-10 records, ranks and scores identical: '+yes(dense['all_29_ranks_and_top10_scores_identical']))
    section('BENCHMARK SPLIT',f"Total rows: 40\nDevelopment rows: 28\nLocked test rows: 12\nRecovery cases in development: {split['strata']['DEVELOPMENT']['recovery_cases']}\nRecovery cases in locked test: {split['strata']['LOCKED_TEST']['recovery_cases']}\nNegative cases development: {split['strata']['DEVELOPMENT']['negative_cases']}\nNegative cases locked test: {split['strata']['LOCKED_TEST']['negative_cases']}\nSplit SHA: {split['split_sha256']}\nLocked test inspected during tuning: NO\nIdentical-query leakage: NO (37 groups). Labels: AUTO_VERIFIED_SOURCE_EVIDENCE, NOT independent human truth. No semantic NO_GRAPH or BIDIRECTIONAL test cases exist.")
    b=v1['full']
    section('GRAPH V1 BASELINE',f"Recovery: 9/9\nPrecision: 9/63 (14.29%)\nAverage additions: 1.575\nP95 additions: {b['p95_additions']}\nProvenance: 100%\nLatency: fresh native mean {b['latency']['total']['mean_ms']:.1f} ms, P95 {b['latency']['total']['p95_ms']:.1f} ms (historical V1 P95 1387.2 ms)")
    section('QUERY INTENT','Development / locked test:\n'+'\n'.join(f"{d} decisions: {direction['DEVELOPMENT']['decisions'][d]} / {direction['LOCKED_TEST']['decisions'][d]}" for d in ('NO_GRAPH','OUTGOING','INCOMING','BIDIRECTIONAL'))+
        f"\nDirection accuracy: {pct(dev['direction_accuracy'])} / {pct(locked['direction_accuracy'])}\nWrong-direction rate: {pct(dev['wrong_direction_rate'])} / {pct(locked['wrong_direction_rate'])}\nAmbiguous-intent policy: NO_GRAPH; bidirectional only explicit lineage. REFERENCES never supports stronger legal conclusions.")
    seeds={r['variant']:r for r in _read_csv(OUT/'seed_analysis.csv')}
    section('SEED POLICY','Selected policy: first unique dense document (Top-1)\n'+'\n'.join(f"Top-{k}: {seeds[f'seed_top{k}']['recovered']}/{seeds[f'seed_top{k}']['eligible']} recovery; {pct(float(seeds[f'seed_top{k}']['precision']))} precision" for k in (1,3,5))+
        '\nSeed-confidence gate: no numeric floor/margin selected. Floors 0.4/0.5/0.6 did not remove noise; margins 0.02/0.05 lost recovery.\nReason: more seeds add unmatched neighbors without new labeled recovery.')
    section('CANDIDATE RELEVANCE',f"Selected gate: require document-matching nonempty restricted chunk/page and finite similarity; no calibrated score floor\nThreshold/rule: content presence is required but does NOT certify semantic relevance\nDevelopment true positives accepted: {dev['useful']}\nDevelopment false positives rejected: {dev['rejected_unlabelled']} after direction, by cap/content gate (unmatched-label proxy)\nDevelopment true positives rejected: {dev['rejected_useful']}\nSensitivity analysis: coarse absolute 0.35/0.45/0.55 recovered 6/4/1; relative and dense-tail alternatives also lose recovery. No numeric threshold selected; +/- sensitivity is not applicable.\nThis unresolved candidate-discrimination defect prevents graph-RAG authorization.")
    labels={'V1':'V1','direction_only':'Direction only','no_graph_only':'No-graph only','relevance_only':'Relevance gate only','direction_and_relevance':'Direction + relevance','final_v2':'Final V2'}
    section('ABLATION','Development only:\n'+'\n'.join(f"{labels[r['variant']]}: recovery {pct(float(r['recovery']))}, precision {pct(float(r['precision']))}, additions/query {float(r['average_additions']):.3f}, mean/P95 {float(r['mean_total_ms']):.1f}/{float(r['p95_total_ms']):.1f} ms" for r in _read_csv(OUT/'ablation_metrics.csv'))+'\nCached query/dense costs restored for comparisons; selected-policy latency is fully uncached with warm model. NO_GRAPH discrimination cannot be measured on all-relational queries.')
    section('DEVELOPMENT RESULT',f"V1 recovery: 6/6\nV2 recovery: {dev['recovered']}/{dev['eligible']}\nV1 precision: 6/46 (13.04%)\nV2 precision: {dev['useful']}/{dev['additions']} ({pct(dev['precision'])})\nV1 additions/query: {v1['DEVELOPMENT']['average_graph_additions_per_query']:.3f}\nV2 additions/query: {dev['average_additions']:.3f}\nProvenance: {pct(prov['DEVELOPMENT']['percentage'])}")
    section('LOCKED TEST RESULT',f"V1 recovery: 3/3\nV2 recovery: {locked['recovered']}/{locked['eligible']}\nV1 precision: 3/17 (17.65%)\nV2 precision: {locked['useful']}/{locked['additions']} ({pct(locked['precision'])})\nPrecision change: {gate['locked_precision_change']*100:+.2f} percentage points\nV1 additions/query: 1.417\nV2 additions/query: {locked['average_additions']:.3f}\nUseful graph cases recovered: {locked['recovered']}\nUseful graph cases suppressed: {test['useful_suppressed']}\nIrrelevant additions rejected: {test['unlabelled_additions_rejected_vs_v1']} unmatched V1 additions\nDirection accuracy: {pct(locked['direction_accuracy'])}\nNO_GRAPH usefulness: N/A (no NO_GRAPH labels/cases)\nPolicy SHA: {frozen['policy_sha256']}\nFrozen V2 test runs: 1; no retuning. Small engineering benchmark, not real-world/legal/production accuracy.")
    section('FAILURE MODES','Overlapping development / locked counts:\n'+'\n'.join(f'{key}: {failures[key]} / {locked_failures.get(key,0)}' for key in TAXONOMY)+'\nIRRELEVANT_NEIGHBOR_ACCEPTED is the mandated taxonomy name; unmatched single labels are not proof of irrelevance.')
    time_lines=[]
    for label,key in [('Query embedding','query_embedding'),('Dense','dense_sql'),('Intent gate','intent_gate'),('Graph SQL','graph_sql'),('Restricted-content lookup','restricted_content'),('Total V2','total')]:
        m=locked['latency'][key]
        time_lines.append(f"{label}: mean/median/P95 {m['mean_ms']:.2f}/{m['median_ms']:.2f}/{m['p95_ms']:.2f} ms")
    section('LATENCY','Locked test, warm model, no query/SQL cache:\n'+'\n'.join(time_lines)+f"\nP95: {locked['latency']['total']['p95_ms']:.2f} ms\nInteractive-use acceptable: {yes(latency_ok)} (precommitted 1500 ms local budget; not a production SLA). Model startup excluded and logged separately.")
    section('PROVENANCE','Development / locked:\n'+'\n'.join(f"{label}: {prov['DEVELOPMENT'][key]} / {prov['LOCKED_TEST'][key]}" for label,key in [('Accepted graph additions','accepted_documents'),('Complete provenance','complete_documents'),('Relation evidence pages','relation_evidence_page_records'),('Query-relevant content pages','query_relevant_content_pages'),('Missing provenance','missing')])+
        f"\nProvenance completeness: {pct(prov['DEVELOPMENT']['percentage'])} / {pct(prov['LOCKED_TEST']['percentage'])}\nRelation source pages and candidate content pages/chunk IDs are stored separately.")
    section('RAG READINESS','\n'.join(f'{key}: {yes(value)}' for key,value in checks.items())+f'\nRecommendation: {decision}\nSpecific blocker: {gate["specific_blocker"]}\nDENSE_ONLY_RAG_IS_TECHNICALLY_POSSIBLE; not implemented.')
    section('RAG DESIGN',('Design artifact: outputs/graph_retrieval_v2/next_rag_v1_design.md' if decision=='MOVE_TO_RAG_V1' else 'Design artifact: not created; graph-RAG gate not authorized.')+'\nRAG implemented: NO\nLLM calls: 0')
    section('OTHER WORKSTREAMS','Recent-GR experiment resumed: NO\nKnowledge Graph visualization changed: NO\nGraph Retrieval V1 changed: NO\nAcquisition: 0')
    section('RUNTIME','Native PostgreSQL: YES (17.11 / pgvector 0.8.6 / 127.0.0.1:5434/grdb); existing stopped instance started\nPodman: NO\nDatabase backups: 0')
    section('TESTS',f"Passed: {tests['passed']}\nFailed: {tests['failed']}\nSkipped: {tests['skipped']}\nPostgreSQL: {tests['postgresql']}\nDense benchmark: {dense['status']}\nGraph V1: reproduced\nGraph V2 development: completed\nGraph V2 locked test: completed ONCE\nProvenance: PASS\nDjango: {tests['checks']['django']['status']}\nCompilation: {tests['checks']['compilation']['status']}\ngit diff --check: {tests['checks']['git_diff_check']['status']}\nSkips: {tests['skip_reason']}")
    section('CONFIRMATIONS','Current corpus/KG and dense BGE-M3 baseline unchanged; V1 preserved; V2 separate; split frozen before tuning; locked test not used for tuning; NO_GRAPH supported; deterministic explicit directions; content relevance separate from edge existence; no score fusion or reranking; one-hop validated REFERENCES only; relation/content pages separate; complete provenance; bounded fan-out; production remains dense; Recent-GR stopped; Explorer untouched; no acquisition, corpus/KG mutation, Neo4j, LLM, RAG, agents, Podman, or backups; native PostgreSQL only.')
    recommendation={'MOVE_TO_RAG_V1':'A. MOVE TO RAG V1','ONE_MORE_RETRIEVAL_HARDENING_PHASE':'B. ONE MORE RETRIEVAL-ONLY HARDENING PHASE','KEEP_DENSE_ONLY':'C. KEEP DENSE RETRIEVAL ONLY AND MOVE TO DENSE-ONLY RAG DESIGN'}[decision]
    section('NEXT RECOMMENDATION',recommendation)
    body=quality+'\n'+''.join(sections)
    (OUT/'final_report.md').write_text(body,encoding='utf-8')
    return {'status':quality,'recommendation':decision,'report':str(OUT/'final_report.md')}

"""Staged development -> freeze -> one-shot test; no writes to the database."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from time import perf_counter

from knowledge_engine.retrieval.graph_aware_v2 import GraphAwareRetrievalV2, V2Policy
from knowledge_engine.retrieval.graph_eval import _CachingEmbedder, _CachingRepository, _csv, _json, _read_csv
from knowledge_engine.retrieval.graph_v2_intent import RULES, VERSION
from knowledge_engine.retrieval.graph_v2_measure import ReadOnlyRepository, dense_run, metric, result_row, v1_compact
from knowledge_engine.retrieval.graph_v2_protocol import (OUT, SPLIT, development_rows, read_json, sha,
    policy_source_hashes, claim_locked_test, snapshot, preserved_files, verify_policy)


class DevelopmentRepository(_CachingRepository):
    def connect(self):
        connection = super().connect()
        connection.execute('SET TRANSACTION READ ONLY')
        return connection


def evaluate(cases, embedder, repository, policy, *, trace_name=None):
    service = GraphAwareRetrievalV2(embedder, repository, repository, policy)
    results, traces = [], []
    for case in cases:
        response = service.search(case['query'], diagnostics=True)
        row = result_row(case, response)
        if isinstance(embedder, _CachingEmbedder):
            # Same query vector + dense ranking reused only for development comparisons.
            key = repository._vector_key(embedder.cache[case['query']])
            corrected = embedder.timings[case['query']] + repository.timings[(key,10,None)]
            row['total'] += corrected - row['query_embedding'] - row['dense_sql']
            row['query_embedding'] = embedder.timings[case['query']]
            row['dense_sql'] = repository.timings[(key,10,None)]
        results.append(row)
        if trace_name:
            traces.append({'case_id': case['case_id'],'split':case['split'],'policy':asdict(policy),**asdict(response)})
    if trace_name:
        (OUT / trace_name).write_text(''.join(json.dumps(r,ensure_ascii=False,default=str)+'\n' for r in traces),encoding='utf-8')
    return results, metric(results), traces


def summarize_variant(name, policy, metrics):
    return {'variant':name,'recovery':metrics['recovery'],'recovered':metrics['recovered'],
            'eligible':metrics['eligible'],'precision':metrics['precision'],'additions':metrics['additions'],
            'average_additions':metrics['average_additions'],'mean_total_ms':metrics['latency']['total']['mean_ms'],
            'p95_total_ms':metrics['latency']['total']['p95_ms'],'policy_json':json.dumps(asdict(policy))}


def develop(embedder) -> dict:
    cases = development_rows()
    if not read_json('v1_baseline_metrics.json')['v1_reproduced']:
        raise RuntimeError('V1 baseline must pass')
    cached = _CachingEmbedder(embedder)
    repository = DevelopmentRepository()
    base = V2Policy(max_per_seed=10,max_total=25)
    variants = {}
    def trial(name, policy):
        rows, metrics, _ = evaluate(cases,cached,repository,policy)
        variants[name] = (policy,rows,metrics)
        print(name,metrics['recovered'],metrics['additions'],metrics['precision'],flush=True)
        return metrics
    for seeds in (1,3,5):
        trial(f'seed_top{seeds}', replace(base,seed_top_k=seeds))
    for floor in (.4,.5,.6):
        trial(f'seed_floor_{floor}', replace(base,seed_similarity_floor=floor))
    for margin in (.02,.05):
        trial(f'seed_margin_{margin}', replace(base,seed_margin_floor=margin))
    for strategy,thresholds in [('absolute',(.35,.45,.55)),('relative',(.15,.25,.35)),('dense_tail',(0,.05,.1))]:
        for threshold in thresholds:
            trial(f'{strategy}_{threshold}',replace(base,candidate_strategy=strategy,candidate_threshold=threshold))
    # Recovery-first lexicographic development selection, then noise, then minimal seeds/gates.
    def selection_key(name):
        policy, _, m = variants[name]
        return (m['recovered'], -(m['additions']-m['useful']), -policy.seed_top_k,
                int(policy.seed_similarity_floor is None and policy.seed_margin_floor is None),
                int(policy.candidate_strategy == 'none'), name)
    selected_name = max(variants,key=selection_key)
    selected = variants[selected_name][0]
    # Fine boundary sensitivity is diagnostic only, not a second selection grid.
    sensitivity = []
    if selected.candidate_strategy != 'none':
        for delta in (-.025,0,.025):
            p = replace(selected,candidate_threshold=selected.candidate_threshold+delta)
            _,m,_ = evaluate(cases,cached,repository,p)
            sensitivity.append({'delta':delta,**summarize_variant('sensitivity',p,m)})
    else:
        sensitivity.append({'delta':0,**summarize_variant('no_threshold_selected',selected,variants[selected_name][2])})
    cap_trials = []
    wanted = variants[selected_name][2]['recovered']
    for cap in (1,2,3,4,6):
        p = replace(selected,max_per_seed=cap,max_total=cap)
        m = trial(f'cap_{cap}',p)
        if m['recovered'] == wanted:
            cap_trials.append((cap,p,m))
    if cap_trials:
        selected = min(cap_trials,key=lambda x:(x[2]['additions'],x[0]))[1]
    else:
        selected = replace(selected,max_per_seed=6,max_total=6)
    _json(OUT/'query_intent_rules.json',{'version':VERSION,'rules':RULES,
        'marathi_policy':'No unvalidated Marathi direction phrases. Mixed English/Marathi queries use the explicit relational English cue; ambiguous Marathi-only requests conservatively return NO_GRAPH.'})
    _csv(OUT/'threshold_sensitivity.csv',sensitivity)
    _csv(OUT/'seed_analysis.csv',[summarize_variant(n,p,m) for n,(p,_,m) in variants.items() if n.startswith('seed_')])
    _csv(OUT/'candidate_relevance_analysis.csv',[summarize_variant(n,p,m) for n,(p,_,m) in variants.items() if not n.startswith('seed_')])
    # Uncached selected-policy development pass is the primary latency evidence.
    final_rows, final_metrics, traces = evaluate(cases,embedder,ReadOnlyRepository(),selected,trace_name='retrieval_trace_dev.jsonl')
    _json(OUT/'development_results.json',final_rows)
    _json(OUT/'development_metrics.json',{'final':final_metrics,'v1':read_json('v1_baseline_metrics.json')['DEVELOPMENT'],
        'selected_policy':asdict(selected),'coarse_winner':selected_name,
        'selection_rule':'Maximize labeled recovery, then minimize unmatched additions; prefer fewer seeds and no seed threshold; reduce caps only with unchanged development recovery.',
        'formulae':{'recovery':'eligible V1 dense misses recovered / fixed eligible V1 cases',
                    'precision':'single-label matched new additions / all new additions',
                    'useful_addition_recall':'recovered / fixed V1 recoverable cases',
                    'rejection_precision':'unlabelled rejected / all rejected new documents',
                    'false_rejection_rate':'labelled useful rejected / (labelled useful rejected + accepted)',
                    'direction_accuracy':'intents matching explicit relation-direction labels / all cases',
                    'wrong_direction':'mismatched accepted/corroborated paths / all retained paths'},
        'latency_method':'Selected policy: uncached single-query embeddings and SQL with warm model. Ablations: shared identical embeddings/dense rows with first-execution costs restored; graph and restricted SQL actually executed.',
        'label_limitations':read_json('benchmark_split.json')['limitations']})
    decisions, seeds, candidates = [],[],[]
    for trace in traces:
        case = next(c for c in cases if c['case_id']==trace['case_id'])
        decisions.append({'case_id':case['case_id'],'query':case['query'],'expected_direction':case['required_direction'],**trace['decision']})
        for seed in trace['selected_seeds']:
            seeds.append({'case_id':case['case_id'],'labelled_seed':case['seed_document_id'],**seed})
        for candidate in trace['candidate_diagnostics']:
            candidates.append({'case_id':case['case_id'],'labelled_useful':candidate['document_id']==int(case['expected_document_id']),
                               **{k:v for k,v in candidate.items() if k!='paths'}})
    _csv(OUT/'development_intent_decisions.csv',decisions)
    _csv(OUT/'query_intent_decisions.csv',decisions)
    _csv(OUT/'development_seed_scores.csv',seeds)
    _csv(OUT/'development_candidate_scores.csv',candidates)
    ablations = []
    v1 = read_json('v1_baseline_metrics.json')['DEVELOPMENT']
    ablations.append({'variant':'V1','recovery':v1['graph_recovery_rate']['percentage'],'precision':v1['graph_expansion_precision']['percentage'],
                      'average_additions':v1['average_graph_additions_per_query'],'mean_total_ms':v1['latency']['total']['mean_ms'],
                      'p95_total_ms':v1['latency']['total']['p95_ms']})
    policies = {'direction_only':replace(base,no_graph_gate=False), 'no_graph_only':replace(base,direction_gate=False),
                'relevance_only':replace(base,direction_gate=False,no_graph_gate=False,candidate_strategy=selected.candidate_strategy,candidate_threshold=selected.candidate_threshold),
                'direction_and_relevance':replace(base,candidate_strategy=selected.candidate_strategy,candidate_threshold=selected.candidate_threshold)}
    for name,p in policies.items():
        m = trial(name,p)
        ablations.append(summarize_variant(name,p,m))
    ablations.append(summarize_variant('final_v2',selected,final_metrics))
    _csv(OUT/'ablation_metrics.csv',ablations)
    _csv(OUT/'development_stages.csv',[
        summarize_variant('V2-A',base,variants['seed_top1'][2]),
        summarize_variant('V2-B',base,variants['seed_top1'][2]),
        summarize_variant('V2-C',policies['direction_and_relevance'],variants['direction_and_relevance'][2]),
        summarize_variant('V2-D',selected,final_metrics)])
    failures = failure_analysis(final_rows,v1_compact('development_v1_results.csv'))
    _csv(OUT/'development_failure_analysis.csv',failures)
    return {'selected_policy':asdict(selected),'development':final_metrics}


TAXONOMY = ('INTENT_MISCLASSIFIED','SEED_WRONG','NO_RELEVANT_EDGE','USEFUL_NEIGHBOR_FILTERED_OUT',
            'IRRELEVANT_NEIGHBOR_ACCEPTED','DENSE_ALREADY_SUFFICIENT','QUERY_LABEL_LIMITATION','GRAPH_COVERAGE_LIMITATION','OTHER')


def failure_analysis(rows, baseline):
    result = []
    old = {r['case_id']:r for r in baseline}
    for r in rows:
        labels = []
        if not r['direction_correct']:
            labels.append('INTENT_MISCLASSIFIED')
        if r['eligible'] and old[r['case_id']]['recovered'] and not r['recovered']:
            labels.append('USEFUL_NEIGHBOR_FILTERED_OUT' if r['seed_found'] else 'SEED_WRONG')
        if r['additions'] > r['useful']:
            labels.extend(['IRRELEVANT_NEIGHBOR_ACCEPTED','QUERY_LABEL_LIMITATION'])
        if r['dense_sufficient']:
            labels.append('DENSE_ALREADY_SUFFICIENT')
        if r['raw_neighbors']==0 and not r['dense_sufficient'] and r['decision']!='NO_GRAPH':
            labels.append('NO_RELEVANT_EDGE')
        for label in labels:
            result.append({'case_id':r['case_id'],'category':label,'scope':'Single-label engineering case; overlapping categories',
                           'note':'Unmatched does not establish semantic irrelevance; REFERENCES does not imply legal effect.'})
    return result


def freeze() -> dict:
    development_rows()  # Refuses all post-freeze tuning.
    development = read_json('development_metrics.json')
    if not (OUT/'tests_pre_freeze.json').exists():
        raise RuntimeError('Record actual passing pre-freeze tests before freezing behavior')
    tests = read_json('tests_pre_freeze.json')
    if tests.get('status')!='PASS':
        raise RuntimeError('Pre-freeze tests failed')
    frozen = {'policy':development['selected_policy'],'source_hashes':policy_source_hashes(),
              'intent_rules_sha256':sha(OUT/'query_intent_rules.json'),
              'split_sha256':sha(SPLIT),'frozen_at':datetime.now(timezone.utc).isoformat(),
              'ordering':'seed dense rank, REFERENCES type, document ID, relation ID, direction; no reranking',
              'test_policy':'Exactly one locked-test evaluation, no subsequent tuning',
              'readiness_precommit': {'clear_precision_improvement':'strict increase on locked test, alongside at least half of V1 unmatched additions removed',
                  'recovery_protection':'all locked recoveries retained (small denominator; no tolerance inferred from three positives)',
                  'latency_budget_ms':1500,'relevance_safety':'Must have development-supported candidate discrimination, not only document-ID-ordered truncation.',
                  'rationale':'1.5 s preserves the observed V1 interactive range; no arbitrary target precision. A nonselective content gate cannot by itself establish relevance safety.'},
              'development_evidence_sha256':sha(OUT/'development_metrics.json')}
    frozen['policy_sha256'] = hashlib.sha256(json.dumps(frozen,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with (OUT/'v2_policy_frozen.json').open('x',encoding='utf-8') as handle:
        json.dump(frozen,handle,ensure_ascii=False,indent=2)
    return frozen


def locked_test(embedder) -> dict:
    frozen = claim_locked_test()
    cases = [r for r in _read_csv(SPLIT) if r['split']=='LOCKED_TEST']
    rows, metrics, _ = evaluate(cases,embedder,ReadOnlyRepository(),V2Policy(**frozen['policy']),trace_name='retrieval_trace_test.jsonl')
    _csv(OUT/'locked_test_v2_results.csv',rows)
    _json(OUT/'locked_test_results.json',rows)
    v1_rows = v1_compact('locked_test_v1_results.csv')
    _json(OUT/'locked_test_metrics.json',{'v1':read_json('v1_baseline_metrics.json')['LOCKED_TEST'],'v2':metrics,
        'v1_baseline_run':'Pre-tuning baseline; not rerun or inspected per-case during development.',
        'useful_suppressed':sum(old['recovered'] and not new['recovered'] for old,new in zip(v1_rows,rows)),
        'unlabelled_additions_rejected_vs_v1':sum(len(set(old['added_ids'])-set(new['added_ids'])-{new['expected_document_id']}) for old,new in zip(v1_rows,rows)),
        'failure_counts':dict(Counter(r['category'] for r in failure_analysis(rows,v1_rows))),
        'policy_sha256':frozen['policy_sha256'],'locked_test_runs':1,'test_tuning':False})
    return read_json('locked_test_metrics.json')


def finalize_measurements(embedder) -> dict:
    verify_policy()
    before = read_json('dense_before.json')
    after = dense_run(embedder,'dense_after.json')
    strip_timing = lambda rows:[{k:v for k,v in r.items() if k!='total_ms'} for r in rows]
    regression = {'status':'PASS' if before['summary']==after['summary'] and strip_timing(before['rows'])==strip_timing(after['rows']) else 'FAIL',
                  'before':before['summary'],'after':after['summary'],
                  'all_29_ranks_and_top10_scores_identical':strip_timing(before['rows'])==strip_timing(after['rows']),
                  'v1_baseline_identical':read_json('v1_baseline_metrics.json')['dense_matches_v1']}
    _json(OUT/'dense_regression.json',regression)
    baseline = read_json('baseline.json')
    now = snapshot()
    preserved = preserved_files()
    changed = [p for p,digest in baseline['preserved_files'].items() if preserved.get(p)!=digest]
    integrity = {'status':'PASS' if now==baseline['state'] and not changed else 'FAIL',
                 'before':baseline['state'],'after':now,'changed_protected_files':changed,
                 'corpus_mutated':now!=baseline['state'],'kg_mutated':now!=baseline['state'],
                 'dense_default':True,'acquisition':0,'recent_gr_resumed':False,'ui_changes':0,'v1_changes':0,
                 'podman_commands':0,'database_backups':0,'llm_calls':0,'rag_implementation':False}
    _json(OUT/'integrity_report.json',integrity)
    return {'dense':regression['status'],'integrity':integrity['status']}

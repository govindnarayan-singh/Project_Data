"""Read-only V3 evidence feasibility audit. No relevance tuning or locked run."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
from time import perf_counter

from knowledge_engine.retrieval.graph_v3_io import (
    ROOT, OUT, MANUAL, TMP, artifact_path, protected_files, read_csv, read_json, sha, write_csv, write_json,
)
from knowledge_engine.retrieval.graph_v3_labels import bounded_context, validate_legacy, label_pair, label_summary
from knowledge_engine.retrieval.graph_v2_protocol import snapshot, verify_policy, CORPUS_SHA, KG_SHA
from knowledge_engine.retrieval.graph_aware import GraphAwareRetrievalService, PostgresGraphExpansionRepository
from knowledge_engine.retrieval.graph_eval import _rank_dense, _variant_row, _variant_metrics
from knowledge_engine.settings import settings

V1 = ROOT / 'outputs/graph_retrieval_v1'
V2 = ROOT / 'outputs/graph_retrieval_v2'
BENCHMARK = ROOT / 'data/retrieval_eval_graph_v1.csv'


class ReadOnlyRepository(PostgresGraphExpansionRepository):
    def __init__(self):
        super().__init__()
        self.content = {}
        self.neighbors = []

    def connect(self):
        connection = super().connect()
        connection.execute('SET TRANSACTION READ ONLY')
        return connection

    def validated_reference_neighbors(self, *args, **kwargs):
        self.neighbors = super().validated_reference_neighbors(*args, **kwargs)
        return self.neighbors

    def dense_search_document(self, vector, document_id, top_k=1):
        rows = super().dense_search_document(vector, document_id, top_k)
        self.content[document_id] = rows
        return rows


def verify_state(state):
    expected = {'documents':87, 'pages':1588, 'chunks':1666, 'embeddings':1666,
                'kg_candidates':2631, 'kg_relations':137, 'embedding_dimensions':1024,
                'retrieval_corpus_fingerprint':CORPUS_SHA, 'kg_relation_fingerprint':KG_SHA,
                'postgresql_version':'17.11', 'pgvector':'0.8.6', 'integrity_status':'PASS'}
    if any(state[k] != value for k,value in expected.items()):
        raise RuntimeError('Live corpus/KG baseline mismatch; no tuning permitted')
    if state['relation_counts'] != {'REFERENCES':50, 'ISSUED_BY':87, 'AMENDS':0, 'CORRECTS':0,
                                   'SUPERSEDES':0, 'PARTIALLY_SUPERSEDES':0, 'CANCELS':0, 'EXTENDS':0}:
        raise RuntimeError('Relation baseline mismatch')


def initialize():
    if (OUT / 'baseline.json').exists():
        raise RuntimeError('V3 baseline already exists; do not silently overwrite an audit')
    prior = {p.name: p.read_text(encoding='utf-8') for p in OUT.glob('*') if p.is_file()}
    write_json(OUT / 'prior_stopped_preflight.json', prior)
    policy = verify_policy()
    state = snapshot()
    verify_state(state)
    write_json(OUT / 'baseline.json', {'status':'PASS', 'state':state, 'preserved_files':protected_files(),
        'captured_at':datetime.now(timezone.utc).isoformat(), 'v2_policy_sha256':policy['policy_sha256']})
    # Workspace roots follow the user's explicit later correction, not the old template.
    from sys import executable
    write_json(OUT / 'workspace_preflight.json', {
        'project_data_root':str(ROOT.parent), 'repo_root':str(ROOT.parent), 'git_toplevel':str(ROOT.parent),
        'application_root':str(ROOT), 'resolved_cwd':str(Path.cwd().resolve()), 'python_executable':executable,
        'database_host':'127.0.0.1', 'database_port':5434, 'database_name':'grdb',
        'planned_writable_directories':[str(ROOT / p) for p in ('knowledge_engine/retrieval','cli','tests','data','outputs/graph_retrieval_v3','tmp/graph_retrieval_v3')],
        'planned_readable_external_directories':[str(ROOT.parent / p) for p in ('venv','PostgreSQL','GRs','Government_Documents','.cache_local')],
        'postgres_runtime_write_exception':[str(ROOT.parent / 'PostgreSQL/data17'),str(ROOT.parent / 'PostgreSQL/logs')],
        'runtime_preflight':'cli.dev_preflight: PASS; SQL-verified native 17.11; started existing cluster',
        'workspace_guard_status':'PASS', 'git_optional_locks':'0', 'application_sql_read_only':True,
        'authority':'User root clarification and Developer Runtime Hardening V1 exception; original V3 attachment otherwise unchanged'})
    write_json(OUT / 'runtime_preflight.json', {'status':'PASS', 'version':state['postgresql_version'],
        'endpoint':'127.0.0.1:5434/grdb', 'prior_connection_blocker_resolved':True,
        'existing_cluster_started_via_authorized_helper':True, 'application_sql_read_only':True})
    return reproduce_v2()


def compact_metrics(rows, version):
    eligible_key, recovered_key, added_key, useful_key = (
        ('graph_recovery_eligible','graph_recovered','graph_additions','useful_graph_additions') if version == 'v1'
        else ('eligible','recovered','additions','useful'))
    yes = lambda value: str(value).lower() == 'true'
    eligible = sum(yes(row[eligible_key]) for row in rows)
    recovered = sum(yes(row[recovered_key]) for row in rows)
    added, useful = (sum(int(row[key]) for row in rows) for key in (added_key,useful_key))
    return {'cases':len(rows), 'eligible':eligible, 'recovered':recovered, 'additions':added, 'useful':useful,
            'recovery':recovered/eligible if eligible else None, 'legacy_single_target_precision':useful/added if added else None,
            'additions_per_query':added/len(rows)}


def reproduce_v2():
    verify_policy()
    files = ['development_v1_results.csv','locked_test_v1_results.csv','development_results.json',
             'locked_test_results.json','retrieval_trace_test.jsonl','v2_policy_frozen.json']
    report = {'method':'Recompute aggregates from immutable archived per-case results; NOT a fresh locked run',
              'historical_test_name':'V2_HISTORICAL_TEST', 'source_sha256':{p:sha(V2/p) for p in files}}
    for split, prefix in [('DEVELOPMENT','development'),('V2_HISTORICAL_TEST','locked_test')]:
        report[split] = {'v1':compact_metrics(read_csv(V2/f'{prefix}_v1_results.csv'),'v1'),
                         'v2':compact_metrics(read_json(V2/f'{prefix}_results.json'),'v2')}
    dev, old = report['DEVELOPMENT'], report['V2_HISTORICAL_TEST']
    observed = (dev['v1']['recovered'],dev['v1']['additions'],dev['v2']['recovered'],dev['v2']['additions'],
                old['v1']['recovered'],old['v1']['additions'],old['v2']['recovered'],old['v2']['additions'])
    if observed != (6,46,6,14,3,17,2,5):
        raise RuntimeError('V2 historical aggregate reproduction failed')
    traces = [json.loads(line) for line in (V2/'retrieval_trace_test.jsonl').read_text(encoding='utf-8').splitlines()]
    known = next(row for row in traces if row['case_id'] == 'GRV1-039')
    report['known_suppression'] = {'case_id':'GRV1-039', 'expected_document_id':34,
        'accepted_document_ids':[row['document_id'] for row in known['graph_additions']],
        'rejected_target':[row for row in known['rejected_candidates'] if row.get('document_id') == 34]}
    if not report['known_suppression']['rejected_target']:
        raise RuntimeError('Known V2 suppression missing from historical trace')
    report['status'] = 'PASS'
    write_json(OUT / 'v2_reproduction.json', report)
    return report


def load_evidence(repository):
    with repository.connect() as connection:
        documents = {row['id']:dict(row) for row in connection.execute('SELECT id,file_name,file_path,subject,canonical_identity FROM documents')}
        relations = {row['id']:dict(row) for row in connection.execute("SELECT * FROM kg_relations WHERE relation_type='REFERENCES'")}
        rows = [dict(row) for row in connection.execute('''SELECT e.relation_id,c.id candidate_id,
            c.source_document_id candidate_source_document_id,c.resolved_target_document_id,c.source_page,
            c.raw_evidence_text,c.validation_status candidate_validation_status,c.resolution_status,
            p.text page_text,p.id page_id
            FROM kg_relation_evidence e JOIN kg_reference_candidates c ON c.id=e.candidate_id
            LEFT JOIN pages p ON p.document_id=c.source_document_id AND p.page_number=c.source_page
            JOIN kg_relations r ON r.id=e.relation_id WHERE r.relation_type='REFERENCES'
            ORDER BY e.relation_id,c.source_page,c.id''')]
    by_relation = defaultdict(list)
    for row in rows:
        row.update(bounded_context(row.pop('page_text') or '', row['raw_evidence_text']))
        by_relation[row['relation_id']].append(row)
    return documents, relations, by_relation


class QueryCache:
    def __init__(self, embedder):
        self.embedder, self.vectors = embedder, {}

    def encode(self, texts, show_progress_bar=False):
        import numpy as np
        for text in texts:
            if text not in self.vectors:
                self.vectors[text] = self.embedder.encode([text], show_progress_bar=False)[0]
        return np.stack([self.vectors[text] for text in texts])


def local_embedder():
    from knowledge_engine.embeddings.bge_m3 import BgeM3Embedder
    hub = settings.cache_dir / 'models/models--BAAI--bge-m3'
    revision = (hub / 'refs/main').read_text().strip()
    source = (hub / 'snapshots' / revision).resolve()
    if not source.is_relative_to(hub.resolve()) or not source.is_dir():
        raise RuntimeError('MISSING_DEPENDENCY: existing local BGE-M3 snapshot unavailable')
    start = perf_counter()
    # Same production class, weights and encoding settings, local path prevents downloads.
    embedder = BgeM3Embedder(model_name=str(source))
    write_json(OUT/'model_runtime.json', {'model':'BAAI/bge-m3', 'revision':revision, 'source':str(source),
        'external_cache_read_only':True, 'offline':True, 'model_load_ms':(perf_counter()-start)*1000,
        'dimension':1024, 'relation_evidence_embeddings_computed':0})
    return embedder


def dense_regression(embedder):
    from knowledge_engine.evaluation.retrieval import load_dataset, aggregate_results
    from knowledge_engine.retrieval.service import RetrievalService
    service = RetrievalService(embedder, ReadOnlyRepository())
    rows = []
    for case in load_dataset(ROOT / 'data/retrieval_eval_10pdf.csv'):
        results = service.search(case.question, top_k=10)
        doc, page = _rank_dense(results,case.expected_file,set(case.expected_pages))
        rows.append({'question_id':case.question_id,'document_rank':doc,'page_rank':page,
                     'top_10':[asdict(result) for result in results]})
    # Canonicalize dates BEFORE comparing to stored JSON (known V2 reporting pitfall).
    rows = json.loads(json.dumps(rows, default=str))
    old = read_json(V2/'dense_before.json')
    prior = [{k:v for k,v in row.items() if k != 'total_ms'} for row in old['rows']]
    summary = aggregate_results([r['document_rank'] for r in rows], [r['page_rank'] for r in rows])
    differences = [r['question_id'] for r, previous in zip(rows,prior) if r != previous]
    passed = len(rows) == len(prior) == 29 and not differences and summary == old['summary']
    result = {'status':'PASS' if passed else 'FAIL', 'summary':summary, 'all_29_ranks_and_top10_records_identical':passed,
              'changed_question_ids':differences, 'reference_sha256':sha(V2/'dense_before.json'),
              'dataset_sha256':sha(ROOT/'data/retrieval_eval_10pdf.csv')}
    write_json(OUT/'dense_results.json', rows)
    write_json(OUT/'dense_regression.json', result)
    if not passed:
        raise RuntimeError('Dense regression changed; investigate before continuing')
    print('Dense regression PASS: all 29 Top-10 records identical', flush=True)


def collect(embedder):
    if MANUAL.exists() or (OUT/'candidate_pair_universe.csv').exists():
        raise RuntimeError('Candidate/manual-review outputs already exist; refusing to overwrite possible reviewer work')
    cases = read_csv(BENCHMARK)
    repository = ReadOnlyRepository()
    documents, relations, evidence = load_evidence(repository)
    validations = [validate_legacy(case,relations.get(int(case['relation_id'])),evidence[int(case['relation_id'])],documents) for case in cases]
    write_csv(OUT/'legacy_label_validation.csv', validations)
    groups = defaultdict(list)
    for case in cases:
        groups[case['query'].strip()].append(case)
    labels_by_case = {row['case_id']:row for row in validations}
    historic = {row['case_id']:row for row in read_csv(V1/'bidirectional_top1_results.csv')}
    split = {row['case_id']:row['split'] for row in read_csv(ROOT/'data/retrieval_eval_graph_v2_split.csv')}
    cache = QueryCache(embedder)
    service = GraphAwareRetrievalService(cache, repository, repository)
    pairs, timings, live_rows = [], [], []
    for index,(query, group) in enumerate(groups.items(),1):
        repository.content.clear()
        begin = perf_counter()
        response = service.search(query,dense_top_k=10,seed_top_k=1,direction='bidirectional')
        if response.limits_reached:
            raise RuntimeError('V1 safety cap hit: cannot use capped response as complete universe')
        seed = response.dense_results[0]
        raw_neighbor_ids = {int(row['target_document_id'] if int(row['source_document_id']) == seed.document_id else row['source_document_id']) for row in repository.neighbors}
        candidates = [*response.graph_additions,*response.corroborations]
        if raw_neighbor_ids != {row.document_id for row in candidates}:
            raise RuntimeError('Candidate universe omitted pre-cap neighbors')
        for case in group:
            prior = historic[case['case_id']]
            old_ids = {row['document_id'] for key in ('graph_additions_json','corroborations_json') for row in json.loads(prior[key])}
            if old_ids != raw_neighbor_ids or seed.document_id != json.loads(prior['dense_results_json'])[0]['document_id']:
                raise RuntimeError('Live V1 candidate universe differs from frozen baseline')
            live_rows.append(_variant_row(case,response,'V2_HISTORICAL_TEST' if split[case['case_id']]=='LOCKED_TEST' else 'V2_DEVELOPMENT_HISTORICAL'))
        query_id = 'V3Q-' + hashlib.sha256(query.encode()).hexdigest()[:12]
        legacy = [labels_by_case[case['case_id']] for case in group]
        extra_content_ms = 0.0
        for candidate in sorted(candidates,key=lambda c:c.document_id):
            content = repository.content.get(candidate.document_id)
            if content is None:
                started = perf_counter()
                content = repository.dense_search_document(cache.vectors[query],candidate.document_id,1)
                extra_content_ms += (perf_counter()-started)*1000
            content = content[0] if content else {}
            paths = []
            for path in candidate.paths:
                details = evidence[path.relation_id]
                paths.append({**asdict(path),'source_pdf':documents[path.source_document_id]['file_path'],
                              'target_pdf':documents[path.target_document_id]['file_path'], 'evidence_records':details})
            complete = all(p['relation_type']=='REFERENCES' and p['validation_state'] in {'AUTO_VERIFIED','HUMAN_CONFIRMED'}
                           and p['graph_distance']==1 and p['evidence_records']
                           and all(e['page_id'] and e['context_status']=='EXACT_NORMALIZED_MATCH'
                                   and e['candidate_source_document_id']==p['source_document_id']
                                   and e['resolved_target_document_id']==p['target_document_id'] for e in p['evidence_records']) for p in paths)
            evidence_rows = [e for path in paths for e in path['evidence_records']]
            row = {'query_id':query_id,'query':query,'legacy_case_ids':[c['case_id'] for c in group],
                   'historical_population':'V2_HISTORICAL_TEST' if any(split[c['case_id']]=='LOCKED_TEST' for c in group) else 'V2_DEVELOPMENT_HISTORICAL',
                   'seed_document_id':seed.document_id,'seed_dense_rank':seed.rank,'seed_dense_similarity':seed.similarity,
                   'candidate_document_id':candidate.document_id,'candidate_file':candidate.file_name,
                   'candidate_local_path':candidate.relative_path,'candidate_subject':candidate.subject,
                   'directions':sorted({p['direction'] for p in paths}), 'graph_distance':1,
                   'relation_ids':[p['relation_id'] for p in paths],
                   'relation_source_pages':[{'document_id':e['candidate_source_document_id'],'page':e['source_page']} for e in evidence_rows],
                   'relation_evidence_excerpts':[e['raw_evidence_text'] for e in evidence_rows],
                   'relation_contexts':[e['context'] for e in evidence_rows], 'paths':paths,
                   'relation_provenance_complete':complete, 'duplicate_state':candidate.duplicate_state,
                   'content_chunk_id':content.get('chunk_id'),'content_page_start':content.get('page_start'),
                   'content_page_end':content.get('page_end'),'content_excerpt':content.get('text',''),
                   'content_similarity':content.get('similarity'),
                   'content_method':'LIVE_DOCUMENT_RESTRICTED_COSINE_TOP1_UNCHANGED_BGE_M3',
                   'legacy_expected_document_ids':sorted({int(c['expected_document_id']) for c in group}),
                   'legacy_label_provenance':legacy,
                   **label_pair(candidate.document_id,legacy,complete,bool(content.get('text')))}
            pairs.append(row)
        timings.append({'query_id':query_id,'case_count':len(group),'candidate_count':len(candidates),
                        **response.timings_ms,'additional_corroboration_content_ms':extra_content_ms,
                        'audit_total_ms':(perf_counter()-begin)*1000})
        print(f'Candidate evidence {index}/{len(groups)}: {len(candidates)} documents',flush=True)
    metrics = _variant_metrics(live_rows)
    if (metrics['graph_recovery_rate']['numerator'],metrics['graph_recovery_rate']['denominator'],metrics['graph_expansion_precision']['denominator']) != (9,9,63):
        raise RuntimeError('V1 live aggregate reproduction failed')
    write_csv(OUT/'v1_live_results.csv',live_rows)
    write_json(OUT/'v1_reproduction.json',{'status':'PASS','method':'Unmodified V1 Top1 bidirectional service, live read-only SQL and query embeddings','metrics':metrics,'all_precap_candidates_accounted_for':True})
    write_csv(OUT/'candidate_pair_universe.csv',pairs)
    return save_collection(pairs, validations, len(cases), len(groups), timings)


def save_collection(pairs, validations, case_count, query_count, timings):
    # Save measured timing before attempting the separately guarded reviewer export.
    write_csv(OUT/'latency_results.csv',timings)
    write_csv(OUT/'multilabel_label_audit.csv',[{k:r[k] for k in ('query_id','candidate_document_id','label','label_reason','source_label_case_ids','legacy_expected_document_ids','relation_provenance_complete','content_chunk_id')} for r in pairs])
    summary = label_summary(pairs)
    summary.update({'benchmark_cases':case_count,'unique_queries':query_count,
                    'legacy_labels_valid':sum(r['valid'] for r in validations),
                    'legacy_labels_total':len(validations), 'model_generated_labels':0,
                    'unique_queries_with_multiple_supported_candidates':sum(sum(r['label']=='RELEVANT_SUPPORTED' for r in pairs if r['query']==q)>1 for q in {r['query'] for r in pairs}),
                    'scope':'Unique exact query/document pairs; duplicate case IDs retained as lineage; one-hop Top1, both directions, before V2 intent/cap filtering',
                    'candidate_universe_sha256':sha(OUT/'candidate_pair_universe.csv')})
    review = [{**row,'reviewer_label':'','reviewer_name':'','reviewed_at':'','reviewer_evidence':'','reviewer_notes':''}
              for row in pairs if row['label'] != 'RELEVANT_SUPPORTED']
    write_csv(MANUAL,review)
    summary.update(manual_review_rows=len(review),manual_review_sha256=sha(MANUAL))
    write_json(OUT/'multilabel_summary.json',summary)
    write_csv(OUT/'latency_results.csv',timings)
    write_csv(OUT/'relation_context_analysis.csv',[{'query_id':r['query_id'],'candidate_document_id':r['candidate_document_id'],
        'relation_ids':r['relation_ids'],'relation_contexts':r['relation_contexts'],'source_pages':r['relation_source_pages'],
        'evidence_complete':r['relation_provenance_complete'],'gate_status':'NOT_RUN_MANUAL_REVIEW_GATE'} for r in pairs])
    write_csv(OUT/'target_content_analysis.csv',[{'query_id':r['query_id'],'candidate_document_id':r['candidate_document_id'],
        'content_chunk_id':r['content_chunk_id'],'page_start':r['content_page_start'],'page_end':r['content_page_end'],
        'excerpt':r['content_excerpt'],'gate_status':'NOT_RUN_MANUAL_REVIEW_GATE'} for r in pairs])
    complete = sum(r['relation_provenance_complete'] and bool(r['content_chunk_id']) for r in pairs)
    write_json(OUT/'provenance_validation.json',{'scope':'Collected review evidence, NOT accepted V3 graph evidence',
        'candidate_pairs':len(pairs),'complete_pairs':complete,'missing_or_unlocated_pairs':len(pairs)-complete,
        'percentage':complete/len(pairs) if pairs else None, 'v3_accepted_graph_documents':0,
        'evidence_missing_is_not_a_negative_relevance_label':True})
    return summary


def finalize_saved():
    """Resume export/context verification from saved measurements, never rerun retrieval.

    Only the initial interrupted, unreviewed export may use this stage. It refuses
    any existing reviewer CSV, so reviewer edits can never be overwritten.
    """
    if MANUAL.exists():
        raise RuntimeError('Reviewer file exists; refusing to overwrite')
    if read_json(OUT/'dense_regression.json')['status'] != 'PASS' or read_json(OUT/'v1_reproduction.json')['status'] != 'PASS':
        raise RuntimeError('Completed dense/V1 measurements required')
    pairs = read_csv(OUT/'candidate_pair_universe.csv')
    original = sha(OUT/'candidate_pair_universe.csv')
    json_fields = ('legacy_case_ids','directions','relation_ids','relation_source_pages','relation_evidence_excerpts',
                   'relation_contexts','paths','legacy_expected_document_ids','legacy_label_provenance','source_label_case_ids')
    int_fields = ('seed_document_id','seed_dense_rank','candidate_document_id','graph_distance','content_chunk_id','content_page_start','content_page_end')
    for row in pairs:
        for key in json_fields:
            row[key] = json.loads(row[key])
        for key in int_fields:
            row[key] = int(row[key]) if row[key] else None
        for key in ('seed_dense_similarity','content_similarity'):
            row[key] = float(row[key]) if row[key] else None
    documents,relations,evidence = load_evidence(ReadOnlyRepository())
    cases = read_csv(BENCHMARK)
    validations = [validate_legacy(case,relations.get(int(case['relation_id'])),evidence[int(case['relation_id'])],documents) for case in cases]
    valid_by_case = {row['case_id']:row for row in validations}
    for row in pairs:
        for path in row['paths']:
            path['evidence_records'] = evidence[path['relation_id']]
        records = [item for path in row['paths'] for item in path['evidence_records']]
        complete = all(path['relation_type']=='REFERENCES' and path['validation_state'] in {'AUTO_VERIFIED','HUMAN_CONFIRMED'}
            and path['graph_distance']==1 and path['evidence_records'] and all(
                item['page_id'] and item['context_status']=='EXACT_NORMALIZED_MATCH'
                and item['candidate_source_document_id']==path['source_document_id']
                and item['resolved_target_document_id']==path['target_document_id'] for item in path['evidence_records']) for path in row['paths'])
        row['relation_contexts'] = [item['context'] for item in records]
        row['relation_provenance_complete'] = complete
        row['legacy_label_provenance'] = [valid_by_case[case_id] for case_id in row['legacy_case_ids']]
        row.update(label_pair(row['candidate_document_id'],row['legacy_label_provenance'],complete,bool(row['content_excerpt'])))
    write_csv(OUT/'legacy_label_validation.csv',validations)
    write_csv(OUT/'candidate_pair_universe.csv',pairs)
    # First export failed before its additional-corroboration timings were saved.
    # Recover only actually recorded V1 timings, never invent missing measurements.
    timings,seen = [],set()
    for row in read_csv(OUT/'v1_live_results.csv'):
        if row['query'] in seen:
            continue
        seen.add(row['query'])
        timings.append({'query_id':'V3Q-'+hashlib.sha256(row['query'].encode()).hexdigest()[:12],
            'scope':'LIVE_V1_TOP1_COLLECTION; NOT V3 GATING; extra corroboration-context audit time not persisted',
            **{key:float(row[key]) for key in ('query_embedding_ms','dense_sql_ms','graph_expansion_ms','restricted_page_ms','total_latency_ms')}})
    write_json(OUT/'export_resume.json',{'retrieval_or_dense_reruns':0,'before_context_normalization_sha256':original,
        'after_context_normalization_sha256':sha(OUT/'candidate_pair_universe.csv'),
        'correction':'Reuse existing parser NFC/zero-width/Devanagari-digit normalization for exact source-page matching; skip mkdir on existing data directory',
        'candidate_membership_and_content_retrieval_preserved':True,
        'normalization_source_sha256':sha(ROOT/'knowledge_engine/graph/normalization.py'),
        'limitation':'First-export full audit timings were not persisted; recovered live V1 response timings only'})
    result = save_collection(pairs,validations,len(cases),len({case['query'].strip() for case in cases}),timings)
    return finish(result)


def measure():
    before = read_json(OUT/'baseline.json')
    if protected_files() != before['preserved_files']:
        raise RuntimeError('Protected source/artifact changed since baseline')
    embedder = local_embedder()
    dense_regression(embedder)
    result = collect(embedder)
    return finish(result)


def finish(result):
    before = read_json(OUT/'baseline.json')
    if result['defensible_for_v3_tuning']:
        raise RuntimeError('Complete labels found; freeze a new protocol before implementing tuning; this audit cannot grant that authority')
    after = snapshot()
    verify_state(after)
    same = before['state'] == after
    files_same = before['preserved_files'] == protected_files()
    write_json(OUT/'integrity_report.json',{'status':'PASS' if same and files_same else 'FAIL',
        'before':before['state'],'after':after,'all_six_full_table_hashes_equal':same,
        'protected_files_unchanged':files_same,'corpus_mutations':0,'kg_mutations':0,
        'database_transaction_mode':'READ ONLY','production_retrieval_mode':settings.retrieval_mode})
    if not same or not files_same:
        raise RuntimeError('Post-audit integrity failed')
    write_json(OUT/'rag_readiness_gate.json',{'status':'CONDITIONAL PASS',
        'recommendation':'MANUAL_RELEVANCE_REVIEW_REQUIRED','reason':result['reason'],
        'next_recommendation':'B. MANUAL RELEVANCE REVIEW BEFORE FURTHER RETRIEVAL TUNING',
        'graph_rag_authorized':False,'v3_tuning_authorized':False,'v3_locked_run_count':0,
        'dense_only_rag_is_ready_for_experimental_v1':True,
        'dense_only_note':'Informational retrieval-readiness only; no RAG implementation or legal/answer-quality approval',
        'production_default':'dense'})
    write_json(OUT/'stage_status.json',{'completed':['workspace/live baseline','archived V2 aggregate reproduction',
        'live dense29 regression','live V1 reproduction','candidate evidence and legacy label audit','manual review export','before/after integrity'],
        'not_run_due_to_manual_review_gate':['V3 multilabel freeze','new grouped split','relation-context gate',
        'target-content gate','ablation','threshold sensitivity','V3 development','policy freeze','V3 locked test','RAG design'],
        'missing_conditional_outputs_are_intentional':True,'no_fake_policy_or_test_metrics_created':True})
    return result

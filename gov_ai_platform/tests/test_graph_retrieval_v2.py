from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from knowledge_engine.retrieval.graph_aware_v2 import GraphAwareRetrievalV2, PostgresGraphV2Repository, V2Policy, relevance_pass
from knowledge_engine.retrieval.graph_v2_intent import RULES, decide_intent
from knowledge_engine.retrieval import graph_v2_protocol as ledger
from knowledge_engine.retrieval.graph_v2_measure import metric
from tests.test_graph_retrieval_v1 import FakeEmbedder, FakeDenseRepository, FakeGraphRepository, dense_row, edge


class Graph(FakeGraphRepository):
    def dense_search_document(self, vector, document_id, top_k=1):
        self.page_calls.append((document_id,top_k))
        return [{**dense_row(document_id,7),'chunk_id': document_id*100,'similarity':.6}]


def service(policy=None, graph=None):
    return GraphAwareRetrievalV2(FakeEmbedder(),FakeDenseRepository(),graph or Graph(),policy or V2Policy(max_per_seed=6,max_total=6))


@pytest.mark.parametrize('query,direction',[
    ('What is the eligibility?','NO_GRAPH'),('What amount is payable?','NO_GRAPH'),
    ('Show the reference','NO_GRAPH'),('reference','NO_GRAPH'),('योजनेची पात्रता काय आहे?','NO_GRAPH'),
    ('Which referenced document defines the basis?','OUTGOING'),('Under which Act?','OUTGOING'),
    ('What earlier GR applies?','OUTGOING'),('Which previous decision is cited?','OUTGOING'),
    ('Show the administrative foundation','OUTGOING'),
    ('Which administrative document cites the official document योजना?','INCOMING'),
    ('Which documents reference this Act?','INCOMING'),('What refers to this GR?','INCOMING'),
    ('Find subsequent documents','INCOMING'),('Find documents based on this GR','INCOMING'),
    ('Show the reference history of this GR','BIDIRECTIONAL'),
    ('Show earlier and later documents','BIDIRECTIONAL'),
    ('Which referenced document and what refers to it?','NO_GRAPH'),
    ('Which GR supersedes this Act?','NO_GRAPH'),('What amendments to this Act exist?','NO_GRAPH'),
    ('Show the administrative history without graph expansion','NO_GRAPH'),
    ('Is this Act still valid?','NO_GRAPH'),
    ('Which administrative document cites the Second Amendment Act?','INCOMING'),
])
def test_intent_rules(query,direction):
    result=decide_intent(query)
    assert result.direction==direction
    assert result.reason and result.confidence and result.rules_triggered


def test_rule_audit_contract():
    for rule in RULES:
        assert {'rule_id','direction','positive_cues','negative_cues','precedence','examples','reason'}<=rule.keys()


def test_no_graph_does_no_graph_or_restricted_sql():
    graph=Graph()
    response=service(graph=graph).search('What is the amount?',diagnostics=True)
    assert response.decision.direction=='NO_GRAPH'
    assert not graph.direction_calls and not graph.page_calls
    assert not response.graph_additions and not response.rejected_candidates
    assert len(response.dense_results)==10


@pytest.mark.parametrize('query,ids',[
    ('Which referenced document applies?',[11,12]),
    ('Which document cites this Act?',[13]),
    ('Show the administrative history',[11,12,13])])
def test_one_hop_directions(query,ids):
    response=service(V2Policy(candidate_strategy='absolute',candidate_threshold=.5,max_total=6,max_per_seed=6)).search(query,diagnostics=True)
    assert [r.document_id for r in response.graph_additions]==ids
    assert all(p.graph_distance==1 for r in response.graph_additions for p in r.paths)


@pytest.mark.parametrize('query',['Which referenced document?', 'Which document cites this GR?', 'What is the date?'])
def test_dense_order_values_filters_and_query_encoding_unchanged(query):
    embed=FakeEmbedder()
    dense=FakeDenseRepository()
    result=GraphAwareRetrievalV2(embed,dense,Graph(),V2Policy()).search(query)
    assert [r.document_id for r in result.dense_results]==list(range(1,11))
    assert [r.similarity for r in result.dense_results]==[r['similarity'] for r in dense.rows]
    assert dense.calls==[([.1,.2,.3],10,None)]
    assert embed.calls==[([query],False)]


def test_restricted_lookup_reuses_query_vector_and_preserves_distinct_pages():
    graph=Graph()
    result=service(V2Policy(candidate_strategy='absolute',candidate_threshold=.5,max_per_seed=6,max_total=6),graph).search('Which referenced document?',diagnostics=True)
    assert graph.page_calls==[(11,1),(12,1)]
    for doc in result.graph_additions:
        assert doc.content_chunk_id==doc.document_id*100
        assert doc.query_relevant_page_start==7
        assert doc.paths[0].source_evidence_page in (1,2)
        assert doc.paths[0].candidate_id and doc.paths[0].relation_id
        assert doc.paths[0].seed_dense_rank==1 and doc.paths[0].seed_dense_similarity==.99


@pytest.mark.parametrize('strategy,threshold,score,accepted',[
    ('none',0,.1,True),('absolute',.5,.5,True),('absolute',.5,.49,False),
    ('relative',.2,.79,False),('relative',.2,.8,True),
    ('dense_tail',.05,.44,False),('dense_tail',.05,.45,True),
    ('none',0,float('nan'),False)])
def test_binary_relevance(strategy,threshold,score,accepted):
    assert relevance_pass(V2Policy(candidate_strategy=strategy,candidate_threshold=threshold),score,1,.5)[0] is accepted


def test_rejected_candidates_only_in_diagnostics_and_not_evidence():
    obj=service(V2Policy(candidate_strategy='absolute',candidate_threshold=.8))
    result=obj.search('Which referenced document?',diagnostics=True)
    assert not result.graph_additions
    assert {r['document_id'] for r in result.rejected_candidates}=={11,12}
    assert all('FLOOR' in r['reason'] for r in result.rejected_candidates)
    assert not obj.search('Which referenced document?').rejected_candidates


@pytest.mark.parametrize('defect',['missing','wrong_document','bad_page','no_chunk','no_text'])
def test_content_contract_fail_closed(defect):
    class Broken(Graph):
        def dense_search_document(self, vector, document_id, top_k=1):
            if defect=='missing': return []
            page=super().dense_search_document(vector,document_id,top_k)[0]
            page.update({'document_id':900} if defect=='wrong_document' else {'page_start':0} if defect=='bad_page' else {'chunk_id':None} if defect=='no_chunk' else {'text':''})
            return [page]
    result=service(graph=Broken()).search('Which referenced document?',diagnostics=True)
    assert not result.graph_additions
    assert all(r['reason']=='MISSING_OR_MISMATCHED_CONTENT_PAGE' for r in result.rejected_candidates)


@pytest.mark.parametrize('change',[
    {'relation_type':'ISSUED_BY'}, {'relation_type':'AMENDS'}, {'validation_status':'CANDIDATE'},
    {'validation_status':'UNRESOLVED'}, {'validation_status':'REJECTED'},
    {'candidate_id':None}, {'source_page':0}, {'raw_evidence_text':''}, {'relation_id':0}])
def test_edges_fail_closed_even_if_repository_returns_invalid_rows(change):
    graph=Graph()
    graph.edges=[{**edge(1,1,12),**change}]
    result=service(graph=graph).search('Which referenced document?',diagnostics=True)
    assert not result.graph_additions and not graph.page_calls
    assert result.rejected_candidates[0]['reason']=='UNTRUSTED_OR_INCOMPLETE_EDGE'


def test_valid_human_confirmed_not_fabricated_and_non_seed_edges_not_traversed():
    graph=Graph()
    graph.edges=[{**edge(1,1,12),'validation_status':'HUMAN_CONFIRMED'},edge(2,12,13)]
    result=service(graph=graph).search('Which referenced document?')
    assert [d.document_id for d in result.graph_additions]==[12]
    assert result.graph_additions[0].paths[0].validation_state=='HUMAN_CONFIRMED'


def test_dense_corroboration_no_lookup_duplicate_or_rank_change():
    result=service().search('Which referenced document?',diagnostics=True)
    assert len(result.corroborations)==1
    item=result.corroborations[0]
    assert item.document_id==2 and item.dense_rank==2
    assert item.duplicate_state=='ALREADY_IN_DENSE_GRAPH_CORROBORATED'
    assert item.paths and item.document_id not in [d.document_id for d in result.graph_additions]


@pytest.mark.parametrize('count',[1,3,5])
def test_seed_counts_are_unique_dense_documents(count):
    graph=Graph()
    obj=service(V2Policy(seed_top_k=count),graph)
    obj.search('Which referenced document?')
    assert len(graph.direction_calls[0][0])==count


@pytest.mark.parametrize('gate',[{'seed_similarity_floor':1.0},{'seed_margin_floor':.5}])
def test_seed_confidence_blocks_before_graph_lookup(gate):
    graph=Graph()
    result=service(V2Policy(**gate),graph).search('Which referenced document?',diagnostics=True)
    assert result.decision.direction=='NO_GRAPH' and not graph.direction_calls
    assert not result.selected_seeds[0]['accepted']


def test_deterministic_caps_and_multiple_paths():
    graph=Graph()
    policy=V2Policy(seed_top_k=3,max_per_seed=1,max_total=2)
    first=service(policy,graph).search('Show the administrative history',diagnostics=True)
    graph.edges.reverse()
    second=service(policy,graph).search('Show the administrative history',diagnostics=True)
    assert first.graph_additions==second.graph_additions
    assert len(first.graph_additions)<=2 and first.limits_reached
    counts={}
    for d in first.graph_additions:
        for p in d.paths: counts.setdefault(p.source_seed_document_id,set()).add(d.document_id)
    assert all(len(v)<=1 for v in counts.values())


def test_explicit_mode_cannot_bypass_legal_effect_guard():
    result=service().search('Which decision supersedes this Act?',graph_mode='incoming')
    assert result.decision.direction=='NO_GRAPH'


def test_ablation_flags_cannot_bypass_legal_effect_guard():
    result=service(V2Policy(direction_gate=False,no_graph_gate=False)).search('Which decision supersedes this Act?')
    assert result.decision.direction=='NO_GRAPH'


def test_default_policy_equals_development_selection():
    if not (ledger.OUT/'development_metrics.json').exists(): pytest.skip('Development not run')
    assert asdict(V2Policy())==ledger.read_json('development_metrics.json')['selected_policy']


@pytest.mark.parametrize('kwargs',[{'seed_top_k':2},{'max_total':26},{'max_per_seed':11},
    {'candidate_strategy':'fusion'},{'candidate_threshold':float('nan')},{'seed_margin_floor':float('inf')}])
def test_policy_rejects_invalid_knobs(kwargs):
    with pytest.raises(ValueError): V2Policy(**kwargs)


@pytest.mark.parametrize('kwargs',[{'query':''},{'query':'x','dense_top_k':100},{'query':'x','graph_mode':'automatic'},
    {'query':'x','max_graph_additions':26},{'query':'x','max_graph_additions':0}])
def test_search_validates_inputs(kwargs):
    with pytest.raises(ValueError): service().search(**kwargs)


@pytest.mark.parametrize('dsn',['postgresql://x@127.0.0.1:5432/grdb','postgresql://x@elsewhere:5434/grdb',
    'postgresql://x@127.0.0.1:5434/other'])
def test_native_only(dsn):
    with pytest.raises(ValueError): PostgresGraphV2Repository(dsn)


def fake_split_inputs():
    cases=[]
    old=[]
    for i in range(40):
        cases.append({'case_id':str(i),'query':f'query {i//2}','required_direction':'INCOMING' if i%4<2 else 'OUTGOING',
                      'expected_document_id':str(i+1),'label_source':'AUTO_VERIFIED_SOURCE_EVIDENCE'})
        old.append({'case_id':str(i),'graph_recovery_eligible':i<10,'graph_additions':2 if i<20 else 0,
                    'useful_graph_additions':int(i<10),'dense_document_rank':'' if i<10 else '1'})
    return cases,old


def test_split_is_deterministic_and_groups_duplicate_queries():
    cases,old=fake_split_inputs()
    rows,summary=ledger.freeze_split(cases,old)
    again,_=ledger.freeze_split(cases,old)
    assert rows==again and summary['total_rows']==40
    assert sum(r['split']=='LOCKED_TEST' for r in rows)==12
    for query in {r['query'] for r in rows}:
        assert len({r['split'] for r in rows if r['query']==query})==1


def test_development_refuses_after_freeze(monkeypatch,tmp_path):
    monkeypatch.setattr(ledger,'OUT',tmp_path)
    monkeypatch.setattr(ledger,'verify_split',lambda:None)
    (tmp_path/'v2_policy_frozen.json').write_text('{}')
    with pytest.raises(RuntimeError,match='Development closed'): ledger.development_rows()


def test_locked_test_claim_is_exclusive_and_requires_policy(monkeypatch,tmp_path):
    monkeypatch.setattr(ledger,'OUT',tmp_path)
    monkeypatch.setattr(ledger,'verify_policy',lambda:{'policy_sha256':'abc'})
    ledger.claim_locked_test()
    with pytest.raises(FileExistsError): ledger.claim_locked_test()


def test_split_modification_rejected(monkeypatch,tmp_path):
    monkeypatch.setattr(ledger,'OUT',tmp_path)
    monkeypatch.setattr(ledger,'SPLIT',tmp_path/'split.csv')
    monkeypatch.setattr(ledger,'BENCHMARK',tmp_path/'original.csv')
    ledger.SPLIT.write_text('a')
    ledger.BENCHMARK.write_text('b')
    (tmp_path/'benchmark_split.json').write_text(json.dumps({'split_sha256':'wrong','original_benchmark_sha256':ledger.sha(ledger.BENCHMARK)}))
    with pytest.raises(RuntimeError,match='modified'): ledger.verify_split()


def test_policy_tampering_rejected(monkeypatch,tmp_path):
    monkeypatch.setattr(ledger,'OUT',tmp_path)
    (tmp_path/'v2_policy_frozen.json').write_text(json.dumps({'policy':{},'policy_sha256':'wrong'}))
    with pytest.raises(RuntimeError,match='modified'): ledger.verify_policy()


def test_frozen_source_change_rejected(monkeypatch,tmp_path):
    monkeypatch.setattr(ledger,'OUT',tmp_path)
    value={'source_hashes':{'service':'old'}}
    value['policy_sha256']=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    (tmp_path/'v2_policy_frozen.json').write_text(json.dumps(value))
    monkeypatch.setattr(ledger,'policy_source_hashes',lambda:{'service':'new'})
    with pytest.raises(RuntimeError,match='source changed'): ledger.verify_policy()


def test_protected_existing_files_remain_byte_identical():
    if not (ledger.OUT/'baseline.json').exists(): pytest.skip('Live baseline not captured')
    baseline=ledger.read_json('baseline.json')['preserved_files']
    assert all(ledger.sha(ledger.PROJECT_ROOT/p)==digest for p,digest in baseline.items())


def test_no_dense_wiring_mutation_or_external_work_in_v2_service():
    source=Path('knowledge_engine/retrieval/graph_aware_v2.py').read_text(encoding='utf-8').lower()
    for term in ('insert into','delete from','update kg_','pg_dump','subprocess','requests.','graph_score','create table'):
        assert term not in source
    assert 'graph_aware_v2' not in Path('knowledge_engine/retrieval/service.py').read_text()
    assert ledger.settings.retrieval_mode=='dense'


@pytest.mark.skipif(os.getenv('RUN_GRAPH_V2_POSTGRES')!='1',reason='Opt-in read-only native PostgreSQL integration')
def test_native_postgres_readonly_live_counts_and_trust():
    repo=PostgresGraphV2Repository()
    with repo.connect() as connection:
        assert connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
        row=connection.execute('SELECT inet_server_port() port,(SELECT count(*) FROM documents) docs,(SELECT count(*) FROM chunks) chunks').fetchone()
        assert row=={'port':5434,'docs':87,'chunks':1666}
        seed=connection.execute("SELECT source_document_id FROM kg_relations WHERE relation_type='REFERENCES' ORDER BY id LIMIT 1").fetchone()['source_document_id']
    from knowledge_engine.retrieval.graph_aware import GraphDirection
    edges=repo.validated_reference_neighbors([seed],GraphDirection.OUTGOING)
    assert edges and all(r['relation_type']=='REFERENCES' and r['validation_status'] in {'AUTO_VERIFIED','HUMAN_CONFIRMED'} and r['candidate_id'] for r in edges)


@pytest.mark.skipif(os.getenv('RUN_GRAPH_V2_POSTGRES')!='1',reason='Opt-in read-only native PostgreSQL integration')
def test_live_restricted_content_and_fingerprints():
    from knowledge_engine.retrieval.graph_aware import GraphDirection
    repo=PostgresGraphV2Repository()
    with repo.connect() as connection:
        chunk=connection.execute('SELECT document_id,embedding FROM chunks ORDER BY id LIMIT 1').fetchone()
    rows=repo.dense_search_document(chunk['embedding'],chunk['document_id'],top_k=1)
    assert len(rows)==1 and rows[0]['document_id']==chunk['document_id'] and rows[0]['chunk_id']
    assert rows[0]['page_start']>0 and rows[0]['similarity']>.999
    assert ledger.snapshot()==ledger.read_json('baseline.json')['state']

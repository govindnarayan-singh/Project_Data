from copy import deepcopy
from pathlib import Path

import pytest

from knowledge_engine.retrieval import graph_v3_io as io
from knowledge_engine.retrieval.graph_v3_labels import bounded_context, label_pair, label_summary, validate_legacy


@pytest.mark.parametrize('path', ['../outside.json','D:/javascript_practice/task.json','C:/Users/Singh/task.json'])
def test_application_boundary_rejects_external_paths(path):
    with pytest.raises(ValueError):
        io.guarded(path)


@pytest.mark.parametrize('path', ['outputs/graph_retrieval_v2/new.json','docs/CURRENT_STATE.md','data/retrieval_eval_graph_v1.csv'])
def test_artifact_writer_cannot_modify_other_workstreams(path):
    with pytest.raises(ValueError):
        io.artifact_path(path)


def test_relative_and_absolute_v3_artifact_paths():
    path = io.OUT / 'example.json'
    assert io.artifact_path(path) == io.artifact_path('outputs/graph_retrieval_v3/example.json')


def test_context_is_source_matched_and_bounded():
    row = bounded_context('prefix ' * 500 + 'According to GR 123\n dated today.' + ' suffix' * 500,
                          'According to GR 123 dated today.', 40)
    assert row['context_status'] == 'EXACT_NORMALIZED_MATCH'
    assert 'According to GR 123 dated today.' in row['context']
    assert len(row['context']) <= len('According to GR 123 dated today.') + 80


def test_missing_excerpt_does_not_substitute_unrelated_page_text():
    assert bounded_context('unrelated content','missing')['context'] == ''
    assert bounded_context('anything','')['context_status'] == 'EXCERPT_NOT_LOCATED'


def test_context_uses_existing_parser_digit_normalization():
    context = bounded_context('दिनांक ५ डिसेंबर २०२२\nनिर्णय १२३', 'दिनांक 5 डिसेंबर 2022 निर्णय 123')
    assert context['context_status'] == 'EXACT_NORMALIZED_MATCH'


def evidence_fixture(direction='OUTGOING'):
    case = {'case_id':'C1','query':'Which referenced decision?', 'expected_document_id':'2',
            'seed_document_id':'1','relation_id':'10','required_direction':direction,
            'expected_file':'two.pdf','seed_file':'one.pdf','evidence_page':'3',
            'raw_evidence':'Reference 123','label_source':'AUTO_VERIFIED_SOURCE_EVIDENCE'}
    source,target = (1,2) if direction == 'OUTGOING' else (2,1)
    relation = {'source_document_id':source,'target_document_id':target,'relation_type':'REFERENCES','validation_status':'AUTO_VERIFIED'}
    evidence = [{'candidate_id':7,'candidate_source_document_id':source,'resolved_target_document_id':target,
                 'source_page':3,'raw_evidence_text':'Reference 123','context_status':'EXACT_NORMALIZED_MATCH'}]
    docs = {1:{'file_name':'one.pdf'},2:{'file_name':'two.pdf'}}
    return case,relation,evidence,docs


@pytest.mark.parametrize('direction',['INCOMING','OUTGOING'])
def test_existing_positive_revalidated_against_correct_source_direction(direction):
    args = evidence_fixture(direction)
    legacy = validate_legacy(*args)
    assert legacy['valid']
    assert label_pair(2,[legacy],True,True)['label'] == 'RELEVANT_SUPPORTED'


@pytest.mark.parametrize('defect',['direction','trust','file','page','excerpt','context','target'])
def test_invalid_existing_label_is_not_silently_accepted(defect):
    case,relation,evidence,docs = evidence_fixture()
    if defect == 'direction': relation['source_document_id'] = 2
    if defect == 'trust': relation['validation_status'] = 'CANDIDATE'
    if defect == 'file': docs[2]['file_name'] = 'wrong.pdf'
    if defect == 'page': evidence[0]['source_page'] = 4
    if defect == 'excerpt': evidence[0]['raw_evidence_text'] = 'other'
    if defect == 'context': evidence[0]['context_status'] = 'EXCERPT_NOT_LOCATED'
    if defect == 'target': evidence[0]['resolved_target_document_id'] = 3
    legacy = validate_legacy(case,relation,evidence,docs)
    assert not legacy['valid']
    assert label_pair(2,[legacy],True,True)['label'] == 'LABEL_CONFLICT'


def test_identical_query_positive_union_retains_lineage():
    first = validate_legacy(*evidence_fixture())
    second = {**first,'case_id':'C2','expected_document_id':3}
    assert label_pair(2,[first,second],True,True)['source_label_case_ids'] == ['C1']
    assert label_pair(3,[first,second],True,True)['source_label_case_ids'] == ['C2']


@pytest.mark.parametrize('candidate_id',[1,3,34,100000])
def test_unmatched_neighbor_is_review_required_not_negative(candidate_id):
    legacy = validate_legacy(*evidence_fixture())
    result = label_pair(candidate_id,[legacy],True,True)
    assert result['label'] == 'REVIEW_REQUIRED'
    assert 'NEGATIVE' in result['label_reason']


@pytest.mark.parametrize('relation,content',[(False,True),(True,False),(False,False)])
def test_missing_evidence_remains_neutral(relation,content):
    assert label_pair(3,[],relation,content)['label'] == 'INSUFFICIENT_EVIDENCE'


def test_manual_gate_requires_real_positive_and_negative_support():
    rows = [{'label':'RELEVANT_SUPPORTED'},{'label':'REVIEW_REQUIRED'}]
    result = label_summary(rows)
    assert result['NOT_RELEVANT_SUPPORTED'] == 0
    assert result['REVIEW_REQUIRED'] == 1
    assert result['defensible_for_v3_tuning'] is False
    assert result['tuning_authorized'] is False


def test_csv_preserves_blank_review_fields_and_unicode(monkeypatch,tmp_path):
    monkeypatch.setattr(io,'ROOT',tmp_path)
    monkeypatch.setattr(io,'OUT',tmp_path/'outputs')
    monkeypatch.setattr(io,'TMP',tmp_path/'tmp')
    monkeypatch.setattr(io,'MANUAL',tmp_path/'data/review.csv')
    io.write_csv(io.MANUAL,[{'query':'मराठी','label':'REVIEW_REQUIRED','reviewer_label':'','paths':[{'relation_id':1}]}])
    row = io.read_csv(io.MANUAL)[0]
    assert row['reviewer_label'] == '' and row['query'] == 'मराठी'


def test_v2_frozen_policy_and_sources_unchanged():
    from knowledge_engine.retrieval.graph_v2_protocol import verify_policy
    assert verify_policy()['policy_sha256'] == '5d136808d0afcf82a7126a9e99616ee4f9e6c5bd3070918d0d1b12be6f255d97'


@pytest.fixture
def collected_pairs():
    import json
    path = io.OUT/'candidate_pair_universe.csv'
    if not path.exists():
        pytest.skip('Run the read-only V3 evidence audit first')
    rows = io.read_csv(path)
    for row in rows:
        row['paths'] = json.loads(row['paths'])
        row['legacy_case_ids'] = json.loads(row['legacy_case_ids'])
    return rows


def test_collected_universe_is_unique_top1_one_hop_and_complete(collected_pairs):
    rows = collected_pairs
    assert len(rows) == len({(r['query_id'],r['candidate_document_id']) for r in rows})
    assert len({r['query_id'] for r in rows}) == 37
    assert len({case for row in rows for case in row['legacy_case_ids']}) == 40
    for row in rows:
        assert int(row['seed_dense_rank']) == 1 and int(row['graph_distance']) == 1
        assert row['content_chunk_id'] and row['content_excerpt']
        assert row['relation_provenance_complete'] == 'True'
        for path in row['paths']:
            assert path['relation_type'] == 'REFERENCES'
            assert path['validation_state'] in {'AUTO_VERIFIED','HUMAN_CONFIRMED'}
            assert path['graph_distance'] == 1 and path['direction'] in {'INCOMING','OUTGOING'}
            assert path['evidence_records']
            for evidence in path['evidence_records']:
                assert evidence['context_status'] == 'EXACT_NORMALIZED_MATCH'
                assert evidence['candidate_source_document_id'] == path['source_document_id']
                assert evidence['resolved_target_document_id'] == path['target_document_id']


def test_review_export_is_neutral_blank_and_complete(collected_pairs):
    review = io.read_csv(io.MANUAL)
    expected = {(r['query_id'],r['candidate_document_id']) for r in collected_pairs if r['label']=='REVIEW_REQUIRED'}
    assert {(r['query_id'],r['candidate_document_id']) for r in review} == expected
    assert len(review) == 76
    for row in review:
        assert row['label'] == 'REVIEW_REQUIRED'
        assert all(row[key] == '' for key in ('reviewer_label','reviewer_name','reviewed_at','reviewer_evidence','reviewer_notes'))
    assert not any(row['label']=='NOT_RELEVANT_SUPPORTED' for row in collected_pairs)


def test_v2_suppressed_useful_target_not_truncated_from_audit(collected_pairs):
    row = next(r for r in collected_pairs if 'GRV1-039' in r['legacy_case_ids'] and r['candidate_document_id']=='34')
    assert row['label'] == 'RELEVANT_SUPPORTED'
    assert any(r['candidate_document_id']=='29' and r['query_id']==row['query_id'] for r in collected_pairs)


def test_manual_gate_did_not_create_split_policy_or_consume_locked_test(collected_pairs):
    summary = io.read_json(io.OUT/'multilabel_summary.json')
    assert not summary['tuning_authorized']
    assert not (io.OUT/'v3_policy_frozen.json').exists()
    assert not (io.OUT/'locked_test_started.json').exists()
    assert not (io.ROOT/'data/retrieval_eval_graph_v3_split.csv').exists()
    assert io.read_json(io.OUT/'rag_readiness_gate.json')['v3_locked_run_count'] == 0


def test_current_v3_protected_baseline_remains_unchanged(collected_pairs):
    baseline = io.read_json(io.OUT/'baseline.json')['preserved_files']
    assert io.protected_files() == baseline

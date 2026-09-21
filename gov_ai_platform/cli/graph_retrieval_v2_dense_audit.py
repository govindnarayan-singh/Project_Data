"""Post-freeze reporting audit; does not invoke or change any retrieval behavior.

The frozen finalizer compares loaded JSON date strings with in-memory date
objects. Audit the persisted artifacts in the SAME representation instead.
Keep its initial result as evidence rather than changing frozen evaluator code.
"""
from __future__ import annotations

import json

from knowledge_engine.retrieval.graph_eval import _json
from knowledge_engine.retrieval.graph_v2_protocol import OUT, read_json, sha, verify_policy


def comparable(rows):
    return [{key:value for key,value in row.items() if key!='total_ms'} for row in rows]


def main():
    frozen=verify_policy()
    before=read_json('dense_before.json')
    after=read_json('dense_after.json')
    before_rows=comparable(before['rows'])
    after_rows=comparable(after['rows'])
    same=(len(before_rows)==len(after_rows)==29 and before_rows==after_rows and before['summary']==after['summary'])
    changed=[a['question_id'] for a,b in zip(before_rows,after_rows) if a!=b]
    original=read_json('dense_regression.json')
    initial_path=OUT/'dense_regression_pre_serialization_audit.json'
    if not initial_path.exists():
        _json(initial_path,original)
    evidence={'status':'PASS' if same else 'FAIL','persisted_artifact_comparison':True,
              'before_sha256':sha(OUT/'dense_before.json'),'after_sha256':sha(OUT/'dense_after.json'),
              'changed_question_ids':changed,'all_29_records_equal_excluding_latency':same,
              'root_cause':'Frozen finalizer compared loaded JSON gr_date strings against Python date objects before serialization. Both persisted artifacts are exactly equal excluding latency.',
              'policy_sha256':frozen['policy_sha256'],'frozen_sources_modified':False,
              'locked_test_replayed':False,'retrieval_changed':False}
    _json(OUT/'dense_serialization_audit.json',evidence)
    corrected={**original,'status':'PASS' if same else 'FAIL',
               'all_29_ranks_and_top10_scores_identical':same,
               'comparison_method':'Persisted before/after JSON; exclude only total_ms. See dense_serialization_audit.json.',
               'initial_reporting_flag_preserved_at':initial_path.name}
    _json(OUT/'dense_regression.json',corrected)
    print(json.dumps(evidence,indent=2))


if __name__=='__main__':
    main()

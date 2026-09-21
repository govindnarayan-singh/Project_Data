"""Read-only V1/dense measurement and explicitly single-label engineering metrics."""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from time import perf_counter

from knowledge_engine.evaluation.retrieval import aggregate_results, load_dataset
from knowledge_engine.retrieval.graph_aware import GraphAwareRetrievalService, PostgresGraphExpansionRepository
from knowledge_engine.retrieval.graph_eval import _csv, _json, _read_csv, _rank_dense, _percentile, _variant_row, _variant_metrics
from knowledge_engine.retrieval.graph_v2_protocol import OUT, SPLIT, V1, read_json, verify_split
from knowledge_engine.retrieval.service import RetrievalService
from knowledge_engine.settings import PROJECT_ROOT


class ReadOnlyRepository(PostgresGraphExpansionRepository):
    def connect(self):
        connection = super().connect()
        connection.execute('SET TRANSACTION READ ONLY')
        return connection


def dense_run(embedder, name: str) -> dict:
    service = RetrievalService(embedder, ReadOnlyRepository())
    rows = []
    for case in load_dataset(PROJECT_ROOT / 'data/retrieval_eval_10pdf.csv'):
        start = perf_counter()
        results = service.search(case.question, top_k=10)
        doc_rank, page_rank = _rank_dense(results, case.expected_file, set(case.expected_pages))
        rows.append({'question_id': case.question_id, 'document_rank': doc_rank, 'page_rank': page_rank,
                     'top_10': [asdict(r) for r in results], 'total_ms': (perf_counter()-start)*1000})
    summary = aggregate_results([r['document_rank'] for r in rows], [r['page_rank'] for r in rows])
    value = {'summary': summary, 'rows': rows}
    _json(OUT / name, value)
    return value


def baseline(embedder) -> dict:
    verify_split()
    if (OUT / 'v1_baseline_metrics.json').exists():
        raise RuntimeError('V1 baseline already recorded')
    if (OUT / 'development_metrics.json').exists():
        raise RuntimeError('Baseline cannot access locked rows during tuning')
    dense = dense_run(embedder, 'dense_before.json')
    repository = ReadOnlyRepository()
    service = GraphAwareRetrievalService(embedder, repository, repository)
    all_rows = []
    for case in _read_csv(SPLIT):
        response = service.search(case['query'], dense_top_k=10, seed_top_k=1,
                                  direction='bidirectional', retrieve_graph_pages=True)
        row = _variant_row(case, response, case['split'])
        all_rows.append(row)
        print('V1 baseline', case['case_id'], flush=True)
    for split, filename in [('DEVELOPMENT', 'development_v1_results.csv'), ('LOCKED_TEST', 'locked_test_v1_results.csv')]:
        _csv(OUT / filename, [r for r in all_rows if r['dataset'] == split])
    metrics = {split: _variant_metrics([r for r in all_rows if r['dataset'] == split]) for split in ('DEVELOPMENT','LOCKED_TEST')}
    metrics['full'] = _variant_metrics(all_rows)
    metrics['full']['p95_additions'] = _percentile([r['graph_additions'] for r in all_rows], .95)
    old = json.loads((V1 / 'dense_baseline_metrics.json').read_text(encoding='utf-8'))
    metrics['dense_matches_v1'] = all(dense['summary'][k] == old['summary'][k] for k in ('document','page'))
    metrics['dense_ranks_match_v1'] = [(r['document_rank'],r['page_rank']) for r in dense['rows']] == [(r['document_rank'],r['page_rank']) for r in old['rows']]
    metrics['v1_reproduced'] = (metrics['full']['graph_recovery_rate']['numerator'] == 9 and metrics['full']['graph_expansion_precision']['denominator'] == 63)
    _json(OUT / 'v1_baseline_metrics.json', metrics)
    if not all(metrics[k] for k in ('dense_matches_v1','dense_ranks_match_v1','v1_reproduced')):
        raise RuntimeError('V1 baseline not reproduced')
    return metrics


def true(value) -> bool:
    return str(value).lower() == 'true'


def result_row(case, response) -> dict:
    target = int(case['expected_document_id'])
    ids = {r.document_id for r in response.dense_results}
    additions = {r.document_id for r in response.graph_additions}
    paths = [p for r in (*response.graph_additions, *response.corroborations) for p in r.paths]
    complete = sum(bool(p.relation_id and p.candidate_id and p.source_evidence_page > 0 and p.raw_edge_evidence
                        and p.relation_type == 'REFERENCES' and p.validation_state in {'AUTO_VERIFIED','HUMAN_CONFIRMED'}
                        and p.graph_distance == 1 and p.source_seed_document_id and p.seed_dense_rank) for p in paths)
    rejected = [r for r in response.rejected_candidates if r.get('document_id') not in ids]
    rejected_ids = {r['document_id'] for r in rejected} - additions
    content_complete = sum(bool(r.content_chunk_id and r.query_relevant_page_start and r.query_relevant_page_text) for r in response.graph_additions)
    return {'case_id': case['case_id'], 'split': case['split'], 'query': case['query'],
            'expected_document_id': target, 'required_direction': case['required_direction'],
            'eligible': true(case['graph_recovery_eligible']), 'dense_sufficient': target in ids,
            'recovered': target not in ids and target in additions,
            'additions': len(additions), 'useful': int(target in additions), 'added_ids': sorted(additions),
            'rejected_ids': sorted(rejected_ids), 'rejected_useful': int(target not in ids and target in rejected_ids),
            'rejected_unlabelled': len(rejected_ids - {target}), 'decision': response.decision.direction,
            'intent_decision': response.intent_decision.direction,
            'seed_found': any(s['document_id'] == int(case['seed_document_id']) and s['accepted'] for s in response.selected_seeds),
            'direction_correct': response.intent_decision.direction == case['required_direction'],
            'wrong_direction_paths': sum(p.direction != case['required_direction'] for p in paths),
            'provenance_records': len(paths), 'complete_provenance_records': complete,
            'content_complete': content_complete, 'raw_neighbors': response.raw_neighbors,
            'corroborations': len(response.corroborations), 'limits_reached': list(response.limits_reached),
            **response.timings_ms}


def metric(rows: list[dict]) -> dict:
    n = len(rows)
    eligible = sum(r['eligible'] for r in rows)
    recovered = sum(r['recovered'] for r in rows)
    additions = sum(r['additions'] for r in rows)
    useful = sum(r['useful'] for r in rows)
    rejected_true = sum(r['rejected_useful'] for r in rows)
    rejected_other = sum(r['rejected_unlabelled'] for r in rows)
    provenance = sum(r['provenance_records'] for r in rows)
    complete = sum(r['complete_provenance_records'] for r in rows)
    no_graph = [r for r in rows if r['decision'] == 'NO_GRAPH']
    latencies = {}
    for key in ('query_embedding','dense_sql','intent_gate','graph_sql','restricted_content','total'):
        values = [r[key] for r in rows]
        latencies[key] = {'mean_ms': statistics.fmean(values) if values else 0,
                          'median_ms': statistics.median(values) if values else 0,
                          'p95_ms': _percentile(values,.95)}
    return {'cases': n, 'eligible': eligible, 'recovered': recovered,
            'recovery': recovered / eligible if eligible else None,
            'additions': additions, 'useful': useful, 'precision': useful / additions if additions else None,
            'useful_addition_recall': recovered / eligible if eligible else None,
            'average_additions': additions / n if n else 0,
            'median_additions': statistics.median([r['additions'] for r in rows]) if rows else 0,
            'p95_additions': _percentile([r['additions'] for r in rows],.95),
            'max_additions': max((r['additions'] for r in rows),default=0),
            'limit_hits': sum(bool(r['limits_reached']) for r in rows),
            'provenance_records': provenance, 'complete_provenance_records': complete,
            'provenance': complete / provenance if provenance else None,
            'content_pages_complete': sum(r['content_complete'] for r in rows),
            'evidence_coverage': sum(r['content_complete'] for r in rows)/additions if additions else None,
            'direction_accuracy': sum(r['direction_correct'] for r in rows)/n if n else None,
            'wrong_direction_rate': sum(r['wrong_direction_paths'] for r in rows)/provenance if provenance else 0,
            'rejected_useful': rejected_true, 'rejected_unlabelled': rejected_other,
            'rejection_precision': rejected_other/(rejected_true+rejected_other) if rejected_true+rejected_other else None,
            'false_rejection_rate': rejected_true/(rejected_true+useful) if rejected_true+useful else None,
            'no_graph_cases': len(no_graph),
            'no_graph_dense_sufficient': sum(r['dense_sufficient'] for r in no_graph),
            'no_graph_accuracy': None,
            'no_graph_accuracy_reason': 'No independent NO_GRAPH labels in original relational benchmark; dense sufficiency is only a single-label proxy.',
            'latency': latencies}


def v1_compact(name: str) -> list[dict]:
    # Caller is responsible for phase access; development never loads test results.
    rows = _read_csv(OUT / name)
    return [{'case_id': r['case_id'], 'eligible': true(r['graph_recovery_eligible']),
             'recovered': true(r['graph_recovered']), 'added_ids': [x['document_id'] for x in json.loads(r['graph_additions_json'])],
             'additions': int(r['graph_additions']), 'useful': int(r['useful_graph_additions'])} for r in rows]

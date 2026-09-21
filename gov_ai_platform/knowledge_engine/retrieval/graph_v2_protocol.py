"""Immutable experiment ledger. No V2 tuning code may read locked test outcomes."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from knowledge_engine.retrieval.graph_eval import _csv, _json, _read_csv, live_state
from knowledge_engine.settings import PROJECT_ROOT, settings

OUT = PROJECT_ROOT / 'outputs/graph_retrieval_v2'
SPLIT = PROJECT_ROOT / 'data/retrieval_eval_graph_v2_split.csv'
BENCHMARK = PROJECT_ROOT / 'data/retrieval_eval_graph_v1.csv'
V1 = PROJECT_ROOT / 'outputs/graph_retrieval_v1'
CORPUS_SHA = 'ebd956d83e7070a181ce33b295874e1739e877693fed31449035b6ee44f1f7a7'
KG_SHA = '09fbd3129ee0aacc41d21243c6cf01aa6255529da5530698831e93fcfe3c3dd4'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(name: str):
    return json.loads((OUT / name).read_text(encoding='utf-8'))


def native_guard() -> None:
    url = urlsplit(settings.database_url)
    if (url.hostname, url.port, url.path) != ('127.0.0.1', 5434, '/grdb'):
        raise RuntimeError('Only the native project PostgreSQL endpoint is allowed')
    if settings.retrieval_mode != 'dense':
        raise RuntimeError('Production default must remain dense')


def snapshot() -> dict:
    native_guard()
    # PGOPTIONS also protects V1 helpers; no mutation method is called here.
    from knowledge_engine.retrieval.graph_aware import PostgresGraphExpansionRepository
    state = live_state()
    with PostgresGraphExpansionRepository().connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        for table in ('documents', 'pages', 'chunks', 'kg_reference_candidates',
                      'kg_relations', 'kg_relation_evidence'):
            # Controlled table names, full stored rows including content and vectors.
            rows = connection.execute(f'SELECT row_to_json(t)::text value FROM {table} t ORDER BY row_to_json(t)::text').fetchall()
            state[table + '_full_sha256'] = hashlib.sha256('\n'.join(r['value'] for r in rows).encode()).hexdigest()
    return state


def preserved_files() -> dict[str, str]:
    """Includes V1, dense, UI, stopped Recent-GR files, acquisition and KG sources."""
    allowed_docs = {'docs/ARCHITECTURE.md', 'docs/CURRENT_STATE.md', 'docs/DEVELOPMENT_PLAN.md'}
    found = {}
    for folder in ('knowledge_engine', 'cli', 'tests', 'config', 'templates', 'static', 'data', 'docs', 'outputs'):
        for path in (PROJECT_ROOT / folder).rglob('*'):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if relative in allowed_docs or 'graph_retrieval_v2' in relative or 'graph_v2_' in relative or 'graph_aware_v2' in relative or 'GRAPH_RETRIEVAL_V2' in relative:
                continue
            found[relative] = sha(path)
    return found


def freeze_split(cases: list[dict], baseline: list[dict]) -> tuple[list[dict], dict]:
    """Group identical queries; deterministic DP approximates 70/30 strata counts."""
    by_id = {r['case_id']: r for r in baseline}
    groups = defaultdict(list)
    rows = []
    for case in cases:
        base = by_id[case['case_id']]
        recovery = str(base['graph_recovery_eligible']).lower() == 'true'
        negative = int(base['graph_additions']) > int(base['useful_graph_additions'])
        sufficient = bool(base['dense_document_rank'])
        row = {**case, 'expected_target': case['expected_document_id'],
               'direction_evidence': case['required_direction'],
               'graph_recovery_eligible': recovery, 'negative_addition_case': negative,
               'dense_already_finds_target': sufficient,
               'no_useful_graph_addition': not recovery,
               'case_category': '|'.join([case['required_direction'], 'RECOVERY' if recovery else 'DENSE_SUFFICIENT' if sufficient else 'NO_USEFUL_GRAPH', 'NEGATIVE_ADDITION' if negative else 'NO_NEGATIVE_ADDITION'])}
        rows.append(row)
        groups[case['query'].strip()].append(row)
    ordered = sorted(groups, key=lambda q: hashlib.sha256(('GRAPH-V2-SPLIT-1|' + q).encode()).hexdigest())
    dp = {(0, 0, 0, 0): ()}
    for index, query in enumerate(ordered):
        group = groups[query]
        delta = (len(group), sum(r['graph_recovery_eligible'] for r in group),
                 sum(r['required_direction'] == 'INCOMING' for r in group),
                 sum(r['negative_addition_case'] for r in group))
        for key, choice in list(dp.items()):
            candidate = tuple(a+b for a, b in zip(key, delta))
            if candidate[0] <= 15:
                dp.setdefault(candidate, (*choice, index))
    totals = (len(rows), sum(r['graph_recovery_eligible'] for r in rows),
              sum(r['required_direction'] == 'INCOMING' for r in rows), sum(r['negative_addition_case'] for r in rows))
    best = min(dp, key=lambda k: (sum(w*abs(a - round(b*.3)) for a,b,w in zip(k,totals,(100,20,5,1))), dp[k]))
    test_queries = {ordered[i] for i in dp[best]}
    for row in rows:
        row['split'] = 'LOCKED_TEST' if row['query'].strip() in test_queries else 'DEVELOPMENT'
    summary = {}
    for split in ('DEVELOPMENT', 'LOCKED_TEST'):
        subset = [r for r in rows if r['split'] == split]
        summary[split] = {'rows': len(subset), 'recovery_cases': sum(r['graph_recovery_eligible'] for r in subset),
                          'negative_cases': sum(r['negative_addition_case'] for r in subset),
                          'dense_sufficient': sum(r['dense_already_finds_target'] for r in subset),
                          'directions': dict(Counter(r['required_direction'] for r in subset))}
    return rows, {'strata': summary, 'total_rows': len(rows), 'unique_query_groups': len(groups),
                  'algorithm': 'SHA-ordered identical-query groups; subset DP minimizing rounded 30% counts (rows/recovery/incoming/negative weights 100/20/5/1)',
                  'seed': 'GRAPH-V2-SPLIT-1', 'duplicate_query_leakage': False,
                  'limitations': ['All 40 original questions are relational; no true content-only or bidirectional intent labels exist.',
                                 'Negative means unmatched single target, not independent proof of irrelevance.',
                                 'Group-held-out engineering test, not unseen human ground truth; V1 outcomes pre-exist.'],
                  'label_source': 'AUTO_VERIFIED_SOURCE_EVIDENCE', 'independent_human_ground_truth': False}


def initialize() -> dict:
    if (OUT / 'baseline.json').exists() or SPLIT.exists():
        raise RuntimeError('Baseline/split already frozen; refusing overwrite')
    state = snapshot()
    required = {'documents': 87, 'pages': 1588, 'chunks': 1666, 'embeddings': 1666,
                'kg_candidates': 2631, 'kg_relations': 137, 'embedding_dimensions': 1024,
                'retrieval_corpus_fingerprint': CORPUS_SHA, 'kg_relation_fingerprint': KG_SHA,
                'postgresql_version': '17.11', 'pgvector': '0.8.6', 'integrity_status': 'PASS'}
    if any(state[k] != v for k,v in required.items()):
        raise RuntimeError(f'Baseline mismatch: {state}')
    baseline_rows = _read_csv(V1 / 'bidirectional_top1_results.csv')
    cases = _read_csv(BENCHMARK)
    rows, metadata = freeze_split(cases, baseline_rows)
    _csv(SPLIT, rows)
    _csv(OUT / 'benchmark_split.csv', rows)
    metadata.update({'split_sha256': sha(SPLIT), 'original_benchmark_sha256': sha(BENCHMARK),
                     'frozen_at': datetime.now(timezone.utc).isoformat(), 'locked_test_tuning': False})
    _json(OUT / 'benchmark_split.json', metadata)
    _json(OUT / 'baseline.json', {'state': state, 'preserved_files': preserved_files(),
                                'captured_at': metadata['frozen_at'], 'status': 'PASS'})
    return metadata


def verify_split() -> None:
    frozen = read_json('benchmark_split.json')
    if sha(SPLIT) != frozen['split_sha256'] or sha(BENCHMARK) != frozen['original_benchmark_sha256']:
        raise RuntimeError('Frozen benchmark modified')
    if sha(OUT / 'benchmark_split.csv') != frozen['split_sha256']:
        raise RuntimeError('Split copy modified')


def development_rows() -> list[dict]:
    verify_split()
    if (OUT / 'v2_policy_frozen.json').exists() or (OUT / 'locked_test_started.json').exists():
        raise RuntimeError('Development closed; policy is frozen')
    return [r for r in _read_csv(SPLIT) if r['split'] == 'DEVELOPMENT']


def policy_source_hashes() -> dict[str, str]:
    return {name: sha(PROJECT_ROOT / name) for name in (
        'knowledge_engine/retrieval/graph_aware_v2.py', 'knowledge_engine/retrieval/graph_v2_intent.py',
        'knowledge_engine/retrieval/graph_v2_protocol.py', 'knowledge_engine/retrieval/graph_v2_eval.py',
        'knowledge_engine/retrieval/graph_v2_measure.py', 'knowledge_engine/retrieval/graph_v2_report.py',
        'cli/graph_retrieval_v2_eval.py')}


def verify_policy() -> dict:
    frozen = read_json('v2_policy_frozen.json')
    body = dict(frozen)
    digest = body.pop('policy_sha256')
    if hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != digest:
        raise RuntimeError('Policy artifact modified')
    if frozen['source_hashes'] != policy_source_hashes():
        raise RuntimeError('Frozen behavior source changed')
    if frozen['intent_rules_sha256'] != sha(OUT / 'query_intent_rules.json'):
        raise RuntimeError('Frozen intent rules artifact changed')
    if frozen['development_evidence_sha256'] != sha(OUT / 'development_metrics.json'):
        raise RuntimeError('Frozen development evidence changed')
    verify_split()
    return frozen


def claim_locked_test() -> dict:
    frozen = verify_policy()
    OUT.mkdir(parents=True, exist_ok=True)
    # Exclusive creation BEFORE retrieval: a crashed test cannot silently be retuned/replayed.
    with (OUT / 'locked_test_started.json').open('x', encoding='utf-8') as handle:
        json.dump({'policy_sha256': frozen['policy_sha256'], 'started_at': datetime.now(timezone.utc).isoformat()}, handle)
    return frozen

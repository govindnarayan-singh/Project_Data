"""Experimental one-hop evidence augmentation. Dense ranking is never modified."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from knowledge_engine.embeddings.bge_m3 import get_embedder
from knowledge_engine.retrieval.graph_aware import (ALLOWED_VALIDATION_STATES, GraphDirection,
    GraphDocument, GraphPath, PostgresGraphExpansionRepository, _search_result)
from knowledge_engine.retrieval.graph_v2_intent import IntentDecision, decide_intent
from knowledge_engine.retrieval.models import SearchResult
from knowledge_engine.settings import settings


@dataclass(frozen=True, slots=True)
class V2Policy:
    seed_top_k: int = 1
    seed_similarity_floor: float | None = None
    seed_margin_floor: float | None = None
    candidate_strategy: str = 'none'
    candidate_threshold: float = 0.0
    max_per_seed: int = 1
    max_total: int = 1
    direction_gate: bool = True
    no_graph_gate: bool = True
    direction_policy_version: str = 'graph-intent-2.0'

    def __post_init__(self):
        if self.seed_top_k not in {1,3,5}:
            raise ValueError('seed_top_k must be 1, 3, or 5')
        if not 1 <= self.max_per_seed <= 10 or not 1 <= self.max_total <= 25:
            raise ValueError('Caps must be positive and no larger than V1 (10/25)')
        if self.candidate_strategy not in {'none','absolute','relative','dense_tail'}:
            raise ValueError('Unsupported candidate strategy')
        for value in (self.candidate_threshold, self.seed_similarity_floor, self.seed_margin_floor):
            if value is not None and (not math.isfinite(value) or not -1 <= value <= 1):
                raise ValueError('Thresholds must be finite cosine values in [-1,1]')


class PostgresGraphV2Repository(PostgresGraphExpansionRepository):
    def __init__(self, dsn: str = settings.database_url):
        parsed = urlsplit(dsn)
        if (parsed.hostname, parsed.port, parsed.path) != ('127.0.0.1',5434,'/grdb'):
            raise ValueError('Graph V2 requires the native project database')
        super().__init__(dsn)

    def connect(self):
        connection = super().connect()
        connection.execute('SET TRANSACTION READ ONLY')
        return connection


@dataclass(frozen=True, slots=True)
class V2GraphDocument(GraphDocument):
    content_chunk_id: int | None = None
    candidate_relevance_reason: str = ''


@dataclass(frozen=True, slots=True)
class V2Response:
    query: str
    dense_top_k: int
    dense_results: tuple[SearchResult, ...]
    intent_decision: IntentDecision
    decision: IntentDecision
    selected_seeds: tuple[dict, ...]
    graph_additions: tuple[V2GraphDocument, ...]
    corroborations: tuple[V2GraphDocument, ...]
    rejected_candidates: tuple[dict, ...]
    raw_neighbors: int
    candidate_diagnostics: tuple[dict, ...]
    limits_reached: tuple[str, ...]
    timings_ms: dict[str, float]


def relevance_pass(policy: V2Policy, similarity: float, seed_similarity: float, dense_tail: float) -> tuple[bool,str]:
    if not math.isfinite(similarity):
        return False, 'NONFINITE_CONTENT_SIMILARITY'
    if policy.candidate_strategy == 'none':
        return True, 'CONTENT_PRESENT_NO_SCORE_GATE'
    floor = {'absolute': policy.candidate_threshold,
             'relative': seed_similarity - policy.candidate_threshold,
             'dense_tail': dense_tail - policy.candidate_threshold}[policy.candidate_strategy]
    return similarity >= floor, f'{policy.candidate_strategy.upper()}_FLOOR:{floor:.6f}'


class GraphAwareRetrievalV2:
    def __init__(self, embedder=None, dense_repository=None, graph_repository=None, policy: V2Policy | None = None):
        self._embedder = embedder
        self.dense_repository = dense_repository or PostgresGraphV2Repository()
        self.graph_repository = graph_repository or self.dense_repository
        self.policy = policy or V2Policy()

    def search(self, query: str, *, dense_top_k: int = 10, graph_mode: str = 'auto',
               max_graph_additions: int | None = None, diagnostics: bool = False) -> V2Response:
        query = query.strip()
        if not query or dense_top_k not in {3,5,10}:
            raise ValueError('A nonempty query and unchanged dense Top-K 3/5/10 are required')
        graph_mode = graph_mode.upper()
        if graph_mode not in {'AUTO','NO_GRAPH','OUTGOING','INCOMING','BIDIRECTIONAL'}:
            raise ValueError('Invalid graph mode')
        policy = self.policy
        if max_graph_additions is not None:
            if not 1 <= max_graph_additions <= policy.max_total:
                raise ValueError('Request cap may only reduce the policy cap')
            policy = replace(policy, max_total=max_graph_additions)
        started_total = perf_counter()
        started = perf_counter()
        self._embedder = self._embedder or get_embedder()
        vector = self._embedder.encode([query], show_progress_bar=False)[0]
        embedding_ms = (perf_counter()-started)*1000
        started = perf_counter()
        dense_rows = self.dense_repository.dense_search(vector, top_k=dense_top_k, department=None)
        dense_ms = (perf_counter()-started)*1000
        dense = tuple(_search_result(row, rank) for rank,row in enumerate(dense_rows,1))
        started = perf_counter()
        intent = decide_intent(query)
        decision = intent
        if graph_mode != 'AUTO':
            # Diagnostic explicit modes never bypass an unsupported legal-effect/opt-out rule.
            if not set(intent.rules_triggered) & {'UNSUPPORTED_LEGAL_EFFECT','EXPLICIT_NO_GRAPH'}:
                decision = IntentDecision(graph_mode, 'Explicit diagnostic mode.', 'EXPLICIT_MODE', ('EXPLICIT_MODE',))
        elif not policy.direction_gate and (intent.direction != 'NO_GRAPH' or not policy.no_graph_gate):
            decision = IntentDecision('BIDIRECTIONAL','Development ablation: direction gate disabled.', 'ABLATION', intent.rules_triggered)
        elif not policy.no_graph_gate and intent.direction == 'NO_GRAPH':
            decision = IntentDecision('BIDIRECTIONAL','Development ablation: use gate disabled.', 'ABLATION', intent.rules_triggered)
        if set(intent.rules_triggered) & {'UNSUPPORTED_LEGAL_EFFECT','EXPLICIT_NO_GRAPH'}:
            decision = intent
        unique = {}
        for item in dense:
            unique.setdefault(item.document_id, item)
        unique_rows = list(unique.values())
        margin = unique_rows[0].similarity-unique_rows[1].similarity if len(unique_rows)>1 else None
        seeds = []
        for item in unique_rows[:policy.seed_top_k]:
            score_ok = math.isfinite(item.similarity) and (policy.seed_similarity_floor is None or item.similarity >= policy.seed_similarity_floor)
            margin_ok = policy.seed_margin_floor is None or (margin is not None and margin >= policy.seed_margin_floor)
            seeds.append({'document_id': item.document_id, 'rank': item.rank, 'similarity': item.similarity,
                          'top1_unique_document_margin': margin, 'accepted': score_ok and margin_ok,
                          'reason': 'SEED_ACCEPTED' if score_ok and margin_ok else 'SEED_CONFIDENCE_REJECTED'})
        seed_map = {s['document_id']: s for s in seeds if s['accepted']}
        if decision.direction != 'NO_GRAPH' and not seed_map:
            decision = IntentDecision('NO_GRAPH','No credible dense seed.', 'NO_SEED', (*decision.rules_triggered,'NO_CREDIBLE_SEED'))
        intent_ms = (perf_counter()-started)*1000
        additions, corroborations, rejected, traces = [], [], [], []
        limits = set()
        graph_ms = content_ms = 0.0
        raw_count = 0
        if decision.direction != 'NO_GRAPH':
            started = perf_counter()
            edges = self.graph_repository.validated_reference_neighbors(list(seed_map), GraphDirection(decision.direction.lower()))
            graph_ms = (perf_counter()-started)*1000
            candidates = []
            for row in edges:
                source, target = int(row['source_document_id']), int(row['target_document_id'])
                for direction, seed, neighbor in [('OUTGOING',source,target),('INCOMING',target,source)]:
                    if seed not in seed_map or decision.direction not in {direction,'BIDIRECTIONAL'}:
                        continue
                    raw_count += 1
                    valid = (row.get('relation_type') == 'REFERENCES' and row.get('validation_status') in ALLOWED_VALIDATION_STATES
                             and row.get('candidate_id') and int(row.get('source_page') or 0)>0 and str(row.get('raw_evidence_text') or '').strip()
                             and row.get('relation_id') and neighbor != seed)
                    if not valid:
                        rejected.append({'document_id': neighbor,'relation_id': row.get('relation_id'),'reason': 'UNTRUSTED_OR_INCOMPLETE_EDGE'})
                        continue
                    candidates.append((seed_map[seed]['rank'],neighbor,row,direction,seed))
            candidates.sort(key=lambda x:(x[0],str(x[2]['relation_type']),x[1],int(x[2]['relation_id']),x[3]))
            grouped = {}
            for rank, neighbor, row, direction, seed in candidates:
                path = GraphPath(int(row['relation_id']), 'REFERENCES', direction, row['validation_status'], seed, rank,
                                 seed_map[seed]['similarity'], int(row['source_document_id']), int(row['target_document_id']),
                                 int(row['source_page']), str(row['raw_evidence_text']), int(row['candidate_id']))
                grouped.setdefault(neighbor, {'row': row, 'paths': []})['paths'].append(path)
            per_seed = {s:set() for s in seed_map}
            for neighbor, group in grouped.items():
                row, paths = group['row'], tuple(group['paths'])
                prefix = 'target' if neighbor == int(row['target_document_id']) else 'source'
                common = dict(document_id=neighbor, file_name=str(row[f'{prefix}_file']), relative_path=str(row[f'{prefix}_file_path']),
                              department=str(row[f'{prefix}_department']), gr_number=row.get(f'{prefix}_gr_number'),subject=row.get(f'{prefix}_subject'),
                              graph_added=neighbor not in unique, dense_rank=unique[neighbor].rank if neighbor in unique else None,paths=paths)
                if neighbor in unique:
                    corroborations.append(V2GraphDocument(**common, duplicate_state='ALREADY_IN_DENSE_GRAPH_CORROBORATED'))
                    continue
                started = perf_counter()
                page_rows = self.graph_repository.dense_search_document(vector, neighbor, top_k=1)
                content_ms += (perf_counter()-started)*1000
                page = page_rows[0] if page_rows else None
                valid_page = bool(page and int(page['document_id']) == neighbor and page.get('chunk_id')
                                  and int(page.get('page_start') or 0)>0 and int(page.get('page_end') or 0)>=int(page['page_start']) and page.get('text'))
                accepted, reason = relevance_pass(policy,float(page['similarity']),paths[0].seed_dense_similarity,dense[-1].similarity) if valid_page else (False,'MISSING_OR_MISMATCHED_CONTENT_PAGE')
                eligible_paths = [p for p in paths if len(per_seed[p.source_seed_document_id]) < policy.max_per_seed]
                if accepted and len(additions) >= policy.max_total:
                    accepted, reason = False, 'TOTAL_CAP'
                    limits.add('TOTAL')
                elif accepted and not eligible_paths:
                    accepted, reason = False, 'PER_SEED_CAP'
                    limits.add('PER_SEED')
                trace = {'document_id': neighbor, 'candidate_similarity': float(page['similarity']) if page else None,
                         'seed_similarity': paths[0].seed_dense_similarity, 'dense_tail_similarity': dense[-1].similarity,
                         'content_chunk_id': page.get('chunk_id') if page else None,
                         'content_page_start': page.get('page_start') if page else None,
                         'accepted': accepted,'reason': reason,'paths': paths}
                traces.append(trace)
                if not accepted:
                    rejected.append(trace)
                    continue
                # Every retained path respects its seed cap; deterministic first path owns eligibility.
                for path in eligible_paths:
                    per_seed[path.source_seed_document_id].add(neighbor)
                common['paths'] = tuple(eligible_paths)
                additions.append(V2GraphDocument(**common, duplicate_state='GRAPH_ADDED',
                    query_relevant_page_start=int(page['page_start']),query_relevant_page_end=int(page['page_end']),
                    query_relevant_page_similarity=float(page['similarity']),query_relevant_page_text=str(page['text']),
                    content_chunk_id=int(page['chunk_id']),candidate_relevance_reason=reason))
        timings = {'query_embedding': embedding_ms,'dense_sql': dense_ms,'intent_gate': intent_ms,
                   'graph_sql': graph_ms,'restricted_content': content_ms,'total': (perf_counter()-started_total)*1000}
        return V2Response(query,dense_top_k,dense,intent,decision,tuple(seeds),tuple(additions),tuple(corroborations),
                          tuple(rejected) if diagnostics else (),raw_count,tuple(traces) if diagnostics else (),tuple(sorted(limits)),timings)

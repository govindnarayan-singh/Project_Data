"""Conservative provenance audit, NOT a semantic relevance classifier.

Only revalidate existing positive labels and union them for identical queries.
Missing labels, opposite edge directions and scores never create negatives.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from knowledge_engine.graph.normalization import normalize_reference_whitespace

STATES = ('RELEVANT_SUPPORTED', 'NOT_RELEVANT_SUPPORTED', 'REVIEW_REQUIRED', 'LABEL_CONFLICT', 'INSUFFICIENT_EVIDENCE')
TRUSTED = {'AUTO_VERIFIED', 'HUMAN_CONFIRMED'}


def normalized(text: str) -> str:
    # Use the SAME NFC, zero-width removal and Devanagari digit translation as
    # the existing parser; no fuzzy matching or new semantic normalization.
    return ' '.join(normalize_reference_whitespace(text or '').split())


def bounded_context(page: str, evidence: str, radius: int = 300) -> dict:
    """Locate the complete stored excerpt, without fuzzy/semantic interpolation."""
    page, evidence = normalized(page), normalized(evidence)
    index = page.find(evidence) if evidence else -1
    if index < 0:
        return {'context_status': 'EXCERPT_NOT_LOCATED', 'context': '', 'context_start': None,
                'context_end': None, 'page_text_sha256': hashlib.sha256(page.encode()).hexdigest()}
    start, end = max(0, index-radius), min(len(page), index+len(evidence)+radius)
    return {'context_status': 'EXACT_NORMALIZED_MATCH', 'context': page[start:end], 'context_start': start,
            'context_end': end, 'page_text_sha256': hashlib.sha256(page.encode()).hexdigest()}


def validate_legacy(case: dict, relation: dict | None, evidence: list[dict], documents: dict) -> dict:
    target, seed = int(case['expected_document_id']), int(case['seed_document_id'])
    result = {'case_id': case['case_id'], 'query': case['query'], 'expected_document_id': target,
              'seed_document_id': seed, 'relation_id': int(case['relation_id']),
              'label_source': case['label_source'], 'independent_human_ground_truth': False,
              'valid': False, 'reason': '', 'supporting_candidate_ids': []}
    if not relation or seed not in documents or target not in documents:
        return {**result, 'reason': 'MISSING_RELATION_OR_DOCUMENT'}
    expected_pair = (seed, target) if case['required_direction'] == 'OUTGOING' else (target, seed)
    if (relation['source_document_id'], relation['target_document_id']) != expected_pair:
        return {**result, 'reason': 'RELATION_IDENTITY_CONFLICT'}
    if (relation['relation_type'] != 'REFERENCES' or relation['validation_status'] not in TRUSTED
            or documents[target]['file_name'] != case['expected_file'] or documents[seed]['file_name'] != case['seed_file']):
        return {**result, 'reason': 'TRUST_OR_FILE_IDENTITY_CONFLICT'}
    matching = [e for e in evidence if e['candidate_source_document_id'] == relation['source_document_id']
                and e['resolved_target_document_id'] == relation['target_document_id']
                and e['source_page'] == int(case['evidence_page'])
                and normalized(e['raw_evidence_text']) == normalized(case['raw_evidence'])
                and e['context_status'] == 'EXACT_NORMALIZED_MATCH']
    if not matching:
        return {**result, 'reason': 'SOURCE_PAGE_EVIDENCE_NOT_REVALIDATED'}
    return {**result, 'valid': True, 'reason': 'LEGACY_POSITIVE_REVALIDATED_AGAINST_LIVE_SOURCE_PAGE',
            'supporting_candidate_ids': [e['candidate_id'] for e in matching]}


def label_pair(document_id: int, legacy: list[dict], evidence_complete: bool, content_present: bool) -> dict:
    # No score, ID ordering, or inferred semantic judgment is an input.
    matching = [row for row in legacy if row['expected_document_id'] == document_id]
    valid = [row for row in matching if row['valid']]
    if matching and len(valid) != len(matching):
        state, reason = 'LABEL_CONFLICT', 'EXISTING_POSITIVE_COULD_NOT_BE_FULLY_REVALIDATED'
    elif valid:
        state, reason = 'RELEVANT_SUPPORTED', 'LIVE_REVALIDATED_EXISTING_POSITIVE_OR_IDENTICAL_QUERY_POSITIVE'
    elif not evidence_complete or not content_present:
        state, reason = 'INSUFFICIENT_EVIDENCE', 'MISSING_LOCATABLE_RELATION_OR_CONTENT_EVIDENCE'
    else:
        state, reason = 'REVIEW_REQUIRED', 'NO_EXISTING_EXPLICIT_RELEVANCE_JUDGMENT; UNMATCHED_IS_NOT_NEGATIVE'
    return {'label': state, 'label_reason': reason, 'source_label_case_ids': [r['case_id'] for r in valid],
            'label_version': 'V3_SOURCE_AUDIT_1', 'independent_human_ground_truth': False}


def label_summary(rows: list[dict]) -> dict:
    counts = Counter(row['label'] for row in rows)
    unresolved = sum(counts[state] for state in ('REVIEW_REQUIRED','LABEL_CONFLICT','INSUFFICIENT_EVIDENCE'))
    sufficient = counts['RELEVANT_SUPPORTED'] > 0 and counts['NOT_RELEVANT_SUPPORTED'] > 0 and unresolved == 0
    return {'candidate_pairs': len(rows), **{state: counts[state] for state in STATES},
            'defensible_for_v3_tuning': sufficient, 'tuning_authorized': False,
            'reason': 'MANUAL_RELEVANCE_REVIEW_REQUIRED_BEFORE_V3_TUNING' if not sufficient else 'LABEL_FEASIBILITY_PASSED_PROTOCOL_FREEZE_REQUIRED',
            'label_method': 'Revalidate existing explicit positive source labels; union identical-query positives. No new semantic labels.',
            'supported_negative_pairs_found': counts['NOT_RELEVANT_SUPPORTED'],
            'limitations': ['Legacy source-derived positives are not independent human relevance judgments.',
                           'REFERENCES, content similarity, and unmatched legacy labels do not prove irrelevance.',
                           'No independent negative relevance annotations exist in the audited V1/V2 inputs.',
                           'Reviewers must assess all candidate documents, not just the legacy expected target.']}

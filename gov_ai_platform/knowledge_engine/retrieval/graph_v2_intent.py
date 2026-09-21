"""Small, auditable relation-intent rules; never infer legal effects from REFERENCES."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

VERSION = 'graph-intent-2.0'
RULES = (
    {'rule_id': 'EXPLICIT_NO_GRAPH', 'direction': 'NO_GRAPH', 'precedence': 100,
     'positive_cues': [r'\b(?:without|do not use|ignore) (?:the )?(?:graph|relationships?|references?)\b'],
     'negative_cues': [], 'examples': ['Find the amount without graph expansion'],
     'reason': 'Explicit request to use content evidence only.'},
    {'rule_id': 'UNSUPPORTED_LEGAL_EFFECT', 'direction': 'NO_GRAPH', 'precedence': 90,
     'positive_cues': [r'\b(?:which|what)\b[^?]{0,80}\b(?:amends?|supersedes?|cancels?|repeals?|extends?)\b',
                       r'\b(?:amendments?|successors?) (?:to|of)\b', r'\b(?:in force|still valid|legal validity)\b'],
     'negative_cues': [], 'examples': ['Which GR supersedes this decision?'],
     'reason': 'REFERENCES cannot prove amendment, repeal, succession, or legal validity.'},
    {'rule_id': 'EXPLICIT_LINEAGE', 'direction': 'BIDIRECTIONAL', 'precedence': 80,
     'positive_cues': [r'\b(?:administrative|reference|document) (?:history|lineage)\b',
                       r'\b(?:both )?(?:predecessors? and successors?|earlier and later documents)\b'],
     'negative_cues': ['UNSUPPORTED_LEGAL_EFFECT', 'EXPLICIT_NO_GRAPH'],
     'examples': ['Show the administrative reference history of this GR'],
     'reason': 'Explicit broad reference lineage requests both directions, without legal-effect inference.'},
    {'rule_id': 'INCOMING_REFERENCE', 'direction': 'INCOMING', 'precedence': 60,
     'positive_cues': [r'\b(?:which|what) (?:\w+\s+){0,3}documents? (?:cites?|references?|refers? to|depends? on)\b',
                       r'\bwhat refers to\b', r'\b(?:later|subsequent|follow-up) (?:\w+\s+){0,2}documents?\b',
                       r'\bdocuments? (?:issued |based )*(?:based on|under) (?:this|the target)\b',
                       r'\b(?:referenced|cited|implemented) by\b', r'\bwhat came after\b'],
     'negative_cues': ['OUTGOING_REFERENCE unless explicit lineage'],
     'examples': ['Which administrative document cites this Act?', 'Find follow-up documents'],
     'reason': 'Find citing documents pointing toward the dense seed; no temporal/legal claim is inferred.'},
    {'rule_id': 'OUTGOING_REFERENCE', 'direction': 'OUTGOING', 'precedence': 60,
     'positive_cues': [r'\b(?:referenced|cited) documents?\b', r'\b(?:earlier|previous|prior|original|governing) (?:gr|act|decision|documents?)\b',
                       r'\b(?:under|according to) which (?:act|gr|decision|document)\b',
                       r'\b(?:this|the) (?:gr|document|decision) (?:depends on|is based on|refers to|cites|references)\b',
                       r'\b(?:documents? referred to|administrative foundation|source documents?|basis of this)\b'],
     'negative_cues': ['INCOMING_REFERENCE unless explicit lineage'],
     'examples': ['Which earlier GR is this decision based on?', 'Under which Act?'],
     'reason': 'Find documents referenced by the dense seed.'},
    {'rule_id': 'AMBIGUOUS_OR_CONTENT', 'direction': 'NO_GRAPH', 'precedence': 0,
     'positive_cues': [], 'negative_cues': ['unambiguous relational intent'],
     'examples': ['What is the eligibility?', 'Tell me about the scheme', 'reference'],
     'reason': 'Content-only, unsupported language, or conflicting direction has no defensible graph expansion.'},
)


@dataclass(frozen=True, slots=True)
class IntentDecision:
    direction: str
    reason: str
    confidence: str
    rules_triggered: tuple[str, ...]


def decide_intent(query: str) -> IntentDecision:
    query = unicodedata.normalize('NFKC', query).casefold()
    matched = [rule for rule in RULES if any(re.search(cue, query) for cue in rule['positive_cues'])]
    high = [r for r in matched if r['precedence'] >= 80]
    if high:
        winner = max(high, key=lambda r: r['precedence'])
        return IntentDecision(winner['direction'], winner['reason'], 'EXPLICIT_RULE', tuple(r['rule_id'] for r in matched))
    directions = {r['direction'] for r in matched}
    if len(directions) == 1:
        return IntentDecision(matched[0]['direction'], matched[0]['reason'], 'EXPLICIT_RULE', tuple(r['rule_id'] for r in matched))
    return IntentDecision('NO_GRAPH', 'Conflicting directions.' if len(directions)>1 else RULES[-1]['reason'],
                          'AMBIGUOUS' if directions else 'NO_RELATIONAL_CUE',
                          tuple(r['rule_id'] for r in matched) + ('AMBIGUOUS_OR_CONTENT',))

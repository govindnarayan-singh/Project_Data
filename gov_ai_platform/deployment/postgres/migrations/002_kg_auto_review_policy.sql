-- Allow provenance-linked development automation to retain AUTO_VERIFIED for
-- strong relations. HUMAN_CONFIRMED remains reserved for actual human review.
ALTER TABLE kg_relations DROP CONSTRAINT IF EXISTS kg_relations_check1;
ALTER TABLE kg_relations DROP CONSTRAINT IF EXISTS kg_relations_strong_validation_check;
ALTER TABLE kg_relations ADD CONSTRAINT kg_relations_strong_validation_check CHECK (
    relation_type IN ('REFERENCES', 'ISSUED_BY')
    OR validation_status = 'HUMAN_CONFIRMED'
    OR (validation_status = 'AUTO_VERIFIED' AND created_from_candidate_id IS NOT NULL)
);

COMMENT ON TABLE kg_relations IS
    'Validated edges. AUTO_VERIFIED means deterministic machine validation; HUMAN_CONFIRMED requires an actual human review event.';

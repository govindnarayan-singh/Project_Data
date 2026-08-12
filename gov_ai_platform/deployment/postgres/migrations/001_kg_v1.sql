CREATE TABLE IF NOT EXISTS kg_departments (
    id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kg_department_aliases (
    id BIGSERIAL PRIMARY KEY,
    department_id BIGINT NOT NULL REFERENCES kg_departments(id) ON DELETE CASCADE,
    raw_alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'CORPUS_METADATA',
    validation_status TEXT NOT NULL DEFAULT 'AUTO_VERIFIED'
        CHECK (validation_status IN ('CANDIDATE','AUTO_VERIFIED','HUMAN_CONFIRMED','REJECTED','UNRESOLVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kg_extraction_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    documents_processed INTEGER NOT NULL DEFAULT 0,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    resolved_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING','SUCCESS','FAILED')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS kg_reference_candidates (
    id BIGSERIAL PRIMARY KEY,
    source_document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_page INTEGER NOT NULL CHECK (source_page > 0),
    reference_kind TEXT NOT NULL CHECK (reference_kind IN (
        'GOVERNMENT_RESOLUTION','CIRCULAR','GOVERNMENT_CIRCULAR','NOTIFICATION','ACT','RULE',
        'GOVERNMENT_LETTER','DEPARTMENT_LETTER','GOVERNMENT_ORDER','COURT_ORDER',
        'SCHEME_POLICY_DOCUMENT','MEETING_MINUTES','CORRIGENDUM','SUPPLEMENT','UNKNOWN'
    )),
    raw_evidence_text TEXT NOT NULL CHECK (length(raw_evidence_text) > 0),
    raw_document_number TEXT NOT NULL DEFAULT '',
    normalized_document_number TEXT NOT NULL DEFAULT '',
    raw_reference_date TEXT NOT NULL DEFAULT '',
    normalized_reference_date DATE,
    date_parse_status TEXT NOT NULL,
    raw_department TEXT NOT NULL DEFAULT '',
    normalized_department TEXT NOT NULL DEFAULT '',
    pattern_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    parser_status TEXT NOT NULL CHECK (parser_status IN ('VALID','PARTIAL','NEEDS_REVIEW','SELF_REFERENCE_REJECTED')),
    parser_notes TEXT NOT NULL DEFAULT '',
    relation_signal TEXT NOT NULL DEFAULT '',
    candidate_relation_type TEXT NOT NULL DEFAULT '' CHECK (
        candidate_relation_type IN ('','AMENDS','SUPERSEDES','PARTIALLY_SUPERSEDES','CANCELS','CORRECTS','EXTENDS')
    ),
    resolution_status TEXT NOT NULL CHECK (resolution_status IN (
        'RESOLVED_EXACT','RESOLVED_STRONG','AMBIGUOUS','NOT_IN_CORPUS','UNPARSEABLE',
        'SELF_REFERENCE_REJECTED','NEEDS_REVIEW'
    )),
    resolved_target_document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    resolution_method TEXT NOT NULL,
    resolution_evidence TEXT NOT NULL,
    resolution_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (resolution_candidate_count >= 0),
    confidence_category TEXT NOT NULL CHECK (confidence_category IN ('HIGH','MEDIUM','LOW')),
    validation_status TEXT NOT NULL CHECK (validation_status IN ('CANDIDATE','AUTO_VERIFIED','HUMAN_CONFIRMED','REJECTED','UNRESOLVED')),
    blocked_by_page_quality BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_by_target_metadata BOOLEAN NOT NULL DEFAULT FALSE,
    reviewer TEXT,
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    evidence_hash CHAR(64) NOT NULL,
    fingerprint CHAR(64) NOT NULL UNIQUE,
    first_run_id UUID NOT NULL REFERENCES kg_extraction_runs(run_id),
    last_run_id UUID NOT NULL REFERENCES kg_extraction_runs(run_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (source_document_id, source_page) REFERENCES pages(document_id, page_number) ON DELETE CASCADE,
    CHECK ((resolution_status IN ('RESOLVED_EXACT','RESOLVED_STRONG') AND resolved_target_document_id IS NOT NULL)
        OR (resolution_status NOT IN ('RESOLVED_EXACT','RESOLVED_STRONG') AND resolved_target_document_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_kg_candidates_source ON kg_reference_candidates(source_document_id, source_page);
CREATE INDEX IF NOT EXISTS idx_kg_candidates_target ON kg_reference_candidates(resolved_target_document_id) WHERE resolved_target_document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_candidates_resolution ON kg_reference_candidates(resolution_status, validation_status);
CREATE INDEX IF NOT EXISTS idx_kg_candidates_unresolved_number ON kg_reference_candidates(normalized_document_number)
    WHERE resolution_status IN ('NOT_IN_CORPUS','UNPARSEABLE','NEEDS_REVIEW');
CREATE INDEX IF NOT EXISTS idx_kg_candidates_relation_signal ON kg_reference_candidates(candidate_relation_type)
    WHERE candidate_relation_type <> '';

CREATE TABLE IF NOT EXISTS kg_relations (
    id BIGSERIAL PRIMARY KEY,
    source_document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    target_document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    target_department_id BIGINT REFERENCES kg_departments(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'REFERENCES','ISSUED_BY','AMENDS','SUPERSEDES','PARTIALLY_SUPERSEDES','CANCELS','CORRECTS','EXTENDS'
    )),
    validation_status TEXT NOT NULL CHECK (validation_status IN ('AUTO_VERIFIED','HUMAN_CONFIRMED')),
    created_from_candidate_id BIGINT REFERENCES kg_reference_candidates(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (relation_type = 'ISSUED_BY' AND target_department_id IS NOT NULL AND target_document_id IS NULL)
        OR (relation_type <> 'ISSUED_BY' AND target_document_id IS NOT NULL AND target_department_id IS NULL)
    ),
    CHECK (relation_type IN ('REFERENCES','ISSUED_BY') OR validation_status = 'HUMAN_CONFIRMED')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_relation_document_target
    ON kg_relations(source_document_id, target_document_id, relation_type)
    WHERE target_document_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_relation_department_target
    ON kg_relations(source_document_id, target_department_id, relation_type)
    WHERE target_department_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_relations_target_document ON kg_relations(target_document_id) WHERE target_document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS kg_relation_evidence (
    id BIGSERIAL PRIMARY KEY,
    relation_id BIGINT NOT NULL REFERENCES kg_relations(id) ON DELETE CASCADE,
    candidate_id BIGINT NOT NULL REFERENCES kg_reference_candidates(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(relation_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS kg_reviews (
    id BIGSERIAL PRIMARY KEY,
    candidate_id BIGINT NOT NULL REFERENCES kg_reference_candidates(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('HUMAN_CONFIRMED','REJECTED','CANDIDATE','UNRESOLVED')),
    reviewer TEXT NOT NULL,
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE gr_relations IS 'Deprecated zero-row placeholder retained for compatibility; KG V1 uses kg_reference_candidates and kg_relations.';
COMMENT ON TABLE kg_reference_candidates IS 'Provenance-first unresolved/resolved reference candidates; raw evidence is never replaced by normalized values.';
COMMENT ON TABLE kg_relations IS 'Validated graph edges only. Strong legal relations require HUMAN_CONFIRMED.';

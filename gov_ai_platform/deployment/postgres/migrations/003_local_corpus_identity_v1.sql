CREATE TABLE IF NOT EXISTS local_document_catalog (
    id BIGSERIAL PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    department_folder TEXT NOT NULL,
    normalized_department TEXT NOT NULL DEFAULT '',
    sha256 CHAR(64) NOT NULL,
    file_size BIGINT NOT NULL CHECK (file_size >= 0),
    file_mtime_ns BIGINT NOT NULL DEFAULT 0,
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    filename_date DATE,
    document_kind TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (document_kind IN (
        'GOVERNMENT_RESOLUTION','CIRCULAR','GOVERNMENT_CIRCULAR','NOTIFICATION','ACT','RULE',
        'GOVERNMENT_LETTER','DEPARTMENT_LETTER','GOVERNMENT_ORDER','COURT_ORDER',
        'SCHEME_POLICY_DOCUMENT','MEETING_MINUTES','CORRIGENDUM','SUPPLEMENT','UNKNOWN'
    )),
    raw_document_number TEXT NOT NULL DEFAULT '',
    normalized_document_number TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT '',
    normalized_date DATE,
    raw_department TEXT NOT NULL DEFAULT '',
    raw_subject TEXT NOT NULL DEFAULT '',
    subject_preview TEXT NOT NULL DEFAULT '',
    legal_canonical_key TEXT NOT NULL DEFAULT '',
    identity_source_page INTEGER CHECK (identity_source_page IS NULL OR identity_source_page > 0),
    identity_source_method TEXT NOT NULL CHECK (identity_source_method IN ('DIGITAL','OCR','INGESTED_METADATA','NONE')),
    identity_parse_status TEXT NOT NULL CHECK (identity_parse_status IN (
        'IDENTIFIED_EXACT','IDENTIFIED_PARTIAL','NO_IDENTIFIER','AMBIGUOUS_METADATA',
        'EXTRACTION_FAILED','NEEDS_OCR','QUALITY_BLOCKED'
    )),
    identity_quality TEXT NOT NULL CHECK (identity_quality IN ('HIGH','MEDIUM','LOW','BLOCKED')),
    digital_quality TEXT NOT NULL DEFAULT '',
    probe_pages INTEGER[] NOT NULL DEFAULT '{}',
    ocr_used BOOLEAN NOT NULL DEFAULT FALSE,
    ocr_pages INTEGER[] NOT NULL DEFAULT '{}',
    extraction_error TEXT NOT NULL DEFAULT '',
    ingested_document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    catalog_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_local_catalog_sha ON local_document_catalog(sha256);
CREATE INDEX IF NOT EXISTS idx_local_catalog_number ON local_document_catalog(normalized_document_number)
    WHERE normalized_document_number <> '';
CREATE INDEX IF NOT EXISTS idx_local_catalog_number_date ON local_document_catalog(normalized_document_number, normalized_date)
    WHERE normalized_document_number <> '';
CREATE INDEX IF NOT EXISTS idx_local_catalog_number_department ON local_document_catalog(normalized_document_number, normalized_department)
    WHERE normalized_document_number <> '';
CREATE INDEX IF NOT EXISTS idx_local_catalog_number_date_department
    ON local_document_catalog(normalized_document_number, normalized_date, normalized_department)
    WHERE normalized_document_number <> '';
CREATE INDEX IF NOT EXISTS idx_local_catalog_legal_key ON local_document_catalog(legal_canonical_key)
    WHERE legal_canonical_key <> '';
CREATE INDEX IF NOT EXISTS idx_local_catalog_kind ON local_document_catalog(document_kind);

ALTER TABLE kg_reference_candidates
    ADD COLUMN IF NOT EXISTS resolved_target_catalog_id BIGINT REFERENCES local_document_catalog(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS local_resolution_status TEXT,
    ADD COLUMN IF NOT EXISTS local_resolution_method TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS local_resolution_evidence TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS local_resolution_candidate_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS local_validation_status TEXT,
    ADD COLUMN IF NOT EXISTS local_ingestion_eligibility TEXT,
    ADD COLUMN IF NOT EXISTS local_resolved_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_candidate_local_resolution_status') THEN
        ALTER TABLE kg_reference_candidates ADD CONSTRAINT ck_kg_candidate_local_resolution_status
            CHECK (local_resolution_status IS NULL OR local_resolution_status IN (
                'RESOLVED_LOCAL_EXACT','RESOLVED_LOCAL_STRONG','AMBIGUOUS_LOCAL',
                'NOT_FOUND_LOCAL','SOURCE_UNPARSEABLE','BLOCKED_QUALITY'
            ));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_candidate_local_validation_status') THEN
        ALTER TABLE kg_reference_candidates ADD CONSTRAINT ck_kg_candidate_local_validation_status
            CHECK (local_validation_status IS NULL OR local_validation_status IN ('AUTO_VERIFIED','UNRESOLVED','REJECTED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_candidate_local_ingestion_eligibility') THEN
        ALTER TABLE kg_reference_candidates ADD CONSTRAINT ck_kg_candidate_local_ingestion_eligibility
            CHECK (local_ingestion_eligibility IS NULL OR local_ingestion_eligibility IN ('READY_FOR_INGESTION','NOT_READY','ALREADY_INGESTED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_candidate_local_target_consistency') THEN
        ALTER TABLE kg_reference_candidates ADD CONSTRAINT ck_kg_candidate_local_target_consistency
            CHECK (
                (local_resolution_status IN ('RESOLVED_LOCAL_EXACT','RESOLVED_LOCAL_STRONG') AND resolved_target_catalog_id IS NOT NULL)
                OR (coalesce(local_resolution_status, '') NOT IN ('RESOLVED_LOCAL_EXACT','RESOLVED_LOCAL_STRONG') AND resolved_target_catalog_id IS NULL)
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_candidates_local_target ON kg_reference_candidates(resolved_target_catalog_id)
    WHERE resolved_target_catalog_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_candidates_local_resolution ON kg_reference_candidates(local_resolution_status, local_validation_status);

COMMENT ON TABLE local_document_catalog IS
    'Lightweight identity catalogue for local PDFs. Rows are not search documents and have no chunks or embeddings.';
COMMENT ON COLUMN kg_reference_candidates.resolved_target_catalog_id IS
    'Machine-resolved local PDF target; does not imply that the target is ingested or that a graph edge exists.';

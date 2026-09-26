-- External ingestion V1: distinguish approved source roots and retain the
-- acquisition-catalogue lineage without duplicating the catalogue payload.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS source_origin text NOT NULL DEFAULT 'LOCAL_ORIGINAL',
    ADD COLUMN IF NOT EXISTS document_kind text NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS canonical_identity text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS document_year integer;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_documents_source_origin') THEN
        ALTER TABLE documents ADD CONSTRAINT ck_documents_source_origin
            CHECK (source_origin IN ('LOCAL_ORIGINAL','EXTERNAL_OFFICIAL','APPROVED_SUBMISSION'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_documents_document_year') THEN
        ALTER TABLE documents ADD CONSTRAINT ck_documents_document_year
            CHECK (document_year IS NULL OR document_year BETWEEN 1800 AND 2200);
    END IF;
END $$;

UPDATE documents
   SET document_kind = CASE
       WHEN upper(coalesce(document_type,'')) IN (
           'GOVERNMENT_RESOLUTION','CIRCULAR','GOVERNMENT_CIRCULAR','NOTIFICATION','ACT','RULE',
           'GOVERNMENT_LETTER','DEPARTMENT_LETTER','GOVERNMENT_ORDER','COURT_ORDER',
           'SCHEME_POLICY_DOCUMENT','MEETING_MINUTES','CORRIGENDUM','SUPPLEMENT'
       ) THEN upper(document_type)
       ELSE 'UNKNOWN'
   END
 WHERE document_kind = 'UNKNOWN';

ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_file_path_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_origin_path
    ON documents(source_origin,file_path);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_external_canonical_identity
    ON documents(document_kind,canonical_identity)
    WHERE source_origin='EXTERNAL_OFFICIAL' AND canonical_identity<>'';

CREATE TABLE IF NOT EXISTS external_document_provenance (
    document_id bigint PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    external_candidate_id bigint NOT NULL UNIQUE
        REFERENCES external_acquisition_candidates(id) ON DELETE RESTRICT,
    original_acquisition_path text NOT NULL,
    acquired_sha256 char(64) NOT NULL,
    linked_at timestamptz NOT NULL DEFAULT now(),
    CHECK (original_acquisition_path<>''),
    CHECK (length(acquired_sha256)=64)
);

CREATE TABLE IF NOT EXISTS external_acquisition_lifecycle_events (
    id bigserial PRIMARY KEY,
    external_candidate_id bigint NOT NULL
        REFERENCES external_acquisition_candidates(id) ON DELETE RESTRICT,
    lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
        'DISCOVERED','DOWNLOADED','AUTO_VERIFIED','READY_FOR_INGESTION','INGESTED'
    )),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    evidence text NOT NULL DEFAULT '',
    UNIQUE(external_candidate_id,lifecycle_status)
);

CREATE INDEX IF NOT EXISTS idx_external_provenance_candidate
    ON external_document_provenance(external_candidate_id);
CREATE INDEX IF NOT EXISTS idx_external_lifecycle_candidate
    ON external_acquisition_lifecycle_events(external_candidate_id,recorded_at);

COMMENT ON COLUMN documents.source_origin IS
    'Logical approved source class; never inferred solely from file_path.';
COMMENT ON TABLE external_document_provenance IS
    'One-to-one lineage from an ingested external document to its immutable acquisition catalogue row.';
COMMENT ON TABLE external_acquisition_lifecycle_events IS
    'Append-only lifecycle milestones retained when the catalogue current status advances.';

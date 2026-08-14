-- External Acquisition V2: persistent immutable archive provenance.

ALTER TABLE external_acquisition_candidates
    ADD COLUMN IF NOT EXISTS archive_source_root text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS archive_relative_path text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS safe_local_filename text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS original_remote_filename text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS archive_status text NOT NULL DEFAULT 'NOT_ARCHIVED',
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;

ALTER TABLE external_acquisition_candidates
    DROP CONSTRAINT IF EXISTS external_acquisition_candidates_identity_match_status_check;
ALTER TABLE external_acquisition_candidates
    DROP CONSTRAINT IF EXISTS ck_external_candidate_identity_match_status;
ALTER TABLE external_acquisition_candidates
    ADD CONSTRAINT ck_external_candidate_identity_match_status CHECK (
        identity_match_status IN (
            'NOT_EVALUATED','MATCH_EXACT','MATCH_STRONG','MATCH_EXACT_VERSION',
            'VERSION_VARIANT','AMBIGUOUS_VERSION','AMBIGUOUS','MISMATCH'
        )
    );

ALTER TABLE external_acquisition_candidates
    DROP CONSTRAINT IF EXISTS ck_external_candidate_archive_status;
ALTER TABLE external_acquisition_candidates
    ADD CONSTRAINT ck_external_candidate_archive_status CHECK (
        archive_status IN ('NOT_ARCHIVED','EXTERNAL_OFFICIAL_ARCHIVED','ARCHIVE_FAILED')
    );

ALTER TABLE external_acquisition_candidates
    DROP CONSTRAINT IF EXISTS ck_external_candidate_archive_complete;
ALTER TABLE external_acquisition_candidates
    ADD CONSTRAINT ck_external_candidate_archive_complete CHECK (
        archive_status <> 'EXTERNAL_OFFICIAL_ARCHIVED' OR (
            archive_source_root = 'EXTERNAL_OFFICIAL'
            AND archive_relative_path <> ''
            AND safe_local_filename <> ''
            AND sha256 <> ''
            AND file_size IS NOT NULL
            AND archived_at IS NOT NULL
            AND verification_status = 'AUTO_VERIFIED'
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_candidate_archive_path
    ON external_acquisition_candidates(archive_source_root, archive_relative_path)
    WHERE archive_status='EXTERNAL_OFFICIAL_ARCHIVED';
CREATE INDEX IF NOT EXISTS idx_external_candidate_archive_sha
    ON external_acquisition_candidates(sha256)
    WHERE archive_status='EXTERNAL_OFFICIAL_ARCHIVED';

ALTER TABLE external_document_provenance
    ADD COLUMN IF NOT EXISTS archive_source_root text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS archive_relative_path text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS archived_sha256 char(64),
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;

ALTER TABLE external_document_provenance
    DROP CONSTRAINT IF EXISTS ck_external_document_archive_complete;
ALTER TABLE external_document_provenance
    ADD CONSTRAINT ck_external_document_archive_complete CHECK (
        archive_relative_path = '' OR (
            archive_source_root = 'EXTERNAL_OFFICIAL'
            AND archived_sha256 IS NOT NULL
            AND length(archived_sha256) = 64
            AND archived_at IS NOT NULL
        )
    );

ALTER TABLE external_acquisition_lifecycle_events
    DROP CONSTRAINT IF EXISTS external_acquisition_lifecycle_events_lifecycle_status_check;
ALTER TABLE external_acquisition_lifecycle_events
    DROP CONSTRAINT IF EXISTS ck_external_lifecycle_status;
ALTER TABLE external_acquisition_lifecycle_events
    ADD CONSTRAINT ck_external_lifecycle_status CHECK (
        lifecycle_status IN (
            'DISCOVERED','DOWNLOADED','AUTO_VERIFIED','EXTERNAL_OFFICIAL_ARCHIVED',
            'READY_FOR_INGESTION','INGESTED'
        )
    );

COMMENT ON COLUMN external_acquisition_candidates.local_acquired_path IS
    'Controlled quarantine/evidence path; never the authoritative permanent source after V2 archival.';
COMMENT ON COLUMN external_acquisition_candidates.archive_relative_path IS
    'Permanent immutable PDF path relative to the configured EXTERNAL_OFFICIAL source root.';
COMMENT ON COLUMN external_acquisition_candidates.original_remote_filename IS
    'Untrusted server path basename retained as provenance only; never used as the archive filename.';
COMMENT ON COLUMN external_document_provenance.archive_relative_path IS
    'Permanent source path for an ingested external document, relative to EXTERNAL_DOCUMENT_ROOT.';

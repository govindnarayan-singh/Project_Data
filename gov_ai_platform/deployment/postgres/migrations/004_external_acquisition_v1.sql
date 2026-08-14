CREATE TABLE IF NOT EXISTS external_acquisition_runs (
    run_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('RUNNING','SUCCESS','FAILED')),
    targets_selected integer NOT NULL DEFAULT 0 CHECK (targets_selected >= 0),
    candidates_discovered integer NOT NULL DEFAULT 0 CHECK (candidates_discovered >= 0),
    notes text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS external_acquisition_candidates (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES external_acquisition_runs(run_id) ON DELETE RESTRICT,
    target_key text NOT NULL,
    reference_kind text NOT NULL,
    raw_reference_identifier text NOT NULL DEFAULT '',
    normalized_identifier text NOT NULL,
    reference_date date,
    issuer text NOT NULL DEFAULT '',
    discovery_query text NOT NULL DEFAULT '',
    discovery_status text NOT NULL CHECK (discovery_status IN
        ('PENDING_DISCOVERY','DISCOVERED','AMBIGUOUS','NOT_FOUND','BLOCKED','NETWORK_FAILED')),
    source_url text NOT NULL DEFAULT '',
    final_url text NOT NULL DEFAULT '',
    source_domain text NOT NULL,
    source_authority text NOT NULL CHECK (source_authority IN
        ('OFFICIAL_PRIMARY','OFFICIAL_SECONDARY','TRUSTED_GOVERNMENT_INDEX','NON_AUTHORITATIVE')),
    download_url text NOT NULL DEFAULT '',
    discovered_title text NOT NULL DEFAULT '',
    discovered_identifier text NOT NULL DEFAULT '',
    discovered_date date,
    identity_match_status text NOT NULL CHECK (identity_match_status IN
        ('NOT_EVALUATED','MATCH_EXACT','MATCH_STRONG','AMBIGUOUS','MISMATCH')),
    identity_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    sha256 text NOT NULL DEFAULT '' CHECK (sha256='' OR length(sha256)=64),
    acquisition_status text NOT NULL CHECK (acquisition_status IN
        ('PENDING_DISCOVERY','DISCOVERED','AMBIGUOUS','NOT_FOUND','DOWNLOAD_FAILED','DOWNLOADED',
         'AUTO_VERIFIED','READY_FOR_INGESTION','INGESTED','BLOCKED')),
    verification_status text NOT NULL CHECK (verification_status IN
        ('UNVERIFIED','AUTO_VERIFIED','BLOCKED','REJECTED')),
    local_acquired_path text NOT NULL DEFAULT '',
    content_type text NOT NULL DEFAULT '',
    file_size bigint CHECK (file_size IS NULL OR file_size >= 0),
    source_adapter text NOT NULL,
    raw_source_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority_tier text NOT NULL DEFAULT '',
    priority_score integer NOT NULL DEFAULT 0,
    discovered_at timestamptz,
    downloaded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_candidate_identity
    ON external_acquisition_candidates(target_key,source_adapter,source_url,discovered_identifier);
CREATE INDEX IF NOT EXISTS idx_external_candidate_target
    ON external_acquisition_candidates(target_key,verification_status,acquisition_status);
CREATE INDEX IF NOT EXISTS idx_external_candidate_status
    ON external_acquisition_candidates(discovery_status,identity_match_status);

COMMENT ON TABLE external_acquisition_candidates IS
    'Non-search external discovery/acquisition catalogue; rows never imply a documents record.';

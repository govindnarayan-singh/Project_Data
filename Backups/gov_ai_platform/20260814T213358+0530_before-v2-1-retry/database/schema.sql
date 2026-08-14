--
-- PostgreSQL database dump
--

\restrict AVrJIF3CkG8SFbOErIUsuBH8pes2DPVnZmfe6DDVvOwKegd2Jb2KzKxAHlNqXPZ

-- Dumped from database version 17.10 (Debian 17.10-1.pgdg12+1)
-- Dumped by pg_dump version 17.10 (Debian 17.10-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunks (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    chunk_index integer NOT NULL,
    page_start integer NOT NULL,
    page_end integer NOT NULL,
    token_count integer NOT NULL,
    text text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunks_id_seq OWNED BY public.chunks.id;


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id bigint NOT NULL,
    sha256 character(64) NOT NULL,
    file_path text NOT NULL,
    file_name text NOT NULL,
    department text NOT NULL,
    page_count integer NOT NULL,
    digital_pages integer DEFAULT 0 NOT NULL,
    ocr_pages integer DEFAULT 0 NOT NULL,
    extraction_method text NOT NULL,
    document_type text,
    gr_number text,
    gr_date date,
    subject text,
    references_text text,
    raw_text text NOT NULL,
    normalized_text text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_origin text DEFAULT 'LOCAL_ORIGINAL'::text NOT NULL,
    document_kind text DEFAULT 'UNKNOWN'::text NOT NULL,
    canonical_identity text DEFAULT ''::text NOT NULL,
    document_year integer,
    CONSTRAINT ck_documents_document_year CHECK (((document_year IS NULL) OR ((document_year >= 1800) AND (document_year <= 2200)))),
    CONSTRAINT ck_documents_source_origin CHECK ((source_origin = ANY (ARRAY['LOCAL_ORIGINAL'::text, 'EXTERNAL_OFFICIAL'::text])))
);


--
-- Name: COLUMN documents.source_origin; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.documents.source_origin IS 'Logical approved source class; never inferred solely from file_path.';


--
-- Name: documents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;


--
-- Name: external_acquisition_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_acquisition_candidates (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    target_key text NOT NULL,
    reference_kind text NOT NULL,
    raw_reference_identifier text DEFAULT ''::text NOT NULL,
    normalized_identifier text NOT NULL,
    reference_date date,
    issuer text DEFAULT ''::text NOT NULL,
    discovery_query text DEFAULT ''::text NOT NULL,
    discovery_status text NOT NULL,
    source_url text DEFAULT ''::text NOT NULL,
    final_url text DEFAULT ''::text NOT NULL,
    source_domain text NOT NULL,
    source_authority text NOT NULL,
    download_url text DEFAULT ''::text NOT NULL,
    discovered_title text DEFAULT ''::text NOT NULL,
    discovered_identifier text DEFAULT ''::text NOT NULL,
    discovered_date date,
    identity_match_status text NOT NULL,
    identity_evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    sha256 text DEFAULT ''::text NOT NULL,
    acquisition_status text NOT NULL,
    verification_status text NOT NULL,
    local_acquired_path text DEFAULT ''::text NOT NULL,
    content_type text DEFAULT ''::text NOT NULL,
    file_size bigint,
    source_adapter text NOT NULL,
    raw_source_evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    priority_tier text DEFAULT ''::text NOT NULL,
    priority_score integer DEFAULT 0 NOT NULL,
    discovered_at timestamp with time zone,
    downloaded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    archive_source_root text DEFAULT ''::text NOT NULL,
    archive_relative_path text DEFAULT ''::text NOT NULL,
    safe_local_filename text DEFAULT ''::text NOT NULL,
    original_remote_filename text DEFAULT ''::text NOT NULL,
    archive_status text DEFAULT 'NOT_ARCHIVED'::text NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT ck_external_candidate_archive_complete CHECK (((archive_status <> 'EXTERNAL_OFFICIAL_ARCHIVED'::text) OR ((archive_source_root = 'EXTERNAL_OFFICIAL'::text) AND (archive_relative_path <> ''::text) AND (safe_local_filename <> ''::text) AND (sha256 <> ''::text) AND (file_size IS NOT NULL) AND (archived_at IS NOT NULL) AND (verification_status = 'AUTO_VERIFIED'::text)))),
    CONSTRAINT ck_external_candidate_archive_status CHECK ((archive_status = ANY (ARRAY['NOT_ARCHIVED'::text, 'EXTERNAL_OFFICIAL_ARCHIVED'::text, 'ARCHIVE_FAILED'::text]))),
    CONSTRAINT ck_external_candidate_identity_match_status CHECK ((identity_match_status = ANY (ARRAY['NOT_EVALUATED'::text, 'MATCH_EXACT'::text, 'MATCH_STRONG'::text, 'MATCH_EXACT_VERSION'::text, 'VERSION_VARIANT'::text, 'AMBIGUOUS_VERSION'::text, 'AMBIGUOUS'::text, 'MISMATCH'::text]))),
    CONSTRAINT external_acquisition_candidates_acquisition_status_check CHECK ((acquisition_status = ANY (ARRAY['PENDING_DISCOVERY'::text, 'DISCOVERED'::text, 'AMBIGUOUS'::text, 'NOT_FOUND'::text, 'DOWNLOAD_FAILED'::text, 'DOWNLOADED'::text, 'AUTO_VERIFIED'::text, 'READY_FOR_INGESTION'::text, 'INGESTED'::text, 'BLOCKED'::text]))),
    CONSTRAINT external_acquisition_candidates_discovery_status_check CHECK ((discovery_status = ANY (ARRAY['PENDING_DISCOVERY'::text, 'DISCOVERED'::text, 'AMBIGUOUS'::text, 'NOT_FOUND'::text, 'BLOCKED'::text, 'NETWORK_FAILED'::text]))),
    CONSTRAINT external_acquisition_candidates_file_size_check CHECK (((file_size IS NULL) OR (file_size >= 0))),
    CONSTRAINT external_acquisition_candidates_sha256_check CHECK (((sha256 = ''::text) OR (length(sha256) = 64))),
    CONSTRAINT external_acquisition_candidates_source_authority_check CHECK ((source_authority = ANY (ARRAY['OFFICIAL_PRIMARY'::text, 'OFFICIAL_SECONDARY'::text, 'TRUSTED_GOVERNMENT_INDEX'::text, 'NON_AUTHORITATIVE'::text]))),
    CONSTRAINT external_acquisition_candidates_verification_status_check CHECK ((verification_status = ANY (ARRAY['UNVERIFIED'::text, 'AUTO_VERIFIED'::text, 'BLOCKED'::text, 'REJECTED'::text])))
);


--
-- Name: TABLE external_acquisition_candidates; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.external_acquisition_candidates IS 'Non-search external discovery/acquisition catalogue; rows never imply a documents record.';


--
-- Name: COLUMN external_acquisition_candidates.local_acquired_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.external_acquisition_candidates.local_acquired_path IS 'Controlled quarantine/evidence path; never the authoritative permanent source after V2 archival.';


--
-- Name: COLUMN external_acquisition_candidates.archive_relative_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.external_acquisition_candidates.archive_relative_path IS 'Permanent immutable PDF path relative to the configured EXTERNAL_OFFICIAL source root.';


--
-- Name: COLUMN external_acquisition_candidates.original_remote_filename; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.external_acquisition_candidates.original_remote_filename IS 'Untrusted server path basename retained as provenance only; never used as the archive filename.';


--
-- Name: external_acquisition_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.external_acquisition_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: external_acquisition_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.external_acquisition_candidates_id_seq OWNED BY public.external_acquisition_candidates.id;


--
-- Name: external_acquisition_lifecycle_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_acquisition_lifecycle_events (
    id bigint NOT NULL,
    external_candidate_id bigint NOT NULL,
    lifecycle_status text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    evidence text DEFAULT ''::text NOT NULL,
    CONSTRAINT ck_external_lifecycle_status CHECK ((lifecycle_status = ANY (ARRAY['DISCOVERED'::text, 'DOWNLOADED'::text, 'AUTO_VERIFIED'::text, 'EXTERNAL_OFFICIAL_ARCHIVED'::text, 'READY_FOR_INGESTION'::text, 'INGESTED'::text])))
);


--
-- Name: TABLE external_acquisition_lifecycle_events; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.external_acquisition_lifecycle_events IS 'Append-only lifecycle milestones retained when the catalogue current status advances.';


--
-- Name: external_acquisition_lifecycle_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.external_acquisition_lifecycle_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: external_acquisition_lifecycle_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.external_acquisition_lifecycle_events_id_seq OWNED BY public.external_acquisition_lifecycle_events.id;


--
-- Name: external_acquisition_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_acquisition_runs (
    run_id uuid NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    targets_selected integer DEFAULT 0 NOT NULL,
    candidates_discovered integer DEFAULT 0 NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    CONSTRAINT external_acquisition_runs_candidates_discovered_check CHECK ((candidates_discovered >= 0)),
    CONSTRAINT external_acquisition_runs_status_check CHECK ((status = ANY (ARRAY['RUNNING'::text, 'SUCCESS'::text, 'FAILED'::text]))),
    CONSTRAINT external_acquisition_runs_targets_selected_check CHECK ((targets_selected >= 0))
);


--
-- Name: external_document_provenance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_document_provenance (
    document_id bigint NOT NULL,
    external_candidate_id bigint NOT NULL,
    original_acquisition_path text NOT NULL,
    acquired_sha256 character(64) NOT NULL,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    archive_source_root text DEFAULT ''::text NOT NULL,
    archive_relative_path text DEFAULT ''::text NOT NULL,
    archived_sha256 character(64),
    archived_at timestamp with time zone,
    CONSTRAINT ck_external_document_archive_complete CHECK (((archive_relative_path = ''::text) OR ((archive_source_root = 'EXTERNAL_OFFICIAL'::text) AND (archived_sha256 IS NOT NULL) AND (length(archived_sha256) = 64) AND (archived_at IS NOT NULL)))),
    CONSTRAINT external_document_provenance_acquired_sha256_check CHECK ((length(acquired_sha256) = 64)),
    CONSTRAINT external_document_provenance_original_acquisition_path_check CHECK ((original_acquisition_path <> ''::text))
);


--
-- Name: TABLE external_document_provenance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.external_document_provenance IS 'One-to-one lineage from an ingested external document to its immutable acquisition catalogue row.';


--
-- Name: COLUMN external_document_provenance.archive_relative_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.external_document_provenance.archive_relative_path IS 'Permanent source path for an ingested external document, relative to EXTERNAL_DOCUMENT_ROOT.';


--
-- Name: gr_relations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gr_relations (
    id bigint NOT NULL,
    from_document_id bigint NOT NULL,
    to_document_id bigint,
    relation_type text NOT NULL,
    target_gr_number text,
    evidence_page integer,
    evidence_text text,
    confidence real
);


--
-- Name: TABLE gr_relations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.gr_relations IS 'Deprecated zero-row placeholder retained for compatibility; KG V1 uses kg_reference_candidates and kg_relations.';


--
-- Name: gr_relations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gr_relations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gr_relations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gr_relations_id_seq OWNED BY public.gr_relations.id;


--
-- Name: kg_department_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_department_aliases (
    id bigint NOT NULL,
    department_id bigint NOT NULL,
    raw_alias text NOT NULL,
    normalized_alias text NOT NULL,
    source text DEFAULT 'CORPUS_METADATA'::text NOT NULL,
    validation_status text DEFAULT 'AUTO_VERIFIED'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT kg_department_aliases_validation_status_check CHECK ((validation_status = ANY (ARRAY['CANDIDATE'::text, 'AUTO_VERIFIED'::text, 'HUMAN_CONFIRMED'::text, 'REJECTED'::text, 'UNRESOLVED'::text])))
);


--
-- Name: kg_department_aliases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_department_aliases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_department_aliases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_department_aliases_id_seq OWNED BY public.kg_department_aliases.id;


--
-- Name: kg_departments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_departments (
    id bigint NOT NULL,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kg_departments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_departments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_departments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_departments_id_seq OWNED BY public.kg_departments.id;


--
-- Name: kg_extraction_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_extraction_runs (
    run_id uuid NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    documents_processed integer DEFAULT 0 NOT NULL,
    pages_processed integer DEFAULT 0 NOT NULL,
    candidate_count integer DEFAULT 0 NOT NULL,
    resolved_count integer DEFAULT 0 NOT NULL,
    edge_count integer DEFAULT 0 NOT NULL,
    parser_version text NOT NULL,
    normalizer_version text NOT NULL,
    resolver_version text NOT NULL,
    status text NOT NULL,
    notes text,
    CONSTRAINT kg_extraction_runs_status_check CHECK ((status = ANY (ARRAY['RUNNING'::text, 'SUCCESS'::text, 'FAILED'::text])))
);


--
-- Name: kg_reference_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_reference_candidates (
    id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    source_page integer NOT NULL,
    reference_kind text NOT NULL,
    raw_evidence_text text NOT NULL,
    raw_document_number text DEFAULT ''::text NOT NULL,
    normalized_document_number text DEFAULT ''::text NOT NULL,
    raw_reference_date text DEFAULT ''::text NOT NULL,
    normalized_reference_date date,
    date_parse_status text NOT NULL,
    raw_department text DEFAULT ''::text NOT NULL,
    normalized_department text DEFAULT ''::text NOT NULL,
    pattern_id text NOT NULL,
    parser_version text NOT NULL,
    parser_status text NOT NULL,
    parser_notes text DEFAULT ''::text NOT NULL,
    relation_signal text DEFAULT ''::text NOT NULL,
    candidate_relation_type text DEFAULT ''::text NOT NULL,
    resolution_status text NOT NULL,
    resolved_target_document_id bigint,
    resolution_method text NOT NULL,
    resolution_evidence text NOT NULL,
    resolution_candidate_count integer DEFAULT 0 NOT NULL,
    confidence_category text NOT NULL,
    validation_status text NOT NULL,
    blocked_by_page_quality boolean DEFAULT false NOT NULL,
    blocked_by_target_metadata boolean DEFAULT false NOT NULL,
    reviewer text,
    review_note text,
    reviewed_at timestamp with time zone,
    evidence_hash character(64) NOT NULL,
    fingerprint character(64) NOT NULL,
    first_run_id uuid NOT NULL,
    last_run_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_target_catalog_id bigint,
    local_resolution_status text,
    local_resolution_method text DEFAULT ''::text NOT NULL,
    local_resolution_evidence text DEFAULT ''::text NOT NULL,
    local_resolution_candidate_count integer DEFAULT 0 NOT NULL,
    local_validation_status text,
    local_ingestion_eligibility text,
    local_resolved_at timestamp with time zone,
    CONSTRAINT ck_kg_candidate_local_ingestion_eligibility CHECK (((local_ingestion_eligibility IS NULL) OR (local_ingestion_eligibility = ANY (ARRAY['READY_FOR_INGESTION'::text, 'NOT_READY'::text, 'ALREADY_INGESTED'::text])))),
    CONSTRAINT ck_kg_candidate_local_resolution_status CHECK (((local_resolution_status IS NULL) OR (local_resolution_status = ANY (ARRAY['RESOLVED_LOCAL_EXACT'::text, 'RESOLVED_LOCAL_STRONG'::text, 'AMBIGUOUS_LOCAL'::text, 'NOT_FOUND_LOCAL'::text, 'SOURCE_UNPARSEABLE'::text, 'BLOCKED_QUALITY'::text])))),
    CONSTRAINT ck_kg_candidate_local_target_consistency CHECK ((((local_resolution_status = ANY (ARRAY['RESOLVED_LOCAL_EXACT'::text, 'RESOLVED_LOCAL_STRONG'::text])) AND (resolved_target_catalog_id IS NOT NULL)) OR ((COALESCE(local_resolution_status, ''::text) <> ALL (ARRAY['RESOLVED_LOCAL_EXACT'::text, 'RESOLVED_LOCAL_STRONG'::text])) AND (resolved_target_catalog_id IS NULL)))),
    CONSTRAINT ck_kg_candidate_local_validation_status CHECK (((local_validation_status IS NULL) OR (local_validation_status = ANY (ARRAY['AUTO_VERIFIED'::text, 'UNRESOLVED'::text, 'REJECTED'::text])))),
    CONSTRAINT kg_reference_candidates_candidate_relation_type_check CHECK ((candidate_relation_type = ANY (ARRAY[''::text, 'AMENDS'::text, 'SUPERSEDES'::text, 'PARTIALLY_SUPERSEDES'::text, 'CANCELS'::text, 'CORRECTS'::text, 'EXTENDS'::text]))),
    CONSTRAINT kg_reference_candidates_check CHECK ((((resolution_status = ANY (ARRAY['RESOLVED_EXACT'::text, 'RESOLVED_STRONG'::text])) AND (resolved_target_document_id IS NOT NULL)) OR ((resolution_status <> ALL (ARRAY['RESOLVED_EXACT'::text, 'RESOLVED_STRONG'::text])) AND (resolved_target_document_id IS NULL)))),
    CONSTRAINT kg_reference_candidates_confidence_category_check CHECK ((confidence_category = ANY (ARRAY['HIGH'::text, 'MEDIUM'::text, 'LOW'::text]))),
    CONSTRAINT kg_reference_candidates_parser_status_check CHECK ((parser_status = ANY (ARRAY['VALID'::text, 'PARTIAL'::text, 'NEEDS_REVIEW'::text, 'SELF_REFERENCE_REJECTED'::text]))),
    CONSTRAINT kg_reference_candidates_raw_evidence_text_check CHECK ((length(raw_evidence_text) > 0)),
    CONSTRAINT kg_reference_candidates_reference_kind_check CHECK ((reference_kind = ANY (ARRAY['GOVERNMENT_RESOLUTION'::text, 'CIRCULAR'::text, 'GOVERNMENT_CIRCULAR'::text, 'NOTIFICATION'::text, 'ACT'::text, 'RULE'::text, 'GOVERNMENT_LETTER'::text, 'DEPARTMENT_LETTER'::text, 'GOVERNMENT_ORDER'::text, 'COURT_ORDER'::text, 'SCHEME_POLICY_DOCUMENT'::text, 'MEETING_MINUTES'::text, 'CORRIGENDUM'::text, 'SUPPLEMENT'::text, 'UNKNOWN'::text]))),
    CONSTRAINT kg_reference_candidates_resolution_candidate_count_check CHECK ((resolution_candidate_count >= 0)),
    CONSTRAINT kg_reference_candidates_resolution_status_check CHECK ((resolution_status = ANY (ARRAY['RESOLVED_EXACT'::text, 'RESOLVED_STRONG'::text, 'AMBIGUOUS'::text, 'NOT_IN_CORPUS'::text, 'UNPARSEABLE'::text, 'SELF_REFERENCE_REJECTED'::text, 'NEEDS_REVIEW'::text]))),
    CONSTRAINT kg_reference_candidates_source_page_check CHECK ((source_page > 0)),
    CONSTRAINT kg_reference_candidates_validation_status_check CHECK ((validation_status = ANY (ARRAY['CANDIDATE'::text, 'AUTO_VERIFIED'::text, 'HUMAN_CONFIRMED'::text, 'REJECTED'::text, 'UNRESOLVED'::text])))
);


--
-- Name: TABLE kg_reference_candidates; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.kg_reference_candidates IS 'Provenance-first unresolved/resolved reference candidates; raw evidence is never replaced by normalized values.';


--
-- Name: COLUMN kg_reference_candidates.resolved_target_catalog_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kg_reference_candidates.resolved_target_catalog_id IS 'Machine-resolved local PDF target; does not imply that the target is ingested or that a graph edge exists.';


--
-- Name: kg_reference_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_reference_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_reference_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_reference_candidates_id_seq OWNED BY public.kg_reference_candidates.id;


--
-- Name: kg_relation_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_relation_evidence (
    id bigint NOT NULL,
    relation_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kg_relation_evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_relation_evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_relation_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_relation_evidence_id_seq OWNED BY public.kg_relation_evidence.id;


--
-- Name: kg_relations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_relations (
    id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    target_document_id bigint,
    target_department_id bigint,
    relation_type text NOT NULL,
    validation_status text NOT NULL,
    created_from_candidate_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT kg_relations_check CHECK ((((relation_type = 'ISSUED_BY'::text) AND (target_department_id IS NOT NULL) AND (target_document_id IS NULL)) OR ((relation_type <> 'ISSUED_BY'::text) AND (target_document_id IS NOT NULL) AND (target_department_id IS NULL)))),
    CONSTRAINT kg_relations_relation_type_check CHECK ((relation_type = ANY (ARRAY['REFERENCES'::text, 'ISSUED_BY'::text, 'AMENDS'::text, 'SUPERSEDES'::text, 'PARTIALLY_SUPERSEDES'::text, 'CANCELS'::text, 'CORRECTS'::text, 'EXTENDS'::text]))),
    CONSTRAINT kg_relations_strong_validation_check CHECK (((relation_type = ANY (ARRAY['REFERENCES'::text, 'ISSUED_BY'::text])) OR (validation_status = 'HUMAN_CONFIRMED'::text) OR ((validation_status = 'AUTO_VERIFIED'::text) AND (created_from_candidate_id IS NOT NULL)))),
    CONSTRAINT kg_relations_validation_status_check CHECK ((validation_status = ANY (ARRAY['AUTO_VERIFIED'::text, 'HUMAN_CONFIRMED'::text])))
);


--
-- Name: TABLE kg_relations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.kg_relations IS 'Validated edges. AUTO_VERIFIED means deterministic machine validation; HUMAN_CONFIRMED requires an actual human review event.';


--
-- Name: kg_relations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_relations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_relations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_relations_id_seq OWNED BY public.kg_relations.id;


--
-- Name: kg_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kg_reviews (
    id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    previous_status text NOT NULL,
    decision text NOT NULL,
    reviewer text NOT NULL,
    review_note text DEFAULT ''::text NOT NULL,
    reviewed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT kg_reviews_decision_check CHECK ((decision = ANY (ARRAY['HUMAN_CONFIRMED'::text, 'REJECTED'::text, 'CANDIDATE'::text, 'UNRESOLVED'::text])))
);


--
-- Name: kg_reviews_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kg_reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: kg_reviews_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kg_reviews_id_seq OWNED BY public.kg_reviews.id;


--
-- Name: local_document_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.local_document_catalog (
    id bigint NOT NULL,
    relative_path text NOT NULL,
    filename text NOT NULL,
    department_folder text NOT NULL,
    normalized_department text DEFAULT ''::text NOT NULL,
    sha256 character(64) NOT NULL,
    file_size bigint NOT NULL,
    file_mtime_ns bigint DEFAULT 0 NOT NULL,
    page_count integer,
    filename_date date,
    document_kind text DEFAULT 'UNKNOWN'::text NOT NULL,
    raw_document_number text DEFAULT ''::text NOT NULL,
    normalized_document_number text DEFAULT ''::text NOT NULL,
    raw_date text DEFAULT ''::text NOT NULL,
    normalized_date date,
    raw_department text DEFAULT ''::text NOT NULL,
    raw_subject text DEFAULT ''::text NOT NULL,
    subject_preview text DEFAULT ''::text NOT NULL,
    legal_canonical_key text DEFAULT ''::text NOT NULL,
    identity_source_page integer,
    identity_source_method text NOT NULL,
    identity_parse_status text NOT NULL,
    identity_quality text NOT NULL,
    digital_quality text DEFAULT ''::text NOT NULL,
    probe_pages integer[] DEFAULT '{}'::integer[] NOT NULL,
    ocr_used boolean DEFAULT false NOT NULL,
    ocr_pages integer[] DEFAULT '{}'::integer[] NOT NULL,
    extraction_error text DEFAULT ''::text NOT NULL,
    ingested_document_id bigint,
    catalog_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT local_document_catalog_document_kind_check CHECK ((document_kind = ANY (ARRAY['GOVERNMENT_RESOLUTION'::text, 'CIRCULAR'::text, 'GOVERNMENT_CIRCULAR'::text, 'NOTIFICATION'::text, 'ACT'::text, 'RULE'::text, 'GOVERNMENT_LETTER'::text, 'DEPARTMENT_LETTER'::text, 'GOVERNMENT_ORDER'::text, 'COURT_ORDER'::text, 'SCHEME_POLICY_DOCUMENT'::text, 'MEETING_MINUTES'::text, 'CORRIGENDUM'::text, 'SUPPLEMENT'::text, 'UNKNOWN'::text]))),
    CONSTRAINT local_document_catalog_file_size_check CHECK ((file_size >= 0)),
    CONSTRAINT local_document_catalog_identity_parse_status_check CHECK ((identity_parse_status = ANY (ARRAY['IDENTIFIED_EXACT'::text, 'IDENTIFIED_PARTIAL'::text, 'NO_IDENTIFIER'::text, 'AMBIGUOUS_METADATA'::text, 'EXTRACTION_FAILED'::text, 'NEEDS_OCR'::text, 'QUALITY_BLOCKED'::text]))),
    CONSTRAINT local_document_catalog_identity_quality_check CHECK ((identity_quality = ANY (ARRAY['HIGH'::text, 'MEDIUM'::text, 'LOW'::text, 'BLOCKED'::text]))),
    CONSTRAINT local_document_catalog_identity_source_method_check CHECK ((identity_source_method = ANY (ARRAY['DIGITAL'::text, 'OCR'::text, 'INGESTED_METADATA'::text, 'NONE'::text]))),
    CONSTRAINT local_document_catalog_identity_source_page_check CHECK (((identity_source_page IS NULL) OR (identity_source_page > 0))),
    CONSTRAINT local_document_catalog_page_count_check CHECK (((page_count IS NULL) OR (page_count >= 0)))
);


--
-- Name: TABLE local_document_catalog; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.local_document_catalog IS 'Lightweight identity catalogue for local PDFs. Rows are not search documents and have no chunks or embeddings.';


--
-- Name: local_document_catalog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.local_document_catalog_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: local_document_catalog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.local_document_catalog_id_seq OWNED BY public.local_document_catalog.id;


--
-- Name: pages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pages (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_number integer NOT NULL,
    extraction_method text NOT NULL,
    quality_score real,
    text text NOT NULL
);


--
-- Name: pages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pages_id_seq OWNED BY public.pages.id;


--
-- Name: chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks ALTER COLUMN id SET DEFAULT nextval('public.chunks_id_seq'::regclass);


--
-- Name: documents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);


--
-- Name: external_acquisition_candidates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_candidates ALTER COLUMN id SET DEFAULT nextval('public.external_acquisition_candidates_id_seq'::regclass);


--
-- Name: external_acquisition_lifecycle_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_lifecycle_events ALTER COLUMN id SET DEFAULT nextval('public.external_acquisition_lifecycle_events_id_seq'::regclass);


--
-- Name: gr_relations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gr_relations ALTER COLUMN id SET DEFAULT nextval('public.gr_relations_id_seq'::regclass);


--
-- Name: kg_department_aliases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_department_aliases ALTER COLUMN id SET DEFAULT nextval('public.kg_department_aliases_id_seq'::regclass);


--
-- Name: kg_departments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_departments ALTER COLUMN id SET DEFAULT nextval('public.kg_departments_id_seq'::regclass);


--
-- Name: kg_reference_candidates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates ALTER COLUMN id SET DEFAULT nextval('public.kg_reference_candidates_id_seq'::regclass);


--
-- Name: kg_relation_evidence id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relation_evidence ALTER COLUMN id SET DEFAULT nextval('public.kg_relation_evidence_id_seq'::regclass);


--
-- Name: kg_relations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations ALTER COLUMN id SET DEFAULT nextval('public.kg_relations_id_seq'::regclass);


--
-- Name: kg_reviews id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reviews ALTER COLUMN id SET DEFAULT nextval('public.kg_reviews_id_seq'::regclass);


--
-- Name: local_document_catalog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.local_document_catalog ALTER COLUMN id SET DEFAULT nextval('public.local_document_catalog_id_seq'::regclass);


--
-- Name: pages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pages ALTER COLUMN id SET DEFAULT nextval('public.pages_id_seq'::regclass);


--
-- Name: chunks chunks_document_id_chunk_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_chunk_index_key UNIQUE (document_id, chunk_index);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: documents documents_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_sha256_key UNIQUE (sha256);


--
-- Name: external_acquisition_candidates external_acquisition_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_candidates
    ADD CONSTRAINT external_acquisition_candidates_pkey PRIMARY KEY (id);


--
-- Name: external_acquisition_lifecycle_events external_acquisition_lifecycl_external_candidate_id_lifecyc_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_lifecycle_events
    ADD CONSTRAINT external_acquisition_lifecycl_external_candidate_id_lifecyc_key UNIQUE (external_candidate_id, lifecycle_status);


--
-- Name: external_acquisition_lifecycle_events external_acquisition_lifecycle_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_lifecycle_events
    ADD CONSTRAINT external_acquisition_lifecycle_events_pkey PRIMARY KEY (id);


--
-- Name: external_acquisition_runs external_acquisition_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_runs
    ADD CONSTRAINT external_acquisition_runs_pkey PRIMARY KEY (run_id);


--
-- Name: external_document_provenance external_document_provenance_external_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_document_provenance
    ADD CONSTRAINT external_document_provenance_external_candidate_id_key UNIQUE (external_candidate_id);


--
-- Name: external_document_provenance external_document_provenance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_document_provenance
    ADD CONSTRAINT external_document_provenance_pkey PRIMARY KEY (document_id);


--
-- Name: gr_relations gr_relations_from_document_id_relation_type_target_gr_numbe_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gr_relations
    ADD CONSTRAINT gr_relations_from_document_id_relation_type_target_gr_numbe_key UNIQUE (from_document_id, relation_type, target_gr_number, evidence_page);


--
-- Name: gr_relations gr_relations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gr_relations
    ADD CONSTRAINT gr_relations_pkey PRIMARY KEY (id);


--
-- Name: kg_department_aliases kg_department_aliases_normalized_alias_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_department_aliases
    ADD CONSTRAINT kg_department_aliases_normalized_alias_key UNIQUE (normalized_alias);


--
-- Name: kg_department_aliases kg_department_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_department_aliases
    ADD CONSTRAINT kg_department_aliases_pkey PRIMARY KEY (id);


--
-- Name: kg_departments kg_departments_canonical_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_departments
    ADD CONSTRAINT kg_departments_canonical_name_key UNIQUE (canonical_name);


--
-- Name: kg_departments kg_departments_normalized_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_departments
    ADD CONSTRAINT kg_departments_normalized_name_key UNIQUE (normalized_name);


--
-- Name: kg_departments kg_departments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_departments
    ADD CONSTRAINT kg_departments_pkey PRIMARY KEY (id);


--
-- Name: kg_extraction_runs kg_extraction_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_extraction_runs
    ADD CONSTRAINT kg_extraction_runs_pkey PRIMARY KEY (run_id);


--
-- Name: kg_reference_candidates kg_reference_candidates_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_fingerprint_key UNIQUE (fingerprint);


--
-- Name: kg_reference_candidates kg_reference_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_pkey PRIMARY KEY (id);


--
-- Name: kg_relation_evidence kg_relation_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relation_evidence
    ADD CONSTRAINT kg_relation_evidence_pkey PRIMARY KEY (id);


--
-- Name: kg_relation_evidence kg_relation_evidence_relation_id_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relation_evidence
    ADD CONSTRAINT kg_relation_evidence_relation_id_candidate_id_key UNIQUE (relation_id, candidate_id);


--
-- Name: kg_relations kg_relations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations
    ADD CONSTRAINT kg_relations_pkey PRIMARY KEY (id);


--
-- Name: kg_reviews kg_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reviews
    ADD CONSTRAINT kg_reviews_pkey PRIMARY KEY (id);


--
-- Name: local_document_catalog local_document_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.local_document_catalog
    ADD CONSTRAINT local_document_catalog_pkey PRIMARY KEY (id);


--
-- Name: local_document_catalog local_document_catalog_relative_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.local_document_catalog
    ADD CONSTRAINT local_document_catalog_relative_path_key UNIQUE (relative_path);


--
-- Name: pages pages_document_id_page_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_page_number_key UNIQUE (document_id, page_number);


--
-- Name: pages pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_pkey PRIMARY KEY (id);


--
-- Name: idx_chunks_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_document_id ON public.chunks USING btree (document_id);


--
-- Name: idx_chunks_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_embedding_hnsw ON public.chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_chunks_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_text_trgm ON public.chunks USING gin (text public.gin_trgm_ops);


--
-- Name: idx_documents_department; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_department ON public.documents USING btree (department);


--
-- Name: idx_documents_gr_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_gr_date ON public.documents USING btree (gr_date);


--
-- Name: idx_documents_gr_number_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_gr_number_trgm ON public.documents USING gin (gr_number public.gin_trgm_ops);


--
-- Name: idx_documents_subject_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_subject_trgm ON public.documents USING gin (subject public.gin_trgm_ops);


--
-- Name: idx_external_candidate_archive_sha; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_candidate_archive_sha ON public.external_acquisition_candidates USING btree (sha256) WHERE (archive_status = 'EXTERNAL_OFFICIAL_ARCHIVED'::text);


--
-- Name: idx_external_candidate_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_candidate_status ON public.external_acquisition_candidates USING btree (discovery_status, identity_match_status);


--
-- Name: idx_external_candidate_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_candidate_target ON public.external_acquisition_candidates USING btree (target_key, verification_status, acquisition_status);


--
-- Name: idx_external_lifecycle_candidate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_lifecycle_candidate ON public.external_acquisition_lifecycle_events USING btree (external_candidate_id, recorded_at);


--
-- Name: idx_external_provenance_candidate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_provenance_candidate ON public.external_document_provenance USING btree (external_candidate_id);


--
-- Name: idx_kg_candidates_local_resolution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_local_resolution ON public.kg_reference_candidates USING btree (local_resolution_status, local_validation_status);


--
-- Name: idx_kg_candidates_local_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_local_target ON public.kg_reference_candidates USING btree (resolved_target_catalog_id) WHERE (resolved_target_catalog_id IS NOT NULL);


--
-- Name: idx_kg_candidates_relation_signal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_relation_signal ON public.kg_reference_candidates USING btree (candidate_relation_type) WHERE (candidate_relation_type <> ''::text);


--
-- Name: idx_kg_candidates_resolution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_resolution ON public.kg_reference_candidates USING btree (resolution_status, validation_status);


--
-- Name: idx_kg_candidates_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_source ON public.kg_reference_candidates USING btree (source_document_id, source_page);


--
-- Name: idx_kg_candidates_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_target ON public.kg_reference_candidates USING btree (resolved_target_document_id) WHERE (resolved_target_document_id IS NOT NULL);


--
-- Name: idx_kg_candidates_unresolved_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_candidates_unresolved_number ON public.kg_reference_candidates USING btree (normalized_document_number) WHERE (resolution_status = ANY (ARRAY['NOT_IN_CORPUS'::text, 'UNPARSEABLE'::text, 'NEEDS_REVIEW'::text]));


--
-- Name: idx_kg_relations_target_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kg_relations_target_document ON public.kg_relations USING btree (target_document_id) WHERE (target_document_id IS NOT NULL);


--
-- Name: idx_local_catalog_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_kind ON public.local_document_catalog USING btree (document_kind);


--
-- Name: idx_local_catalog_legal_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_legal_key ON public.local_document_catalog USING btree (legal_canonical_key) WHERE (legal_canonical_key <> ''::text);


--
-- Name: idx_local_catalog_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_number ON public.local_document_catalog USING btree (normalized_document_number) WHERE (normalized_document_number <> ''::text);


--
-- Name: idx_local_catalog_number_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_number_date ON public.local_document_catalog USING btree (normalized_document_number, normalized_date) WHERE (normalized_document_number <> ''::text);


--
-- Name: idx_local_catalog_number_date_department; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_number_date_department ON public.local_document_catalog USING btree (normalized_document_number, normalized_date, normalized_department) WHERE (normalized_document_number <> ''::text);


--
-- Name: idx_local_catalog_number_department; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_number_department ON public.local_document_catalog USING btree (normalized_document_number, normalized_department) WHERE (normalized_document_number <> ''::text);


--
-- Name: idx_local_catalog_sha; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_local_catalog_sha ON public.local_document_catalog USING btree (sha256);


--
-- Name: uq_documents_external_canonical_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_documents_external_canonical_identity ON public.documents USING btree (document_kind, canonical_identity) WHERE ((source_origin = 'EXTERNAL_OFFICIAL'::text) AND (canonical_identity <> ''::text));


--
-- Name: uq_documents_origin_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_documents_origin_path ON public.documents USING btree (source_origin, file_path);


--
-- Name: uq_external_candidate_archive_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_external_candidate_archive_path ON public.external_acquisition_candidates USING btree (archive_source_root, archive_relative_path) WHERE (archive_status = 'EXTERNAL_OFFICIAL_ARCHIVED'::text);


--
-- Name: uq_external_candidate_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_external_candidate_identity ON public.external_acquisition_candidates USING btree (target_key, source_adapter, source_url, discovered_identifier);


--
-- Name: uq_kg_relation_department_target; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_kg_relation_department_target ON public.kg_relations USING btree (source_document_id, target_department_id, relation_type) WHERE (target_department_id IS NOT NULL);


--
-- Name: uq_kg_relation_document_target; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_kg_relation_document_target ON public.kg_relations USING btree (source_document_id, target_document_id, relation_type) WHERE (target_document_id IS NOT NULL);


--
-- Name: chunks chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: external_acquisition_candidates external_acquisition_candidates_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_candidates
    ADD CONSTRAINT external_acquisition_candidates_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.external_acquisition_runs(run_id) ON DELETE RESTRICT;


--
-- Name: external_acquisition_lifecycle_events external_acquisition_lifecycle_event_external_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_acquisition_lifecycle_events
    ADD CONSTRAINT external_acquisition_lifecycle_event_external_candidate_id_fkey FOREIGN KEY (external_candidate_id) REFERENCES public.external_acquisition_candidates(id) ON DELETE RESTRICT;


--
-- Name: external_document_provenance external_document_provenance_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_document_provenance
    ADD CONSTRAINT external_document_provenance_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: external_document_provenance external_document_provenance_external_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_document_provenance
    ADD CONSTRAINT external_document_provenance_external_candidate_id_fkey FOREIGN KEY (external_candidate_id) REFERENCES public.external_acquisition_candidates(id) ON DELETE RESTRICT;


--
-- Name: gr_relations gr_relations_from_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gr_relations
    ADD CONSTRAINT gr_relations_from_document_id_fkey FOREIGN KEY (from_document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: gr_relations gr_relations_to_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gr_relations
    ADD CONSTRAINT gr_relations_to_document_id_fkey FOREIGN KEY (to_document_id) REFERENCES public.documents(id) ON DELETE SET NULL;


--
-- Name: kg_department_aliases kg_department_aliases_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_department_aliases
    ADD CONSTRAINT kg_department_aliases_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.kg_departments(id) ON DELETE CASCADE;


--
-- Name: kg_reference_candidates kg_reference_candidates_first_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_first_run_id_fkey FOREIGN KEY (first_run_id) REFERENCES public.kg_extraction_runs(run_id);


--
-- Name: kg_reference_candidates kg_reference_candidates_last_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_last_run_id_fkey FOREIGN KEY (last_run_id) REFERENCES public.kg_extraction_runs(run_id);


--
-- Name: kg_reference_candidates kg_reference_candidates_resolved_target_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_resolved_target_catalog_id_fkey FOREIGN KEY (resolved_target_catalog_id) REFERENCES public.local_document_catalog(id) ON DELETE SET NULL;


--
-- Name: kg_reference_candidates kg_reference_candidates_resolved_target_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_resolved_target_document_id_fkey FOREIGN KEY (resolved_target_document_id) REFERENCES public.documents(id) ON DELETE SET NULL;


--
-- Name: kg_reference_candidates kg_reference_candidates_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: kg_reference_candidates kg_reference_candidates_source_document_id_source_page_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reference_candidates
    ADD CONSTRAINT kg_reference_candidates_source_document_id_source_page_fkey FOREIGN KEY (source_document_id, source_page) REFERENCES public.pages(document_id, page_number) ON DELETE CASCADE;


--
-- Name: kg_relation_evidence kg_relation_evidence_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relation_evidence
    ADD CONSTRAINT kg_relation_evidence_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.kg_reference_candidates(id) ON DELETE CASCADE;


--
-- Name: kg_relation_evidence kg_relation_evidence_relation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relation_evidence
    ADD CONSTRAINT kg_relation_evidence_relation_id_fkey FOREIGN KEY (relation_id) REFERENCES public.kg_relations(id) ON DELETE CASCADE;


--
-- Name: kg_relations kg_relations_created_from_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations
    ADD CONSTRAINT kg_relations_created_from_candidate_id_fkey FOREIGN KEY (created_from_candidate_id) REFERENCES public.kg_reference_candidates(id) ON DELETE SET NULL;


--
-- Name: kg_relations kg_relations_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations
    ADD CONSTRAINT kg_relations_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: kg_relations kg_relations_target_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations
    ADD CONSTRAINT kg_relations_target_department_id_fkey FOREIGN KEY (target_department_id) REFERENCES public.kg_departments(id) ON DELETE CASCADE;


--
-- Name: kg_relations kg_relations_target_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_relations
    ADD CONSTRAINT kg_relations_target_document_id_fkey FOREIGN KEY (target_document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: kg_reviews kg_reviews_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kg_reviews
    ADD CONSTRAINT kg_reviews_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.kg_reference_candidates(id) ON DELETE CASCADE;


--
-- Name: local_document_catalog local_document_catalog_ingested_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.local_document_catalog
    ADD CONSTRAINT local_document_catalog_ingested_document_id_fkey FOREIGN KEY (ingested_document_id) REFERENCES public.documents(id) ON DELETE SET NULL;


--
-- Name: pages pages_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict AVrJIF3CkG8SFbOErIUsuBH8pes2DPVnZmfe6DDVvOwKegd2Jb2KzKxAHlNqXPZ


CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    sha256 CHAR(64) UNIQUE NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    department TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    digital_pages INTEGER NOT NULL DEFAULT 0,
    ocr_pages INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT NOT NULL,
    source_origin TEXT NOT NULL DEFAULT 'LOCAL_ORIGINAL'
        CHECK (source_origin IN ('LOCAL_ORIGINAL','EXTERNAL_OFFICIAL','APPROVED_SUBMISSION')),
    document_kind TEXT NOT NULL DEFAULT 'UNKNOWN',
    canonical_identity TEXT NOT NULL DEFAULT '',
    document_year INTEGER CHECK (document_year IS NULL OR document_year BETWEEN 1800 AND 2200),
    document_type TEXT,
    gr_number TEXT,
    gr_date DATE,
    subject TEXT,
    references_text TEXT,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pages (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    extraction_method TEXT NOT NULL,
    quality_score REAL,
    text TEXT NOT NULL,
    UNIQUE(document_id, page_number)
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS gr_relations (
    id BIGSERIAL PRIMARY KEY,
    from_document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    to_document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
    relation_type TEXT NOT NULL,
    target_gr_number TEXT,
    evidence_page INTEGER,
    evidence_text TEXT,
    confidence REAL,
    UNIQUE(from_document_id, relation_type, target_gr_number, evidence_page)
);

CREATE INDEX IF NOT EXISTS idx_documents_department ON documents(department);
CREATE INDEX IF NOT EXISTS idx_documents_gr_date ON documents(gr_date);
CREATE INDEX IF NOT EXISTS idx_documents_gr_number_trgm ON documents USING gin (gr_number gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_documents_subject_trgm ON documents USING gin (subject gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_origin_path ON documents(source_origin,file_path);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_external_canonical_identity
    ON documents(document_kind,canonical_identity)
    WHERE source_origin='EXTERNAL_OFFICIAL' AND canonical_identity<>'';
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

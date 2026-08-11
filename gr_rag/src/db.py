from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .chunker import Chunk
from .config import PROJECT_ROOT, settings
from .pdf_extract import ExtractedPage


class Database:
    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        register_vector(conn)
        return conn

    def ensure_schema(self) -> None:
        sql_path = PROJECT_ROOT / "sql" / "init.sql"
        sql = sql_path.read_text(encoding="utf-8")
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql)
            conn.commit()

    def exists_sha(self, sha256: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM documents WHERE sha256 = %s LIMIT 1", (sha256,)
            ).fetchone()
        return row is not None

    def replace_document(
        self,
        *,
        sha256: str,
        file_path: str,
        file_name: str,
        department: str,
        page_count: int,
        digital_pages: int,
        ocr_pages: int,
        extraction_method: str,
        raw_text: str,
        normalized_text: str,
        extracted_metadata: dict[str, Any],
        pages: list[ExtractedPage],
        chunks: list[Chunk],
        embeddings,
    ) -> int:
        if not pages:
            raise ValueError("Cannot store a document without pages")
        if len(chunks) != len(embeddings):
            raise ValueError(
                "Chunk/embedding count mismatch: "
                f"{len(chunks)} chunks but {len(embeddings)} embeddings"
            )

        # PostgreSQL's DATE adapter handles gr_date in its typed column, but the
        # duplicate metadata JSONB value must be JSON-serializable.
        metadata_json = dict(extracted_metadata)
        if metadata_json.get("gr_date") is not None:
            metadata_json["gr_date"] = metadata_json["gr_date"].isoformat()

        with self.connect() as conn:
            with conn.transaction():
                # If the same path was re-scraped with changed bytes, replace its old record.
                conn.execute("DELETE FROM documents WHERE file_path = %s", (file_path,))

                cur = conn.execute(
                    """
                    INSERT INTO documents (
                        sha256, file_path, file_name, department, page_count,
                        digital_pages, ocr_pages, extraction_method,
                        document_type, gr_number, gr_date, subject, references_text,
                        raw_text, normalized_text, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        sha256,
                        file_path,
                        file_name,
                        department,
                        page_count,
                        digital_pages,
                        ocr_pages,
                        extraction_method,
                        extracted_metadata.get("document_type"),
                        extracted_metadata.get("gr_number"),
                        extracted_metadata.get("gr_date"),
                        extracted_metadata.get("subject"),
                        extracted_metadata.get("references_text"),
                        raw_text,
                        normalized_text,
                        Jsonb(metadata_json),
                    ),
                )
                document_id = cur.fetchone()["id"]

                with conn.cursor() as batch_cur:
                    batch_cur.executemany(
                        """
                        INSERT INTO pages (
                            document_id, page_number, extraction_method, quality_score, text
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                document_id,
                                p.page_number,
                                p.method,
                                p.quality_score,
                                p.text,
                            )
                            for p in pages
                        ],
                    )

                    batch_cur.executemany(
                        """
                        INSERT INTO chunks (
                            document_id, chunk_index, page_start, page_end,
                            token_count, text, embedding, metadata
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                document_id,
                                c.chunk_index,
                                c.page_start,
                                c.page_end,
                                c.token_count,
                                c.text,
                                embeddings[i],
                                Jsonb(
                                    {
                                        "department": department,
                                        "gr_number": extracted_metadata.get("gr_number"),
                                        "gr_date": (
                                            extracted_metadata["gr_date"].isoformat()
                                            if extracted_metadata.get("gr_date")
                                            else None
                                        ),
                                        "subject": extracted_metadata.get("subject"),
                                    }
                                ),
                            )
                            for i, c in enumerate(chunks)
                        ],
                    )

            return document_id


    def dense_search(
        self,
        query_vector,
        top_k: int = 10,
        department: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if department:
                rows = conn.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        d.id AS document_id,
                        d.file_name,
                        d.file_path,
                        d.department,
                        d.gr_number,
                        d.gr_date,
                        d.subject,
                        c.chunk_index,
                        c.page_start,
                        c.page_end,
                        c.text,
                        1 - (c.embedding <=> %s) AS similarity
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE d.department = %s
                    ORDER BY c.embedding <=> %s
                    LIMIT %s
                    """,
                    (query_vector, department, query_vector, top_k),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        d.id AS document_id,
                        d.file_name,
                        d.file_path,
                        d.department,
                        d.gr_number,
                        d.gr_date,
                        d.subject,
                        c.chunk_index,
                        c.page_start,
                        c.page_end,
                        c.text,
                        1 - (c.embedding <=> %s) AS similarity
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    ORDER BY c.embedding <=> %s
                    LIMIT %s
                    """,
                    (query_vector, query_vector, top_k),
                ).fetchall()
        return list(rows)

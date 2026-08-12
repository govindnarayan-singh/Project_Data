from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from knowledge_engine.retrieval.repository import PostgresRetrievalRepository
from knowledge_engine.settings import PROJECT_ROOT, settings


class PostgresStorage(PostgresRetrievalRepository):
    """Validated persistence operations, separated from read-only retrieval."""

    def ensure_schema(self) -> None:
        import psycopg

        sql = (PROJECT_ROOT / "deployment" / "postgres" / "init.sql").read_text(encoding="utf-8")
        with psycopg.connect(self.dsn) as connection:
            connection.execute(sql)
            connection.commit()

    def exists_sha(self, sha256: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM documents WHERE sha256 = %s LIMIT 1", (sha256,)).fetchone() is not None

    def find_document_identity(
        self, file_path: str, sha256: str
    ) -> dict[str, Any] | None:
        """Find one document by source path or content identity.

        Two different rows matching the path and hash indicate pre-existing
        identity corruption and are never resolved automatically.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, file_path, file_name, sha256
                   FROM documents
                   WHERE file_path = %s OR sha256 = %s
                   ORDER BY id""",
                (file_path, sha256),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(
                "Document path and SHA-256 resolve to different existing rows"
            )
        return dict(rows[0]) if rows else None

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
        pages,
        chunks,
        embeddings,
    ) -> int:
        from psycopg.types.json import Jsonb

        if not pages:
            raise ValueError("Cannot store a document without pages")
        if not chunks:
            raise ValueError("Cannot store a document without chunks")
        if len(chunks) != len(embeddings):
            raise ValueError(f"Chunk/embedding count mismatch: {len(chunks)} chunks but {len(embeddings)} embeddings")
        if any(chunk.token_count > settings.chunk_max_tokens for chunk in chunks):
            raise ValueError(
                f"A chunk exceeds CHUNK_MAX_TOKENS={settings.chunk_max_tokens}"
            )
        dimensions = {len(embedding) for embedding in embeddings}
        if dimensions != {settings.embed_dim}:
            raise ValueError(
                f"Embedding dimensions {sorted(dimensions)} do not match EMBED_DIM={settings.embed_dim}"
            )
        metadata_json = dict(extracted_metadata)
        if metadata_json.get("gr_date") is not None:
            metadata_json["gr_date"] = metadata_json["gr_date"].isoformat()
        with self.connect() as connection:
            with connection.transaction():
                identities = connection.execute(
                    """SELECT id, file_path, sha256
                       FROM documents
                       WHERE file_path = %s OR sha256 = %s
                       ORDER BY id
                       FOR UPDATE""",
                    (file_path, sha256),
                ).fetchall()
                if len(identities) > 1:
                    raise ValueError(
                        "Document path and SHA-256 resolve to different existing rows"
                    )

                document_values = (
                    sha256, file_path, file_name, department, page_count,
                    digital_pages, ocr_pages, extraction_method,
                    extracted_metadata.get("document_type"), extracted_metadata.get("gr_number"),
                    extracted_metadata.get("gr_date"), extracted_metadata.get("subject"),
                    extracted_metadata.get("references_text"), raw_text, normalized_text, Jsonb(metadata_json),
                )
                if identities:
                    document_id = int(identities[0]["id"])
                    connection.execute(
                        """UPDATE documents SET
                               sha256 = %s, file_path = %s, file_name = %s,
                               department = %s, page_count = %s,
                               digital_pages = %s, ocr_pages = %s,
                               extraction_method = %s, document_type = %s,
                               gr_number = %s, gr_date = %s, subject = %s,
                               references_text = %s, raw_text = %s,
                               normalized_text = %s, metadata = %s
                           WHERE id = %s""",
                        (*document_values, document_id),
                    )
                    connection.execute(
                        "DELETE FROM chunks WHERE document_id = %s", (document_id,)
                    )
                    connection.execute(
                        "DELETE FROM pages WHERE document_id = %s", (document_id,)
                    )
                else:
                    cursor = connection.execute(
                    """INSERT INTO documents (
                           sha256, file_path, file_name, department, page_count,
                           digital_pages, ocr_pages, extraction_method,
                           document_type, gr_number, gr_date, subject, references_text,
                           raw_text, normalized_text, metadata
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                        document_values,
                    )
                    document_id = int(cursor.fetchone()["id"])
                with connection.cursor() as batch_cursor:
                    batch_cursor.executemany(
                        "INSERT INTO pages (document_id, page_number, extraction_method, quality_score, text) VALUES (%s, %s, %s, %s, %s)",
                        [(document_id, page.page_number, page.method, page.quality_score, page.text) for page in pages],
                    )
                    batch_cursor.executemany(
                        """INSERT INTO chunks (document_id, chunk_index, page_start, page_end, token_count, text, embedding, metadata)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        [
                            (
                                document_id, chunk.chunk_index, chunk.page_start, chunk.page_end,
                                chunk.token_count, chunk.text, embeddings[index],
                                Jsonb({
                                    "department": department,
                                    "gr_number": extracted_metadata.get("gr_number"),
                                    "gr_date": extracted_metadata["gr_date"].isoformat() if extracted_metadata.get("gr_date") else None,
                                    "subject": extracted_metadata.get("subject"),
                                }),
                            )
                            for index, chunk in enumerate(chunks)
                        ],
                    )
                self.validate_document_integrity(
                    document_id,
                    expected_pages=len(pages),
                    expected_chunks=len(chunks),
                    max_tokens=settings.chunk_max_tokens,
                    embedding_dimension=settings.embed_dim,
                    connection=connection,
                )
        return document_id

    def validate_document_integrity(
        self,
        document_id: int,
        *,
        expected_pages: int | None = None,
        expected_chunks: int | None = None,
        max_tokens: int,
        embedding_dimension: int,
        connection=None,
    ) -> dict[str, int]:
        """Validate stored derived data, optionally inside the write transaction."""
        manager = nullcontext(connection) if connection is not None else self.connect()
        with manager as active_connection:
            document = active_connection.execute(
                "SELECT page_count FROM documents WHERE id = %s", (document_id,)
            ).fetchone()
            if document is None:
                raise ValueError(f"Document {document_id} was not found after persistence")
            page_count = int(document["page_count"])
            pages = active_connection.execute(
                """SELECT count(*) AS count, count(DISTINCT page_number) AS distinct_count,
                          min(page_number) AS minimum, max(page_number) AS maximum
                   FROM pages WHERE document_id = %s""",
                (document_id,),
            ).fetchone()
            chunks = active_connection.execute(
                """SELECT count(*) AS count, min(token_count) AS minimum_tokens,
                          max(token_count) AS maximum_tokens,
                          count(*) FILTER (
                              WHERE token_count > %s OR page_start < 1 OR
                                    page_end < page_start OR page_end > %s
                          ) AS invalid_chunks,
                          count(*) FILTER (
                              WHERE vector_dims(embedding) <> %s
                          ) AS invalid_embeddings
                   FROM chunks WHERE document_id = %s""",
                (max_tokens, page_count, embedding_dimension, document_id),
            ).fetchone()

        stored_pages = int(pages["count"])
        stored_chunks = int(chunks["count"])
        if expected_pages is not None and stored_pages != expected_pages:
            raise ValueError(
                f"Stored page count {stored_pages} does not match expected {expected_pages}"
            )
        if expected_chunks is not None and stored_chunks != expected_chunks:
            raise ValueError(
                f"Stored chunk count {stored_chunks} does not match expected {expected_chunks}"
            )
        if (
            stored_pages != page_count
            or int(pages["distinct_count"]) != page_count
            or int(pages["minimum"] or 0) != 1
            or int(pages["maximum"] or 0) != page_count
        ):
            raise ValueError("Stored page numbering is not contiguous")
        if int(chunks["invalid_chunks"]) != 0:
            raise ValueError("Stored chunks violate token or page-reference invariants")
        if int(chunks["invalid_embeddings"]) != 0:
            raise ValueError("Stored embeddings have an unexpected dimension")
        return {
            "document_id": int(document_id),
            "pages": stored_pages,
            "chunks": stored_chunks,
            "min_chunk_tokens": int(chunks["minimum_tokens"] or 0),
            "max_chunk_tokens": int(chunks["maximum_tokens"] or 0),
            "embedding_dimension": int(embedding_dimension),
        }


Database = PostgresStorage

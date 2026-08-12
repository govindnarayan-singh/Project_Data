from __future__ import annotations

from typing import Any

from knowledge_engine.retrieval.repository import PostgresRetrievalRepository
from knowledge_engine.settings import PROJECT_ROOT


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
        if len(chunks) != len(embeddings):
            raise ValueError(f"Chunk/embedding count mismatch: {len(chunks)} chunks but {len(embeddings)} embeddings")
        metadata_json = dict(extracted_metadata)
        if metadata_json.get("gr_date") is not None:
            metadata_json["gr_date"] = metadata_json["gr_date"].isoformat()
        with self.connect() as connection:
            with connection.transaction():
                connection.execute("DELETE FROM documents WHERE file_path = %s", (file_path,))
                cursor = connection.execute(
                    """INSERT INTO documents (
                           sha256, file_path, file_name, department, page_count,
                           digital_pages, ocr_pages, extraction_method,
                           document_type, gr_number, gr_date, subject, references_text,
                           raw_text, normalized_text, metadata
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        sha256, file_path, file_name, department, page_count,
                        digital_pages, ocr_pages, extraction_method,
                        extracted_metadata.get("document_type"), extracted_metadata.get("gr_number"),
                        extracted_metadata.get("gr_date"), extracted_metadata.get("subject"),
                        extracted_metadata.get("references_text"), raw_text, normalized_text, Jsonb(metadata_json),
                    ),
                )
                document_id = cursor.fetchone()["id"]
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
        return int(document_id)


Database = PostgresStorage

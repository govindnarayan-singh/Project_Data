from __future__ import annotations

from typing import Any, Protocol

from knowledge_engine.settings import settings


class RetrievalRepository(Protocol):
    def dense_search(self, query_vector, top_k: int = 5, department: str | None = None) -> list[dict[str, Any]]: ...

    def get_document(self, document_id: int) -> dict[str, Any] | None: ...


class PostgresRetrievalRepository:
    """Read-only access to the existing pgvector schema."""

    def __init__(self, dsn: str = settings.database_url):
        self.dsn = dsn

    def connect(self):
        import psycopg
        from pgvector.psycopg import register_vector
        from psycopg.rows import dict_row

        connection = psycopg.connect(self.dsn, row_factory=dict_row)
        register_vector(connection)
        return connection

    def dense_search(self, query_vector, top_k: int = 5, department: str | None = None) -> list[dict[str, Any]]:
        select_sql = """
            SELECT c.id AS chunk_id, d.id AS document_id, d.file_name,
                   d.file_path, d.department, d.gr_number, d.gr_date, d.subject,
                   c.chunk_index, c.page_start, c.page_end, c.text,
                   1 - (c.embedding <=> %s) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
        """
        with self.connect() as connection:
            if department:
                rows = connection.execute(
                    select_sql + " WHERE d.department = %s ORDER BY c.embedding <=> %s LIMIT %s",
                    (query_vector, department, query_vector, top_k),
                ).fetchall()
            else:
                rows = connection.execute(
                    select_sql + " ORDER BY c.embedding <=> %s LIMIT %s",
                    (query_vector, query_vector, top_k),
                ).fetchall()
        return list(rows)

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT id, file_name, file_path, source_origin FROM documents WHERE id = %s",
                (document_id,),
            ).fetchone()

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT (SELECT count(*) FROM documents) AS documents,
                          (SELECT count(*) FROM pages) AS pages,
                          (SELECT count(*) FROM chunks) AS chunks"""
            ).fetchone()
        return {key: int(value) for key, value in row.items()}

from __future__ import annotations

from contextlib import AbstractContextManager
import unittest

from knowledge_engine.chunking.chunker import Chunk
from knowledge_engine.extraction.pdf import ExtractedPage
from knowledge_engine.storage.postgres import PostgresStorage


class FakeTransaction(AbstractContextManager):
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rolled_back = exc_type is not None
        return False


class FakeCursor(AbstractContextManager):
    def __init__(self):
        self.calls = 0

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def executemany(self, sql, rows):
        self.calls += 1
        if "INSERT INTO chunks" in sql:
            raise RuntimeError("simulated insert failure")


class QueryResult:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class FakeConnection(AbstractContextManager):
    def __init__(self):
        self.transaction_state = FakeTransaction()
        self.cursor_state = FakeCursor()

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def transaction(self): return self.transaction_state
    def cursor(self): return self.cursor_state

    def execute(self, sql, params=None):
        if "FOR UPDATE" in sql:
            return QueryResult([{"id": 9, "file_path": "old.pdf", "sha256": "a" * 64}])
        return QueryResult([])


class FakeStorage(PostgresStorage):
    def __init__(self, connection): self.connection = connection
    def connect(self): return self.connection


class PostgresTransactionTests(unittest.TestCase):
    def test_derived_insert_failure_rolls_back_replacement_transaction(self):
        connection = FakeConnection()
        storage = FakeStorage(connection)
        page = ExtractedPage(1, "text", "digital", 1.0)
        chunk = Chunk(0, 1, 1, 1, "text")
        with self.assertRaisesRegex(RuntimeError, "insert failure"):
            storage.replace_document(
                sha256="a" * 64, file_path="old.pdf", file_name="old.pdf",
                department="Finance", page_count=1, digital_pages=1,
                ocr_pages=0, extraction_method="digital", raw_text="text",
                normalized_text="text", extracted_metadata={}, pages=[page],
                chunks=[chunk], embeddings=[[0.0] * 1024],
            )
        self.assertTrue(connection.transaction_state.rolled_back)

from datetime import date
import unittest
from unittest.mock import patch

from knowledge_engine.embeddings import bge_m3
from knowledge_engine.retrieval.service import RetrievalService


class FakeEmbedder:
    def __init__(self): self.calls = []
    def encode(self, texts, show_progress_bar=False):
        self.calls.append((texts, show_progress_bar))
        return [[0.1, 0.2]]


class FakeRepository:
    def __init__(self): self.calls = []
    def dense_search(self, vector, top_k=5, department=None):
        self.calls.append((vector, top_k, department))
        return [{
            "document_id": 7, "file_name": "gr.pdf", "file_path": "Finance/gr.pdf",
            "department": "Finance", "gr_number": "GR-7", "gr_date": date(2024, 1, 2),
            "subject": "Test", "chunk_index": 4, "page_start": 2, "page_end": 3,
            "text": "मराठी पुरावा", "similarity": 0.875,
        }]
    def get_document(self, document_id): return None


class RetrievalServiceTests(unittest.TestCase):
    def test_typed_search_result_and_dependencies(self):
        embedder, repository = FakeEmbedder(), FakeRepository()
        result = RetrievalService(embedder=embedder, repository=repository).search("  प्रश्न  ", top_k=3, department=" Finance ")[0]
        self.assertEqual(result.rank, 1)
        self.assertEqual(result.document_id, 7)
        self.assertEqual(result.text, "मराठी पुरावा")
        self.assertEqual(embedder.calls, [(["प्रश्न"], False)])
        self.assertEqual(repository.calls[0][1:], (3, "Finance"))

    def test_query_and_top_k_validation_happen_before_loading_model(self):
        factory_calls = []
        service = RetrievalService(repository=FakeRepository(), embedder_factory=lambda: factory_calls.append(True))
        for query, top_k in (("", 5), ("question", 8), ("question", 0)):
            with self.subTest(query=query, top_k=top_k), self.assertRaises(ValueError):
                service.search(query, top_k=top_k)
        self.assertEqual(factory_calls, [])

    def test_lazy_provider_is_cached_once(self):
        bge_m3.get_embedder.cache_clear()
        sentinel = object()
        with patch.object(bge_m3, "BgeM3Embedder", return_value=sentinel) as constructor:
            self.assertIs(bge_m3.get_embedder(), sentinel)
            self.assertIs(bge_m3.get_embedder(), sentinel)
        constructor.assert_called_once_with()
        bge_m3.get_embedder.cache_clear()

from __future__ import annotations

import re
import unittest

from knowledge_engine.chunking.chunker import TokenChunker, Unit
from knowledge_engine.extraction.pdf import ExtractedPage


class WordTokenizer:
    def encode(self, text: str, add_special_tokens: bool = False):
        return re.findall(r"\S+", text)


class CharacterTokenizer:
    def encode(self, text: str, add_special_tokens: bool = False):
        return list(text)


def make_chunker(tokenizer, max_tokens: int = 20, overlap_tokens: int = 4) -> TokenChunker:
    chunker = object.__new__(TokenChunker)
    chunker.tokenizer = tokenizer
    chunker.max_tokens = max_tokens
    chunker.overlap_tokens = overlap_tokens
    return chunker


def page(number: int, text: str) -> ExtractedPage:
    return ExtractedPage(number, text, "digital", 1.0)


class TokenChunkerGuaranteeTests(unittest.TestCase):
    def assert_maximum(self, chunker: TokenChunker, chunks) -> None:
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.token_count <= chunker.max_tokens for chunk in chunks))
        self.assertTrue(all(chunk.token_count == chunker.count_tokens(chunk.text) for chunk in chunks))

    def test_normal_paragraph_splitting(self):
        chunker = make_chunker(WordTokenizer(), max_tokens=8, overlap_tokens=2)
        chunks = chunker.chunk_pages([
            page(1, "one two three four five\n\nsix seven eight nine ten")
        ])
        self.assertEqual(len(chunks), 2)
        self.assert_maximum(chunker, chunks)

    def test_oversized_paragraph_is_split_without_text_loss(self):
        chunker = make_chunker(WordTokenizer(), max_tokens=7, overlap_tokens=0)
        source = " ".join(f"word{index}" for index in range(25))
        units = chunker._split_long_text(1, source)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(unit.text for unit in units), source)
        self.assertTrue(all(unit.token_count <= 7 for unit in units))

    def test_oversized_table_like_text_uses_deterministic_fallback(self):
        chunker = make_chunker(CharacterTokenizer(), max_tokens=24, overlap_tokens=0)
        source = "अ.क्र|विभाग|रक्कम|2024|" * 8
        first = chunker._split_long_text(3, source)
        second = chunker._split_long_text(3, source)
        self.assertEqual([unit.text for unit in first], [unit.text for unit in second])
        self.assertEqual("".join(unit.text for unit in first), source)
        self.assertTrue(all(unit.token_count <= 24 for unit in first))

    def test_multilingual_marathi_english_text(self):
        chunker = make_chunker(CharacterTokenizer(), max_tokens=32, overlap_tokens=5)
        source = "महाराष्ट्र शासन Environment Department action २०२४ " * 5
        chunks = chunker.chunk_pages([page(1, source)])
        self.assert_maximum(chunker, chunks)
        self.assertTrue(any("महाराष्ट्र" in chunk.text for chunk in chunks))
        self.assertTrue(any("Environment" in chunk.text for chunk in chunks))

    def test_page_boundary_traceability(self):
        chunker = make_chunker(WordTokenizer(), max_tokens=10, overlap_tokens=2)
        chunks = chunker.chunk_pages([
            page(1, "one two three"),
            page(2, "चार पाच सहा"),
        ])
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].page_start, chunks[0].page_end), (1, 2))
        self.assert_maximum(chunker, chunks)

    def test_no_punctuation_fallback_path(self):
        chunker = make_chunker(CharacterTokenizer(), max_tokens=10, overlap_tokens=0)
        source = "अखंडमजकूरकोणतेहीविरामचिन्हनाही"
        units = chunker._split_long_text(4, source)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(unit.text for unit in units), source)
        self.assertTrue(all(unit.token_count <= 10 for unit in units))

    def test_overlap_never_breaks_maximum_token_invariant(self):
        chunker = make_chunker(CharacterTokenizer(), max_tokens=10, overlap_tokens=5)
        units = [Unit(1, "a" * 7, 7), Unit(2, "b" * 8, 8)]
        chunker._page_units = lambda pages: units
        chunks = chunker.chunk_pages([])
        self.assertEqual([chunk.token_count for chunk in chunks], [7, 8])
        self.assertEqual([(chunk.page_start, chunk.page_end) for chunk in chunks], [(1, 1), (2, 2)])
        self.assert_maximum(chunker, chunks)

    def test_overlap_is_retained_when_it_fits(self):
        chunker = make_chunker(CharacterTokenizer(), max_tokens=8, overlap_tokens=3)
        units = [Unit(1, "aaa", 3), Unit(1, "bbb", 3), Unit(2, "ccc", 3)]
        chunker._page_units = lambda pages: units
        chunks = chunker.chunk_pages([])
        self.assertEqual([chunk.text for chunk in chunks], ["aaa\n\nbbb", "bbb\n\nccc"])
        self.assert_maximum(chunker, chunks)

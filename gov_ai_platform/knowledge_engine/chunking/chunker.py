from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge_engine.extraction.pdf import ExtractedPage
from knowledge_engine.extraction.text_quality import normalize_marathi_text
from knowledge_engine.settings import settings


@dataclass
class Chunk:
    chunk_index: int
    page_start: int
    page_end: int
    token_count: int
    text: str


@dataclass
class Unit:
    page_number: int
    text: str
    token_count: int


class TokenChunker:
    """Paragraph-aware chunker with a hard final-token limit."""

    def __init__(
        self,
        model_name: str = settings.embed_model,
        max_tokens: int = settings.chunk_max_tokens,
        overlap_tokens: int = settings.chunk_overlap_tokens,
    ):
        from transformers import AutoTokenizer

        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if overlap_tokens < 0:
            raise ValueError("overlap_tokens cannot be negative")
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            cache_dir=str(settings.cache_dir / "models"),
        )

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def _split_atomic_text(self, page_number: int, text: str) -> list[Unit]:
        """Split an indivisible-looking string by exact token-counted prefixes.

        Character boundaries retain every source character and work for prose,
        OCR output, long identifiers, and table-like text. Binary search keeps
        tokenizer calls bounded; each emitted prefix is verified independently.
        """
        remaining = text
        units: list[Unit] = []
        while remaining:
            remaining_tokens = self.count_tokens(remaining)
            if remaining_tokens <= self.max_tokens:
                units.append(Unit(page_number, remaining, remaining_tokens))
                break

            low, high = 1, len(remaining)
            split_at = 0
            while low <= high:
                midpoint = (low + high) // 2
                if self.count_tokens(remaining[:midpoint]) <= self.max_tokens:
                    split_at = midpoint
                    low = midpoint + 1
                else:
                    high = midpoint - 1

            if split_at == 0:
                raise ValueError(
                    "Tokenizer cannot fit even one source character within "
                    f"max_tokens={self.max_tokens}"
                )
            prefix = remaining[:split_at]
            prefix_tokens = self.count_tokens(prefix)
            if prefix_tokens > self.max_tokens:
                raise AssertionError("Atomic split exceeded the configured token limit")
            units.append(Unit(page_number, prefix, prefix_tokens))
            remaining = remaining[split_at:]
        return units

    def _split_long_text(self, page_number: int, text: str) -> list[Unit]:
        text = normalize_marathi_text(text)
        if not text:
            return []
        count = self.count_tokens(text)
        if count <= self.max_tokens:
            return [Unit(page_number, text, count)]
        sentences = [sentence.strip() for sentence in re.split(r"(?<=[।.!?])\s+", text) if sentence.strip()]
        if len(sentences) <= 1:
            return self._split_atomic_text(page_number, text)

        safe_sentences: list[Unit] = []
        for sentence in sentences:
            sentence_tokens = self.count_tokens(sentence)
            if sentence_tokens <= self.max_tokens:
                safe_sentences.append(Unit(page_number, sentence, sentence_tokens))
            else:
                safe_sentences.extend(self._split_atomic_text(page_number, sentence))

        units: list[Unit] = []
        buffer: list[str] = []
        for sentence_unit in safe_sentences:
            candidate = " ".join([*buffer, sentence_unit.text])
            if buffer and self.count_tokens(candidate) > self.max_tokens:
                joined = " ".join(buffer)
                units.append(Unit(page_number, joined, self.count_tokens(joined)))
                buffer = []
            buffer.append(sentence_unit.text)
        if buffer:
            joined = " ".join(buffer)
            units.append(Unit(page_number, joined, self.count_tokens(joined)))
        if any(unit.token_count > self.max_tokens for unit in units):
            raise AssertionError("Sentence splitting exceeded the configured token limit")
        return units

    def _page_units(self, pages: list[ExtractedPage]) -> list[Unit]:
        units: list[Unit] = []
        for page in pages:
            paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n+", page.text) if paragraph.strip()]
            if len(paragraphs) <= 1:
                lines = [line.strip() for line in page.text.splitlines() if line.strip()]
                paragraphs = [" ".join(lines[index:index + 6]) for index in range(0, len(lines), 6)]
            for paragraph in paragraphs:
                units.extend(self._split_long_text(page.page_number, paragraph))
        return units

    def chunk_pages(self, pages: list[ExtractedPage]) -> list[Chunk]:
        units = self._page_units(pages)
        chunks: list[Chunk] = []
        current: list[Unit] = []

        def joined(units_to_join: list[Unit]) -> str:
            return "\n\n".join(unit.text for unit in units_to_join)

        def token_count(units_to_count: list[Unit]) -> int:
            return self.count_tokens(joined(units_to_count))

        def append_chunk(units_to_append: list[Unit]) -> None:
            text = joined(units_to_append)
            final_token_count = self.count_tokens(text)
            if final_token_count > self.max_tokens:
                raise AssertionError(
                    f"Final chunk has {final_token_count} tokens; maximum is {self.max_tokens}"
                )
            chunks.append(
                Chunk(
                    len(chunks),
                    min(unit.page_number for unit in units_to_append),
                    max(unit.page_number for unit in units_to_append),
                    final_token_count,
                    text,
                )
            )

        def select_overlap(previous: list[Unit]) -> list[Unit]:
            overlap: list[Unit] = []
            for unit in reversed(previous):
                candidate = [unit, *overlap]
                if token_count(candidate) > self.overlap_tokens:
                    break
                overlap = candidate
            return overlap

        for unit in units:
            if unit.token_count > self.max_tokens:
                raise AssertionError("Page-unit splitting failed to enforce max_tokens")
            candidate = [*current, unit]
            if current and token_count(candidate) > self.max_tokens:
                append_chunk(current)
                overlap = select_overlap(current)
                candidate = [*overlap, unit]
                if token_count(candidate) > self.max_tokens:
                    candidate = [unit]
            current = candidate

        if current:
            text = joined(current)
            if not chunks or text != chunks[-1].text:
                append_chunk(current)

        if any(chunk.token_count > self.max_tokens for chunk in chunks):
            raise AssertionError("Final chunk invariant violated")
        return chunks

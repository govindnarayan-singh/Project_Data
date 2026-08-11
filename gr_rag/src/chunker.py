from __future__ import annotations

import re
from dataclasses import dataclass

from transformers import AutoTokenizer

from .config import settings
from .pdf_extract import ExtractedPage
from .text_utils import normalize_marathi_text


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
    """
    Paragraph-aware chunker.

    Why not fixed character slicing?
    Because a GR paragraph/clause often carries one administrative idea. We try to keep
    those boundaries intact while limiting the final chunk by the embedding tokenizer.
    """

    def __init__(
        self,
        model_name: str = settings.embed_model,
        max_tokens: int = settings.chunk_max_tokens,
        overlap_tokens: int = settings.chunk_overlap_tokens,
    ):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            cache_dir=str(settings.cache_dir / "models"),
        )

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def _split_long_text(self, page_number: int, text: str) -> list[Unit]:
        text = normalize_marathi_text(text)
        if not text:
            return []

        count = self.count_tokens(text)
        if count <= self.max_tokens:
            return [Unit(page_number, text, count)]

        # Split on Marathi danda / sentence punctuation first.
        sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", text) if s.strip()]
        if len(sentences) <= 1:
            # Last-resort character windows; original text is preserved.
            approx_chars = max(800, self.max_tokens * 4)
            return [
                Unit(page_number, text[i : i + approx_chars], self.count_tokens(text[i : i + approx_chars]))
                for i in range(0, len(text), approx_chars)
            ]

        units: list[Unit] = []
        buf: list[str] = []
        buf_tokens = 0
        for sentence in sentences:
            n = self.count_tokens(sentence)
            if buf and buf_tokens + n > self.max_tokens:
                joined = " ".join(buf)
                units.append(Unit(page_number, joined, self.count_tokens(joined)))
                buf = []
                buf_tokens = 0
            buf.append(sentence)
            buf_tokens += n
        if buf:
            joined = " ".join(buf)
            units.append(Unit(page_number, joined, self.count_tokens(joined)))
        return units

    def _page_units(self, pages: list[ExtractedPage]) -> list[Unit]:
        units: list[Unit] = []
        for page in pages:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", page.text) if p.strip()]
            if len(paragraphs) <= 1:
                # OCR commonly returns one line per visual line. Group into useful blocks.
                lines = [x.strip() for x in page.text.splitlines() if x.strip()]
                paragraphs = [" ".join(lines[i : i + 6]) for i in range(0, len(lines), 6)]
            for paragraph in paragraphs:
                units.extend(self._split_long_text(page.page_number, paragraph))
        return units

    def chunk_pages(self, pages: list[ExtractedPage]) -> list[Chunk]:
        units = self._page_units(pages)
        chunks: list[Chunk] = []
        current: list[Unit] = []
        current_tokens = 0

        def flush() -> None:
            nonlocal current, current_tokens
            if not current:
                return
            text = "\n\n".join(u.text for u in current)
            chunks.append(
                Chunk(
                    chunk_index=len(chunks),
                    page_start=min(u.page_number for u in current),
                    page_end=max(u.page_number for u in current),
                    token_count=self.count_tokens(text),
                    text=text,
                )
            )

            # Keep complete tail units as overlap. No sentence is cut.
            overlap: list[Unit] = []
            overlap_count = 0
            for u in reversed(current):
                if overlap and overlap_count + u.token_count > self.overlap_tokens:
                    break
                overlap.insert(0, u)
                overlap_count += u.token_count
                if overlap_count >= self.overlap_tokens:
                    break
            current = overlap
            current_tokens = overlap_count

        for unit in units:
            if current and current_tokens + unit.token_count > self.max_tokens:
                flush()
            current.append(unit)
            current_tokens += unit.token_count

        if current:
            # Avoid emitting an exact duplicate consisting only of the previous overlap.
            text = "\n\n".join(u.text for u in current)
            if not chunks or text != chunks[-1].text:
                chunks.append(
                    Chunk(
                        chunk_index=len(chunks),
                        page_start=min(u.page_number for u in current),
                        page_end=max(u.page_number for u in current),
                        token_count=self.count_tokens(text),
                        text=text,
                    )
                )

        return chunks

from __future__ import annotations

import time
import uuid
from collections import defaultdict

from .models import GraphRunResult
from .parser import ReferenceParser
from .quality import QualityPolicy
from .repository import GraphRepository
from .resolver import ReferenceResolver
from .types import NORMALIZER_VERSION, PARSER_VERSION, RESOLVER_VERSION, ResolutionStatus


class KnowledgeGraphPipeline:
    def __init__(
        self,
        repository: GraphRepository | None = None,
        parser: ReferenceParser | None = None,
        quality_policy: QualityPolicy | None = None,
    ):
        self.repository = repository or GraphRepository()
        self.parser = parser or ReferenceParser()
        self.quality_policy = quality_policy or QualityPolicy.from_csv()

    def run(self, *, auto_verify_enabled: bool, expected_documents: int = 61) -> tuple[GraphRunResult, dict[str, int]]:
        wall_started = time.perf_counter()
        documents, pages = self.repository.load_corpus()
        if len(documents) != expected_documents:
            raise ValueError(f"Expected exactly {expected_documents} controlled documents, found {len(documents)}")
        pages_by_document: dict[int, list[dict]] = defaultdict(list)
        for page in pages:
            pages_by_document[int(page["document_id"])].append(page)
        run_id = str(uuid.uuid4())
        self.repository.start_run(
            run_id, parser_version=PARSER_VERSION,
            normalizer_version=NORMALIZER_VERSION, resolver_version=RESOLVER_VERSION,
        )
        items = []
        try:
            parse_started = time.perf_counter()
            candidates = []
            for document in documents:
                candidates.extend(self.parser.parse_document(document, pages_by_document[int(document["id"])]))
            parser_seconds = time.perf_counter() - parse_started

            resolve_started = time.perf_counter()
            resolver = ReferenceResolver(documents, self.quality_policy, auto_verify_enabled=auto_verify_enabled)
            items = [(candidate, resolver.resolve(candidate)) for candidate in candidates]
            resolver_seconds = time.perf_counter() - resolve_started

            persist_started = time.perf_counter()
            persistence = self.repository.persist_run(run_id, documents, items)
            persistence_seconds = time.perf_counter() - persist_started
            resolved = sum(resolution.status in {ResolutionStatus.RESOLVED_EXACT, ResolutionStatus.RESOLVED_STRONG} for _, resolution in items)
            edge_count = self.repository.get_graph_summary()["references_edges"] + self.repository.get_graph_summary()["issued_by_edges"]
            self.repository.finish_run(
                run_id, status="SUCCESS", documents=len(documents), pages=len(pages),
                candidates=len(items), resolved=resolved, edges=edge_count,
                notes=f"auto_verify_enabled={auto_verify_enabled}",
            )
        except Exception as error:
            self.repository.finish_run(
                run_id, status="FAILED", documents=len(documents), pages=len(pages),
                candidates=len(items), resolved=0, edges=0, notes=str(error)[:1000],
            )
            raise
        result = GraphRunResult(
            run_id=run_id, documents_processed=len(documents), pages_processed=len(pages),
            candidate_count=len(items), resolved_count=resolved,
            reference_edge_count=self.repository.get_graph_summary()["references_edges"],
            issued_by_edge_count=self.repository.get_graph_summary()["issued_by_edges"],
            parser_seconds=parser_seconds, resolver_seconds=resolver_seconds,
            persistence_seconds=persistence_seconds, wall_seconds=time.perf_counter() - wall_started,
        )
        return result, persistence

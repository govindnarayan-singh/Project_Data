from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import date

from .models import GraphRunResult, ReferenceCandidate
from .normalization import normalize_document_number, normalize_legal_reference
from .parser import ReferenceParser
from .quality import QualityPolicy
from .repository import GraphRepository
from .resolver import ReferenceResolver
from .types import NORMALIZER_VERSION, PARSER_VERSION, RESOLVER_VERSION, ParserStatus, ReferenceKind, ResolutionStatus
from .validation import KnowledgeGraphValidationPolicy


class KnowledgeGraphPipeline:
    def __init__(
        self,
        repository: GraphRepository | None = None,
        parser: ReferenceParser | None = None,
        quality_policy: QualityPolicy | None = None,
        validation_policy: KnowledgeGraphValidationPolicy | None = None,
    ):
        self.repository = repository or GraphRepository()
        self.parser = parser or ReferenceParser()
        self.quality_policy = quality_policy or QualityPolicy.from_csv()
        self.validation_policy = validation_policy or KnowledgeGraphValidationPolicy.from_env()

    @staticmethod
    def _stored_candidate(row: dict) -> ReferenceCandidate:
        normalized_date = row.get("normalized_reference_date")
        if normalized_date and not isinstance(normalized_date, date):
            normalized_date = date.fromisoformat(str(normalized_date))
        return ReferenceCandidate(
            source_document_id=int(row["source_document_id"]),
            source_file=str(row["source_file"]),
            source_department=str(row["source_department"]),
            source_page=int(row["source_page"]),
            reference_kind=ReferenceKind(str(row["reference_kind"])),
            raw_evidence_text=str(row["raw_evidence_text"]),
            raw_document_number=str(row.get("raw_document_number") or ""),
            normalized_document_number=str(row.get("normalized_document_number") or ""),
            raw_reference_date=str(row.get("raw_reference_date") or ""),
            normalized_reference_date=normalized_date,
            date_parse_status=str(row.get("date_parse_status") or ""),
            raw_department=str(row.get("raw_department") or ""),
            normalized_department=str(row.get("normalized_department") or ""),
            pattern_id=str(row["pattern_id"]),
            parser_version=str(row["parser_version"]),
            parser_status=ParserStatus(str(row["parser_status"])),
            parser_notes=str(row.get("parser_notes") or ""),
            relation_signal=str(row.get("relation_signal") or ""),
            candidate_relation_type=str(row.get("candidate_relation_type") or ""),
            evidence_hash=str(row["evidence_hash"]),
            fingerprint=str(row["fingerprint"]),
        )

    def run_targeted(self, document_id: int, *, auto_verify_enabled: bool = True) -> dict[str, int | str]:
        """Parse one admitted document and re-resolve only identity-matching unresolved evidence."""
        documents, pages = self.repository.load_corpus()
        target = next((row for row in documents if int(row["id"]) == int(document_id)), None)
        if target is None:
            raise ValueError(f"Document {document_id} does not exist in the corpus")
        target_pages = [row for row in pages if int(row["document_id"]) == int(document_id)]
        run_id = str(uuid.uuid4())
        self.repository.start_run(
            run_id,
            parser_version=PARSER_VERSION,
            normalizer_version=NORMALIZER_VERSION,
            resolver_version=RESOLVER_VERSION,
        )
        try:
            new_candidates = self.parser.parse_document(target, target_pages)
            kind = str(target.get("document_kind") or target.get("document_type") or "UNKNOWN").upper()
            raw_identity = str(target.get("canonical_identity") or target.get("gr_number") or "")
            normalized_identity = (
                normalize_legal_reference(raw_identity, kind)
                if kind in {"ACT", "RULE"}
                else normalize_document_number(raw_identity)
            )
            stored_rows = self.repository.targeted_unresolved_candidates(normalized_identity)
            existing_candidates = [
                self._stored_candidate(row)
                for row in stored_rows
                if int(row["source_document_id"]) != int(document_id)
            ]
            resolver = ReferenceResolver(
                documents,
                self.quality_policy,
                auto_verify_enabled=auto_verify_enabled,
                validation_policy=KnowledgeGraphValidationPolicy(
                    self.validation_policy.mode,
                    reference_gate_passed=auto_verify_enabled,
                ),
            )
            items = [(candidate, resolver.resolve(candidate)) for candidate in [*new_candidates, *existing_candidates]]
            persistence = self.repository.persist_run(run_id, documents, items)
            resolved_statuses = {ResolutionStatus.RESOLVED_EXACT, ResolutionStatus.RESOLVED_STRONG}
            new_resolved = sum(resolution.status in resolved_statuses for _, resolution in items[: len(new_candidates)])
            existing_resolved = sum(resolution.status in resolved_statuses for _, resolution in items[len(new_candidates) :])
            still_unresolved = sum(resolution.status not in resolved_statuses for _, resolution in items)
            self.repository.finish_run(
                run_id,
                status="SUCCESS",
                documents=1,
                pages=len(target_pages),
                candidates=len(items),
                resolved=new_resolved + existing_resolved,
                edges=int(persistence["reference_edges_inserted"] + persistence["issued_by_edges_inserted"]),
                notes="Stage 3 targeted admission resolution; no vector-derived relations",
            )
            return {
                "run_id": run_id,
                "new_candidates": len(new_candidates),
                "new_resolved": new_resolved,
                "existing_reprocessed": len(existing_candidates),
                "existing_newly_resolved": existing_resolved,
                "still_unresolved": still_unresolved,
                **persistence,
            }
        except Exception as exc:
            self.repository.finish_run(
                run_id,
                status="FAILED",
                documents=1,
                pages=len(target_pages),
                candidates=0,
                resolved=0,
                edges=0,
                notes=f"{type(exc).__name__}: targeted Stage 3 processing failed"[:500],
            )
            raise

    def run(
        self,
        *,
        auto_verify_enabled: bool,
        expected_documents: int | None = None,
    ) -> tuple[GraphRunResult, dict[str, int]]:
        wall_started = time.perf_counter()
        documents, pages = self.repository.load_corpus()
        if expected_documents is not None and len(documents) != expected_documents:
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
            resolver = ReferenceResolver(
                documents, self.quality_policy, auto_verify_enabled=auto_verify_enabled,
                validation_policy=KnowledgeGraphValidationPolicy(
                    self.validation_policy.mode, reference_gate_passed=auto_verify_enabled
                ),
            )
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
                notes=f"auto_verify_enabled={auto_verify_enabled};review_mode={self.validation_policy.mode.value}",
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

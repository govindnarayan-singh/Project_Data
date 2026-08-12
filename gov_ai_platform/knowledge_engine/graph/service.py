from __future__ import annotations

from typing import Any

from .repository import GraphRepository


class GraphService:
    """Read-only application boundary used by Django and reporting."""

    def __init__(self, repository: GraphRepository | None = None):
        self.repository = repository or GraphRepository()

    def get_document_detail(self, document_id: int) -> dict[str, Any] | None:
        document = self.repository.get_document(document_id)
        if document is None:
            return None
        return {
            "document": document,
            "outgoing_references": self.get_outgoing_references(document_id),
            "incoming_references": self.get_incoming_references(document_id),
            "unresolved_references": self.get_unresolved_references(document_id),
            "issued_by": self.get_issued_by(document_id),
            "relation_signals": self.repository.get_relation_signals(document_id),
        }

    def get_outgoing_references(self, document_id: int) -> list[dict[str, Any]]:
        return self.repository.get_outgoing_references(document_id)

    def get_incoming_references(self, document_id: int) -> list[dict[str, Any]]:
        return self.repository.get_incoming_references(document_id)

    def get_unresolved_references(self, document_id: int) -> list[dict[str, Any]]:
        return self.repository.get_unresolved_references(document_id)

    def get_issued_by(self, document_id: int) -> dict[str, Any] | None:
        return self.repository.get_issued_by(document_id)

    def get_relation_evidence(self, relation_id: int) -> list[dict[str, Any]]:
        if not isinstance(relation_id, int) or relation_id <= 0:
            return []
        return self.repository.get_relation_evidence(relation_id)

    def get_graph_summary(self) -> dict[str, Any]:
        return self.repository.get_graph_summary()

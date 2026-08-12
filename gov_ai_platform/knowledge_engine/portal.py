from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.retrieval.repository import PostgresRetrievalRepository
from knowledge_engine.settings import PROJECT_ROOT, settings


PAGE_SIZE = 12
UNRESOLVED_PAGE_SIZE = 20


class PortalRepository(PostgresRetrievalRepository):
    """Bounded, read-only projections used by the Django portal."""

    def departments(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT department FROM documents ORDER BY department"
            ).fetchall()
        return [str(row["department"]) for row in rows]

    def years(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT extract(year FROM gr_date)::int AS year FROM documents WHERE gr_date IS NOT NULL ORDER BY year DESC"
            ).fetchall()
        return [int(row["year"]) for row in rows]

    def list_documents(
        self, *, query: str = "", department: str = "", year: int | None = None,
        sort: str = "date_desc", page: int = 1, page_size: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        orderings = {
            "date_desc": "d.gr_date DESC NULLS LAST, d.file_name",
            "date_asc": "d.gr_date ASC NULLS LAST, d.file_name",
            "department": "d.department, d.gr_date DESC NULLS LAST, d.file_name",
            "filename": "d.file_name",
        }
        order_by = orderings.get(sort, orderings["date_desc"])
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(d.file_name ILIKE %s OR coalesce(d.gr_number,'') ILIKE %s OR coalesce(d.subject,'') ILIKE %s)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        if department:
            clauses.append("d.department = %s")
            params.append(department)
        if year is not None:
            clauses.append("extract(year FROM d.gr_date)::int = %s")
            params.append(year)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        page = max(1, page)
        page_size = min(max(1, page_size), 50)
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT count(*) AS n FROM documents d{where}", params).fetchone()["n"])
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            rows = connection.execute(
                f"""SELECT d.id,d.file_name,d.department,d.document_type,d.gr_number,d.gr_date,d.subject,
                            d.page_count,d.digital_pages,d.ocr_pages,d.extraction_method,
                            count(c.id) AS chunk_count
                       FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                       {where} GROUP BY d.id ORDER BY {order_by} LIMIT %s OFFSET %s""",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pages": pages}

    def get_document_workspace(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.id,d.file_name,d.department,d.document_type,d.gr_number,d.gr_date,d.subject,
                          d.page_count,d.digital_pages,d.ocr_pages,d.extraction_method,d.metadata,d.created_at,
                          count(c.id) AS chunk_count
                     FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                    WHERE d.id=%s GROUP BY d.id""", (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_chunks(self, document_id: int, *, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        page = max(1, page)
        with self.connect() as connection:
            total = int(connection.execute("SELECT count(*) AS n FROM chunks WHERE document_id=%s", (document_id,)).fetchone()["n"])
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            rows = connection.execute(
                """SELECT id,chunk_index,page_start,page_end,token_count,text
                     FROM chunks WHERE document_id=%s ORDER BY chunk_index LIMIT %s OFFSET %s""",
                (document_id, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pages": pages}

    def corpus_health(self) -> dict[str, Any]:
        with self.connect() as connection:
            summary = dict(connection.execute(
                """SELECT (SELECT count(*) FROM documents) documents,
                          (SELECT count(*) FROM pages) pages,
                          (SELECT count(*) FROM chunks) chunks,
                          (SELECT count(*) FROM chunks) embeddings,
                          (SELECT count(DISTINCT department) FROM documents) departments,
                          (SELECT max(vector_dims(embedding)) FROM chunks) embedding_dimension,
                          (SELECT count(*) FROM chunks WHERE token_count > %s) chunks_over_limit,
                          (SELECT count(*) FROM chunks WHERE vector_dims(embedding) <> %s) invalid_embeddings,
                          (SELECT count(*) FROM (SELECT file_path FROM documents GROUP BY file_path HAVING count(*)>1) x) duplicate_paths,
                          (SELECT count(*) FROM (SELECT sha256 FROM documents GROUP BY sha256 HAVING count(*)>1) x) duplicate_sha,
                          (SELECT coalesce(sum(digital_pages),0) FROM documents) digital_pages,
                          (SELECT coalesce(sum(ocr_pages),0) FROM documents) ocr_pages""",
                (settings.chunk_max_tokens, settings.embed_dim),
            ).fetchone())
            extraction = [dict(row) for row in connection.execute(
                "SELECT extraction_method,count(*) AS count FROM pages GROUP BY extraction_method ORDER BY count DESC"
            ).fetchall()]
        summary["status"] = "PASS" if not any(summary[key] for key in (
            "chunks_over_limit", "invalid_embeddings", "duplicate_paths", "duplicate_sha"
        )) else "REVIEW"
        return {"summary": summary, "extraction": extraction}

    def list_unresolved_groups(self, *, page: int = 1, page_size: int = UNRESOLVED_PAGE_SIZE) -> dict[str, Any]:
        page = max(1, page)
        group = "c.normalized_document_number,c.reference_kind,c.normalized_reference_date"
        where = "c.resolution_status='NOT_IN_CORPUS'"
        with self.connect() as connection:
            total = int(connection.execute(
                f"SELECT count(*) AS n FROM (SELECT 1 FROM kg_reference_candidates c WHERE {where} GROUP BY {group}) g"
            ).fetchone()["n"])
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            rows = connection.execute(
                f"""SELECT min(c.id) AS candidate_id,c.normalized_document_number,c.reference_kind,
                            c.normalized_reference_date,count(*) AS mentions,
                            count(DISTINCT c.source_document_id) AS source_documents,
                            min(d.department) AS source_department,
                            max(c.confidence_category) AS confidence_category
                       FROM kg_reference_candidates c JOIN documents d ON d.id=c.source_document_id
                      WHERE {where} GROUP BY {group}
                      ORDER BY count(DISTINCT c.source_document_id) DESC,count(*) DESC,min(c.id)
                      LIMIT %s OFFSET %s""", (page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "pages": pages}

    def get_unresolved_group(self, candidate_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            key = connection.execute(
                """SELECT normalized_document_number,reference_kind,normalized_reference_date
                     FROM kg_reference_candidates WHERE id=%s AND resolution_status='NOT_IN_CORPUS'""",
                (candidate_id,),
            ).fetchone()
            if not key:
                return None
            rows = connection.execute(
                """SELECT c.id AS candidate_id,c.source_page,c.raw_evidence_text,c.raw_document_number,
                          c.normalized_document_number,c.reference_kind,c.normalized_reference_date,
                          c.resolution_status,c.validation_status,c.confidence_category,
                          d.id AS source_document_id,d.file_name AS source_file,d.department AS source_department
                     FROM kg_reference_candidates c JOIN documents d ON d.id=c.source_document_id
                    WHERE c.resolution_status='NOT_IN_CORPUS'
                      AND c.normalized_document_number IS NOT DISTINCT FROM %s
                      AND c.reference_kind IS NOT DISTINCT FROM %s
                      AND c.normalized_reference_date IS NOT DISTINCT FROM %s
                    ORDER BY d.file_name,c.source_page,c.id""",
                (key["normalized_document_number"], key["reference_kind"], key["normalized_reference_date"]),
            ).fetchall()
        items = [dict(row) for row in rows]
        return {"target": dict(key), "items": items, "mentions": len(items),
                "source_documents": len({row["source_document_id"] for row in items})}


class PortalService:
    def __init__(self, repository: PortalRepository | None = None, graph_repository: GraphRepository | None = None):
        self.repository = repository or PortalRepository()
        self.graph_repository = graph_repository or GraphRepository()

    def corpus_dashboard(self) -> dict[str, Any]:
        data = self.repository.corpus_health()
        data["graph"] = self.graph_repository.get_graph_summary()
        validation = self._read_json(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "final_validation.json")
        data["artifact_integrity"] = validation.get("integrity", {})
        data["generated_at"] = validation.get("generated_at")
        return data

    def evaluation_dashboard(self) -> dict[str, Any]:
        regression = self._read_json(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "retrieval_regression.json")
        validation = self._read_json(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "final_validation.json")
        return {"retrieval": regression, "parser": validation.get("parser_evaluation", {}),
                "generated_at": validation.get("generated_at")}

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from knowledge_engine.graph.closure import build_closure_snapshot
from knowledge_engine.graph.repository import GraphRepository
from knowledge_engine.settings import PROJECT_ROOT


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_closure_metrics_v1"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _before_candidates(repository: GraphRepository) -> list[dict[str, Any]]:
    prior = _read_csv(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "reference_resolution.csv")
    current = {int(row["id"]): row for row in repository.all_candidates()}
    local_fields = (
        "resolved_target_catalog_id",
        "local_resolution_status",
        "local_resolution_method",
        "local_resolution_evidence",
        "local_validation_status",
        "local_ingestion_eligibility",
    )
    for row in prior:
        present = current.get(int(row["id"]))
        if present:
            row.update({field: present.get(field) for field in local_fields})
    return prior


def run(output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    repository = GraphRepository()
    before_candidates = _before_candidates(repository)
    before_relations = _read_csv(PROJECT_ROOT / "outputs" / "knowledge_graph_v1" / "graph_edges.csv")
    before = build_closure_snapshot(before_candidates, before_relations, total_documents=61)
    after = build_closure_snapshot(repository.all_candidates(), repository.all_edges(), total_documents=66)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    summary = {
        "generated_at": generated_at,
        "before": before["summary"],
        "after": after["summary"],
    }
    (output_dir / "closure_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_csv(output_dir / "reference_mention_closure.csv", after["mention_rows"])
    _write_csv(output_dir / "target_closure.csv", after["target_rows"])
    metrics = (
        "reference_mention_closure",
        "referenced_target_closure",
        "local_availability_closure",
        "known_availability_closure",
        "graph_document_coverage",
    )
    lines = [
        "# Knowledge Graph closure metrics V1",
        "",
        f"Generated: {generated_at}",
        "",
        "The mention denominator includes only parseable explicit target-bearing candidates. "
        "SELF_REFERENCE_REJECTED, UNPARSEABLE, missing identities, and any other ineligible statuses "
        "are reported explicitly and excluded.",
        "",
        "| Metric | Before | After |",
        "|---|---:|---:|",
    ]
    labels = {
        "reference_mention_closure": "Reference Mention Closure",
        "referenced_target_closure": "Referenced Target Closure",
        "local_availability_closure": "Local Availability Closure",
        "known_availability_closure": "Known Availability Closure",
        "graph_document_coverage": "Graph Document Coverage",
    }
    for key in metrics:
        old, new = before["summary"][key], after["summary"][key]
        lines.append(
            f"| {labels[key]} | {old['numerator']}/{old['denominator']} ({old['percentage']:.2%}) "
            f"| {new['numerator']}/{new['denominator']} ({new['percentage']:.2%}) |"
        )
    lines.extend(
        [
            "",
            "## Exact denominator definitions",
            "",
            after["summary"]["mention_denominator_definition"],
            "",
            after["summary"]["target_identity_definition"],
            "",
            f"Before exclusions: `{json.dumps(before['summary']['excluded_mentions'], ensure_ascii=False, sort_keys=True)}`",
            "",
            f"After exclusions: `{json.dumps(after['summary']['excluded_mentions'], ensure_ascii=False, sort_keys=True)}`",
            "",
            "ISSUED_BY is excluded from Graph Document Coverage.",
        ]
    )
    (output_dir / "coverage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

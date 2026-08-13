from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase
from pypdf import PdfWriter

from knowledge_engine.acquisition.groups import consolidate_unresolved
from knowledge_engine.acquisition.inventory import UnsafeCorpusPath, build_inventory, resolve_inside_root
from knowledge_engine.acquisition.manifest import select_manifest, validate_manifest
from knowledge_engine.acquisition.matcher import resolve_groups
from knowledge_engine.acquisition.models import AcquisitionMatch, LocalPdf, MetadataProbe


def unresolved(identifier="ABC/2020/1", **changes):
    row = {
        "resolution_status": "NOT_IN_CORPUS", "normalized_document_number": identifier,
        "raw_document_number": identifier, "reference_kind": "GOVERNMENT_RESOLUTION",
        "normalized_reference_date": date(2020, 1, 2), "source_department": "Finance",
        "source_document_id": 1, "source_file": "source.pdf", "source_page": 2,
        "candidate_relation_type": "", "blocked_by_page_quality": False,
    }
    row.update(changes)
    return row


def probe(path="Finance/target.pdf", identifier="abc/2020/1", *, digest="a" * 64, when=date(2020, 1, 2), department="Finance", error=""):
    return MetadataProbe(path, department, Path(path).name, digest, 1, 1, "DIGITAL_FIRST_PAGES",
                         identifier, identifier, when, "Government Resolution", "Subject", 100, error)


class UnresolvedConsolidationTests(SimpleTestCase):
    def test_spacing_and_separator_variants_consolidate_without_fuzzy_merging(self):
        rows = [unresolved("अभियान2021/प्र.क्र.57/तां.क.1"), unresolved("अभियान-2021/प्र.क्र.57/तां.क.1", source_page=3)]
        groups = consolidate_unresolved(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].mentions, 2)
        self.assertEqual(len(consolidate_unresolved([*rows, unresolved("अभियान-2021/प्र.क्र.58/तां.क.1")])), 2)

    def test_self_rejected_and_unparseable_are_not_acquisition_groups(self):
        rows = [unresolved(), unresolved(resolution_status="SELF_REFERENCE_REJECTED"), unresolved("", resolution_status="UNPARSEABLE")]
        self.assertEqual(len(consolidate_unresolved(rows)), 1)


class LocalInventoryTests(SimpleTestCase):
    def _pdf(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        with path.open("wb") as handle: writer.write(handle)

    def test_inventory_marks_ingested_and_deduplicates_by_sha_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "Finance" / "a.pdf", root / "Finance" / "b.pdf"
            self._pdf(first); second.write_bytes(first.read_bytes())
            items = build_inventory(root, ingested_paths={"Finance/a.pdf"}, ingested_sha=set(), workers=1)
            self.assertEqual(len(items), 2)
            self.assertTrue(next(item for item in items if item.file_name == "a.pdf").already_ingested)
            self.assertEqual(len({item.sha256 for item in items}), 1)

    def test_path_safety_rejects_outside_root_and_non_pdf(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "root"; root.mkdir()
            outside = Path(directory) / "outside.pdf"; self._pdf(outside)
            text = root / "x.txt"; text.write_text("x")
            with self.assertRaises(UnsafeCorpusPath): resolve_inside_root(root, outside)
            with self.assertRaises(UnsafeCorpusPath): resolve_inside_root(root, text)


class AcquisitionMatcherTests(SimpleTestCase):
    def group(self, **changes):
        row = unresolved(**changes)
        return consolidate_unresolved([row])[0]

    def test_exact_strong_ambiguous_not_found_and_blocked(self):
        exact = resolve_groups([self.group()], [probe()])[0]
        self.assertEqual(exact.status, "MATCH_EXACT")
        strong = resolve_groups([self.group(normalized_reference_date=None, source_department="Housing")], [probe(when=None)])[0]
        self.assertEqual(strong.status, "MATCH_STRONG")
        ambiguous = resolve_groups([self.group()], [probe(), probe("Finance/other.pdf", digest="b" * 64)])[0]
        self.assertEqual(ambiguous.status, "AMBIGUOUS")
        self.assertEqual(resolve_groups([self.group()], [])[0].status, "NOT_FOUND")
        blocked = resolve_groups([self.group(blocked_by_page_quality=True)], [])[0]
        self.assertEqual(blocked.status, "BLOCKED_QUALITY")

    def test_date_conflict_is_ambiguous_and_quality_error_blocks(self):
        self.assertEqual(resolve_groups([self.group()], [probe(when=date(2021, 1, 1))])[0].status, "AMBIGUOUS")
        self.assertEqual(resolve_groups([self.group(normalized_reference_date=None)], [probe(when=None, error="NO_DIGITAL_TEXT")])[0].status, "BLOCKED_QUALITY")

    def test_priority_is_transparent_and_rewards_citation_count_and_signal(self):
        low = self.group(normalized_reference_date=None)
        high = consolidate_unresolved([
            unresolved("XYZ/2021/9", candidate_relation_type="AMENDS"),
            unresolved("XYZ/2021/9", candidate_relation_type="AMENDS", source_document_id=2, source_file="two.pdf"),
        ])[0]
        results = resolve_groups([low, high], [probe(), probe("Finance/high.pdf", "xyz/2021/9", digest="b" * 64, when=None)])
        scores = {item.group.group_id: item.priority_score for item in results}
        self.assertGreater(scores[high.group_id], scores[low.group_id])


class ManifestTests(SimpleTestCase):
    def test_selection_is_deterministic_unique_and_excludes_ambiguous(self):
        group = consolidate_unresolved([unresolved()])[0]
        exact = AcquisitionMatch(group, "MATCH_EXACT", (probe(),), "EXACT", "evidence", "HIGH", 900)
        ambiguous = AcquisitionMatch(group, "AMBIGUOUS", (probe("Finance/x.pdf", digest="b" * 64),), priority_score=999)
        self.assertEqual(select_manifest([ambiguous, exact]), select_manifest([exact, ambiguous]))
        self.assertEqual(len(select_manifest([exact, exact])), 1)

    def test_manifest_validation_detects_existing_and_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); path = root / "Finance" / "a.pdf"; path.parent.mkdir()
            writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
            with path.open("wb") as handle: writer.write(handle)
            rows = [{"file_path": "Finance/a.pdf"}, {"file_path": "Finance/a.pdf"}]
            result = validate_manifest(rows, root=root, ingested_paths={"Finance/a.pdf"}, ingested_sha=set())
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["duplicate_paths"], 1)
            self.assertEqual(result["already_ingested"], 2)

import unittest

from src.evaluate_retrieval import (
    aggregate_results,
    first_matching_rank,
    is_document_hit,
    is_page_hit,
    mean_reciprocal_rank,
    page_range_overlaps,
    parse_expected_pages,
    recall_at_k,
    reciprocal_rank,
)


class RetrievalEvaluationTests(unittest.TestCase):
    def test_expected_page_parser(self):
        self.assertEqual(parse_expected_pages("1"), {1})
        self.assertEqual(parse_expected_pages("1-3"), {1, 2, 3})
        self.assertEqual(parse_expected_pages("2,4,7-9"), {2, 4, 7, 8, 9})

    def test_expected_page_parser_rejects_invalid_ranges(self):
        for value in ("", "0", "3-1", "1-2-3", "2,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_expected_pages(value)

    def test_page_range_overlap(self):
        self.assertTrue(page_range_overlaps(2, 3, {3, 4}))
        self.assertFalse(page_range_overlaps(1, 2, {3, 4}))

    def test_document_and_page_hits(self):
        row = {"file_name": "expected.pdf", "page_start": 2, "page_end": 3}
        self.assertTrue(is_document_hit(row, "expected.pdf"))
        self.assertFalse(is_document_hit(row, "other.pdf"))
        self.assertTrue(is_page_hit(row, "expected.pdf", {3, 4}))
        self.assertFalse(is_page_hit(row, "expected.pdf", {4}))
        self.assertFalse(is_page_hit(row, "other.pdf", {3}))

    def test_recall_at_k(self):
        ranks = [1, 3, None, 7]
        self.assertEqual(recall_at_k(ranks, 1), 0.25)
        self.assertEqual(recall_at_k(ranks, 3), 0.5)
        self.assertEqual(recall_at_k(ranks, 10), 0.75)

    def test_reciprocal_rank_and_mrr(self):
        self.assertEqual(reciprocal_rank(1), 1.0)
        self.assertEqual(reciprocal_rank(2), 0.5)
        self.assertEqual(reciprocal_rank(None), 0.0)
        self.assertAlmostEqual(mean_reciprocal_rank([1, 2, None]), 0.5)

    def test_first_matching_rank(self):
        self.assertEqual(first_matching_rank([False, True, True]), 2)
        self.assertIsNone(first_matching_rank([False, False]))

    def test_result_aggregation(self):
        metrics = aggregate_results([1, 3, None], [2, None, 5])
        self.assertEqual(metrics["question_count"], 3)
        self.assertAlmostEqual(metrics["document"]["recall_at_1"], 1 / 3)
        self.assertAlmostEqual(metrics["document"]["recall_at_3"], 2 / 3)
        self.assertAlmostEqual(metrics["document"]["mrr"], (1 + 1 / 3) / 3)
        self.assertAlmostEqual(metrics["page"]["recall_at_5"], 2 / 3)


if __name__ == "__main__":
    unittest.main()

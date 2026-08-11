import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.run_questions import (
    DEFAULT_TOP_K,
    QuestionInput,
    build_output_row,
    load_questions,
    process_questions,
    write_answers_atomic,
)


def result(file_name="result.pdf", text="मराठी पुरावा", similarity=0.75):
    return {
        "file_name": file_name,
        "gr_number": "जीआर-१",
        "gr_date": date(2024, 8, 6),
        "department": "Environment Department",
        "page_start": 2,
        "page_end": 3,
        "similarity": similarity,
        "text": text,
    }


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, show_progress_bar=False):
        self.calls.append(list(texts))
        return [f"vector:{text}" for text in texts]


class FakeDatabase:
    def dense_search(self, vector, top_k=5, department=None):
        if "अयशस्वी" in vector:
            raise RuntimeError("simulated database error")
        return [result(file_name=f"{department or 'all'}.pdf")][:top_k]


class RunQuestionsTests(unittest.TestCase):
    def _csv(self, text):
        directory = TemporaryDirectory()
        path = Path(directory.name) / "questions.csv"
        path.write_text(text, encoding="utf-8-sig")
        self.addCleanup(directory.cleanup)
        return path

    def test_minimal_csv_generates_ids_and_defaults(self):
        path = self._csv('question\n"पहिला प्रश्न"\n"दुसरा प्रश्न"\n')
        questions, skipped = load_questions(path)
        self.assertEqual(skipped, 0)
        self.assertEqual([item.question_id for item in questions], ["Q001", "Q002"])
        self.assertTrue(all(item.top_k == DEFAULT_TOP_K for item in questions))
        self.assertTrue(all(item.department is None for item in questions))

    def test_blank_questions_are_skipped(self):
        path = self._csv('question\n""\n"   "\n"वाचनीय प्रश्न"\n')
        questions, skipped = load_questions(path)
        self.assertEqual(skipped, 2)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].question_id, "Q001")

    def test_optional_id_top_k_and_department(self):
        path = self._csv(
            'question,question_id,top_k,department\n'
            '"प्रश्न","CLIENT-7",3,"Finance Department"\n'
        )
        questions, _ = load_questions(path)
        self.assertEqual(questions[0].question_id, "CLIENT-7")
        self.assertEqual(questions[0].top_k, 3)
        self.assertEqual(questions[0].department, "Finance Department")

    def test_invalid_top_k_is_a_row_error(self):
        path = self._csv('question,top_k\n"प्रश्न","many"\n')
        questions, _ = load_questions(path)
        self.assertIn("positive integer", questions[0].validation_error)

    def test_output_row_contains_retrieval_finding(self):
        item = QuestionInput("Q001", "प्रश्न", top_k=5)
        row = build_output_row(item, [result()], result_slots=5)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["best_file"], "result.pdf")
        self.assertEqual(row["best_gr_date"], "2024-08-06")
        self.assertEqual(row["best_pages"], "2-3")
        self.assertEqual(row["best_finding"], "मराठी पुरावा")
        self.assertEqual(row["result_1_text"], "मराठी पुरावा")

    def test_utf8_sig_round_trip_preserves_marathi(self):
        item = QuestionInput("Q001", "मराठी प्रश्न")
        row = build_output_row(item, [result()], result_slots=5)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "answers.csv"
            write_answers_atomic(path, [row], 5)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                stored = next(csv.DictReader(handle))
        self.assertEqual(stored["question"], "मराठी प्रश्न")
        self.assertEqual(stored["best_finding"], "मराठी पुरावा")

    def test_one_search_failure_does_not_stop_remaining_questions(self):
        questions = [
            QuestionInput("Q001", "पहिला प्रश्न"),
            QuestionInput("Q002", "अयशस्वी प्रश्न"),
            QuestionInput("Q003", "तिसरा प्रश्न", department="Finance Department"),
        ]
        embedder = FakeEmbedder()
        rows = process_questions(questions, embedder, FakeDatabase())
        self.assertEqual([row["status"] for row in rows], ["OK", "ERROR", "OK"])
        self.assertIn("simulated database error", rows[1]["error"])
        self.assertEqual(rows[2]["best_file"], "Finance Department.pdf")
        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(len(embedder.calls[0]), 3)

    def test_missing_question_column_is_rejected(self):
        path = self._csv("prompt\nwrong heading\n")
        with self.assertRaisesRegex(ValueError, "question"):
            load_questions(path)


if __name__ == "__main__":
    unittest.main()

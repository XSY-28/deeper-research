import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from deeper_research.storage import Store, BudgetExceeded


def spec(effort="low"):
    return {"question": "What is supported?", "criteria": ["Cite evidence"],
            "source_ids": ["s1"], "effort": effort,
            "profile": {"model": "test", "protocol": "openai_chat"}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Decimal("10"))

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, task):
        return self.store.reserve_call(task, "solve", [{"role": "user", "content": "q"}],
                                       1000, 1000, Decimal("0.3"), Decimal("1.2"))

    def test_dispatched_unknown_call_still_consumes_low_budget_after_restart(self):
        task = self.store.create_task(spec())
        call = self.reserve(task)
        self.store.mark_dispatched(call)
        restarted = Store(self.root, Decimal("10"))
        restarted.recover(task)
        self.assertEqual(restarted.call(call)["status"], "outcome_unknown")
        with self.assertRaises(BudgetExceeded):
            self.reserve(task)

    def test_project_reservation_spans_tasks_and_restarts(self):
        root = self.root / "small"
        first = Store(root, Decimal("0.002"))
        one = first.create_task(spec())
        first.reserve_call(one, "solve", [], 1000, 1000, Decimal("0.3"), Decimal("1.2"))
        second = Store(root, Decimal("10"))
        two = second.create_task(spec())
        with self.assertRaises(BudgetExceeded):
            second.reserve_call(two, "solve", [], 1000, 1000, Decimal("0.3"), Decimal("1.2"))
        self.assertEqual(second.budget()["limit_usd"], "0.002")

    def test_actual_usage_releases_only_unused_reservation(self):
        task = self.store.create_task(spec())
        call = self.reserve(task)
        self.store.mark_dispatched(call)
        self.store.complete_call(call, {"content": "answer", "input_tokens": 100,
            "output_tokens": 200, "total_tokens": 300, "raw_usage": {}, "model": "test", "request_id": "r1"})
        self.assertEqual(self.store.budget()["accounted_usd"], "0.00027")
        self.assertEqual(self.store.call(call)["tokens"], 300)

    def test_artifact_content_is_immutable_and_checked(self):
        task = self.store.create_task(spec())
        path = self.store.save_artifact(task, "candidate", {"answer": "original"})
        self.assertEqual(self.store.read_artifact(path)["answer"], "original")
        (self.root / path).write_text('{"answer":"changed"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.read_artifact(path)

    def test_task_parameters_are_frozen_and_paths_checked(self):
        original = spec()
        task = self.store.create_task(original)
        original["criteria"].append("Changed")
        self.assertEqual(self.store.task(task)["spec"]["criteria"], ["Cite evidence"])
        with self.assertRaises(ValueError):
            self.store.task("../../outside")


if __name__ == "__main__":
    unittest.main()

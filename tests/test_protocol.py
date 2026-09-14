from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from codemap.core import ProtocolError, apply_batch, claim_task, completion_report, recover_expired, validate_graph
from codemap.store import GraphStore
from examples.build_example import BASE, make_batch, make_graph


class GraphContractTests(unittest.TestCase):
    def setUp(self):
        self.graph = make_graph()

    def test_example_evidence_matches_real_fixture_files(self):
        validate_graph(self.graph, BASE / "sample_repo")
        report = completion_report(self.graph)
        self.assertEqual(report["status"], "partial")
        self.assertIn("task:diagnostics", report["outstanding_tasks"])
        self.assertIn("fn:test", report["incomplete_entities"])

    def test_malformed_direction_is_rejected(self):
        self.graph["ports"][1]["direction"] = "out"
        with self.assertRaisesRegex(ProtocolError, "output to input"):
            validate_graph(self.graph)

    def test_other_callsite_cannot_receive_first_calls_return(self):
        claimed = claim_task(self.graph, "task:link", "reader", expected_revision=0, now=100)
        batch = make_batch(claimed)
        second = deepcopy(claimed["contexts"][1])
        second["id"] = "ctx:other-call"
        claimed["contexts"].append(second)
        batch["upserts"]["relations"][0]["context_id"] = second["id"]
        with self.assertRaisesRegex(ProtocolError, "unrelated call contexts"):
            apply_batch(claimed, batch, now=101)

    def test_wrong_callee_rejected(self):
        self.graph["relations"][0]["to_id"] = "fn:measure"
        with self.assertRaisesRegex(ProtocolError, "callsite target mismatch"):
            validate_graph(self.graph)

    def test_explicit_control_relation_preserves_evidence_and_cannot_use_data_ports(self):
        relation = deepcopy(self.graph['relations'][0])
        relation.update(id='control:test', kind='control')
        self.graph['relations'].append(relation)
        validate_graph(self.graph)
        relation['from_id'] = self.graph['ports'][0]['id']
        with self.assertRaises(ProtocolError):
            validate_graph(self.graph)

    def test_duplicate_ids_and_missing_references_rejected(self):
        self.graph["entities"].append(deepcopy(self.graph["entities"][0]))
        with self.assertRaisesRegex(ProtocolError, "duplicate id"):
            validate_graph(self.graph)
        self.graph = make_graph()
        self.graph["relations"][1]["to_id"] = "missing"
        with self.assertRaisesRegex(ProtocolError, "unknown data port"):
            validate_graph(self.graph)

    def test_cycles_rejected_in_grouping_and_derivation(self):
        self.graph["memberships"].append({"id": "loop", "parent_id": "dir:src", "child_id": "repo:sample", "axis": "physical"})
        with self.assertRaisesRegex(ProtocolError, "cycle"):
            validate_graph(self.graph)
        self.graph = make_graph()
        self.graph["flows"][0]["derived_from"] = ["flow:next"]
        with self.assertRaisesRegex(ProtocolError, "cycle"):
            validate_graph(self.graph)

    def test_reviewed_symbol_needs_evidence_and_function_details(self):
        self.graph["entities"][5]["details"].pop("writes")
        with self.assertRaisesRegex(ProtocolError, "function-level record is incomplete"):
            validate_graph(self.graph)

    def test_malformed_function_details_report_a_protocol_error(self):
        self.graph["entities"][5]["details"] = []
        with self.assertRaisesRegex(ProtocolError, "details must be an object"):
            validate_graph(self.graph)

    def test_unresolved_edge_needs_reason(self):
        self.graph["relations"][0]["basis"] = "unresolved"
        with self.assertRaisesRegex(ProtocolError, "uncertainty reason"):
            validate_graph(self.graph)

    def test_stale_source_and_out_of_range_evidence_rejected(self):
        self.graph["evidence"][0]["end_line"] = 2000
        with self.assertRaisesRegex(ProtocolError, "exceeds file"):
            validate_graph(self.graph, BASE / "sample_repo")
        self.graph = make_graph()
        self.graph["sources"][0]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ProtocolError, "snapshot changed"):
            validate_graph(self.graph, BASE / "sample_repo")

    def test_source_path_cannot_escape_repository(self):
        self.graph["sources"][0]["path"] = "../outside.cpp"
        with self.assertRaisesRegex(ProtocolError, "invalid source path"):
            validate_graph(self.graph)


class BatchProtocolTests(unittest.TestCase):
    def setUp(self):
        self.graph = claim_task(make_graph(), "task:link", "reader", expected_revision=0, now=100, lease_seconds=30)
        self.batch = make_batch(self.graph)

    def test_commit_retry_is_idempotent_and_keeps_unrelated_work(self):
        committed = apply_batch(self.graph, self.batch, now=101)
        replay = apply_batch(committed, self.batch, now=1000)
        self.assertEqual(committed, replay)
        self.assertEqual(committed["project"]["revision"], 2)
        self.assertEqual(len(committed["relations"]), 5)
        self.assertIn("task:diagnostics", completion_report(committed)["outstanding_tasks"])

    def test_batch_id_cannot_be_reused_for_different_result(self):
        committed = apply_batch(self.graph, self.batch, now=101)
        self.batch["reason"] = "changed"
        with self.assertRaisesRegex(ProtocolError, "different content"):
            apply_batch(committed, self.batch, now=102)

    def test_old_revision_cannot_overwrite_new_graph(self):
        self.batch["base_revision"] = 0
        with self.assertRaisesRegex(ProtocolError, "stale project revision"):
            apply_batch(self.graph, self.batch, now=101)

    def test_expired_execution_cannot_commit_after_reassignment(self):
        recovered = recover_expired(self.graph, expected_revision=1, now=131)
        newer = claim_task(recovered, "task:link", "new reader", expected_revision=2, now=132)
        self.batch["base_revision"] = 3
        with self.assertRaisesRegex(ProtocolError, "stale execution lease"):
            apply_batch(newer, self.batch, now=133)
        self.assertEqual(newer["tasks"][0]["attempt"], 2)

    def test_partial_result_requeues_remaining_work(self):
        self.batch.update(result="partial", reason="Still need to check the second callsite.")
        committed = apply_batch(self.graph, self.batch, now=101)
        self.assertEqual(committed["tasks"][0]["state"], "queued")
        self.assertIsNone(committed["tasks"][0]["lease"])

    def test_blocked_work_is_not_completed(self):
        self.batch.update(result="blocked", reason="Missing build configuration.")
        committed = apply_batch(self.graph, self.batch, now=101)
        self.assertIn("task:link", completion_report(committed)["outstanding_tasks"])

    def test_batch_cannot_silently_exclude_source(self):
        source = deepcopy(self.graph["sources"][1])
        source.update(included=False, reason="too large")
        self.batch["upserts"]["sources"] = [source]
        with self.assertRaisesRegex(ProtocolError, "narrow scope"):
            apply_batch(self.graph, self.batch, now=101)

    def test_inventory_and_unreviewed_entities_prevent_completion(self):
        for task in self.graph["tasks"]:
            task.update(state="done", lease=None)
        report = completion_report(self.graph)
        self.assertEqual(report["status"], "partial")
        self.graph["project"]["inventory_complete"] = False
        self.assertEqual(completion_report(self.graph)["status"], "inventory_incomplete")

    def test_analysis_task_cannot_finish_with_unread_scope(self):
        claimed = claim_task(make_graph(), "task:diagnostics", "reader", expected_revision=0, now=100)
        task = next(t for t in claimed["tasks"] if t["id"] == "task:diagnostics")
        batch = deepcopy(self.batch)
        batch.update(task_id=task["id"], lease_id=task["lease"]["id"], upserts={})
        with self.assertRaisesRegex(ProtocolError, "task completion requires this scope node status"):
            apply_batch(claimed, batch, now=101)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GraphStore(Path(self.temp.name) / "map.sqlite")
        self.store.initialize(make_graph())

    def test_two_claims_have_one_winner(self):
        def claim(worker):
            try:
                self.store.claim("task:link", worker, expected_revision=0, now=100)
                return "claimed"
            except ProtocolError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["one", "two"]))
        self.assertCountEqual(results, ["claimed", "rejected"])

    def test_invalid_commit_rolls_back_graph_and_task_together(self):
        claimed = self.store.claim("task:link", "reader", expected_revision=0, now=100)
        batch = make_batch(claimed)
        batch["upserts"]["relations"][0]["to_id"] = "missing"
        with self.assertRaises(ProtocolError):
            self.store.commit(batch, now=101)
        self.assertEqual(self.store.read(), claimed)

    def test_layout_and_color_survive_analysis_commit_and_reopen(self):
        view = {"positions": {"fn:move": {"x": 60, "y": 90, "locked": True}}, "colors": {"D1": "blue"}, "notes": ["user note"]}
        self.store.save_view("main", view)
        claimed = self.store.claim("task:link", "reader", expected_revision=0, now=100)
        self.store.commit(make_batch(claimed), now=101)
        reopened = GraphStore(self.store.path)
        self.assertEqual(reopened.read_view("main"), view)
        self.assertEqual(reopened.read()["project"]["revision"], 2)

    def test_pause_stops_claims_and_resume_keeps_queue(self):
        self.store.set_paused(True, expected_revision=0)
        with self.assertRaisesRegex(ProtocolError, "paused"):
            self.store.claim("task:link", "reader", expected_revision=1, now=100)
        self.store.set_paused(False, expected_revision=1)
        claimed = self.store.claim("task:link", "reader", expected_revision=2, now=100)
        self.assertEqual(claimed["tasks"][0]["state"], "running")


if __name__ == "__main__":
    unittest.main()

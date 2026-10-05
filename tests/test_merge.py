import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_case(self, dedupe, source="email", product="DrugA", patient="P-1"):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": patient, "region": "CN", "product": product, "event_term": "肝损伤",
             "source": source, "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def merge(self, source_id, master_id, actor="admin-1", role="global_admin"):
        return self.svc.merge_cases(source_id, actor, role, {"target_case_id": master_id})

    def test_merge_moves_intakes_and_followups(self):
        master = self.make_case("m-1", source="email")
        source = self.make_case("m-2", source="fax")
        # A follow-up on the source must move onto the master.
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "传真随访", "source": "fax", "expected_revision": 1,
                               "received_at": iso(utcnow())})
        result = self.merge(source["id"], master["id"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["merge"]["status"], "completed")

        src = self.svc._case(self.svc.repo.conn, source["id"])
        self.assertEqual(src["status"], "merged")
        self.assertEqual(src["merged_into"], master["id"])

        detail = self.svc.get_case(master["id"], "global_admin", "")
        # Both intakes now hang under the master.
        self.assertEqual({i["source"] for i in detail["intakes"]}, {"email", "fax"})
        # The source follow-up moved onto the master, renumbered past the master's revision.
        self.assertEqual(len(detail["followups"]), 1)
        self.assertEqual(detail["followups"][0]["content"], "传真随访")
        self.assertGreater(detail["followups"][0]["revision"], master["revision"])

        merge = self.svc.get_merge(result["merge"]["id"], "global_admin")
        kinds = {(it["kind"], it["source_case_id"]) for it in merge["items"]}
        self.assertIn(("intake", source["id"]), kinds)
        self.assertIn(("followup", source["id"]), kinds)

    def test_undo_restores_intakes_and_followups(self):
        master = self.make_case("m-1", source="email")
        source = self.make_case("m-2", source="fax")
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "传真随访", "source": "fax", "expected_revision": 1,
                               "received_at": iso(utcnow())})
        result = self.merge(source["id"], master["id"])
        merge_id = result["merge"]["id"]

        undo = self.svc.undo_merge(merge_id, "admin-1", "global_admin")
        self.assertFalse(undo["idempotent"])
        self.assertEqual(undo["merge"]["status"], "undone")

        src = self.svc._case(self.svc.repo.conn, source["id"])
        self.assertEqual(src["status"], "open")
        self.assertIsNone(src["merged_into"])

        # Intakes and follow-ups are back on the source with their original revision.
        src_detail = self.svc.get_case(source["id"], "global_admin", "")
        self.assertEqual(len(src_detail["intakes"]), 1)
        self.assertEqual(src_detail["intakes"][0]["source"], "fax")
        self.assertEqual(len(src_detail["followups"]), 1)
        self.assertEqual(src_detail["followups"][0]["revision"], 2)

        master_detail = self.svc.get_case(master["id"], "global_admin", "")
        self.assertEqual(len(master_detail["intakes"]), 1)
        self.assertEqual(len(master_detail["followups"]), 0)

    def test_undo_is_idempotent(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        merge_id = self.merge(source["id"], master["id"])["merge"]["id"]
        first = self.svc.undo_merge(merge_id, "admin-1", "global_admin")
        second = self.svc.undo_merge(merge_id, "admin-1", "global_admin")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["merge"]["status"], "undone")

    def test_irreversible_merge_with_submitted_report(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        report = self.svc.create_report(source["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})

        result = self.merge(source["id"], master["id"])
        self.assertEqual(result["merge"]["status"], "irreversible")
        self.assertTrue(result["merge"]["irreversible_reason"])

        with self.assertRaises(ApiError) as ctx:
            self.svc.undo_merge(result["merge"]["id"], "admin-1", "global_admin")
        self.assertEqual(ctx.exception.code, "merge_irreversible")
        # The source stays merged; nothing was restored.
        self.assertEqual(self.svc._case(self.svc.repo.conn, source["id"])["status"], "merged")

    def test_followup_to_merged_case_conflicts(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        self.merge(source["id"], master["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                                  {"content": "合并后随访", "source": "phone", "expected_revision": 1,
                                   "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "case_merged")

    def test_followup_to_merging_case_conflicts(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        # Hold the source in the in-progress 'merging' lock.
        with self.svc.repo.tx() as conn:
            conn.execute("UPDATE cases SET status='merging',updated_at=? WHERE id=?", (iso(), source["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                                  {"content": "合并中随访", "source": "phone", "expected_revision": 1,
                                   "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "case_merged")

    def test_concurrent_followups_first_wins_later_conflicts(self):
        case = self.make_case("c-1")
        # First follow-up wins.
        first = self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                      {"content": "先到", "source": "email", "expected_revision": 1,
                                       "received_at": iso(utcnow())})
        self.assertEqual(first["revision"], 2)
        # A concurrent follow-up carrying the stale revision conflicts.
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "后到", "source": "fax", "expected_revision": 1,
                                   "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_master_revision_bumped_after_merge(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "源随访", "source": "fax", "expected_revision": 1,
                               "received_at": iso(utcnow())})
        result = self.merge(source["id"], master["id"])
        # Read the master's true revision (the returned case is the source).
        new_rev = self.svc._case(self.svc.repo.conn, master["id"])["revision"]
        # A follow-up with the stale pre-merge revision conflicts.
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(master["id"], "reporter-a", "reporter", "CN",
                                  {"content": "过期", "source": "email", "expected_revision": master["revision"],
                                   "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        # Re-reading and using the new revision succeeds.
        ok = self.svc.add_followup(master["id"], "reporter-a", "reporter", "CN",
                                   {"content": "新随访", "source": "email", "expected_revision": new_rev,
                                    "received_at": iso(utcnow())})
        self.assertEqual(ok["revision"], new_rev + 1)

    def test_resume_after_failure(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        self.svc.add_followup(source["id"], "reporter-a", "reporter", "CN",
                              {"content": "源随访", "source": "fax", "expected_revision": 1,
                               "received_at": iso(utcnow())})
        # Simulate a crash after the in-progress lock was taken but before the move.
        with self.svc.repo.tx() as conn:
            cur = conn.execute(
                "INSERT INTO merges(master_case_id,source_case_id,status,created_by,created_at) VALUES(?,?,?,?,?)",
                (master["id"], source["id"], "in_progress", "admin-1", iso()),
            )
            merge_id = cur.lastrowid
            conn.execute("UPDATE cases SET status='merging',updated_at=? WHERE id=?", (iso(), source["id"]))
        # Retrying resumes and completes the merge.
        result = self.merge(source["id"], master["id"])
        self.assertEqual(result["merge"]["id"], merge_id)
        self.assertEqual(result["merge"]["status"], "completed")
        self.assertEqual(self.svc._case(self.svc.repo.conn, source["id"])["status"], "merged")
        detail = self.svc.get_case(master["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 2)
        self.assertEqual(len(detail["followups"]), 1)

    def test_failed_merge_leaves_no_half_state(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        # Close the master so the merge is rejected.
        with self.svc.repo.tx() as conn:
            conn.execute("UPDATE cases SET status='closed',updated_at=? WHERE id=?", (iso(), master["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.merge(source["id"], master["id"])
        self.assertEqual(ctx.exception.code, "merge_conflict")
        # Nothing moved: source is still open and still owns its intake.
        self.assertEqual(self.svc._case(self.svc.repo.conn, source["id"])["status"], "open")
        detail = self.svc.get_case(source["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 1)
        # No in-progress merge record was left behind.
        merges = self.svc.list_merges("global_admin")
        self.assertEqual(merges, [])

    def test_merge_requires_same_product(self):
        master = self.make_case("m-1", product="DrugA")
        source = self.make_case("m-2", product="DrugB")
        with self.assertRaises(ApiError) as ctx:
            self.merge(source["id"], master["id"])
        self.assertEqual(ctx.exception.code, "merge_conflict")

    def test_merge_requires_global_admin(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(source["id"], "reporter-a", "reporter", {"target_case_id": master["id"]})
        self.assertEqual(ctx.exception.code, "merge_forbidden")

    def test_undo_requires_global_admin(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        merge_id = self.merge(source["id"], master["id"])["merge"]["id"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.undo_merge(merge_id, "reporter-a", "reporter")
        self.assertEqual(ctx.exception.code, "merge_forbidden")

    def test_list_and_get_merges(self):
        master = self.make_case("m-1")
        source = self.make_case("m-2")
        merge_id = self.merge(source["id"], master["id"])["merge"]["id"]
        listing = self.svc.list_merges("global_admin")
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], merge_id)
        detail = self.svc.get_merge(merge_id, "global_admin")
        self.assertEqual(detail["master_case_id"], master["id"])
        self.assertEqual(detail["source_case_id"], source["id"])
        self.assertIn("items", detail)


if __name__ == "__main__":
    unittest.main()

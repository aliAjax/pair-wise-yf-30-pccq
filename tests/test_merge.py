import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


class CaseMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")
        # 每个测试独立钩子，结束后清理
        self.addCleanup(self._clear_hooks)

    def tearDown(self):
        self._clear_hooks()
        self.tmp.cleanup()

    def _clear_hooks(self):
        PharmacovigilanceService._merge_phase2_hook = None
        PharmacovigilanceService._merge_after_phase1_hook = None

    def _make_case(self, key, source="email", patient="P-1", product="DrugA", region="CN"):
        return self.svc.create_case(
            "reporter-a", "reporter", region,
            {"patient_ref": patient, "region": region, "product": product, "event_term": "肝损伤",
             "source": source, "dedupe_key": key, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def _followup(self, case_id, expected, content="补充信息", by="reporter-a"):
        return self.svc.add_followup(
            case_id, by, "reporter", "CN",
            {"content": content, "source": "phone", "expected_revision": expected,
             "received_at": iso(utcnow())},
        )

    # ---- 主流程：邮件/传真/电话三条重复案例合并并撤销 ----------------------
    def test_merge_three_channels_moves_intakes_and_followups_then_revert(self):
        master = self._make_case("intake-email", source="email")
        fax = self._make_case("intake-fax", source="fax")
        phone = self._make_case("intake-phone", source="phone")
        # 被合并案例各自带着随访
        self._followup(fax["id"], 1, "传真补充住院信息")
        self._followup(phone["id"], 1, "电话补充用药史")
        self._followup(master["id"], 1, "邮件主案例随访")

        result = self.svc.merge_cases(
            master["id"], "admin-1", "global_admin",
            {"case_ids": [master["id"], fax["id"], phone["id"]]},
        )
        merge = result["merge"]
        self.assertFalse(result["idempotent"])
        self.assertEqual(merge["status"], "completed")
        self.assertEqual(merge["irreversible"], 0)
        self.assertIsNone(merge["irreversible_reason"])

        conn = self.svc.repo.conn
        # 三条来源登记全部改挂到主案例，并保留原属案例
        intakes = conn.execute("SELECT id,origin_case_id FROM intakes WHERE case_id=?", (master["id"],)).fetchall()
        self.assertEqual({r["origin_case_id"] for r in intakes}, {master["id"], fax["id"], phone["id"]})
        # 随访同样改挂，在主案例版本序列内重新编号，原始版本记入 original_revision
        followups = conn.execute(
            "SELECT id,origin_case_id,revision,original_revision FROM followups WHERE case_id=? ORDER BY revision",
            (master["id"],)).fetchall()
        self.assertEqual(len(followups), 3)
        origins = {r["origin_case_id"] for r in followups}
        self.assertEqual(origins, {master["id"], fax["id"], phone["id"]})
        moved = [r for r in followups if r["origin_case_id"] != master["id"]]
        # 原案例上唯一一条随访的版本号为 2
        self.assertEqual({r["original_revision"] for r in moved}, {2})
        self.assertEqual(sorted(r["revision"] for r in followups), [2, 3, 4])
        # 主案例 revision 随改挂随访递增
        self.assertEqual(conn.execute("SELECT revision FROM cases WHERE id=?", (master["id"],)).fetchone()[0], 4)
        # 流向记录：2 intake + 2 followup
        movements = conn.execute(
            "SELECT entity_type,from_case_id,to_case_id,original_revision FROM merge_movements WHERE merge_id=?",
            (merge["id"],)).fetchall()
        self.assertEqual(len(movements), 4)
        self.assertTrue(all(r["to_case_id"] == master["id"] for r in movements))
        self.assertEqual({r["from_case_id"] for r in movements}, {fax["id"], phone["id"]})

        # 被合并案例状态
        for cid in (fax["id"], phone["id"]):
            row = conn.execute("SELECT status,merged_into FROM cases WHERE id=?", (cid,)).fetchone()
            self.assertEqual(row["status"], "merged")
            self.assertEqual(row["merged_into"], master["id"])

        # 合并后主案例仍可写随访（revision 顺延到 5）
        ok = self._followup(master["id"], 4, "合并后的新随访")
        self.assertEqual(ok["revision"], 5)

        # 撤销合并：按流向记录原样退回（合并后在主案例新增的随访没有流向记录，留在主案例）
        reverted = self.svc.revert_merge(merge["id"], "admin-1", "global_admin")["merge"]
        self.assertEqual(reverted["status"], "reverted")
        for cid in (fax["id"], phone["id"], master["id"]):
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM intakes WHERE case_id=? AND origin_case_id=?", (cid, cid)).fetchone()[0],
                1)
        # 被合并案例各退回 1 条随访；主案例保留自己原有的 1 条 + 合并后新增的 1 条
        for cid in (fax["id"], phone["id"]):
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM followups WHERE case_id=? AND origin_case_id=?", (cid, cid)).fetchone()[0],
                1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM followups WHERE case_id=? AND origin_case_id=?",
                         (master["id"], master["id"])).fetchone()[0],
            2)
        # 随访恢复原版本号
        self.assertEqual(
            tuple(conn.execute("SELECT revision,original_revision FROM followups WHERE origin_case_id=?",
                         (fax["id"],)).fetchone()),
            (2, None),
        )
        # 主案例 revision 回到合并前继续：快照 2 + 合并后新增 1 条 = 3
        self.assertEqual(conn.execute("SELECT revision FROM cases WHERE id=?", (master["id"],)).fetchone()[0], 3)
        # 合并后才在主案例上新增的随访仍挂主案例，被重排为 revision 3
        row = conn.execute("SELECT revision,original_revision FROM followups WHERE case_id=? AND content=?",
                           (master["id"], "合并后的新随访")).fetchone()
        self.assertEqual((row[0], row[1]), (3, None))
        for cid in (fax["id"], phone["id"]):
            row = conn.execute("SELECT status,merged_into FROM cases WHERE id=?", (cid,)).fetchone()
            self.assertEqual(row["status"], "open")
            self.assertIsNone(row["merged_into"])

        # 撤销后原案例可继续接收随访
        self._followup(fax["id"], 2, "撤销后的随访")

    # ---- 合并期间：随访写入被拒，先到先得 ------------------------------
    def test_followups_blocked_while_merge_in_progress(self):
        master = self._make_case("a-email")
        member = self._make_case("a-fax", source="fax")
        seen = []

        def hook(merge_id):
            for cid in (master["id"], member["id"]):
                try:
                    self._followup(cid, 1)
                except ApiError as exc:
                    seen.append(exc.code)

        PharmacovigilanceService._merge_after_phase1_hook = hook
        self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                             {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(seen, ["merge_in_progress", "merge_in_progress"])

    def test_concurrent_followup_first_wins_later_gets_conflict(self):
        case = self._make_case("race-1")
        outcomes = []

        def worker():
            try:
                self._followup(case["id"], 1, content=f"随访-{threading.get_ident()}")
                outcomes.append("ok")
            except ApiError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("revision_conflict"), 7)

    def test_followup_landing_during_merge_wins_merge_retries_as_whole(self):
        master = self._make_case("b-email")
        member = self._make_case("b-fax", source="fax")

        def hook(merge_id):
            # 模拟随访请求先落库：直接写库（绕过合并守卫，等价于它先拿到写锁）
            now = iso()
            with self.svc.repo.write_tx() as conn:
                conn.execute(
                    "INSERT INTO followups(case_id,origin_case_id,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (member["id"], member["id"], "并发先到的随访", "phone", now, 2, "reporter-a", now),
                )
                conn.execute("UPDATE cases SET revision=2,updated_at=? WHERE id=?", (now, member["id"]))

        PharmacovigilanceService._merge_after_phase1_hook = hook
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                 {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(ctx.exception.code, "merge_revision_conflict")

        conn = self.svc.repo.conn
        job = conn.execute("SELECT status FROM case_merges WHERE master_case_id=?", (master["id"],)).fetchone()
        self.assertEqual(job["status"], "in_progress")  # 没有半成品，可整体重试
        # 随访先到已保留
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM followups WHERE case_id=?", (member["id"],)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT status FROM cases WHERE id=?", (member["id"],)).fetchone()[0], "open")

        merge_id = conn.execute("SELECT id FROM case_merges WHERE master_case_id=?", (master["id"],)).fetchone()[0]
        done = self.svc.retry_merge(merge_id, "admin-1", "global_admin", {"refresh_snapshots": True})["merge"]
        self.assertEqual(done["status"], "completed")
        # 先到的随访随合并一起改挂到主案例
        self.assertEqual(
            conn.execute("SELECT case_id,origin_case_id FROM followups WHERE content=?",
                         ("并发先到的随访",)).fetchone()[0],
            master["id"],
        )

    # ---- 已提交国家报告：整条合并不可撤销 ------------------------------
    def test_merge_with_submitted_report_is_irreversible(self):
        master = self._make_case("c-email")
        member = self._make_case("c-fax", source="fax")
        report = self.svc.create_report(member["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})

        merge = self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                     {"case_ids": [master["id"], member["id"]]})["merge"]
        self.assertEqual(merge["irreversible"], 1)
        self.assertIn("已提交的国家报告", merge["irreversible_reason"])
        self.assertIn("CN", merge["irreversible_reason"])

        with self.assertRaises(ApiError) as ctx:
            self.svc.revert_merge(merge["id"], "admin-1", "global_admin")
        self.assertEqual(ctx.exception.code, "merge_irreversible")

    def test_report_submitted_after_merge_blocks_revert(self):
        master = self._make_case("d-email")
        member = self._make_case("d-fax", source="fax")
        merge = self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                     {"case_ids": [master["id"], member["id"]]})["merge"]
        self.assertEqual(merge["irreversible"], 0)
        # 合并完成后从主案例报送
        report = self.svc.create_report(master["id"], "lead-cn", "regional_lead", "CN", {"country": "US"})
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.revert_merge(merge["id"], "admin-1", "global_admin")
        self.assertEqual(ctx.exception.code, "merge_irreversible")
        self.assertIn("US", ctx.exception.message)

    # ---- 中途失败：整笔回滚、可重试，不留半成品 --------------------------
    def test_phase2_failure_rolls_back_and_retry_succeeds(self):
        master = self._make_case("e-email")
        member = self._make_case("e-fax", source="fax")
        calls = {"n": 0}

        def boom(merge_id, conn):
            calls["n"] += 1
            raise RuntimeError("simulated phase2 failure")

        PharmacovigilanceService._merge_phase2_hook = boom
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                 {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(ctx.exception.code, "merge_phase_failed")
        self.assertIn("可重试", ctx.exception.message)
        self._clear_hooks()

        conn = self.svc.repo.conn
        job = conn.execute("SELECT * FROM case_merges WHERE master_case_id=?", (master["id"],)).fetchone()
        self.assertEqual(job["status"], "in_progress")
        # 没有半成品
        self.assertEqual(conn.execute("SELECT status FROM cases WHERE id=?", (member["id"],)).fetchone()[0], "open")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM merge_movements").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT case_id FROM intakes WHERE dedupe_key='e-fax'").fetchone()[0], member["id"])

        done = self.svc.retry_merge(job["id"], "admin-1", "global_admin", {})["merge"]
        self.assertEqual(done["status"], "completed")
        self.assertEqual(calls["n"], 1)

    def test_abort_merge_allows_followups_again(self):
        master = self._make_case("f-email")
        member = self._make_case("f-fax", source="fax")
        PharmacovigilanceService._merge_phase2_hook = lambda mid, conn: (_ for _ in ()).throw(RuntimeError("x"))
        with self.assertRaises(ApiError):
            self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                 {"case_ids": [master["id"], member["id"]]})
        self._clear_hooks()
        job_id = conn = self.svc.repo.conn.execute("SELECT id FROM case_merges").fetchone()[0]
        aborted = self.svc.abort_merge(job_id, "admin-1", "global_admin", {"reason": "录错患者"})["merge"]
        self.assertEqual(aborted["status"], "aborted")
        # 中止后随访恢复
        self._followup(member["id"], 1, "恢复写入")

    # ---- 合并完成后的行为与校验 ----------------------------------------
    def test_merged_member_rejects_followup_master_accepts(self):
        master = self._make_case("g-email")
        member = self._make_case("g-fax", source="fax")
        self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                             {"case_ids": [master["id"], member["id"]]})
        with self.assertRaises(ApiError) as ctx:
            self._followup(member["id"], 1)
        self.assertEqual(ctx.exception.code, "case_merged")
        self._followup(master["id"], 1, "写在主案例上")

    def test_get_merged_member_shows_master_records(self):
        master = self._make_case("h-email")
        member = self._make_case("h-fax", source="fax")
        self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                             {"case_ids": [master["id"], member["id"]]})
        detail = self.svc.get_case(member["id"], "global_admin", "")
        self.assertEqual(len(detail["intakes"]), 2)
        self.assertEqual(len(detail["merges"]), 1)
        self.assertEqual(detail["merges"][0]["status"], "completed")

    def test_merge_rejects_product_mismatch_and_merged_member(self):
        master = self._make_case("i-1", product="DrugA")
        other = self._make_case("i-2", product="DrugB")
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                 {"case_ids": [master["id"], other["id"]]})
        self.assertEqual(ctx.exception.code, "product_mismatch")

        dup_a = self._make_case("i-3", product="DrugA", source="fax")
        dup_b = self._make_case("i-4", product="DrugA", source="phone")
        self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                             {"case_ids": [master["id"], dup_a["id"]]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(dup_b["id"], "admin-1", "global_admin",
                                 {"case_ids": [dup_b["id"], dup_a["id"]]})
        self.assertEqual(ctx.exception.code, "merge_conflict")

    def test_only_global_admin_can_merge(self):
        master = self._make_case("j-1")
        member = self._make_case("j-2", source="fax")
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(master["id"], "lead-cn", "regional_lead",
                                 {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(ctx.exception.status, 403)

    def test_legacy_target_case_id_payload_still_works(self):
        master = self._make_case("k-1")
        member = self._make_case("k-2", source="fax")
        merge = self.svc.merge_cases(member["id"], "admin-1", "global_admin",
                                     {"target_case_id": master["id"]})["merge"]
        self.assertEqual(merge["status"], "completed")
        self.assertEqual(merge["master_case_id"], master["id"])

    def test_completed_merge_retry_is_idempotent_conflict(self):
        master = self._make_case("m-1")
        member = self._make_case("m-2", source="fax")
        merge = self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                     {"case_ids": [master["id"], member["id"]]})["merge"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.retry_merge(merge["id"], "admin-1", "global_admin", {})
        self.assertEqual(ctx.exception.code, "merge_not_pending")

    def test_report_create_blocked_during_merge(self):
        master = self._make_case("n-1")
        member = self._make_case("n-2", source="fax")
        seen = []

        def hook(merge_id):
            try:
                self.svc.create_report(member["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
            except ApiError as exc:
                seen.append(exc.code)

        PharmacovigilanceService._merge_after_phase1_hook = hook
        self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                             {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(seen, ["merge_in_progress"])

    def test_followup_on_master_during_merge_blocks_whole_retry(self):
        master = self._make_case("o-1")
        member = self._make_case("o-2", source="fax")

        def hook(merge_id):
            now = iso()
            with self.svc.repo.write_tx() as conn:
                conn.execute(
                    "INSERT INTO followups(case_id,origin_case_id,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (master["id"], master["id"], "主案例并发随访", "phone", now, 2, "reporter-a", now),
                )
                conn.execute("UPDATE cases SET revision=2,updated_at=? WHERE id=?", (now, master["id"]))

        PharmacovigilanceService._merge_after_phase1_hook = hook
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(master["id"], "admin-1", "global_admin",
                                 {"case_ids": [master["id"], member["id"]]})
        self.assertEqual(ctx.exception.code, "merge_revision_conflict")
        merge_id = self.svc.repo.conn.execute("SELECT id FROM case_merges").fetchone()[0]
        done = self.svc.retry_merge(merge_id, "admin-1", "global_admin", {"refresh_snapshots": True})["merge"]
        self.assertEqual(done["status"], "completed")


import http.client
import json as _json


class MergeHttpTest(unittest.TestCase):
    def setUp(self):
        import tempfile as _tf
        from app import create_server
        self.tmp = _tf.TemporaryDirectory()
        self.server = create_server(Path(self.tmp.name) / "http.db", "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        import threading as _t
        self.thread = _t.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, body=None, role="global_admin", user="admin-1", region=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-User-Id": user, "X-Role": role}
        if region:
            headers["X-Region"] = region
        payload = _json.dumps(body) if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = _json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def _create_case(self, key, source):
        status, data = self.request("POST", "/api/cases", {
            "patient_ref": "P-9", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
            "source": source, "dedupe_key": key,
        }, role="reporter", user="rep-1", region="CN")
        self.assertEqual(status, 201)
        return data["case"]

    def test_merge_revert_flow_over_http(self):
        a = self._create_case("h-1", "email")
        b = self._create_case("h-2", "fax")
        status, data = self.request("POST", f"/api/cases/{a['id']}/merge",
                                    {"case_ids": [a["id"], b["id"]]})
        self.assertEqual(status, 200, data)
        merge_id = data["merge"]["id"]
        self.assertEqual(data["merge"]["status"], "completed")

        # 主案例上看到两条 intake
        status, detail = self.request("GET", f"/api/cases/{a['id']}")
        self.assertEqual(len(detail["intakes"]), 2)

        # 被合并案例随访被拒
        status, err = self.request("POST", f"/api/cases/{b['id']}/followups",
                                   {"content": "x", "source": "phone", "expected_revision": 1},
                                   role="reporter", user="rep-1", region="CN")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "case_merged")

        # 列表 + 撤销
        status, merges = self.request("GET", "/api/merges")
        self.assertEqual(status, 200)
        self.assertEqual(len(merges["merges"]), 1)
        status, reverted = self.request("POST", f"/api/merges/{merge_id}/revert", {})
        self.assertEqual(status, 200)
        self.assertEqual(reverted["merge"]["status"], "reverted")

        # 撤销后可继续随访
        status, _ = self.request("POST", f"/api/cases/{b['id']}/followups",
                                 {"content": "恢复", "source": "phone", "expected_revision": 1},
                                 role="reporter", user="rep-1", region="CN")
        self.assertEqual(status, 201)

    def test_irreversible_merge_rejected_over_http(self):
        a = self._create_case("h-3", "email")
        b = self._create_case("h-4", "fax")
        status, report = self.request("POST", f"/api/cases/{b['id']}/reports", {"country": "CN"},
                                      role="regional_lead", user="lead", region="CN")
        self.assertEqual(status, 201)
        status, _ = self.request("POST", f"/api/reports/{report['id']}/submit", {},
                                 role="regional_lead", user="lead", region="CN")
        self.assertEqual(status, 200)
        status, merged = self.request("POST", f"/api/cases/{a['id']}/merge",
                                      {"case_ids": [a["id"], b["id"]]})
        self.assertEqual(status, 200)
        self.assertEqual(merged["merge"]["irreversible"], 1)
        status, err = self.request("POST", f"/api/merges/{merged['merge']['id']}/revert", {})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "merge_irreversible")
        self.assertIn("CN", err["message"])


if __name__ == "__main__":
    unittest.main()

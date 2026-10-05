#!/usr/bin/env python3
"""Pharmacovigilance case intake service using only the Python standard library."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = 8201
ROLES = {"reporter", "regional_lead", "medical_reviewer", "global_admin"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    current = value or utcnow()
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None, default: datetime | None = None) -> datetime:
    if not value:
        if default is None:
            raise ApiError(400, "missing_time", "必须提供 ISO 8601 时间")
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_deadline(received_at: datetime, serious: bool, fatal: bool) -> datetime:
    if serious:
        return received_at + timedelta(days=7 if fatal else 15)
    return received_at + timedelta(days=90)


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        # SQLite 单连接 + BEGIN IMMEDIATE：所有写操作串行化，先拿到写锁的请求先落库
        self.write_lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def write_tx(self):
        with self.write_lock:
            with self.tx() as conn:
                yield conn

    def init_schema(self) -> None:
        with self.write_lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_no TEXT NOT NULL UNIQUE,
                    patient_ref TEXT NOT NULL,
                    region TEXT NOT NULL,
                    product TEXT NOT NULL,
                    event_term TEXT NOT NULL,
                    onset_at TEXT,
                    received_at TEXT NOT NULL,
                    serious INTEGER NOT NULL DEFAULT 0,
                    fatal INTEGER NOT NULL DEFAULT 0,
                    causality TEXT,
                    report_due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1,
                    merged_into INTEGER REFERENCES cases(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intakes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER REFERENCES cases(id),
                    origin_case_id INTEGER REFERENCES cases(id),
                    source TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    original_revision INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, revision)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    country TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    submitted_at TEXT,
                    submitted_by TEXT,
                    late INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(case_id, country)
                );
                CREATE TABLE IF NOT EXISTS medical_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    case_revision INTEGER NOT NULL,
                    serious INTEGER NOT NULL,
                    fatal INTEGER NOT NULL,
                    causality TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, case_revision)
                );
                CREATE TABLE IF NOT EXISTS case_merges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_case_id INTEGER NOT NULL REFERENCES cases(id),
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    irreversible INTEGER NOT NULL DEFAULT 0,
                    irreversible_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    reverted_at TEXT,
                    reverted_by TEXT,
                    aborted_at TEXT,
                    aborted_by TEXT,
                    abort_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS case_merge_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merge_id INTEGER NOT NULL REFERENCES case_merges(id),
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    role TEXT NOT NULL,
                    revision_snapshot INTEGER NOT NULL,
                    UNIQUE(merge_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS merge_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merge_id INTEGER NOT NULL REFERENCES case_merges(id),
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    from_case_id INTEGER NOT NULL REFERENCES cases(id),
                    to_case_id INTEGER NOT NULL REFERENCES cases(id),
                    original_revision INTEGER,
                    moved_at TEXT NOT NULL,
                    UNIQUE(merge_id, entity_type, entity_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate()

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)

    def _migrate(self) -> None:
        """补齐旧库：intakes.origin_case_id、followups.origin_case_id 及其三元唯一约束。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self._column_exists("intakes", "origin_case_id"):
                self.conn.execute("ALTER TABLE intakes ADD COLUMN origin_case_id INTEGER REFERENCES cases(id)")
                self.conn.execute("UPDATE intakes SET origin_case_id=case_id WHERE origin_case_id IS NULL")
            followup_cols = self.conn.execute("PRAGMA table_info(followups)").fetchall()
            col_names = {r["name"] for r in followup_cols}
            needs_rebuild = "origin_case_id" not in col_names or "original_revision" not in col_names
            if needs_rebuild:
                self.conn.execute(
                    """CREATE TABLE followups_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        case_id INTEGER NOT NULL REFERENCES cases(id),
                        origin_case_id INTEGER NOT NULL REFERENCES cases(id),
                        content TEXT NOT NULL,
                        source TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        original_revision INTEGER,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(case_id, revision)
                    )"""
                )
                if "origin_case_id" in col_names:
                    self.conn.execute(
                        """INSERT INTO followups_new(id,case_id,origin_case_id,content,source,received_at,revision,original_revision,created_by,created_at)
                           SELECT id,case_id,origin_case_id,content,source,received_at,revision,NULL,created_by,created_at FROM followups"""
                    )
                else:
                    self.conn.execute(
                        """INSERT INTO followups_new(id,case_id,origin_case_id,content,source,received_at,revision,original_revision,created_by,created_at)
                           SELECT id,case_id,case_id,content,source,received_at,revision,NULL,created_by,created_at FROM followups"""
                    )
                self.conn.execute("DROP TABLE followups")
                self.conn.execute("ALTER TABLE followups_new RENAME TO followups")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def audit(conn: sqlite3.Connection, case_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()),
        )

    @staticmethod
    def row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None


class PharmacovigilanceService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor = headers.get("X-User-Id", "").strip()
        role = headers.get("X-Role", "").strip()
        region = headers.get("X-Region", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效的 X-Role")
        if role in {"reporter", "regional_lead"} and not region:
            raise ApiError(401, "region_required", "该角色必须提供 X-Region")
        return actor, role, region

    @staticmethod
    def can_access(case: dict[str, Any], role: str, region: str) -> bool:
        return role in {"medical_reviewer", "global_admin"} or case["region"] == region

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ApiError(404, "case_not_found", "案例不存在")
        return row

    def create_case(self, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        required = ("patient_ref", "region", "product", "event_term", "source", "dedupe_key")
        missing = [key for key in required if not str(body.get(key, "")).strip()]
        if missing:
            raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(missing)}")
        if role == "reporter" and body["region"] != region:
            raise ApiError(403, "region_forbidden", "只能录入本区域案例")
        if role == "medical_reviewer" and body["region"] not in {"", region}:
            raise ApiError(403, "reviewer_region_forbidden", "医学审核员不能代表区域录入案例")
        received = parse_time(body.get("received_at"), utcnow())
        serious = bool(body.get("serious", False))
        fatal = bool(body.get("fatal", False))
        due = report_deadline(received, serious, fatal)
        now = iso()
        with self.repo.write_tx() as conn:
            duplicate = conn.execute("SELECT * FROM intakes WHERE dedupe_key=?", (body["dedupe_key"],)).fetchone()
            if duplicate:
                case = self._case(conn, duplicate["case_id"])
                Repository.audit(conn, case["id"], actor, role, "intake_deduplicated", {"dedupe_key": body["dedupe_key"], "source": body["source"]})
                return {"deduplicated": True, "case": dict(case), "intake_id": duplicate["id"]}
            count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
            case_no = body.get("case_no") or f"PV-{received.year}-{count:06d}"
            try:
                cursor = conn.execute(
                    """INSERT INTO cases(case_no,patient_ref,region,product,event_term,onset_at,received_at,
                       serious,fatal,causality,report_due_at,status,revision,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (case_no, body["patient_ref"], body["region"], body["product"], body["event_term"],
                     body.get("onset_at"), iso(received), int(serious), int(fatal), body.get("causality"),
                     iso(due), "open", 1, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "case_number_conflict", "案例编号已存在") from exc
            case_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO intakes(case_id,origin_case_id,source,dedupe_key,payload_json,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, case_id, body["source"], body["dedupe_key"], json.dumps(body, ensure_ascii=False, sort_keys=True), iso(received), actor, now),
            )
            Repository.audit(conn, case_id, actor, role, "case_created", {"case_no": case_no, "source": body["source"]})
            case = self._case(conn, case_id)
            return {"deduplicated": False, "case": dict(case)}

    def get_case(self, case_id: int, role: str, region: str) -> dict[str, Any]:
        case = self._case(self.repo.conn, case_id)
        if not self.can_access(case, role, region):
            raise ApiError(403, "case_forbidden", "无权查看该区域案例")
        conn = self.repo.conn
        effective_ids = [case_id]
        if case["status"] == "merged" and case["merged_into"]:
            master = self._case(conn, case["merged_into"])
            if self.can_access(master, role, region):
                effective_ids = [case["merged_into"]]
        return {
            "case": dict(case),
            "intakes": [dict(r) for r in conn.execute(
                "SELECT id,case_id,origin_case_id,source,dedupe_key,received_at,created_by,created_at FROM intakes WHERE case_id=? ORDER BY id",
                (effective_ids[0],))],
            "followups": [dict(r) for r in conn.execute(
                "SELECT * FROM followups WHERE case_id=? ORDER BY revision,origin_case_id,id",
                (effective_ids[0],))],
            "reports": [dict(r) for r in conn.execute("SELECT * FROM reports WHERE case_id=? ORDER BY country", (case_id,))],
            "reviews": [dict(r) for r in conn.execute("SELECT * FROM medical_reviews WHERE case_id=? ORDER BY id", (case_id,))],
            "merges": self._merge_records(conn, effective_ids),
            "audit": [dict(r) for r in conn.execute("SELECT actor,role,action,detail_json,created_at FROM audit_log WHERE case_id=? ORDER BY id", (case_id,))] if role in {"medical_reviewer", "global_admin"} else [],
        }

    @staticmethod
    def _merge_records(conn: sqlite3.Connection, case_ids: list[int]) -> list[dict[str, Any]]:
        if not case_ids:
            return []
        placeholders = ",".join("?" for _ in case_ids)
        rows = conn.execute(
            f"""SELECT m.* FROM case_merges m
                JOIN case_merge_members mm ON mm.merge_id=m.id
               WHERE mm.case_id IN ({placeholders})
               GROUP BY m.id ORDER BY m.id""",
            case_ids,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_cases(self, role: str, region: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cases WHERE status!='merged'"
        args: list[Any] = []
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND region=?"
            args.append(region)
        if query.get("status"):
            sql += " AND status=?"
            args.append(query["status"][0])
        sql += " ORDER BY received_at DESC,id DESC"
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def _active_merge_guard(self, conn: sqlite3.Connection, case_ids: list[int]) -> None:
        """合并作业进行中：参与案例（含主案例）一律拒绝随访/报告写入。先到先得由写锁串行保证。"""
        if not case_ids:
            return
        placeholders = ",".join("?" for _ in case_ids)
        row = conn.execute(
            f"""SELECT m.id FROM case_merges m
                JOIN case_merge_members mm ON mm.merge_id=m.id
               WHERE m.status='in_progress' AND mm.case_id IN ({placeholders}) LIMIT 1""",
            case_ids,
        ).fetchone()
        if row:
            raise ApiError(409, "merge_in_progress", f"合并作业 {row['id']} 进行中，请稍后重试或联系全局管理员")

    def add_followup(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        content = str(body.get("content", "")).strip()
        source = str(body.get("source", "")).strip()
        if not content or not source:
            raise ApiError(400, "missing_fields", "content 和 source 必填")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.write_tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region) or role in {"medical_reviewer"}:
                raise ApiError(403, "followup_forbidden", "当前角色不能提交随访")
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再写入随访，请在主案例上提交")
            self._active_merge_guard(conn, [case_id])
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例已被其他人员更新，请重新读取")
            revision = case["revision"] + 1
            received = parse_time(body.get("received_at"), utcnow())
            due = report_deadline(received, bool(case["serious"]), bool(case["fatal"]))
            conn.execute(
                "INSERT INTO followups(case_id,origin_case_id,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, case_id, content, source, iso(received), revision, actor, iso()),
            )
            conn.execute(
                "UPDATE cases SET revision=?,received_at=?,report_due_at=?,updated_at=? WHERE id=?",
                (revision, iso(received), iso(due), iso(), case_id),
            )
            Repository.audit(conn, case_id, actor, role, "followup_added", {"revision": revision, "source": source})
            return {"case": dict(self._case(conn, case_id)), "revision": revision}

    def medical_review(self, case_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "medical_reviewer":
            raise ApiError(403, "medical_reviewer_required", "只有医学审核员可以裁定严重性")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        serious = body.get("serious")
        fatal = body.get("fatal")
        causality = str(body.get("causality", "")).strip()
        rationale = str(body.get("rationale", "")).strip()
        if not isinstance(serious, bool) or not isinstance(fatal, bool) or not causality or not rationale:
            raise ApiError(400, "invalid_review", "serious/fatal 必须是布尔值，causality 和 rationale 必填")
        if fatal and not serious:
            raise ApiError(400, "invalid_severity", "死亡案例必须标记为严重")
        received = parse_time(body.get("received_at"))
        due = report_deadline(received, serious, fatal)
        with self.repo.write_tx() as conn:
            case = self._case(conn, case_id)
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能审核")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例版本已变化")
            revision = expected + 1
            conn.execute(
                """UPDATE cases SET serious=?,fatal=?,causality=?,report_due_at=?,revision=?,updated_at=? WHERE id=?""",
                (int(serious), int(fatal), causality, iso(due), revision, iso(), case_id),
            )
            conn.execute(
                """INSERT INTO medical_reviews(case_id,case_revision,serious,fatal,causality,rationale,reviewer,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (case_id, expected, int(serious), int(fatal), causality, rationale, actor, iso()),
            )
            Repository.audit(conn, case_id, actor, role, "medical_reviewed", {"from_revision": expected, "serious": serious, "fatal": fatal, "causality": causality})
            return {"case": dict(self._case(conn, case_id)), "reviewed_revision": expected}

    def create_report(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "report_forbidden", "只有区域负责人或全局管理员可以生成报告")
        country = str(body.get("country", "")).strip().upper()
        if not country:
            raise ApiError(400, "country_required", "country 必填")
        with self.repo.write_tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region):
                raise ApiError(403, "region_forbidden", "不能为本区域之外案例生成报告")
            self._active_merge_guard(conn, [case_id])
            due = report_deadline(parse_time(case["received_at"]), bool(case["serious"]), bool(case["fatal"]))
            try:
                cur = conn.execute("INSERT INTO reports(case_id,country,due_at,status) VALUES(?,?,?,?)", (case_id, country, iso(due), "pending"))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "report_exists", "该国家报告已经存在") from exc
            Repository.audit(conn, case_id, actor, role, "report_created", {"report_id": cur.lastrowid, "country": country})
            return dict(conn.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone())

    def submit_report(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "submit_forbidden", "当前角色不能提交监管报告")
        with self.repo.write_tx() as conn:
            row = conn.execute("SELECT r.*,c.region FROM reports r JOIN cases c ON c.id=r.case_id WHERE r.id=?", (report_id,)).fetchone()
            if not row:
                raise ApiError(404, "report_not_found", "报告不存在")
            if not self.can_access(dict(row), role, region):
                raise ApiError(403, "region_forbidden", "无权提交其他区域报告")
            if row["status"] == "submitted":
                return {"report": dict(row), "idempotent": True}
            now = parse_time(body.get("submitted_at"), utcnow())
            late = int(now > parse_time(row["due_at"]))
            conn.execute("UPDATE reports SET status='submitted',submitted_at=?,submitted_by=?,late=? WHERE id=?", (iso(now), actor, late, report_id))
            Repository.audit(conn, row["case_id"], actor, role, "report_submitted", {"report_id": report_id, "country": row["country"], "late": bool(late)})
            return {"report": dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()), "idempotent": False}

    # ---- 案例合并 ---------------------------------------------------------
    # 测试用故障注入钩子：
    #   _merge_phase2_hook(merge_id, conn) 在阶段二事务内执行，抛错即整笔回滚
    #   _merge_after_phase1_hook(merge_id) 在阶段一提交后执行（模拟崩溃/并发写入）
    _merge_phase2_hook = None
    _merge_after_phase1_hook = None

    @staticmethod
    def _submitted_reports(conn: sqlite3.Connection, case_ids: list[int]) -> list[sqlite3.Row]:
        if not case_ids:
            return []
        placeholders = ",".join("?" for _ in case_ids)
        return conn.execute(
            f"""SELECT r.id,r.case_id,r.country,r.submitted_at,c.case_no
                  FROM reports r JOIN cases c ON c.id=r.case_id
                 WHERE r.status='submitted' AND r.case_id IN ({placeholders})
                 ORDER BY r.id""",
            case_ids,
        ).fetchall()

    @staticmethod
    def _irreversible_reason(reports: list[sqlite3.Row]) -> str:
        items = "；".join(f"报告#{r['id']}({r['case_no']}/{r['country']}，提交于{r['submitted_at']})" for r in reports)
        return "参与案例存在已提交的国家报告，监管报送无法拆分退回：" + items

    @staticmethod
    def _members(conn: sqlite3.Connection, merge_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM case_merge_members WHERE merge_id=? ORDER BY role DESC,case_id", (merge_id,)
        ).fetchall()

    def _merge_json(self, conn: sqlite3.Connection, merge_row: sqlite3.Row) -> dict[str, Any]:
        merge_id = merge_row["id"]
        members = [dict(r) for r in self._members(conn, merge_id)]
        movements = [dict(r) for r in conn.execute(
            "SELECT * FROM merge_movements WHERE merge_id=? ORDER BY id", (merge_id,))]
        return {**dict(merge_row), "members": members, "movements": movements}

    def merge_cases(self, path_case_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以合并案例")
        member_ids = body.get("case_ids")
        if member_ids is None and isinstance(body.get("target_case_id"), int):
            # 兼容旧接口：POST /api/cases/{source}/merge {"target_case_id": master}
            # URL 上的 source 是被合并方，body 里的 target 是主案例
            if body["target_case_id"] == path_case_id:
                raise ApiError(400, "invalid_target", "target_case_id 必须指向不同案例")
            master_id = body["target_case_id"]
            member_ids = [master_id, path_case_id]
        else:
            master_id = body.get("master_case_id", path_case_id)
            if not isinstance(master_id, int) or isinstance(master_id, bool):
                raise ApiError(400, "invalid_master", "master_case_id 必须是整数案例 ID")
        if not isinstance(member_ids, list) or not member_ids:
            raise ApiError(400, "case_ids_required", "case_ids 必须是非空数组")
        unique_ids: list[int] = []
        for value in member_ids:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ApiError(400, "invalid_case_id", "case_ids 中的元素必须是整数案例 ID")
            if value not in unique_ids:
                unique_ids.append(value)
        if master_id not in unique_ids:
            raise ApiError(400, "master_not_in_case_ids", "主案例必须包含在 case_ids 中")
        member_cases = unique_ids.copy()
        member_cases.remove(master_id)
        if not member_cases:
            raise ApiError(400, "invalid_target", "至少需要一个不同于主案例的被合并案例")

        # 阶段一：校验 + 写入合并锁。一旦提交，即使后续崩溃也留下可整体重试的作业，
        # 不会出现只改了一半的案例。
        with self.repo.write_tx() as conn:
            cases = {cid: self._case(conn, cid) for cid in unique_ids}
            master = cases[master_id]
            not_open = [r["case_no"] for r in cases.values() if r["status"] != "open"]
            if not_open:
                raise ApiError(409, "merge_conflict", f"以下案例当前不可合并（非 open 状态）: {', '.join(not_open)}")
            self._active_merge_guard(conn, unique_ids)
            products = {r["product"].casefold() for r in cases.values()}
            if len(products) != 1:
                raise ApiError(409, "product_mismatch", "参与合并的案例产品不一致，不能合并")
            now = iso()
            cur = conn.execute(
                "INSERT INTO case_merges(master_case_id,status,created_by,created_at) VALUES(?,?,?,?)",
                (master_id, "in_progress", actor, now),
            )
            merge_id = cur.lastrowid
            for cid in unique_ids:
                conn.execute(
                    "INSERT INTO case_merge_members(merge_id,case_id,role,revision_snapshot) VALUES(?,?,?,?)",
                    (merge_id, cid, "master" if cid == master_id else "member", cases[cid]["revision"]),
                )
            Repository.audit(conn, master_id, actor, role, "merge_started",
                             {"merge_id": merge_id, "case_ids": unique_ids})

        if PharmacovigilanceService._merge_after_phase1_hook:
            PharmacovigilanceService._merge_after_phase1_hook(merge_id)

        try:
            return self._execute_merge(merge_id, actor, role, refresh_snapshots=False)
        except ApiError:
            raise
        except Exception as exc:
            # 阶段二已整笔回滚，作业停留在 in_progress，可通过 /retry 整体重试
            raise ApiError(500, "merge_phase_failed",
                           f"合并执行失败且已回滚，未改动任何案例，可重试作业 {merge_id}: {exc}") from exc

    def _execute_merge(self, merge_id: int, actor: str, role: str, refresh_snapshots: bool) -> dict[str, Any]:
        # 阶段二：在单个事务里完成版本校验、改挂和留痕；任何失败整体回滚，
        # case_merges 仍停留在 in_progress，可以整笔重试。
        with self.repo.write_tx() as conn:
            merge = conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()
            if not merge:
                raise ApiError(404, "merge_not_found", "合并作业不存在")
            if merge["status"] != "in_progress":
                return {"merge": self._merge_json(conn, merge), "idempotent": True}
            master_id = merge["master_case_id"]
            members = self._members(conn, merge_id)
            cases = {m["case_id"]: self._case(conn, m["case_id"]) for m in members}
            master = cases[master_id]
            if master["status"] != "open":
                raise ApiError(409, "merge_conflict", "主案例在合并期间状态已变化，作业保留待重试或中止")
            if refresh_snapshots:
                # 重试：接受期间先到的写入，用当前版本重新留快照
                conn.execute("UPDATE case_merge_members SET revision_snapshot=? WHERE merge_id=? AND case_id=?",
                             (master["revision"], merge_id, master_id))
                master_snapshot = master["revision"]
            else:
                master_snapshot = next(
                    m["revision_snapshot"] for m in members if m["case_id"] == master_id)
                if master["revision"] != master_snapshot:
                    raise ApiError(409, "merge_revision_conflict",
                                   "主案例在合并期间收到并发更新（先到已保留），请重试合并")
            for member in members:
                row = cases[member["case_id"]]
                if row["id"] == master_id:
                    continue
                if row["status"] != "open":
                    raise ApiError(409, "merge_conflict", f"案例 {row['case_no']} 在合并期间已被处理，请中止本次合并")
                if refresh_snapshots:
                    conn.execute("UPDATE case_merge_members SET revision_snapshot=? WHERE merge_id=? AND case_id=?",
                                 (row["revision"], merge_id, row["id"]))
                elif row["revision"] != member["revision_snapshot"]:
                    raise ApiError(409, "merge_revision_conflict",
                                   f"案例 {row['case_no']} 在合并期间收到并发更新（先到已保留），请重试合并")
            submitted = self._submitted_reports(conn, [m["case_id"] for m in members])

            if PharmacovigilanceService._merge_phase2_hook:
                PharmacovigilanceService._merge_phase2_hook(merge_id, conn)

            now = iso()
            next_revision = master_snapshot
            moved_followups = 0
            for member in members:
                cid = member["case_id"]
                if cid == master_id:
                    continue
                intakes = conn.execute("SELECT id FROM intakes WHERE case_id=?", (cid,)).fetchall()
                followups = conn.execute(
                    "SELECT id,revision FROM followups WHERE case_id=? ORDER BY revision,id", (cid,)).fetchall()
                for r in intakes:
                    conn.execute(
                        "INSERT INTO merge_movements(merge_id,entity_type,entity_id,from_case_id,to_case_id,moved_at) VALUES(?,?,?,?,?,?)",
                        (merge_id, "intake", r["id"], cid, master_id, now),
                    )
                for r in followups:
                    next_revision += 1
                    moved_followups += 1
                    conn.execute(
                        "INSERT INTO merge_movements(merge_id,entity_type,entity_id,from_case_id,to_case_id,original_revision,moved_at) VALUES(?,?,?,?,?,?,?)",
                        (merge_id, "followup", r["id"], cid, master_id, r["revision"], now),
                    )
                    # 改挂到主案例时在主案例版本序列内重新编号，原始版本记入 original_revision
                    conn.execute(
                        "UPDATE followups SET case_id=?,revision=?,original_revision=? WHERE id=?",
                        (master_id, next_revision, r["revision"], r["id"]),
                    )
                conn.execute("UPDATE intakes SET case_id=? WHERE case_id=?", (master_id, cid))
                conn.execute(
                    "UPDATE cases SET status='merged',merged_into=?,updated_at=? WHERE id=?",
                    (master_id, now, cid))
            if moved_followups:
                conn.execute("UPDATE cases SET revision=?,updated_at=? WHERE id=?",
                             (next_revision, now, master_id))
            irreversible = 1 if submitted else 0
            reason = self._irreversible_reason(submitted) if submitted else None
            conn.execute(
                "UPDATE case_merges SET status='completed',irreversible=?,irreversible_reason=?,completed_at=? WHERE id=?",
                (irreversible, reason, now, merge_id),
            )
            Repository.audit(conn, master_id, actor, role, "case_merge_completed",
                             {"merge_id": merge_id, "irreversible": bool(irreversible),
                              "reason": reason})
            return {"merge": self._merge_json(conn, conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()),
                    "idempotent": False}

    def retry_merge(self, merge_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以重试合并")
        # 默认沿用阶段一的版本快照（与并发随访冲突时应先到先得，冲突由调用方决定）；
        # 显式 refresh_snapshots=true 表示接受期间的随访写入，按当前版本整笔重试。
        refresh = bool(body.get("refresh_snapshots", False))
        with self.repo.write_tx() as conn:
            merge = conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()
            if not merge:
                raise ApiError(404, "merge_not_found", "合并作业不存在")
            if merge["status"] != "in_progress":
                raise ApiError(409, "merge_not_pending", f"合并作业当前状态为 {merge['status']}，无需重试")
        try:
            return self._execute_merge(merge_id, actor, role, refresh_snapshots=refresh)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(500, "merge_phase_failed",
                           f"合并重试失败且已回滚，作业 {merge_id} 仍可再次重试: {exc}") from exc

    def revert_merge(self, merge_id: int, actor: str, role: str) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以撤销合并")
        with self.repo.write_tx() as conn:
            merge = conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()
            if not merge:
                raise ApiError(404, "merge_not_found", "合并作业不存在")
            if merge["status"] != "completed":
                raise ApiError(409, "merge_not_revertible", f"合并作业状态为 {merge['status']}，不能撤销")
            if merge["irreversible"]:
                raise ApiError(409, "merge_irreversible",
                               merge["irreversible_reason"] or "该合并已被标记为不可撤销")
            members = self._members(conn, merge_id)
            master_id = merge["master_case_id"]
            cases = {m["case_id"]: self._case(conn, m["case_id"]) for m in members}
            master = cases[master_id]
            if master["status"] == "merged":
                raise ApiError(409, "merge_conflict", "主案例自身已被并入其他案例，无法撤销本合并")
            for member in members:
                cid = member["case_id"]
                if cid == master_id:
                    continue
                row = cases[cid]
                if row["status"] != "merged" or row["merged_into"] != master_id:
                    raise ApiError(409, "merge_state_changed",
                                   f"案例 {row['case_no']} 在合并后又发生过状态变化，不能自动撤销")
            # 实时复核：合并之后新提交的国家报告同样无法拆回
            submitted = self._submitted_reports(conn, [m["case_id"] for m in members])
            if submitted:
                raise ApiError(409, "merge_irreversible", self._irreversible_reason(submitted))

            movements = conn.execute("SELECT * FROM merge_movements WHERE merge_id=? ORDER BY id", (merge_id,)).fetchall()
            master_snapshot = conn.execute(
                "SELECT revision_snapshot FROM case_merge_members WHERE merge_id=? AND case_id=?",
                (merge_id, master_id)).fetchone()["revision_snapshot"]
            now = iso()
            for mv in movements:
                if mv["entity_type"] == "intake":
                    conn.execute("UPDATE intakes SET case_id=? WHERE id=? AND case_id=?",
                                 (mv["from_case_id"], mv["entity_id"], master_id))
                else:
                    # 随访退回原案例，恢复原始版本号
                    conn.execute(
                        "UPDATE followups SET case_id=?,revision=?,original_revision=NULL WHERE id=? AND case_id=?",
                        (mv["from_case_id"], mv["original_revision"], mv["entity_id"], master_id),
                    )
            for member in members:
                cid = member["case_id"]
                if cid != master_id:
                    conn.execute(
                        "UPDATE cases SET status='open',merged_into=NULL,updated_at=? WHERE id=?",
                        (now, cid))
            # 主案例 revision 回到合并前快照；只重排合并后才在主案例上新增的随访（revision 大于快照）
            remaining = conn.execute(
                "SELECT id FROM followups WHERE case_id=? AND origin_case_id=? AND revision>? ORDER BY revision,id",
                (master_id, master_id, master_snapshot)).fetchall()
            new_revision = master_snapshot
            for r in remaining:
                new_revision += 1
                conn.execute("UPDATE followups SET revision=? WHERE id=?", (new_revision, r["id"]))
            conn.execute("UPDATE cases SET revision=?,updated_at=? WHERE id=?",
                         (new_revision, now, master_id))
            conn.execute("UPDATE case_merges SET status='reverted',reverted_at=?,reverted_by=? WHERE id=?",
                         (now, actor, merge_id))
            Repository.audit(conn, master_id, actor, role, "case_merge_reverted",
                             {"merge_id": merge_id, "movements": len(movements)})
            return {"merge": self._merge_json(conn, conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone())}

    def abort_merge(self, merge_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以中止合并")
        reason = str(body.get("reason", "")).strip() or "管理员手动中止"
        with self.repo.write_tx() as conn:
            merge = conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()
            if not merge:
                raise ApiError(404, "merge_not_found", "合并作业不存在")
            if merge["status"] != "in_progress":
                raise ApiError(409, "merge_not_pending", f"合并作业状态为 {merge['status']}，不能中止")
            now = iso()
            conn.execute("UPDATE case_merges SET status='aborted',aborted_at=?,aborted_by=?,abort_reason=? WHERE id=?",
                         (now, actor, reason, merge_id))
            Repository.audit(conn, merge["master_case_id"], actor, role, "case_merge_aborted",
                             {"merge_id": merge_id, "reason": reason})
            return {"merge": self._merge_json(conn, conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone())}

    def list_merges(self, role: str, status_filter: str | None = None) -> list[dict[str, Any]]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以查看合并作业")
        sql = "SELECT * FROM case_merges"
        args: list[Any] = []
        if status_filter:
            sql += " WHERE status=?"
            args.append(status_filter)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def get_merge(self, merge_id: int, role: str) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以查看合并作业")
        merge = self.repo.conn.execute("SELECT * FROM case_merges WHERE id=?", (merge_id,)).fetchone()
        if not merge:
            raise ApiError(404, "merge_not_found", "合并作业不存在")
        return self._merge_json(self.repo.conn, merge)

    def overdue(self, role: str, region: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reports WHERE status!='submitted' AND due_at < ?"
        args: list[Any] = [iso()]
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND case_id IN (SELECT id FROM cases WHERE region=?)"
            args.append(region)
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def escalate_overdue(self, actor: str, role: str, region: str) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "escalation_forbidden", "当前角色不能执行逾期升级")
        rows = self.overdue(role, region)
        with self.repo.write_tx() as conn:
            for row in rows:
                conn.execute("UPDATE reports SET status='overdue' WHERE id=? AND status='pending'", (row["id"],))
                Repository.audit(conn, row["case_id"], actor, role, "report_overdue_escalated", {"report_id": row["id"], "country": row["country"]})
        return {"escalated": len(rows)}

    def state(self, role: str, region: str) -> dict[str, Any]:
        cases = self.list_cases(role, region, {})
        payload: dict[str, Any] = {"cases": cases, "overdue": self.overdue(role, region), "server_time": iso()}
        if role == "global_admin":
            payload["merges"] = self.list_merges(role)
        return payload


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: PharmacovigilanceService
    web_root: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            result = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return result

    def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> Any:
        if path == "/health":
            return 200, {"status": "ok", "service": "pharmacovigilance"}
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/state":
            return 200, self.service.state(role, region)
        if path == "/api/cases":
            return 200, {"cases": self.service.list_cases(role, region, query)}
        if path == "/api/overdue":
            return 200, {"reports": self.service.overdue(role, region)}
        parts = [part for part in path.split("/") if part]
        if parts[:2] == ["api", "merges"]:
            if len(parts) == 2:
                status_filter = query.get("status", [None])[0]
                return 200, {"merges": self.service.list_merges(role, status_filter)}
            if len(parts) == 3 and parts[2].isdigit():
                return 200, self.service.get_merge(int(parts[2]), role)
        if len(parts) == 3 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            return 200, self.service.get_case(int(parts[2]), role, region)
        raise ApiError(404, "not_found", "接口不存在")

    def _dispatch_post(self, path: str, body: dict[str, Any]) -> Any:
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/cases":
            return 201, self.service.create_case(actor, role, region, body)
        if path == "/api/escalate-overdue":
            return 200, self.service.escalate_overdue(actor, role, region)
        parts = [part for part in path.split("/") if part]
        if parts[:2] == ["api", "merges"]:
            if len(parts) == 4 and parts[2].isdigit():
                merge_id, action = int(parts[2]), parts[3]
                if action == "retry":
                    return 200, self.service.retry_merge(merge_id, actor, role, body)
                if action == "revert":
                    return 200, self.service.revert_merge(merge_id, actor, role)
                if action == "abort":
                    return 200, self.service.abort_merge(merge_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            case_id, action = int(parts[2]), parts[3]
            if action == "followups":
                return 201, self.service.add_followup(case_id, actor, role, region, body)
            if action == "medical-review":
                return 200, self.service.medical_review(case_id, actor, role, body)
            if action == "reports":
                return 201, self.service.create_report(case_id, actor, role, region, body)
            if action == "merge":
                return 200, self.service.merge_cases(case_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[2].isdigit() and parts[3] == "submit":
            return 200, self.service.submit_report(int(parts[2]), actor, role, region, body)
        raise ApiError(404, "not_found", "接口不存在")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                page = (self.web_root / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if method == "GET":
                status, payload = self._dispatch_get(parsed.path, parse_qs(parsed.query))
            else:
                status, payload = self._dispatch_post(parsed.path, self._body())
            json_response(self, status, payload)
        except ApiError as exc:
            json_response(self, exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:
            print(f"unhandled error: {exc!r}")
            json_response(self, 500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = PharmacovigilanceService(db_path)
    web_root = Path(__file__).resolve().parent / "static"
    handler = type("PharmacovigilanceHandler", (Handler,), {"service": service, "web_root": web_root})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=os.environ.get("PV_DB", "pharmacovigilance.db"))
    args = parser.parse_args()
    server = create_server(args.db, args.host, args.port)
    print(f"pharmacovigilance listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

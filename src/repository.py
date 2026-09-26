"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spare_stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    warehouse TEXT NOT NULL,
                    cable TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    total_km REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(warehouse, cable, segment)
                );
                CREATE TABLE IF NOT EXISTS spare_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_id INTEGER NOT NULL REFERENCES spare_stock(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    reserved_km REAL NOT NULL,
                    used_km REAL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reservation_record ON spare_reservations(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_reservation_stock ON spare_reservations(stock_id, status);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], reservation_op: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            spare_info = None
            if reservation_op:
                spare_info = self._apply_reservation_op(connection, record_id, reservation_op, actor_id, now)
            version = int(expected_version) + 1
            if spare_info:
                details = dict(details)
                details["spare"] = spare_info
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _held_km(connection: sqlite3.Connection, stock_id: int) -> float:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_km WHEN status='consumed' THEN used_km ELSE 0 END),0) AS held FROM spare_reservations WHERE stock_id=? AND status IN ('reserved','consumed')",
            (stock_id,),
        ).fetchone()
        return float(row["held"])

    def _stock_view(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        held = self._held_km(connection, int(item["id"]))
        item["held_km"] = round(held, 2)
        item["available_km"] = round(float(item["total_km"]) - held, 2)
        return item

    def _apply_reservation_op(self, connection: sqlite3.Connection, record_id: int, op: Dict[str, Any], actor_id: str, now: str) -> Optional[Dict[str, Any]]:
        kind = op.get("kind")
        if kind == "reserve":
            stock = connection.execute("SELECT * FROM spare_stock WHERE id=?", (int(op["stock_id"]),)).fetchone()
            if stock is None:
                connection.rollback()
                raise NotFound("备缆库存不存在")
            reserved_km = float(op["reserved_km"])
            available = float(stock["total_km"]) - self._held_km(connection, int(stock["id"]))
            if available + 1e-9 < reserved_km:
                connection.rollback()
                raise Conflict("仓库备缆余量不足，无法预占")
            connection.execute(
                "INSERT INTO spare_reservations(stock_id,record_id,reserved_km,used_km,status,created_by,created_at,updated_at) VALUES(?,?,?,NULL,'reserved',?,?,?)",
                (int(stock["id"]), record_id, reserved_km, actor_id, now, now),
            )
            return {"stock_id": int(stock["id"]), "warehouse": stock["warehouse"], "reserved_km": round(reserved_km, 2), "available_km": round(available - reserved_km, 2)}
        if kind == "consume":
            row = connection.execute(
                "SELECT * FROM spare_reservations WHERE record_id=? AND status='reserved' ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
            if row is None:
                return None
            used_km = float(op["used_km"])
            reserved_km = float(row["reserved_km"])
            if used_km > reserved_km + 1e-9:
                stock = connection.execute("SELECT * FROM spare_stock WHERE id=?", (int(row["stock_id"]),)).fetchone()
                available = float(stock["total_km"]) - self._held_km(connection, int(row["stock_id"]))
                if available + 1e-9 < used_km - reserved_km:
                    connection.rollback()
                    raise Conflict("实际使用超出预占，仓库余量不足")
            connection.execute(
                "UPDATE spare_reservations SET status='consumed', used_km=?, updated_at=? WHERE id=?",
                (used_km, now, int(row["id"])),
            )
            return {"stock_id": int(row["stock_id"]), "reserved_km": round(reserved_km, 2), "used_km": round(used_km, 2), "returned_km": round(max(reserved_km - used_km, 0.0), 2)}
        if kind == "release":
            row = connection.execute(
                "SELECT * FROM spare_reservations WHERE record_id=? AND status='reserved' ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE spare_reservations SET status='released', updated_at=? WHERE id=?",
                (now, int(row["id"])),
            )
            return {"stock_id": int(row["stock_id"]), "released_km": round(float(row["reserved_km"]), 2)}
        connection.rollback()
        raise ValidationError("未知的备缆操作")

    def create_stock(self, warehouse: str, cable: str, segment: str, total_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO spare_stock(warehouse,cable,segment,total_km,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (warehouse, cable, segment, total_km, actor_id, actor_id, now, now),
                )
                stock_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise Conflict("该仓库已登记此光缆区段的备缆台账") from exc
        return self.get_stock(stock_id)

    def get_stock(self, stock_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM spare_stock WHERE id=?", (stock_id,)).fetchone()
            if row is None:
                raise NotFound("备缆库存不存在")
            return self._stock_view(connection, row)

    def list_stock(self, cable: Optional[str] = None, segment: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM spare_stock"
        clauses = []
        params: List[Any] = []
        if cable:
            clauses.append("cable=?")
            params.append(cable)
        if segment:
            clauses.append("segment=?")
            params.append(segment)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY warehouse, cable, segment"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self._stock_view(connection, row) for row in rows]

    def adjust_stock(self, stock_id: int, delta_km: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM spare_stock WHERE id=?", (stock_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("备缆库存不存在")
            new_total = float(row["total_km"]) + float(delta_km)
            if new_total <= 0:
                connection.rollback()
                raise ValidationError("调整后总量必须大于0")
            if new_total + 1e-9 < self._held_km(connection, stock_id):
                connection.rollback()
                raise Conflict("调整后总量低于已占用长度")
            connection.execute(
                "UPDATE spare_stock SET total_km=?, updated_by=?, updated_at=? WHERE id=?",
                (new_total, actor_id, now, stock_id),
            )
            connection.commit()
        return self.get_stock(stock_id)

    def reservations_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM spare_reservations WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

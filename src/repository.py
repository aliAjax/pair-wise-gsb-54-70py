"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS warehouses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spare_stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                    cable TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    total_km REAL NOT NULL,
                    available_km REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(warehouse_id, cable, segment)
                );
                CREATE TABLE IF NOT EXISTS spare_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    stock_id INTEGER NOT NULL REFERENCES spare_stock(id),
                    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                    cable TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    reserved_km REAL NOT NULL,
                    used_km REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reservations_record ON spare_reservations(record_id, status);
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], tx_hook=None) -> Dict[str, Any]:
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
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            if tx_hook is not None:
                tx_hook(connection)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

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

    def create_warehouse(self, name: str, actor_id: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO warehouses(name,created_by,created_at) VALUES(?,?,?)",
                    (name, actor_id, _now()),
                )
                row = connection.execute("SELECT * FROM warehouses WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("仓库名称已存在") from exc
        result = dict(row)
        result["stock"] = []
        return result

    def get_warehouse(self, warehouse_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()
        if row is None:
            raise NotFound("仓库不存在")
        return dict(row)

    def list_warehouses(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            warehouses = connection.execute("SELECT * FROM warehouses ORDER BY id").fetchall()
            stock_rows = connection.execute("SELECT * FROM spare_stock ORDER BY warehouse_id, cable, segment").fetchall()
        stock_by_warehouse: Dict[int, List[Dict[str, Any]]] = {}
        for row in stock_rows:
            stock_by_warehouse.setdefault(int(row["warehouse_id"]), []).append(
                {
                    "cable": row["cable"],
                    "segment": row["segment"],
                    "total_km": row["total_km"],
                    "available_km": row["available_km"],
                    "held_km": round(float(row["total_km"]) - float(row["available_km"]), 3),
                }
            )
        return [
            {"id": w["id"], "name": w["name"], "created_by": w["created_by"], "created_at": w["created_at"], "stock": stock_by_warehouse.get(int(w["id"]), [])}
            for w in warehouses
        ]

    def upsert_stock(self, warehouse_id: int, cable: str, segment: str, km: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO spare_stock(warehouse_id,cable,segment,total_km,available_km,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(warehouse_id,cable,segment) DO UPDATE SET total_km=round(total_km+excluded.total_km,3), available_km=round(available_km+excluded.available_km,3), updated_at=excluded.updated_at",
                (warehouse_id, cable, segment, km, km, now),
            )
            row = connection.execute(
                "SELECT * FROM spare_stock WHERE warehouse_id=? AND cable=? AND segment=?",
                (warehouse_id, cable, segment),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _held_reservation(connection: sqlite3.Connection, record_id: int) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM spare_reservations WHERE record_id=? AND status='held' ORDER BY id DESC LIMIT 1",
            (record_id,),
        ).fetchone()

    def apply_stock_effect(self, connection: sqlite3.Connection, effect: Dict[str, Any]) -> None:
        """在mutate事务内应用备缆台账副作用，条件更新保证同一段余量不会被重复占用。"""
        kind = effect.get("type")
        now = _now()
        if kind == "reserve":
            km = round(float(effect["km"]), 3)
            row = connection.execute(
                "SELECT * FROM spare_stock WHERE warehouse_id=? AND cable=? AND segment=?",
                (effect["warehouse_id"], effect["cable"], effect["segment"]),
            ).fetchone()
            if row is None:
                raise Conflict("该仓库无此区段备缆台账")
            if float(row["available_km"]) + 1e-9 < km:
                raise Conflict("仓库备缆余量不足")
            cursor = connection.execute(
                "UPDATE spare_stock SET available_km=round(available_km-?,3), updated_at=? WHERE id=? AND available_km>=?",
                (km, now, row["id"], km),
            )
            if cursor.rowcount == 0:
                raise Conflict("仓库备缆余量不足")
            connection.execute(
                "INSERT INTO spare_reservations(record_id,stock_id,warehouse_id,cable,segment,reserved_km,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (effect["record_id"], row["id"], effect["warehouse_id"], effect["cable"], effect["segment"], km, "held", now, now),
            )
        elif kind == "consume":
            held = self._held_reservation(connection, int(effect["record_id"]))
            if held is None:
                return
            reserved = float(held["reserved_km"])
            used = round(float(effect["used_km"]), 3)
            consumed = round(min(used, reserved), 3)
            returned = round(reserved - consumed, 3)
            connection.execute(
                "UPDATE spare_stock SET available_km=round(available_km+?,3), total_km=round(total_km-?,3), updated_at=? WHERE id=?",
                (returned, consumed, now, held["stock_id"]),
            )
            connection.execute(
                "UPDATE spare_reservations SET status='consumed', used_km=?, updated_at=? WHERE id=?",
                (used, now, held["id"]),
            )
        elif kind == "release":
            held = self._held_reservation(connection, int(effect["record_id"]))
            if held is None:
                return
            connection.execute(
                "UPDATE spare_stock SET available_km=round(available_km+?,3), updated_at=? WHERE id=?",
                (float(held["reserved_km"]), now, held["stock_id"]),
            )
            connection.execute(
                "UPDATE spare_reservations SET status='released', updated_at=? WHERE id=?",
                (now, held["id"]),
            )

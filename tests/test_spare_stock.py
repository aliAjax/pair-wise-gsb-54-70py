import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
STOCK_DATA = {"warehouse": "WH-1", "cable": "SEA-1", "segment": "S3", "total_km": 20}
REQUIRED_KM = 15.75  # (135-120) * 1.05


class SpareStockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.keeper = Actor("keeper", "warehouse_keeper")

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, data=None):
        return self.service.register_stock(self.keeper, data or STOCK_DATA)

    def _create_approved(self, reference, data=None):
        record = self.service.create(Actor("creator", "noc_operator"), reference, data or CREATE_DATA)
        return self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})

    def _mobilize(self, record, stock_id, role="dispatcher"):
        data = {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1", "stock_id": stock_id}
        return self.service.act(Actor("worker", role), record["id"], record["version"], "mobilize", data)

    def _available(self, stock_id):
        for item in self.service.list_stock(self.keeper):
            if item["id"] == stock_id:
                return item["available_km"]
        raise AssertionError("库存不存在")

    def test_register_list_and_adjust_stock(self):
        stock = self._register()
        self.assertEqual(stock["available_km"], 20)
        self.assertEqual(stock["held_km"], 0)
        with self.assertRaises(PermissionDenied):
            self.service.register_stock(Actor("op", "noc_operator"), {"warehouse": "WH-2", "cable": "SEA-1", "segment": "S3", "total_km": 5})
        with self.assertRaises(Conflict):
            self._register()
        with self.assertRaises(ValidationError):
            self._register({"warehouse": "WH-2", "cable": "SEA-1", "segment": "S3", "total_km": 0})
        adjusted = self.service.adjust_stock(self.keeper, stock["id"], {"delta_km": 10})
        self.assertEqual(adjusted["total_km"], 30)
        self.assertEqual(adjusted["available_km"], 30)
        with self.assertRaises(ValidationError):
            self.service.adjust_stock(self.keeper, stock["id"], {"delta_km": -100})

    def test_mobilize_reserves_and_insufficient_is_rejected(self):
        stock = self._register()
        first = self._create_approved("CABLE-40001")
        first = self._mobilize(first, stock["id"])
        self.assertEqual(first["state"], "mobilized")
        self.assertEqual(first["payload"]["spare_reserved_km"], REQUIRED_KM)
        self.assertEqual(self._available(stock["id"]), 20 - REQUIRED_KM)
        second_data = dict(CREATE_DATA)
        second_data["start_km"] = 200.0
        second_data["end_km"] = 215.0
        second = self._create_approved("CABLE-40002", second_data)
        with self.assertRaises(Conflict):
            self._mobilize(second, stock["id"])
        unchanged = self.service.get_record(self.keeper, second["id"])
        self.assertEqual(unchanged["state"], "approved")
        self.assertEqual(self._available(stock["id"]), 20 - REQUIRED_KM)

    def test_mobilize_requires_matching_segment_and_existing_stock(self):
        stock = self._register({"warehouse": "WH-9", "cable": "SEA-1", "segment": "S9", "total_km": 50})
        record = self._create_approved("CABLE-40003")
        with self.assertRaises(ValidationError):
            self._mobilize(record, stock["id"])
        with self.assertRaises(NotFound):
            self._mobilize(record, 9999)
        with self.assertRaises(ValidationError):
            self._mobilize(record, None)

    def test_splice_consumes_actual_and_returns_difference(self):
        stock = self._register()
        record = self._create_approved("CABLE-40004")
        record = self._mobilize(record, stock["id"])
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice", {"splice_loss_db": 0.12, "spare_used_km": 15.5})
        self.assertEqual(record["state"], "spliced")
        self.assertEqual(self._available(stock["id"]), 20 - 15.5)
        reservations = self.service.get_record(self.keeper, record["id"])["spare_reservations"]
        self.assertEqual(reservations[0]["status"], "consumed")
        self.assertEqual(reservations[0]["used_km"], 15.5)

    def test_splice_excess_needs_warehouse_headroom(self):
        stock = self._register({"warehouse": "WH-2", "cable": "SEA-1", "segment": "S3", "total_km": 16})
        record = self._create_approved("CABLE-40005")
        record = self._mobilize(record, stock["id"])
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        with self.assertRaises(Conflict):
            self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice", {"splice_loss_db": 0.12, "spare_used_km": 16.5})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice", {"splice_loss_db": 0.12, "spare_used_km": 16})
        self.assertEqual(record["state"], "spliced")
        self.assertEqual(self._available(stock["id"]), 0)

    def test_cancel_releases_reservation(self):
        stock = self._register()
        record = self._create_approved("CABLE-40006")
        record = self._mobilize(record, stock["id"])
        self.assertEqual(self._available(stock["id"]), 20 - REQUIRED_KM)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "cancel", {"cancel_reason": "海况恶化"})
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(self._available(stock["id"]), 20)
        reservations = self.service.get_record(self.keeper, record["id"])["spare_reservations"]
        self.assertEqual(reservations[0]["status"], "released")

    def test_cancel_without_reservation_is_noop(self):
        record = self._create_approved("CABLE-40007")
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "cancel", {"cancel_reason": "误报"})
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(self.service.get_record(self.keeper, record["id"])["spare_reservations"], [])

    def test_concurrent_mobilize_never_double_books(self):
        stock = self._register()
        first = self._create_approved("CABLE-40008")
        second_data = dict(CREATE_DATA)
        second_data["start_km"] = 200.0
        second_data["end_km"] = 215.0
        second = self._create_approved("CABLE-40009", second_data)
        barrier = threading.Barrier(2)
        outcomes = []

        def mobilize(record, user):
            barrier.wait()
            try:
                self._mobilize(record, stock["id"])
                outcomes.append((user, "ok"))
            except Conflict:
                outcomes.append((user, "conflict"))

        threads = [threading.Thread(target=mobilize, args=(first, "d1")), threading.Thread(target=mobilize, args=(second, "d2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result for _, result in outcomes), ["conflict", "ok"])
        self.assertEqual(self._available(stock["id"]), 20 - REQUIRED_KM)
        states = {self.service.get_record(self.keeper, first["id"])["state"], self.service.get_record(self.keeper, second["id"])["state"]}
        self.assertEqual(states, {"approved", "mobilized"})


if __name__ == "__main__":
    unittest.main()

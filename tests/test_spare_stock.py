import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
MOBILIZE_DATA = {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}


class SpareStockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.keeper = Actor("keeper", "warehouse_admin")
        self.warehouse = self.service.create_warehouse(self.keeper, "浦东仓")
        self.service.add_stock(self.keeper, self.warehouse["id"], {"cable": "SEA-1", "segment": "S3", "available_km": 20})

    def tearDown(self):
        self.temp.cleanup()

    def _approved_record(self, reference, start_km, end_km):
        record = self.service.create(Actor("creator", "noc_operator"), reference, dict(CREATE_DATA, start_km=start_km, end_km=end_km))
        return self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})

    def _mobilize(self, record, warehouse_id=None):
        data = dict(MOBILIZE_DATA, warehouse_id=warehouse_id or self.warehouse["id"])
        return self.service.act(Actor("disp", "dispatcher"), record["id"], record["version"], "mobilize", data)

    def _stock(self, warehouse_id=None):
        wanted = warehouse_id or self.warehouse["id"]
        for warehouse in self.service.list_warehouses(Actor("viewer", "noc_operator")):
            if warehouse["id"] == wanted:
                return warehouse["stock"][0]
        raise AssertionError("仓库不存在")

    def test_stock_management_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_warehouse(Actor("op", "noc_operator"), "越权仓")
        with self.assertRaises(PermissionDenied):
            self.service.add_stock(Actor("op", "noc_operator"), self.warehouse["id"], {"cable": "SEA-1", "segment": "S3", "available_km": 5})
        with self.assertRaises(PermissionDenied):
            self.service.create_warehouse(Actor("ghost", "ghost_role"), "幽灵仓")

    def test_mobilize_reserves_and_page_shows_remaining(self):
        record = self._approved_record("CABLE-40001", 120.0, 135.0)
        record = self._mobilize(record)
        self.assertEqual(record["state"], "mobilized")
        self.assertEqual(record["payload"]["spare_warehouse_id"], self.warehouse["id"])
        self.assertEqual(record["payload"]["spare_reserved_km"], 15.75)
        stock = self._stock()
        self.assertEqual(stock["available_km"], 4.25)
        self.assertEqual(stock["held_km"], 15.75)

    def test_insufficient_stock_is_rejected(self):
        small = self.service.create_warehouse(self.keeper, "浅水仓")
        self.service.add_stock(self.keeper, small["id"], {"cable": "SEA-1", "segment": "S3", "available_km": 10})
        record = self._approved_record("CABLE-40002", 120.0, 135.0)
        with self.assertRaises(Conflict):
            self._mobilize(record, small["id"])
        record = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(record["state"], "approved")
        self.assertEqual(self._stock(small["id"])["available_km"], 10)
        record = self._mobilize(record)
        self.assertEqual(record["state"], "mobilized")

    def test_splice_consumes_actual_length(self):
        record = self._approved_record("CABLE-40003", 120.0, 135.0)
        record = self._mobilize(record)
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        record = self.service.act(Actor("eng", "cable_engineer"), record["id"], record["version"], "splice", {"splice_loss_db": 0.12, "spare_used_km": 15.5})
        self.assertEqual(record["state"], "spliced")
        stock = self._stock()
        self.assertEqual(stock["available_km"], 4.5)
        self.assertEqual(stock["total_km"], 4.5)
        self.assertEqual(stock["held_km"], 0)

    def test_cancel_releases_reservation(self):
        record = self._approved_record("CABLE-40004", 120.0, 135.0)
        record = self._mobilize(record)
        self.assertEqual(self._stock()["available_km"], 4.25)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "cancel", {"cancel_reason": "海况恶化"})
        self.assertEqual(record["state"], "cancelled")
        stock = self._stock()
        self.assertEqual(stock["available_km"], 20)
        self.assertEqual(stock["held_km"], 0)

    def test_concurrent_mobilize_no_double_occupancy(self):
        first = self._approved_record("CABLE-40010", 120.0, 135.0)
        second = self._approved_record("CABLE-40011", 200.0, 215.0)
        succeeded = []
        rejected = []

        def mobilize(record):
            try:
                self._mobilize(record)
                succeeded.append(record["id"])
            except Conflict:
                rejected.append(record["id"])

        threads = [threading.Thread(target=mobilize, args=(record,)) for record in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(rejected), 1)
        stock = self._stock()
        self.assertEqual(stock["available_km"], 4.25)
        self.assertEqual(stock["held_km"], 15.75)


if __name__ == "__main__":
    unittest.main()

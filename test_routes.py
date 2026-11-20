import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

import dotenv

# Import app without touching production credentials.
dotenv.load_dotenv = lambda *args, **kwargs: False
import db  # noqa: E402


class RouteTests(unittest.TestCase):
    def setUp(self):
        import importlib
        self.module = importlib.import_module("app")
        self.client = self.module.app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tempdir.name, "test.sqlite")
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("CREATE TABLE transactions (item_id TEXT, transaction_id TEXT, name TEXT, amount REAL, date TEXT, pending INTEGER, transaction_type TEXT)")
        conn.execute("CREATE TABLE bills (item_id TEXT, name TEXT, amount REAL, next_due_date TEXT, interval_days INTEGER)")
        conn.execute("CREATE TABLE income (item_id TEXT, name TEXT, amount REAL, next_pay_date TEXT, interval_days INTEGER)")
        today = date.today().isoformat()
        conn.execute("INSERT INTO transactions VALUES ('item', 'posted', 'Paycheck', -10, ?, 0, 'standard')", (today,))
        conn.execute("INSERT INTO income VALUES ('item', 'Paycheck', 10, ?, 30)", (today,))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tempdir.cleanup()

    def test_projection_route_deduplicates_posted_today(self):
        with patch.object(self.module, "current_item_id", return_value="item"), \
             patch.object(self.module, "fetch_balances", return_value=[{
                 "account_subtype": "checking", "available": 1000, "current": 1000,
                 "fetched_at": datetime.now().isoformat(),
             }]), \
             patch.object(self.module, "refresh_recurring", return_value="plaid"):
            response = self.client.get("/api/cashflow_projection?days=7")
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["timeline"][0]["events"], [])

    def test_link_fallback_and_dashboard_routes(self):
        self.assertEqual(self.client.get("/link").status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)


if __name__ == "__main__":
    unittest.main()

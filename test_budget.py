import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import db


class BudgetMonthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "budget.db"
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(self.path)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE transactions (
                item_id TEXT, transaction_id TEXT, account_id TEXT, date TEXT,
                name TEXT, amount REAL, category TEXT, pending INTEGER,
                transaction_type TEXT NOT NULL DEFAULT 'standard', type_override TEXT,
                PRIMARY KEY (item_id, transaction_id));
            CREATE TABLE bills (
                bill_key TEXT PRIMARY KEY, name TEXT, amount REAL,
                interval_days INTEGER, last_date TEXT, next_due_date TEXT, item_id TEXT);
        """)
        conn.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("item-a", "1", "acct", "2026-08-02", "Grocer", 25, "Food", 0, "standard", None),
            ("item-a", "2", "acct", "2026-08-03", "Pay", -100, "Income", 0, "standard", None),
            ("item-a", "3", "acct", "2026-08-04", "Transfer", 300, "Transfer", 0, "transfer", None),
            ("item-a", "4", "acct", "2026-08-05", "Pending", 99, "Food", 1, "standard", None),
            ("item-b", "5", "acct", "2026-08-06", "Other Item", 500, "Food", 0, "standard", None),
        ])
        conn.execute("INSERT INTO bills VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("b1", "Rent", 900, 30, "2026-07-01", "2026-08-10", "item-a"))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_read_only_and_item_scoped(self):
        before = self.path.read_bytes()
        with patch.object(db, "latest_item_id", return_value="item-a"):
            response = app_module.app.test_client().get("/api/budget/month?month=2026-08")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["totals"], {"spent": 25, "income": 100, "transaction_count": 3})
        self.assertEqual(payload["categories"], [{"category": "Food", "n": 1, "total": 25}])
        self.assertEqual(payload["bills"][0]["bill_key"], "b1")
        self.assertEqual(payload["transactions"][2]["transaction_type"], "transfer")
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_months(self):
        for month in ("2026-13", "2026-00", "2026-8", "bad"):
            with self.subTest(month=month):
                response = app_module.app.test_client().get(f"/api/budget/month?month={month}")
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

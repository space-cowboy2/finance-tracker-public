import os
import hashlib
import importlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

# Keep app imports in safety tests from loading real credentials or touching
# the production environment during test discovery.
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False

import db
import maintenance


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False)
        self.temp.close()
        self.original_path = db.DB_PATH
        db.DB_PATH = self.temp.name
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        os.unlink(self.temp.name)

    def insert(self, transaction_id, item_id, name, amount, category="FOOD_AND_DRINK"):
        db.upsert_transaction((transaction_id, "account", item_id, "2026-08-10", name, amount, category, 0, db.classify_transaction(name)))

    def insert_on(self, transaction_id, item_id, when, name, amount, category="FOOD_AND_DRINK"):
        db.upsert_transaction((transaction_id, "account", item_id, when.isoformat(), name, amount, category, 0, db.classify_transaction(name)))

    def test_analytics_never_mix_items(self):
        self.insert("current", "current-item", "POS Debit Grocer", 40)
        self.insert("old", "old-item", "POS Debit Grocer", 500)
        totals = db.category_totals("current-item")
        self.assertEqual(totals[0]["total"], 40)

    def test_pending_rows_excluded_from_every_aggregate(self):
        self.insert("posted", "current-item", "POS Debit Grocer", 40)
        db.upsert_transaction(("pending", "account", "current-item", "2026-08-10", "POS Debit Grocer", 400,
                               "FOOD_AND_DRINK", 1, "standard"))
        self.assertEqual(db.monthly_summary("current-item")[0]["total"], 40)
        self.assertEqual(db.category_totals("current-item")[0]["total"], 40)
        self.assertEqual(db.merchants("current-item")[0]["total"], 40)
        self.assertEqual(db.daily_flow("current-item")[0]["spent"], 40)
        self.assertEqual(db.month_totals("current-item")[0]["spent"], 40)
        self.assertEqual(db.fees_summary("current-item")["total"], 0)
        self.assertEqual(db.liquidation_summary("current-item")["count"], 0)

    def test_case_and_descriptor_variants_group_one_merchant(self):
        self.insert("one", "current-item", "POS Debit Auto ACH Lease Web", 100)
        self.insert("two", "current-item", "auto ach lease web", 120)
        merchants = db.merchants("current-item")
        self.assertEqual(len(merchants), 1)
        self.assertEqual(merchants[0]["visits"], 2)
        self.assertEqual(merchants[0]["total"], 220)

    def test_liquidation_visible_but_not_income_or_spend(self):
        self.insert("sale", "current-item", "BROKERAGE PPD ID", -700)
        self.assertEqual(db.month_totals("current-item"), [])
        self.assertEqual(db.liquidation_summary("current-item")["total"], 700)

    def test_only_brokerage_ppd_is_a_liquidation(self):
        self.assertEqual(db.classify_transaction("BROKERAGE PPD ID 123"), "liquidation")
        self.assertEqual(db.classify_transaction("BROKERAGE VISA PURCHASE"), "standard")

    def test_ambiguous_p2p_stays_standard(self):
        self.assertEqual(db.classify_transaction("BANK ACH P2P FRIEND"), "standard")
        self.assertEqual(db.classify_transaction("Online Transfer to Savings"), "transfer")

    def test_recurring_dates_roll_forward_and_ignore_transfers(self):
        today = date.today()
        for index, days_ago in enumerate((90, 60, 30)):
            self.insert_on(f"bill-{index}", "current-item", today - timedelta(days=days_ago), "Utility Company", 80)
            self.insert_on(f"transfer-{index}", "current-item", today - timedelta(days=days_ago), "Online Transfer to Savings", 900)
        db.detect_bills("current-item")
        bills = db.get_bills_upcoming("current-item")
        self.assertEqual(len(bills), 1)
        self.assertEqual(bills[0]["name"], "utility company")
        self.assertGreaterEqual(bills[0]["next_due_date"], today.isoformat())

    def test_manual_type_override_survives_later_sync(self):
        self.insert("override", "current-item", "BROKERAGE PPD ID", -700)
        db.set_transaction_type("current-item", "override", "standard")
        self.insert("override", "current-item", "BROKERAGE PPD ID", -700)
        self.assertEqual(db.month_totals("current-item")[0]["income"], 700)

    def test_duplicate_transaction_ids_are_isolated_by_item(self):
        self.insert("shared", "one", "one", 10)
        self.insert("shared", "two", "two", 20)
        self.assertEqual(db.category_totals("one")[0]["total"], 10)
        self.assertEqual(db.category_totals("two")[0]["total"], 20)

    def test_removal_and_override_are_item_scoped(self):
        self.insert("shared", "one", "one", 10)
        self.insert("shared", "two", "two", 20)
        db.set_transaction_type("one", "shared", "transfer")
        db.remove_transaction("two", "shared")
        self.assertEqual(db.liquidation_summary("one")["count"], 0)
        self.assertEqual(db.month_totals("one"), [])
        self.assertEqual(db.category_totals("two"), [])
        self.assertEqual(db.category_totals("one"), [])


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "legacy.db")
        self.backup = os.path.join(self.tempdir.name, "legacy.bak")
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript("""
                CREATE TABLE transactions (transaction_id TEXT PRIMARY KEY, account_id TEXT, item_id TEXT,
                    date TEXT, name TEXT, amount REAL, category TEXT, pending INTEGER);
                CREATE TABLE bills (bill_key TEXT PRIMARY KEY, name TEXT, amount REAL, interval_days INTEGER,
                    last_date TEXT, next_due_date TEXT, item_id TEXT);
                CREATE TABLE income (income_key TEXT PRIMARY KEY, name TEXT, amount REAL, interval_days INTEGER,
                    last_date TEXT, next_pay_date TEXT, item_id TEXT);
            """)
            conn.execute("INSERT INTO transactions VALUES ('legacy', 'account', NULL, '2026-08-01', 'Legacy', 1, NULL, 0)")
            conn.execute("INSERT INTO bills VALUES ('old', 'Old', 1, 30, '2026-08-01', '2026-09-01', NULL)")
            conn.execute("INSERT INTO income VALUES ('old', 'Old', 1, 30, '2026-08-01', '2026-09-01', NULL)")
            conn.commit()
        finally:
            conn.close()
        shutil.copyfile(self.path, self.backup)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_migration_is_idempotent_and_backup_preserves_legacy(self):
        with open(self.backup, "rb") as source:
            before = hashlib.sha256(source.read()).hexdigest()
        db.migrate_db(self.path)
        db.migrate_db(self.path)
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions WHERE item_id IS NULL").fetchone()[0], 1)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(balances)")}
            self.assertTrue({"account_type", "account_subtype", "available", "iso_currency_code", "fetched_at", "item_id"}.issubset(columns))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM income").fetchone()[0], 0)
        finally:
            conn.close()
        with open(self.backup, "rb") as source:
            self.assertEqual(hashlib.sha256(source.read()).hexdigest(), before)
        restored = os.path.join(self.tempdir.name, "restored.db")
        shutil.copyfile(self.backup, restored)
        conn = sqlite3.connect(restored)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        finally:
            conn.close()

    def test_maintenance_refuses_bad_backup(self):
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.path, self.path)
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.path, os.path.join(self.tempdir.name, "missing.db"))
        maintenance.verify_backup(self.path, self.backup)


class AppSafetyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dotenv
        dotenv.load_dotenv = lambda *args, **kwargs: False
        sys.modules.pop("app", None)
        cls.app_module = importlib.import_module("app")
        cls.client = cls.app_module.app.test_client()

    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False)
        self.temp.close()
        self.original_path = db.DB_PATH
        db.DB_PATH = self.temp.name
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        os.unlink(self.temp.name)

    def test_import_does_not_write_database(self):
        with open(self.temp.name, "rb") as source:
            before = hashlib.sha256(source.read()).hexdigest()
        importlib.reload(self.app_module)
        with open(self.temp.name, "rb") as source:
            self.assertEqual(before, hashlib.sha256(source.read()).hexdigest())

    def test_unresolved_item_blocks_writes(self):
        app = self.app_module
        original_token, original_item = app.get_access_token, app.current_item_id
        app.get_access_token, app.current_item_id = lambda: "token", lambda: None
        try:
            self.assertEqual(self.client.post("/api/sync_transactions").status_code, 503)
            self.assertEqual(self.client.patch("/api/transactions/x/type", json={"item_id": "one", "transaction_type": "standard"}).status_code, 503)
            conn = db.get_conn()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_cursor").fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            app.get_access_token, app.current_item_id = original_token, original_item

    def test_override_and_query_validation(self):
        app = self.app_module
        original_item = app.current_item_id
        app.current_item_id = lambda: "one"
        try:
            self.assertEqual(self.client.patch("/api/transactions/x/type", data="{", content_type="application/json").status_code, 400)
            self.assertEqual(self.client.patch("/api/transactions/x/type", json={"transaction_type": "standard"}).status_code, 400)
            self.assertEqual(self.client.patch("/api/transactions/x/type", json={"item_id": "two", "transaction_type": "standard"}).status_code, 409)
            self.assertEqual(self.client.get("/api/analytics/category_totals?days=1.5").status_code, 400)
            self.assertEqual(self.client.get("/api/analytics/category_totals?month=2026-1").status_code, 400)
        finally:
            app.current_item_id = original_item


if __name__ == "__main__":
    unittest.main()

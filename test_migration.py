import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest

import db
import maintenance


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "db.sqlite")
        self.backup = os.path.join(self.tempdir.name, "db.bak")

    def tearDown(self):
        self.tempdir.cleanup()

    def connect(self):
        class ClosingConnection(sqlite3.Connection):
            def __exit__(connection, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    connection.close()

        return sqlite3.connect(self.path, factory=ClosingConnection)

    def test_fresh_current_and_repeat_are_idempotent(self):
        db.migrate_db(self.path)
        db.migrate_db(self.path)
        with self.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            primary = [r[1] for r in conn.execute("PRAGMA table_info(transactions)") if r[5]]
            self.assertEqual(primary, ["item_id", "transaction_id"])
            self.assertEqual([r[2] for r in conn.execute("PRAGMA index_info(transactions_item_date)")], ["item_id", "date"])

    def test_legacy_shapes_preserve_unowned_facts_and_cursor_evidence(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE transactions (transaction_id TEXT PRIMARY KEY, account_id TEXT,
                    date TEXT, name TEXT, amount REAL, category TEXT, pending INTEGER);
                CREATE TABLE balances (account_id TEXT PRIMARY KEY, account_name TEXT, current REAL);
                CREATE TABLE bills (bill_key TEXT PRIMARY KEY, name TEXT, amount REAL, interval_days INTEGER,
                    last_date TEXT, next_due_date TEXT);
                CREATE TABLE income (income_key TEXT PRIMARY KEY, name TEXT, amount REAL, interval_days INTEGER,
                    last_date TEXT, next_pay_date TEXT);
                CREATE TABLE sync_cursor (id INTEGER PRIMARY KEY, cursor TEXT);
                INSERT INTO transactions VALUES ('legacy', 'account', '2026-08-01', 'Legacy', 1, NULL, 0);
                INSERT INTO balances VALUES ('account', 'Checking', 10);
                INSERT INTO bills VALUES ('bill', 'Old', 1, 30, '2026-08-01', '2026-09-01');
                INSERT INTO income VALUES ('income', 'Old', 1, 30, '2026-08-01', '2026-09-01');
                INSERT INTO sync_cursor VALUES (1, 'old-cursor');
            """)
        db.migrate_db(self.path)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT item_id FROM transactions").fetchone()[0], None)
            self.assertEqual(conn.execute("SELECT item_id FROM balances").fetchone()[0], None)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bills WHERE item_id IS NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM income WHERE item_id IS NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT cursor FROM sync_cursor WHERE item_id IS NULL").fetchone()[0], "old-cursor")
        db.DB_PATH = self.path
        self.assertIsNone(db.get_cursor("new-item"))

    def test_duplicate_transaction_ids_across_items_survive_rebuild(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE transactions (transaction_id TEXT, account_id TEXT, item_id TEXT,
                    date TEXT, name TEXT, amount REAL, category TEXT, pending INTEGER,
                    PRIMARY KEY (transaction_id));
            """)
            # Wrong legacy primary key shape cannot hold duplicates, so model
            # legacy/current malformed table with a non-unique rowid table.
            conn.execute("DROP TABLE transactions")
            conn.executescript("""
                CREATE TABLE transactions (transaction_id TEXT, account_id TEXT, item_id TEXT,
                    date TEXT, name TEXT, amount REAL, category TEXT, pending INTEGER);
                INSERT INTO transactions VALUES ('same','a','one','2026-08-01','One',1,NULL,0);
                INSERT INTO transactions VALUES ('same','b','two','2026-08-02','Two',2,NULL,0);
            """)
        db.migrate_db(self.path)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions WHERE transaction_id='same'").fetchone()[0], 2)

    def test_injected_failure_rolls_back_every_schema_change(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE transactions (transaction_id TEXT PRIMARY KEY, account_id TEXT,
                    item_id TEXT, date TEXT, name TEXT, amount REAL, category TEXT, pending INTEGER);
                CREATE TABLE sync_cursor (id INTEGER PRIMARY KEY, cursor TEXT);
                INSERT INTO sync_cursor VALUES (1, 'cursor');
            """)
        original = db._migrate_2
        db._migrate_2 = lambda conn: (_ for _ in ()).throw(RuntimeError("injected"))
        try:
            with self.assertRaises(RuntimeError):
                db.migrate_db(self.path)
        finally:
            db._migrate_2 = original
        with self.connect() as conn:
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(transactions)")],
                             ["transaction_id", "account_id", "item_id", "date", "name", "amount", "category", "pending"])
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(sync_cursor)")], ["id", "cursor"])

    def test_backup_refusal_cases(self):
        with open(self.path, "wb") as target:
            target.write(b"not sqlite")
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.path, self.path)
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.path, os.path.join(self.tempdir.name, "missing"))
        empty = os.path.join(self.tempdir.name, "empty")
        open(empty, "wb").close()
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.path, empty)
        shutil.copyfile(self.path, self.backup)
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            maintenance.verify_backup(self.path, self.backup)


if __name__ == "__main__":
    unittest.main()

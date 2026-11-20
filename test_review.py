import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import db


class ReviewSliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False)
        self.temp.close()
        self.old_path = db.DB_PATH
        db.DB_PATH = self.temp.name
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_path
        os.unlink(self.temp.name)

    def insert(self, item, transaction_id, name, pending=0, override=None, date="2026-08-13"):
        db.upsert_transaction((transaction_id, "account", item, date, name, -25,
                               "TRANSFER_IN", pending, db.classify_transaction(name)))
        if override:
            db.set_transaction_type(item, transaction_id, override)

    def test_review_is_item_scoped_and_excludes_pending_and_overrides(self):
        self.insert("one", "review", "BANK ACH P2P FRIEND")
        self.insert("two", "other", "BANK ACH P2P FRIEND")
        self.insert("one", "pending", "BANK ACH P2P PENDING", pending=1)
        self.insert("one", "resolved", "BANK ACH P2P RESOLVED", override="transfer")
        rows = db.transactions_for_review("one")
        self.assertEqual([r["transaction_id"] for r in rows], ["review"])
        self.assertEqual(rows[0]["confidence"], "low")
        self.assertEqual(rows[0]["reason"], "ambiguous P2P ownership")

    def test_override_persists_and_removes_review(self):
        self.insert("one", "review", "BANK ACH P2P FRIEND")
        self.assertEqual(db.set_transaction_type("one", "review", "liquidation"), 1)
        self.assertEqual(db.transactions_for_review("one"), [])

    def test_review_limit_applies_after_classification(self):
        for index in range(3):
            self.insert("one", f"ordinary-{index}", "BANK CARD PURCHASE GROCER", date="2026-08-13")
        self.insert("one", "older-review", "BANK ACH P2P FRIEND", date="2026-08-12")
        rows = db.transactions_for_review("one", limit=1)
        self.assertEqual([r["transaction_id"] for r in rows], ["older-review"])

    def test_route_uses_cached_item_without_plaid_call(self):
        import importlib
        with patch("dotenv.load_dotenv", return_value=False):
            app = importlib.import_module("app")

        self.insert("one", "review", "BANK ACH P2P FRIEND")
        with patch.object(app, "read_item_id", return_value="one"), \
             patch.object(app.client, "item_get", side_effect=AssertionError("Plaid called")):
            response = app.app.test_client().get("/api/transactions/review?limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["item_id"], "one")

    def test_dashboard_review_controls_are_static_and_accessible(self):
        html = (Path(__file__).parent / "templates" / "analytics.html").read_text(encoding="utf-8")
        for text in ("Transactions to review", "review-badge", "review-panel", "/api/transactions/review",
                     "Keep standard", "Transfer", "Liquidation", "No transactions need review.",
                     "Transactions to review unavailable."):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()

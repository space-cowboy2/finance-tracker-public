import os
import tempfile
import unittest

import db


class ClassificationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False)
        self.temp.close()
        self.original_path = db.DB_PATH
        db.DB_PATH = self.temp.name
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        os.unlink(self.temp.name)

    def insert(self, transaction_id, item_id, name, amount, category="FOOD_AND_DRINK", when="2026-08-13"):
        db.upsert_transaction((transaction_id, "account", item_id, when, name, amount, category, 0,
                               db.classify_transaction(name)))

    def test_payroll_descriptors_remain_income(self):
        self.assertEqual(db.classify_transaction("BANK ACH Credit PAYROLL ACME"), "standard")
        self.assertEqual(db.classify_transaction("Direct Deposit Salary"), "standard")

    def test_own_name_p2p_requires_verified_name(self):
        self.assertEqual(db.classify_transaction("BANK ACH P2P FRIEND"), "standard")
        self.assertEqual(db.classify_transaction("BANK ACH P2P JANE DOE", "Jane Doe"), "transfer")
        self.assertEqual(db.classify_transaction("BANK ACH P2P JANET DOE", "Jane Doe"), "standard")

    def test_classification_details_mark_p2p_for_review(self):
        details = db.classify_transaction_details("BANK ACH P2P FRIEND")
        self.assertEqual(details["transaction_type"], "standard")
        self.assertEqual(details["confidence"], "low")
        self.assertTrue(details["review_required"])
        self.assertEqual(db.classify_transaction_details("BANK ACH P2P JANE DOE", "Jane Doe")["confidence"], "high")

    def test_explicit_transfer_and_investment_liquidation(self):
        self.assertEqual(db.classify_transaction("Online Transfer to Savings"), "transfer")
        self.assertEqual(db.classify_transaction("BROKERAGE PPD ID 123"), "liquidation")
        self.insert("sale", "item", "BROKERAGE PPD ID 123", -700)
        self.assertEqual(db.month_totals("item"), [])
        self.assertEqual(db.liquidation_summary("item")["total"], 700)

    def test_override_precedence_and_item_isolation(self):
        self.insert("same", "one", "BROKERAGE PPD ID", -700)
        self.insert("same", "two", "BANK ACH P2P FRIEND", -50)
        self.assertEqual(db.set_transaction_type("one", "same", "standard"), 1)
        db.upsert_transaction(("same", "account", "one", "2026-08-13", "BROKERAGE PPD ID", -700,
                               "FOOD_AND_DRINK", 0, "liquidation"))
        self.assertEqual(db.month_totals("one")[0]["income"], 700)
        self.assertEqual(db.month_totals("two")[0]["income"], 50)

    def test_fee_annualization_uses_requested_window(self):
        self.insert("fee", "item", "OVERDRAFT FEE", 34, "BANK_FEES", "2026-08-12")
        summary = db.fees_summary("item", days=90)
        self.assertEqual(summary["period_days"], 90)
        self.assertEqual(summary["annualized"], round(34 / 90 * 365, 2))

    def test_sparse_all_time_fee_window_has_floor(self):
        self.insert("fee", "item", "MONTHLY FEE", 5, "BANK_FEES", "2026-08-13")
        summary = db.fees_summary("item")
        self.assertEqual(summary["period_days"], 30)
        self.assertEqual(summary["annualized"], round(5 / 30 * 365, 2))


if __name__ == "__main__":
    unittest.main()

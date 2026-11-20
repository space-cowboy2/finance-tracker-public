import builtins
import importlib
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


class SyntheticDashboardTests(unittest.TestCase):
    read_only_routes = [
        "/api/connection_status", "/api/set_aside", "/api/analytics/months",
        "/api/analytics/fees", "/api/cashflow_projection",
        "/api/analytics/daily_flow", "/api/analytics/category_totals",
        "/api/analytics/category_trend", "/api/analytics/merchants",
        "/api/bills/upcoming", "/api/income/upcoming", "/api/balances",
        "/api/transactions/review", "/api/budget/month",
    ]
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("synthetic_dashboard")
        cls.client = cls.module.app.test_client()

    def setUp(self):
        self.module.review_decisions.clear()

    def test_source_has_no_production_imports_or_data_paths(self):
        source = Path("synthetic_dashboard.py").read_text(encoding="utf-8")
        for forbidden in ("import app", "import db", "dotenv", "Plaid", "finance.db", ".env"):
            self.assertNotIn(forbidden, source)

    def test_routes_render_without_opening_production_files_or_importing_modules(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"app", "db", "dotenv", "plaid"}:
                raise AssertionError(f"production import attempted: {name}")
            return real_import(name, *args, **kwargs)

        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            path = str(file).lower()
            self.assertNotIn("finance.db", path)
            self.assertNotIn(".env", path)
            return real_open(file, *args, **kwargs)

        with patch("builtins.__import__", guarded_import), patch("builtins.open", guarded_open):
            self.assertEqual(self.client.get("/dashboard").status_code, 200)
            self.assertEqual(self.client.get("/api/set_aside").status_code, 200)
            self.assertEqual(self.client.post("/api/sync_transactions").status_code, 200)

    def test_routes_never_need_network(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("network access")):
            self.assertEqual(self.client.get("/dashboard").status_code, 200)
            for route in self.read_only_routes:
                self.assertIn(self.client.get(route).status_code, (200, 503), route)

    def test_all_view_contracts_and_state_switches(self):
        for route in self.read_only_routes:
            self.assertEqual(self.client.get(route).status_code, 200, route)
            self.assertEqual(self.client.get(route + "?state=empty").status_code, 200, route)
            self.assertEqual(self.client.get(route + "?state=stale").status_code, 200, route)
            self.assertEqual(self.client.get(route + "?state=error").status_code, 503, route)
        self.assertEqual(self.client.post("/api/can_i_afford?state=stale").json["confidence"], "insufficient")

    def test_review_is_item_scoped_and_patch_is_memory_only(self):
        row = self.client.get("/api/transactions/review").json[0]
        self.assertEqual(row["item_id"], self.module.SYNTHETIC_ITEM_ID)
        response = self.client.patch(
            "/api/transactions/synthetic-review-1/type",
            json={"item_id": row["item_id"], "transaction_type": "transfer"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/transactions/review").json, [])
        self.assertEqual(self.client.patch(
            "/api/transactions/synthetic-review-1/type",
            json={"item_id": "other-item", "transaction_type": "transfer"},
        ).status_code, 409)

    def test_dashboard_state_persists_to_api_requests(self):
        client = self.module.app.test_client()
        self.assertEqual(client.get("/dashboard?state=empty").status_code, 200)
        self.assertEqual(client.get("/api/analytics/months").json, [])
        self.assertEqual(client.get("/dashboard?state=stale").status_code, 200)
        self.assertFalse(client.get("/api/connection_status").json["healthy"])
        self.assertEqual(client.get("/dashboard?state=error").status_code, 200)
        self.assertEqual(client.get("/api/set_aside").status_code, 503)

    def test_state_query_selection_sets_httponly_cookie(self):
        response = self.client.get("/dashboard?state=STALE")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("synthetic_state=stale", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_budget_fixture_matches_production_contract(self):
        data = self.client.get("/api/budget/month?month=2026-08").json
        self.assertEqual(data["month"], "2026-08")
        self.assertEqual(data["totals"]["transaction_count"], 3)
        self.assertEqual(data["transactions"][0]["date"], "2026-08-10")
        self.assertIn("next_due_date", data["bills"][0])
        self.assertTrue(all("total" in row for row in data["categories"]))


if __name__ == "__main__":
    unittest.main()

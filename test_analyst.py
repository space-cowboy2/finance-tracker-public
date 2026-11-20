import json
import os
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

import analyst
from app import app


class AnalystApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        os.environ["ANALYST_DB_PATH"] = self.tmp.name
        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)
        os.environ.pop("ANALYST_DB_PATH", None)

    def test_missing_key_and_validation(self):
        with patch.dict(os.environ, {"KIMI_API_KEY": ""}), \
                patch("app._analyst_snapshot", return_value={}):
            self.assertEqual(self.client.post("/api/analyst/chat", json={"message": "hi"}).status_code, 503)
        self.assertEqual(self.client.post("/api/analyst/events", json={"confirmed": True, "kind": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/analyst/events", json={"kind": "unexpected_bill"}).status_code, 400)

    def test_proposal_not_saved_and_confirmed_event_is_history(self):
        result = {"reply": "Consider it.", "proposed_event": {"kind": "unexpected_bill", "description": "Dentist", "amount": 80, "due_date": "2026-09-01"}}
        with patch.dict(os.environ, {"KIMI_API_KEY": "test"}), patch("app.analyst.call_kimi", return_value=result), patch("app._analyst_snapshot", return_value={"monthly_summary": {}, "forecast_headline": {}, "upcoming_totals": {}}):
            response = self.client.post("/api/analyst/chat", json={"message": "Plan this"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/analyst/history").json["confirmed_events"], [])
        saved = self.client.post("/api/analyst/events", json={"confirmed": True, **result["proposed_event"]})
        self.assertEqual(saved.status_code, 201)
        history = self.client.get("/api/analyst/history").json
        self.assertEqual(len(history["messages"]), 2)
        self.assertEqual(len(history["confirmed_events"]), 1)

    def test_payload_is_bounded(self):
        captured = {}
        def fake(messages):
            captured["body"] = messages[1]["content"]
            return {"reply": "safe", "proposed_event": None}
        with patch.dict(os.environ, {"KIMI_API_KEY": "test"}), patch("app.analyst.call_kimi", side_effect=fake), patch("app._analyst_snapshot", return_value={"monthly_summary": {"income": 1}, "forecast_headline": {"confidence": "insufficient"}, "upcoming_totals": {"bills": 2}}):
            self.assertEqual(self.client.post("/api/analyst/chat", json={"message": "hello"}).status_code, 200)
        self.assertNotIn("transaction_id", captured["body"])
        self.assertNotIn("account_id", captured["body"])
        self.assertNotIn("raw merchant", captured["body"])

    def test_snapshot_has_read_only_account_context_without_identifiers(self):
        balances = [{"account_id": "secret-account", "item_id": "secret-item",
                     "account_name": "Checking", "account_type": "depository",
                     "account_subtype": "checking", "available": 1200, "current": 1300,
                     "iso_currency_code": "USD", "fetched_at": "2026-08-13"}]
        bills = [{"bill_key": "secret-bill", "name": "Rent", "amount": 800,
                  "interval_days": 30, "last_date": "2026-08-01", "next_due_date": "2026-09-01"}]
        with patch("app.active_item_id", return_value="secret-item"), \
                patch("app.db.get_balances", return_value=balances), \
                patch("app.db.get_bills_upcoming", return_value=bills), \
                patch("app.db.get_income_upcoming", return_value=[]), \
                patch("app.cashflow_projection", return_value={"confidence": "high", "lowest_balance": 500}), \
                patch("app.db.month_totals", return_value=[{"month": "2026-08", "income": 3000, "spent": 1200}]), \
                patch("app.db.category_totals", return_value=[{"category": "FOOD", "total": 200, "n": 4}]), \
                patch("app.db.merchants", return_value=[{"name": "Market", "category": "FOOD", "visits": 4, "total": 200}]), \
                patch("app.db.get_goals", return_value=[{"goal_id": 9, "name": "Emergency fund", "target_amount": 5000}]):
            snapshot = __import__("app")._analyst_snapshot()
        encoded = json.dumps(snapshot)
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["accounts"][0]["account_name"], "Checking")
        self.assertEqual(snapshot["monthly_summary"][0]["income"], 3000)
        self.assertNotIn("secret-account", encoded)
        self.assertNotIn("secret-item", encoded)
        self.assertNotIn("secret-bill", encoded)
        self.assertNotIn("goal_id", encoded)

    def test_kimi_k3_is_default_with_json_mode(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                content = json.dumps({"reply": "Ready.", "proposed_event": None})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, timeout):
            captured.update(json.loads(request.data))
            self.assertEqual(timeout, 30)
            return Response()

        with patch.dict(os.environ, {}, clear=True):
            result = analyst.call_kimi([{"role": "user", "content": "hello"}],
                                       api_key="test", opener=opener)
        self.assertEqual(result["reply"], "Ready.")
        self.assertEqual(captured["model"], "kimi-k3")
        self.assertEqual(captured["reasoning_effort"], "high")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", captured)

    def test_kimi_http_error_is_safe_and_specific(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with self.assertRaisesRegex(analyst.KimiAPIError,
                                    "Kimi API rejected the API key \\(HTTP 401\\)"):
            analyst.call_kimi([{"role": "user", "content": "hello"}],
                               api_key="redacted", api_url="https://invalid.example/v1/chat/completions",
                               opener=opener)

    def test_invalid_proposal_does_not_discard_valid_advice(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                content = json.dumps({"reply": "The subscription fits your current forecast.",
                                      "proposed_event": {"kind": "subscription", "amount": 200}})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        result = analyst.call_kimi([{"role": "user", "content": "Can I afford it?"}],
                                   api_key="redacted", opener=lambda *_args, **_kwargs: Response())
        self.assertIn("subscription", result["reply"])
        self.assertIsNone(result["proposed_event"])

    def test_regional_api_key_falls_back_to_china_platform(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                content = json.dumps({"reply": "K3 ready.", "proposed_event": None})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, timeout):
            requests.append((request.full_url, json.loads(request.data)))
            if len(requests) == 1:
                raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
            return Response()

        with patch.dict(os.environ, {}, clear=True):
            result = analyst.call_kimi([{"role": "user", "content": "hello"}],
                                       api_key="redacted", opener=opener)
        self.assertEqual(result["reply"], "K3 ready.")
        self.assertEqual(requests[0][1]["model"], "kimi-k3")
        self.assertEqual(requests[1][0], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(requests[1][1]["model"], "kimi-k3")

    def test_kimi_code_key_falls_back_after_both_platforms(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                content = json.dumps({"reply": "K3 ready.", "proposed_event": None})
                return json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        def opener(request, timeout):
            requests.append((request.full_url, json.loads(request.data)))
            if len(requests) < 3:
                raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
            return Response()

        with patch.dict(os.environ, {}, clear=True):
            analyst.call_kimi([{"role": "user", "content": "hello"}],
                               api_key="redacted", opener=opener)
        self.assertEqual(requests[2][0], "https://api.kimi.com/coding/v1/chat/completions")
        self.assertEqual(requests[2][1]["model"], "k3")


if __name__ == "__main__":
    unittest.main()

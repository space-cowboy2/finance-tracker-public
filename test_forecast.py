import unittest
import dotenv
from datetime import date, datetime, timedelta

dotenv.load_dotenv = lambda *args, **kwargs: False
from app import forecast_cashflow


def row(kind, name, amount, due, frequency=None, interval_days=None):
    return {"name": name, "amount": amount, kind: due,
            **({"frequency": frequency} if frequency else {}),
            **({"interval_days": interval_days} if interval_days else {})}


class ForecastTests(unittest.TestCase):
    def project(self, bills=(), income=(), days=40, start=10000):
        return forecast_cashflow(start, bills, income, date(2024, 1, 31), days,
                                 balance_fetched_at=datetime.utcnow().isoformat())

    def test_month_end_and_leap_year(self):
        p = self.project(income=[row("next_pay_date", "pay", 1, "2024-01-31", "MONTHLY")], days=65)
        dates = [e["date"] for d in p["timeline"] for e in d["events"]]
        self.assertEqual(dates, ["2024-01-31", "2024-02-29", "2024-03-31"])

    def test_semi_monthly_and_annual(self):
        p = self.project(income=[row("next_pay_date", "pay", 1, "2024-02-01", "SEMI_MONTHLY"),
                                 row("next_pay_date", "bonus", 1, "2024-02-29", "ANNUAL")], days=400)
        dates = [e["date"] for d in p["timeline"] for e in d["events"]]
        self.assertIn("2024-02-15", dates)
        self.assertIn("2024-03-01", dates)
        self.assertIn("2025-02-28", dates)

    def test_posted_occurrence_is_not_readded(self):
        p = self.project(income=[row("next_pay_date", "pay", 1, "2024-01-31", "MONTHLY")],
                         days=2, start=0)
        self.assertEqual(len(p["timeline"][0]["events"]), 1)
        p = forecast_cashflow(0, [], [row("next_pay_date", "pay", 1, "2024-01-31", "MONTHLY")],
                              date(2024, 1, 31), 2, posted={("pay", 100, "2024-01-31")},
                              balance_fetched_at=datetime.utcnow().isoformat())
        self.assertEqual(p["timeline"][0]["events"], [])

    def test_later_posted_occurrence_is_not_readded(self):
        p = forecast_cashflow(
            0, [], [row("next_pay_date", "pay", 1, "2024-01-31", "MONTHLY")],
            date(2024, 1, 31), 65, posted={("pay", 100, "2024-02-29")},
            balance_fetched_at=datetime.utcnow().isoformat())
        dates = [e["date"] for day in p["timeline"] for e in day["events"]]
        self.assertEqual(dates, ["2024-01-31", "2024-03-31"])

    def test_same_day_bill_before_income_and_rounding(self):
        p = self.project(start=10, bills=[row("next_due_date", "bill", "0.10", "2024-01-31")],
                         income=[row("next_pay_date", "pay", "0.20", "2024-01-31")], days=0)
        self.assertEqual([e["kind"] for e in p["timeline"][0]["events"]], ["bill", "income"])
        self.assertEqual(p["ending_balance"], 0.2)

    def test_missing_stale_and_heuristic_are_insufficient(self):
        for kwargs in ({"balance_fetched_at": None},
                       {"balance_fetched_at": (datetime.utcnow() - timedelta(days=2)).isoformat()},
                       {"balance_fetched_at": datetime.utcnow().isoformat(), "recurring_source": "heuristic"}):
            p = forecast_cashflow(100, [], [], date(2024, 1, 1), 1, **kwargs)
            self.assertEqual(p["confidence"], "insufficient")

    def test_affordability_requires_high_confidence_contract(self):
        p = forecast_cashflow(1000, [], [], date(2024, 1, 1), 1, recurring_source="heuristic")
        self.assertFalse(p["confidence"] == "high" and p["lowest_balance"] >= 0)


if __name__ == "__main__":
    unittest.main()

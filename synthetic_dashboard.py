"""Standalone fake-data dashboard server; never imports production app or database."""
import argparse
import os
from datetime import date, timedelta
from flask import Flask, jsonify, make_response, render_template, request

app = Flask(__name__, template_folder="templates")
ALLOWED_STATES = {"normal", "empty", "error", "stale"}
DEFAULT_STATE = os.getenv("SYNTHETIC_STATE", "normal").lower()
if DEFAULT_STATE not in ALLOWED_STATES:
    DEFAULT_STATE = "normal"

def current_state():
    value = request.args.get("state") or request.cookies.get("synthetic_state") or DEFAULT_STATE
    value = value.lower()
    return value if value in ALLOWED_STATES else DEFAULT_STATE

SYNTHETIC_ITEM_ID = "synthetic-item"
REVIEW_ROWS = [
    {"transaction_id": "synthetic-review-1", "item_id": SYNTHETIC_ITEM_ID,
     "date": "2026-08-12", "name": "Synthetic Brokerage Withdrawal",
     "amount": 250.0, "confidence": "medium", "reason": "Possible liquidation"},
]
review_decisions = {}

def dashboard_response():
    response = make_response(render_template("analytics.html"))
    state = (request.args.get("state") or "").lower()
    if state in ALLOWED_STATES:
        response.set_cookie("synthetic_state", state, httponly=True, samesite="Strict")
    return response

def payload(value):
    if current_state() == "error":
        return jsonify({"error": "Synthetic endpoint failure"}), 503
    return jsonify(value)

def stateful_rows(rows):
    if current_state() == "empty":
        return payload([])
    return payload([row for row in rows if row["transaction_id"] not in review_decisions])

@app.get("/")
def index():
    return dashboard_response()

@app.get("/dashboard")
def dashboard():
    return dashboard_response()

@app.get("/api/connection_status")
def connection_status():
    state = current_state()
    if state == "error":
        return jsonify({"error": "Synthetic endpoint failure"}), 503
    if state == "stale":
        return jsonify({"healthy": False, "linked": True, "message": "Synthetic cached connection is stale.", "error_code": "SYNTHETIC_STALE"})
    if state == "empty":
        return jsonify({"healthy": False, "linked": False, "message": "No synthetic account linked."})
    return jsonify({"healthy": True, "linked": True, "message": "Synthetic account connected."})

@app.get("/api/balances")
def balances():
    if current_state() == "empty":
        return payload([])
    return payload([{"account_id": "synthetic-checking", "item_id": SYNTHETIC_ITEM_ID,
                     "account_name": "Synthetic Checking", "account_type": "depository",
                     "account_subtype": "checking", "available": 2142.18,
                     "current": 2142.18, "iso_currency_code": "USD"}])

@app.get("/api/set_aside")
def set_aside():
    return payload({"safe_to_spend": 642.18, "horizon": "2026-08-20", "available_balance": 2142.18, "upcoming_bills_total": 1500})

@app.get("/api/analytics/months")
def months():
    if current_state() == "empty": return payload([])
    return payload([{"month": "2026-06", "income": 4200, "spent": 2780, "net": 1420, "savings_rate": 33.8}, {"month": "2026-07", "income": 4200, "spent": 3010, "net": 1190, "savings_rate": 28.3}, {"month": "2026-08", "income": 2100, "spent": 1320, "net": 780, "savings_rate": 37.1}])

@app.get("/api/analytics/fees")
def fees():
    return payload({"count": 1, "total": 12.0, "overdraft_total": 0, "annualized": 144.0})

@app.get("/api/cashflow_projection")
def cashflow_projection():
    if current_state() == "empty": return payload({"starting_balance": 0, "lowest_balance": 0, "ending_balance": 0, "first_negative_date": None, "timeline": [], "recurring_source": "none", "estimated": False})
    start = date(2026, 8, 13)
    events = [{"date": (start + timedelta(days=4)).isoformat(), "kind": "bill", "name": "Synthetic Utilities", "amount": -125.0}, {"date": (start + timedelta(days=7)).isoformat(), "kind": "income", "name": "Synthetic Payroll", "amount": 2100.0}]
    return payload({"starting_balance": 2142.18, "lowest_balance": 2017.18, "ending_balance": 4117.18, "first_negative_date": None, "timeline": [{"date": e["date"], "events": [e]} for e in events], "recurring_source": "synthetic schedule", "estimated": current_state() == "stale"})

@app.get("/api/analytics/daily_flow")
def daily_flow():
    if current_state() == "empty": return payload([])
    return payload([{"date": "2026-08-10", "spent": 42.50, "received": 0}, {"date": "2026-08-11", "spent": 18.25, "received": 0}, {"date": "2026-08-12", "spent": 0, "received": 2100}])

@app.get("/api/analytics/category_totals")
def category_totals():
    if current_state() == "empty": return payload([])
    return payload([{"category": "FOOD_AND_DRINK", "total": 425.50}, {"category": "TRANSPORTATION", "total": 210.25}, {"category": "GENERAL_MERCHANDISE", "total": 180.00}])

@app.get("/api/analytics/category_trend")
def category_trend():
    if current_state() == "empty": return payload([])
    return payload([{"month": "2026-07", "category": "FOOD_AND_DRINK", "total": 200}, {"month": "2026-08", "category": "FOOD_AND_DRINK", "total": 225.5}, {"month": "2026-07", "category": "TRANSPORTATION", "total": 110}, {"month": "2026-08", "category": "TRANSPORTATION", "total": 100.25}])

@app.get("/api/analytics/merchants")
def merchants():
    if current_state() == "empty": return payload([])
    return payload([{"name": "Synthetic Market", "category": "FOOD_AND_DRINK", "visits": 4, "avg_amount": 34.25, "total": 137.00}, {"name": "Synthetic Fuel", "category": "TRANSPORTATION", "visits": 2, "avg_amount": 45.12, "total": 90.25}])

@app.get("/api/bills/upcoming")
def bills():
    if current_state() == "empty": return payload({"bills": [], "source": "synthetic"})
    return payload({"bills": [{"name": "Synthetic Utilities", "amount": 125.0, "next_due_date": "2026-08-17", "interval_days": 30}], "source": "synthetic schedule"})

@app.get("/api/income/upcoming")
def income():
    if current_state() == "empty": return payload([])
    return payload([{"name": "Synthetic Payroll", "amount": 2100.0, "next_pay_date": "2026-08-20", "interval_days": 14}])

@app.get("/api/budget/month")
def budget_month():
    month = request.args.get("month", "2026-08")
    if current_state() == "empty":
        return payload({"month": month, "totals": {"spent": 0, "income": 0, "transaction_count": 0}, "categories": [], "transactions": [], "bills": []})
    return payload({"month": month,
                    "totals": {"spent": 425.50, "income": 2100, "transaction_count": 3},
                    "categories": [{"category": "FOOD_AND_DRINK", "n": 2, "total": 225.50}, {"category": "GROCERIES", "n": 1, "total": 200}],
                    "transactions": [{"date": f"{month}-10", "name": "Synthetic Cafe", "amount": 42.50, "category": "FOOD_AND_DRINK", "transaction_type": "standard"},
                                     {"date": f"{month}-11", "name": "Synthetic Market", "amount": 200, "category": "GROCERIES", "transaction_type": "standard"},
                                     {"date": f"{month}-20", "name": "Synthetic Payroll", "amount": -2100, "category": "INCOME", "transaction_type": "standard"}],
                    "bills": [{"next_due_date": f"{month}-17", "name": "Synthetic Utilities", "amount": 125}], "stale": current_state() == "stale"})

@app.post("/api/can_i_afford")
def can_i_afford():
    confidence = "insufficient" if current_state() in {"empty", "stale"} else "high"
    return payload({"can_afford": confidence == "high", "confidence": confidence, "projected_low": 2017.18})

@app.post("/api/sync_transactions")
def sync_transactions():
    return payload({"added": 3, "modified": 0, "removed": 0})

@app.post("/api/create_link_token")
def create_link_token():
    return payload({"link_token": "synthetic-link-token"})

@app.post("/api/exchange_public_token")
def exchange_public_token():
    return payload({"ok": True})

@app.get("/api/transactions/review")
def transactions_review():
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 100)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return stateful_rows(REVIEW_ROWS[:limit])

@app.patch("/api/transactions/<transaction_id>/type")
def override_transaction_type(transaction_id):
    if current_state() == "error":
        return payload({})
    body = request.get_json(silent=True) or {}
    item_id = body.get("item_id")
    transaction_type = body.get("transaction_type")
    if item_id != SYNTHETIC_ITEM_ID:
        return jsonify({"error": "item_id does not match current Item"}), 409
    if transaction_type not in {"standard", "transfer", "liquidation"}:
        return jsonify({"error": "transaction_type must be standard, transfer, or liquidation"}), 400
    if not any(row["transaction_id"] == transaction_id for row in REVIEW_ROWS):
        return jsonify({"error": "transaction not found"}), 404
    review_decisions[transaction_id] = transaction_type
    return jsonify({"status": "updated", "transaction_type": transaction_type})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fake dashboard renderer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--state", choices=sorted(ALLOWED_STATES), default=DEFAULT_STATE)
    args = parser.parse_args()
    DEFAULT_STATE = args.state
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

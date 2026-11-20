import os
import json
import math
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import calendar
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import plaid
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, redirect, render_template, request
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

import db
import analyst

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

app = Flask(__name__)

MIN_HORIZON_DAYS = 7
MAX_ANALYTICS_DAYS = 3650
_recurring_lock = threading.Lock()
_recurring_cache = {"item_id": None, "at": None, "source": "none"}

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")

host = plaid.Environment.Sandbox if PLAID_ENV == "sandbox" else plaid.Environment.Production

configuration = plaid.Configuration(
    host=host,
    api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
)
client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
LOCAL_TZ = ZoneInfo("America/Chicago")


LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]"}
EXTRA_HOSTNAMES = {h.strip().lower() for h in os.getenv("FINANCE_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _hostname(netloc):
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc.split("]", 1)[0] + "]"
    return netloc.split(":", 1)[0]


@app.before_request
def _reject_untrusted_host():
    """Block DNS rebinding. A hostile page whose domain is re-pointed at
    127.0.0.1 still sends its own name in Host, so only loopback names (plus
    any listed in FINANCE_ALLOWED_HOSTS) are served, for reads as well as writes."""
    if _hostname(request.host) not in LOCAL_HOSTNAMES | EXTRA_HOSTNAMES:
        return jsonify({"error": "untrusted host"}), 403
    return None


@app.before_request
def _same_origin_mutation():
    if request.method not in {"POST", "PATCH", "DELETE", "PUT"} or not request.path.startswith("/api/"):
        return None
    expected = request.host_url.rstrip("/")
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value and urlparse(value).scheme + "://" + urlparse(value).netloc != expected:
            return jsonify({"error": "cross-origin mutation rejected"}), 403
    return None


@app.route("/")
def index():
    return redirect("/dashboard")


@app.route("/link")
def link():
    """Stable fallback connection page; dashboard remains product home."""
    return render_template("link.html")


@app.route("/api/create_link_token", methods=["POST"])
def create_link_token():
    kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
        client_name="Finance Tracker",
        country_codes=[CountryCode("US")],
        language="en",
    )
    # Update mode repairs an errored Item in place, keeping the same access token.
    # Plaid rejects `products` in update mode, so only send them on a fresh link.
    # Never silently fall back to a fresh link here: the repair flow has no
    # public-token exchange, so the resulting Item would be created at Plaid and
    # then stranded, with every later sync reporting "no account linked yet".
    if request.args.get("update"):
        access_token = get_access_token()
        if not access_token:
            return jsonify({"error": "nothing to repair - no account linked yet"}), 400
        kwargs["access_token"] = access_token
    else:
        kwargs["products"] = [Products("transactions")]
        kwargs["optional_products"] = [Products("liabilities"), Products("investments")]

    resp = client.link_token_create(LinkTokenCreateRequest(**kwargs))
    return jsonify({"link_token": resp.link_token})


@app.route("/api/exchange_public_token", methods=["POST"])
def exchange_public_token():
    body = _json_body()
    public_token = body.get("public_token") if body else None
    if not isinstance(public_token, str) or not public_token.strip():
        return jsonify({"error": "public_token is required"}), 400
    req = ItemPublicTokenExchangeRequest(public_token=public_token)
    resp = client.item_public_token_exchange(req)
    access_token = resp.access_token
    set_key(ENV_PATH, "PLAID_ACCESS_TOKEN", access_token)
    return jsonify({"status": "linked"})


def get_access_token():
    load_dotenv(override=True)
    return os.getenv("PLAID_ACCESS_TOKEN")


def _category_of(t):
    # Optional fields raise ApiAttributeError on attribute access when unset,
    # so the truthiness guard would throw rather than fall through. Use .get().
    pfc = t.get("personal_finance_category")
    return pfc.primary if pfc else None


def get_item_id(access_token):
    resp = client.item_get(ItemGetRequest(access_token=access_token))
    return resp.item.item_id


def active_item_id():
    access_token = get_access_token()
    if not access_token:
        return None
    try:
        return get_item_id(access_token)
    except plaid.ApiException:
        return db.latest_item_id()


def read_item_id():
    """Select one Item for cached reads; never use this for writes."""
    return active_item_id()


def current_item_id():
    access_token = get_access_token()
    if not access_token:
        return None
    try:
        return get_item_id(access_token)
    except plaid.ApiException:
        return None


def _json_body():
    if not request.is_json:
        return None
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _int_arg(name, default, minimum, maximum):
    raw = request.args.get(name)
    if raw is None:
        return default
    if not raw.isdecimal():
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _analytics_args():
    try:
        return _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/sync_transactions", methods=["POST"])
def sync_transactions():
    access_token = get_access_token()
    if not access_token:
        return jsonify({"error": "no account linked yet"}), 400

    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    cursor = db.get_cursor(item_id)
    added = modified = removed = 0
    has_more = True

    while has_more:
        req = TransactionsSyncRequest(access_token=access_token, cursor=cursor or "")
        resp = client.transactions_sync(req)

        for t in resp.added:
            db.upsert_transaction((
                t.transaction_id, t.account_id, item_id, str(t.date), t.name,
                t.amount, _category_of(t),
                int(t.pending), db.classify_transaction_details(t.name)["transaction_type"],
            ))
            added += 1

        for t in resp.modified:
            db.upsert_transaction((
                t.transaction_id, t.account_id, item_id, str(t.date), t.name,
                t.amount, _category_of(t),
                int(t.pending), db.classify_transaction_details(t.name)["transaction_type"],
            ))
            modified += 1

        for t in resp.removed:
            db.remove_transaction(item_id, t.transaction_id)
            removed += 1

        cursor = resp.next_cursor
        has_more = resp.has_more

    db.set_cursor(item_id, cursor)
    _recurring_cache["at"] = None
    return jsonify({"added": added, "modified": modified, "removed": removed})


@app.route("/api/maintenance/purge_orphans", methods=["POST"])
def purge_orphans():
    access_token = get_access_token()
    if not access_token:
        return jsonify({"error": "no account linked yet"}), 400
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    deleted = db.purge_orphans(item_id)
    return jsonify({"deleted": deleted, "item_id": item_id})


@app.route("/api/connection_status")
def connection_status():
    """Item health, so the dashboard can surface a broken bank link where the
    user actually is instead of hiding the fix on the link page."""
    access_token = get_access_token()
    if not access_token:
        return jsonify({"linked": False, "healthy": False, "error_code": None,
                        "message": "No account linked yet."})
    try:
        item = client.item_get(ItemGetRequest(access_token=access_token)).item
    except plaid.ApiException:
        return jsonify({"linked": True, "healthy": False, "error_code": "ITEM_GET_FAILED",
                        "message": "Could not reach Plaid for this connection."})

    err = item.get("error")
    if not err:
        return jsonify({"linked": True, "healthy": True, "error_code": None, "message": "Connected."})
    return jsonify({
        "linked": True,
        "healthy": False,
        "error_code": err.get("error_code"),
        "message": err.get("display_message") or err.get("error_message") or "Connection needs attention.",
    })


@app.route("/api/summary")
def summary():
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.monthly_summary(item_id))


def fetch_balances(expected_item_id=None):
    access_token = get_access_token()
    if not access_token:
        return None
    try:
        item_id = get_item_id(access_token)
        if not item_id or (expected_item_id and item_id != expected_item_id):
            return None
        req = AccountsBalanceGetRequest(access_token=access_token)
        resp = client.accounts_balance_get(req)
    except plaid.ApiException:
        return None
    fetched_at = datetime.utcnow().isoformat()
    result = []
    for a in resp.accounts:
        row = {
            "account_id": a.account_id,
            "item_id": item_id,
            "account_name": a.name,
            "account_type": str(a.type),
            "account_subtype": str(a.subtype) if a.subtype else None,
            "available": a.balances.available,
            "current": a.balances.current,
            "iso_currency_code": a.balances.iso_currency_code,
            "fetched_at": fetched_at,
        }
        db.upsert_balance((
            row["account_id"], row["item_id"], row["account_name"], row["account_type"], row["account_subtype"],
            row["available"], row["current"], row["iso_currency_code"], row["fetched_at"],
        ))
        result.append(row)
    return result


@app.route("/api/balances")
def balances():
    item_id = read_item_id()
    result = fetch_balances(item_id)
    if result is not None:
        return jsonify(result)
    cached = db.get_balances(item_id)
    if cached:
        return jsonify(cached)
    return jsonify({"error": "no balances available - link an account, or the institution is "
                             "temporarily not serving balances"}), 400


# Plaid reports a cadence, not a day count. Approximations are fine here: the
# stored interval is descriptive, and the due date comes from Plaid directly.
_FREQUENCY_DAYS = {
    "WEEKLY": 7, "BIWEEKLY": 14, "SEMI_MONTHLY": 15,
    "MONTHLY": 30, "ANNUALLY": 365,
}


def _stream_rows(streams):
    today = datetime.now(LOCAL_TZ).date()
    rows = []
    for s in streams:
        if not s.is_active:
            continue
        interval = _FREQUENCY_DAYS.get(str(s.frequency), 30)
        nxt = s.get("predicted_next_date") or s.last_date
        nxt = nxt if hasattr(nxt, "isoformat") else datetime.strptime(str(nxt), "%Y-%m-%d").date()
        while nxt < today:  # a lapsed stream must still predict forward
            nxt += timedelta(days=interval)
        rows.append({
            "key": s.stream_id,
            "name": db._normalize_name(s.description) or s.description,
            "amount": round(abs(float(s.average_amount.amount)), 2),
            "interval_days": interval,
            "last_date": str(s.last_date),
            "next_date": nxt.isoformat(),
        })
    return rows


def refresh_recurring(item_id):
    """Prefer Plaid's recurring-stream detection over the local heuristic.

    The heuristic groups transactions by normalised name and infers a cadence,
    which reports frequented merchants (a bakery visited every 12 days) as
    bills. Plaid distinguishes an obligation from a habit. It stays as a
    fallback so the feature still works when Plaid is unreachable or the Item
    is in an error state."""
    access_token = get_access_token()
    if not access_token or current_item_id() != item_id:
        return "none"
    with _recurring_lock:
        cached_at = _recurring_cache["at"]
        if (_recurring_cache["item_id"] == item_id and cached_at and
                datetime.utcnow() - cached_at < timedelta(seconds=45)):
            return _recurring_cache["source"]
        try:
            resp = client.transactions_recurring_get(
                TransactionsRecurringGetRequest(access_token=access_token)
            )
        except plaid.ApiException:
            db.detect_bills(item_id)
            db.detect_income(item_id)
            source = "heuristic"
        else:
            db.replace_streams("bills", item_id, _stream_rows(resp.outflow_streams))
            db.replace_streams("income", item_id, _stream_rows(resp.inflow_streams))
            source = "plaid"
        _recurring_cache.update({"item_id": item_id, "at": datetime.utcnow(), "source": source})
        return source


@app.route("/api/bills/upcoming")
def bills_upcoming():
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    source = refresh_recurring(item_id)
    return jsonify(db.get_bills_upcoming(item_id)) if request.args.get("plain") else jsonify({
        "source": source, "bills": db.get_bills_upcoming(item_id),
    })


@app.route("/api/income/upcoming")
def income_upcoming():
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    source = refresh_recurring(item_id)
    return jsonify(db.get_income_upcoming(item_id)) if request.args.get("plain") else jsonify({
        "source": source, "income": db.get_income_upcoming(item_id),
    })


def goal_with_computed(g):
    today = datetime.utcnow().date()
    target_date = datetime.strptime(g["target_date"], "%Y-%m-%d").date()
    # Clamped: an overfunded goal would otherwise contribute a negative requirement
    # that cancels out other goals' savings in set_aside.
    remaining = max(0, g["target_amount"] - g["current_saved"])
    days_left = max(1, (target_date - today).days)
    weeks_left = max(1, days_left / 7)
    required_weekly = remaining / weeks_left
    return {
        **g,
        "remaining": remaining,
        "weeks_left": weeks_left,
        "required_weekly": required_weekly,
        "required_monthly": required_weekly * 4.33,
    }


def _clean_goal_fields(body, require_all):
    """Validate on write. An unparseable target_date or a non-numeric amount
    stored raw makes goal_with_computed raise on every later call, which
    permanently 500s both GET /api/goals and GET /api/set_aside with no API
    path left to repair the row."""
    fields, errors = {}, []

    if require_all or "name" in body:
        name = str(body.get("name", "")).strip()
        if not name:
            errors.append("name is required")
        else:
            fields["name"] = name[:120]

    for key in ("target_amount", "current_saved"):
        if key in body:
            try:
                value = float(body[key])
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
            if not math.isfinite(value):
                errors.append(f"{key} must be finite")
            elif value < 0:
                errors.append(f"{key} cannot be negative")
            else:
                fields[key] = value
    if require_all and "target_amount" not in fields and "target_amount" not in body:
        errors.append("target_amount is required")

    if require_all or "target_date" in body:
        raw = body.get("target_date")
        try:
            fields["target_date"] = datetime.strptime(str(raw), "%Y-%m-%d").date().isoformat()
        except (TypeError, ValueError):
            errors.append("target_date must be YYYY-MM-DD")

    return fields, errors


@app.route("/api/goals", methods=["GET", "POST"])
def goals():
    if request.method == "POST":
        body = _json_body()
        if body is None:
            return jsonify({"error": "JSON object required"}), 400
        fields, errors = _clean_goal_fields(body, require_all=True)
        if errors:
            return jsonify({"errors": errors}), 400
        goal_id = db.insert_goal(
            fields["name"], fields["target_amount"], fields["target_date"],
            datetime.utcnow().isoformat(),
        )
        return jsonify({"goal_id": goal_id}), 201
    return jsonify([goal_with_computed(g) for g in db.get_goals()])


@app.route("/api/goals/<int:goal_id>", methods=["PATCH", "DELETE"])
def patch_goal(goal_id):
    if request.method == "DELETE":
        # An escape hatch: without it a goal that predates validation and holds a
        # bad value can never be removed through the API.
        if not db.delete_goal(goal_id):
            return jsonify({"error": "goal not found"}), 404
        return jsonify({"status": "deleted"})
    body = _json_body()
    if body is None:
        return jsonify({"error": "JSON object required"}), 400
    fields, errors = _clean_goal_fields(body, require_all=False)
    if errors:
        return jsonify({"errors": errors}), 400
    if not fields:
        return jsonify({"error": "no updatable fields provided"}), 400
    if not db.update_goal(goal_id, fields):
        return jsonify({"error": "goal not found"}), 404
    return jsonify({"status": "updated", "updated": sorted(fields)})


@app.route("/api/set_aside")
def set_aside():
    item_id = read_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    balance_rows = fetch_balances(item_id) or db.get_balances(item_id)
    checking_rows = [b for b in balance_rows if b.get("account_subtype") == "checking"]
    available_balance = sum(
        (b["available"] if b["available"] is not None else b["current"]) or 0
        for b in checking_rows
    )

    refresh_recurring(item_id)
    income_rows = db.get_income_upcoming(item_id)
    today = datetime.utcnow().date()
    upcoming_paydays = [
        d for d in (datetime.strptime(i["next_pay_date"], "%Y-%m-%d").date() for i in income_rows)
        if d >= today
    ]
    next_payday = min(upcoming_paydays) if upcoming_paydays else None
    # Look ahead to the next payday, but never less than MIN_HORIZON_DAYS. On a
    # weekly pay cycle the next payday is often tomorrow, which would shrink the
    # window to a single day and report almost the whole balance as spendable.
    floor_date = today + timedelta(days=MIN_HORIZON_DAYS)
    horizon = max(next_payday, floor_date) if next_payday else today + timedelta(days=30)

    bills_rows = db.get_bills_upcoming(item_id)
    upcoming_bills_total = sum(
        b["amount"] for b in bills_rows
        if today <= datetime.strptime(b["next_due_date"], "%Y-%m-%d").date() <= horizon
    )

    days_until_horizon = max(1, (horizon - today).days)
    savings_due = sum(
        goal_with_computed(g)["required_weekly"] * (days_until_horizon / 7)
        for g in db.get_goals()
    )

    recommended_set_aside = upcoming_bills_total + savings_due
    return jsonify({
        "available_balance": available_balance,
        "upcoming_bills_total": upcoming_bills_total,
        "savings_due": savings_due,
        "recommended_set_aside": recommended_set_aside,
        "horizon": horizon.isoformat(),
        "next_payday": next_payday.isoformat() if next_payday else None,
        "safe_to_spend": available_balance - recommended_set_aside,
    })


@app.route("/api/analytics/category_trend")
def analytics_category_trend():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
        months = _int_arg("months", 6, 1, 60)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.category_trend(item_id, months=months, days=days))


@app.route("/api/analytics/category_totals")
def analytics_category_totals():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    month = request.args.get("month")
    if month is not None:
        try:
            datetime.strptime(month, "%Y-%m")
            if len(month) != 7:
                raise ValueError
        except ValueError:
            return jsonify({"error": "month must be YYYY-MM"}), 400
    return jsonify(db.category_totals(item_id, month=month, days=days))


@app.route("/api/budget/month")
def budget_month():
    month = request.args.get("month")
    if month is None:
        month = datetime.now(LOCAL_TZ).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return jsonify({"error": "month must be YYYY-MM"}), 400
    item_id = db.latest_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    return jsonify(db.budget_month(item_id, month))


@app.route("/api/analytics/merchants")
def analytics_merchants():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
        limit = _int_arg("limit", 25, 1, 100)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.merchants(item_id, category=request.args.get("category"), limit=limit, days=days))


@app.route("/api/analytics/daily_flow")
def analytics_daily_flow():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.daily_flow(item_id, days=days))


@app.route("/api/transactions/pending")
def transactions_pending():
    try:
        limit = _int_arg("limit", 20, 1, 100)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.pending_transactions(item_id, limit=limit))


@app.route("/api/analytics/months")
def analytics_months():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.month_totals(item_id, days=days))


@app.route("/api/analytics/fees")
def analytics_fees():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.fees_summary(item_id, days=days))


@app.route("/api/analytics/liquidations")
def analytics_liquidations():
    try:
        days = _int_arg("days", 90, 0, MAX_ANALYTICS_DAYS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = active_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(db.liquidation_summary(item_id, days=days))


_CENT = Decimal("0.01")


def _cents(value):
    """Convert money through Decimal, never binary floating point."""
    try:
        return int((Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)) * 100)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid money amount")


def _money(cents):
    return float((Decimal(cents) / 100).quantize(_CENT))


def _add_months(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _next_recurrence(day, frequency=None, interval_days=None, anchor_day=None):
    anchor_day = anchor_day or day.day
    frequency = str(frequency or "").upper().replace("-", "_")
    if frequency in ("ANNUAL", "ANNUALLY", "YEARLY") or interval_days == 365:
        year = day.year + 1
        return day.replace(year=year, day=min(anchor_day, calendar.monthrange(year, day.month)[1]))
    if frequency in ("MONTHLY", "MONTH") or interval_days in (28, 29, 30, 31):
        nxt = _add_months(day, 1)
        return nxt.replace(day=min(anchor_day, calendar.monthrange(nxt.year, nxt.month)[1]))
    if frequency in ("SEMI_MONTHLY", "SEMIMONTHLY") or interval_days == 15:
        return day.replace(day=15) if day.day < 15 else _add_months(day.replace(day=1), 1)
    if frequency in ("BIWEEKLY", "BI_WEEKLY") or interval_days == 14:
        return day + timedelta(days=14)
    return day + timedelta(days=max(1, int(interval_days or 7)))


def _scheduled_events(rows, date_key, sign, horizon, today=None, posted=None):
    today = today or datetime.now(LOCAL_TZ).date()
    posted = posted or set()
    events = []
    for row in rows:
        raw_due = row.get(date_key) or row.get("next_date")
        if not raw_due:
            continue
        due = raw_due if isinstance(raw_due, date) else datetime.strptime(str(raw_due)[:10], "%Y-%m-%d").date()
        anchor_day = due.day
        interval = row.get("interval_days")
        frequency = row.get("frequency")
        amount = _cents(abs(Decimal(str(row.get("amount", 0))))) * sign
        # Rows can lag one sync behind. A posted occurrence is evidence, not a
        # prediction, so never add it again.
        while due <= horizon:
            key = (str(row.get("name", "")), amount, due.isoformat())
            posted_match = (key in posted or due.isoformat() in posted or
                            (str(row.get("name", "")), abs(amount), due.isoformat()) in posted)
            if due >= today and not posted_match:
                events.append({"date": due.isoformat(), "name": row.get("name", ""),
                               "amount": _money(amount), "kind": "income" if sign > 0 else "bill"})
            due = _next_recurrence(due, frequency, interval, anchor_day)
    return events


def forecast_cashflow(starting_cents, bills, income, today=None, days=30,
                      recurring_source="plaid", balance_source="live",
                      balance_fetched_at=None, posted=None, extra_expense=None,
                      balance_present=True):
    """Pure deterministic projection. Inputs are rows, output remains API-shaped."""
    today = today or datetime.now(LOCAL_TZ).date()
    horizon = today + timedelta(days=days)
    events = _scheduled_events(bills, "next_due_date", -1, horizon, today, posted)
    events += _scheduled_events(income, "next_pay_date", 1, horizon, today, posted)
    if extra_expense:
        event = dict(extra_expense)
        event["amount"] = _money(-_cents(abs(event.get("amount", 0))))
        events.append(event)
    # Bills settle before income on the same day, making minimum balance safe.
    events.sort(key=lambda event: (event["date"], 0 if event["kind"] == "bill" else 1))
    by_date = {}
    for event in events:
        by_date.setdefault(event["date"], []).append(event)
    running = int(starting_cents)
    lowest = running
    first_negative = today.isoformat() if running < 0 else None
    first_crossing = today.isoformat() if running <= 0 else None
    timeline = []
    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        day_events = by_date.get(day.isoformat(), [])
        opening = running
        daily_low = running
        for event in day_events:
            running += _cents(event["amount"])
            daily_low = min(daily_low, running)
            if running < lowest:
                lowest = running
            if running < 0 and first_negative is None:
                first_negative = day.isoformat()
            if running <= 0 and first_crossing is None:
                first_crossing = day.isoformat()
        timeline.append({"date": day.isoformat(), "opening_balance": _money(opening),
                         "daily_low": _money(daily_low), "closing_balance": _money(running),
                         "balance": _money(running), "events": day_events})
    fresh = None
    if balance_fetched_at:
        try:
            stamp = str(balance_fetched_at).replace("Z", "+00:00")
            fetched = datetime.fromisoformat(stamp)
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=LOCAL_TZ)
            age = (datetime.now(LOCAL_TZ) - fetched.astimezone(LOCAL_TZ)).total_seconds()
            fresh = 0 <= age <= 86400
        except (TypeError, ValueError, OverflowError):
            fresh = False
    schedules_present = bool(bills) and bool(income)
    confidence = ("high" if balance_present and balance_source == "live" and
                  recurring_source == "plaid" and schedules_present and fresh is True
                  else "insufficient")
    end_of_week = timeline[min(6, len(timeline) - 1)] if timeline else None
    return {
        "estimated": confidence != "high",
        "confidence": confidence,
        "balance_source": balance_source,
        "recurring_source": recurring_source,
        "balance_fetched_at": balance_fetched_at,
        "balance_fresh": fresh,
        "freshness": "fresh" if fresh is True else ("stale" if fresh is False else "unknown"),
        "as_of": today.isoformat(),
        "starting_balance": _money(starting_cents),
        "opening_balance": _money(starting_cents),
        "ending_balance": _money(running),
        "lowest_balance": _money(lowest),
        "daily_low": _money(lowest),
        "end_of_week_balance": end_of_week["closing_balance"] if end_of_week else None,
        "end_of_week_date": end_of_week["date"] if end_of_week else None,
        "first_negative_date": first_negative,
        "first_crossing_date": first_crossing,
        "timeline": timeline,
    }


def _posted_occurrences(item_id, today, horizon):
    """Read posted rows for this Item only; used to de-duplicate stale schedules."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT name, amount, date FROM transactions "
        "WHERE item_id = ? AND pending = 0 AND date BETWEEN ? AND ?",
        (item_id, today.isoformat(), horizon.isoformat()),
    ).fetchall()
    conn.close()
    return {(str(row["name"]), _cents(abs(row["amount"])), str(row["date"])[:10]) for row in rows}


def cashflow_projection(item_id, days=30, extra_expense=None):
    """Forecast from cached/live checking plus recurring streams. It remains
    explicitly estimated while Plaid recurring or balance calls are unavailable."""
    balance_rows = fetch_balances(item_id)
    balance_source = "live" if balance_rows is not None else "cached"
    balance_rows = balance_rows if balance_rows is not None else db.get_balances(item_id)
    checking_rows = [b for b in balance_rows if b.get("account_subtype") == "checking"]
    balance = sum(((b.get("available") if b.get("available") is not None else b.get("current")) or 0) for b in checking_rows)
    today = datetime.now(LOCAL_TZ).date()
    horizon = today + timedelta(days=days)
    posted = _posted_occurrences(item_id, today, horizon)
    recurring_source = refresh_recurring(item_id)
    fetched = [b.get("fetched_at") for b in checking_rows if b.get("fetched_at")]
    return forecast_cashflow(_cents(balance), db.get_bills_upcoming(item_id), db.get_income_upcoming(item_id),
                             today=today, days=days, recurring_source=recurring_source,
                             balance_source=balance_source, balance_fetched_at=max(fetched) if fetched else None,
                             balance_present=bool(checking_rows), posted=posted,
                             extra_expense=extra_expense)


@app.route("/api/cashflow_projection")
def projection():
    try:
        days = _int_arg("days", 30, 7, 90)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    # Forecast inputs must come from the same resolvable Item; the cached
    # latest-item fallback is intentionally not safe for a money projection.
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    return jsonify(cashflow_projection(item_id, days))


@app.route("/api/can_i_afford", methods=["POST"])
def can_i_afford():
    body = _json_body()
    if body is None:
        return jsonify({"error": "JSON object required"}), 400
    try:
        amount = float(body.get("amount"))
        when = datetime.strptime(str(body.get("date")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number and date must be YYYY-MM-DD"}), 400
    if not math.isfinite(amount) or amount <= 0:
        return jsonify({"error": "amount must be a positive finite number"}), 400
    today = datetime.now(LOCAL_TZ).date()
    if not today <= when <= today + timedelta(days=90):
        return jsonify({"error": "date must be within the next 90 days"}), 400
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "no account linked yet"}), 400
    forecast = cashflow_projection(item_id, days=(when - today).days + 30, extra_expense={
        "date": when.isoformat(), "name": "Planned purchase", "amount": -round(amount, 2), "kind": "purchase",
    })
    return jsonify({
        # A stale/missing balance or heuristic recurrence must never produce a
        # reassuring affirmative answer. The projection is still returned for
        # transparency and planning.
        "can_afford": forecast["confidence"] == "high" and forecast["lowest_balance"] >= 0,
        "confidence": forecast["confidence"],
        "projected_low": forecast["lowest_balance"],
        "first_negative_date": forecast["first_negative_date"],
        "forecast": forecast,
    })


@app.route("/api/transactions/<transaction_id>/type", methods=["PATCH"])
def override_transaction_type(transaction_id):
    body = _json_body()
    if body is None:
        return jsonify({"error": "JSON object required"}), 400
    transaction_type, requested_item_id = body.get("transaction_type"), body.get("item_id")
    if not isinstance(requested_item_id, str) or not requested_item_id:
        return jsonify({"error": "item_id is required"}), 400
    item_id = current_item_id()
    if not item_id:
        return jsonify({"error": "current Item cannot be resolved"}), 503
    if requested_item_id != item_id:
        return jsonify({"error": "item_id does not match current Item"}), 409
    try:
        updated = db.set_transaction_type(item_id, transaction_id, transaction_type)
    except ValueError:
        return jsonify({"error": "transaction_type must be standard, transfer, or liquidation"}), 400
    if not updated:
        return jsonify({"error": "transaction not found"}), 404
    return jsonify({"status": "updated", "transaction_type": transaction_type})


@app.route("/api/transactions/review")
def transactions_review():
    try:
        limit = _int_arg("limit", 50, 1, 100)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    item_id = read_item_id()
    if not item_id:
        return jsonify([])
    rows = db.transactions_for_review(item_id, limit)
    for row in rows:
        row["item_id"] = item_id
    return jsonify(rows)


@app.route("/dashboard")
def dashboard():
    return render_template("analytics.html")


def _analyst_snapshot():
    """Return useful read-only context without account or transaction identifiers."""
    item_id = active_item_id()
    if not item_id:
        return {"connected": False, "data_warnings": ["No linked account is available."]}

    warnings = []

    def read_section(name, default, function):
        try:
            return function()
        except Exception:
            warnings.append(f"{name} is currently unavailable.")
            return default

    balances = read_section("Account balances", [], lambda: db.get_balances(item_id))
    bills = read_section("Upcoming bills", [], lambda: db.get_bills_upcoming(item_id))
    income = read_section("Upcoming income", [], lambda: db.get_income_upcoming(item_id))
    forecast = read_section("Cash-flow forecast", {}, lambda: cashflow_projection(item_id, 30))
    months = read_section("Monthly cash flow", [], lambda: db.month_totals(item_id)[-6:])
    categories = read_section("Spending categories", [], lambda: db.category_totals(item_id, days=90)[:10])
    merchants = read_section("Merchant trends", [], lambda: db.merchants(item_id, limit=10, days=90))
    goals = read_section("Savings goals", [], db.get_goals)

    return {
        "connected": True,
        "access": "read-only dashboard data; no credentials or money-movement authority",
        "accounts": [{k: row.get(k) for k in
                      ("account_name", "account_type", "account_subtype", "available",
                       "current", "iso_currency_code", "fetched_at")} for row in balances],
        "monthly_summary": months,
        "spending_categories_90d": categories,
        "top_merchants_90d": [{k: row.get(k) for k in
                               ("name", "category", "visits", "total", "avg_amount", "last_seen")}
                              for row in merchants],
        "forecast_headline": {k: forecast.get(k) for k in
                               ("estimated", "confidence", "starting_balance", "lowest_balance",
                                "first_negative_date", "ending_balance", "as_of", "balance_source",
                                "balance_fetched_at", "recurring_source") if k in forecast},
        "upcoming_bills": [{k: row.get(k) for k in
                            ("name", "amount", "interval_days", "last_date", "next_due_date")}
                           for row in bills[:20]],
        "upcoming_income": [{k: row.get(k) for k in
                             ("name", "amount", "interval_days", "last_date", "next_pay_date")}
                            for row in income[:20]],
        "goals": [{k: row.get(k) for k in
                   ("name", "target_amount", "target_date", "current_saved", "created_at")}
                  for row in goals[:20]],
        "data_warnings": warnings,
    }


@app.route("/api/analyst/history")
def analyst_history():
    return jsonify(analyst.history())


@app.route("/api/analyst/events", methods=["POST"])
def analyst_event():
    body = _json_body()
    if not body or body.get("confirmed") is not True:
        return jsonify({"error": "explicit confirmed=true is required"}), 400
    try:
        event = analyst.save_event(body)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"event": event}), 201


@app.route("/api/analyst/chat", methods=["POST"])
def analyst_chat():
    body = _json_body()
    message = body.get("message") if body else None
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        return jsonify({"error": "message is required and must be 1-4000 characters"}), 400
    message = message.strip()
    prior = analyst.history(limit=12)
    try:
        snapshot = _analyst_snapshot()
    except Exception:
        snapshot = {"monthly_summary": {}, "forecast_headline": {}, "upcoming_totals": {}}
    prompt = [{"role": "system", "content": analyst.SYSTEM_PROMPT},
              {"role": "user", "content": json.dumps({"snapshot": snapshot,
               "recent_conversation": prior["messages"],
               "confirmed_planning_events": prior["confirmed_events"],
               "question": message}, separators=(",", ":"))}]
    try:
        result = analyst.call_kimi(prompt)
    except analyst.KimiAPIError as error:
        return jsonify({"error": str(error)}), 502
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except Exception:
        result = {"reply": "I couldn't safely interpret the analyst response. Please try again.",
                  "proposed_event": None}
    analyst.save_message("user", message)
    analyst.save_message("assistant", result["reply"])
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")

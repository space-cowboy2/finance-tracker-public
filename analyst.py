"""Local persistence and bounded Kimi client for the finance analyst."""
import json
import os
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DB_PATH = os.path.join(os.path.dirname(__file__), "analyst.db")
ALLOWED_KINDS = {"unexpected_bill", "extra_credit_card_payment"}
SYSTEM_PROMPT = (
    "You are a personal finance analyst with user-authorized, read-only access to the "
    "account_snapshot included in each request. Use that snapshot as the source of truth. "
    "When it is connected and populated, never claim you cannot access the user's account; "
    "say you have read-only dashboard data, not bank-login or transaction authority. Give "
    "educational, advisory, uncertainty-aware guidance; never claim guarantees or execute "
    "money movement. Answer the user's question first in plain, warm language. Keep reply "
    "to at most 3 short paragraphs or 5 bullets; do not restate the prompt, dump raw data, "
    "mention internal field names, or say 'as an AI'. Use only the few numbers needed to "
    "explain the answer. Say clearly when the answer is uncertain, estimated, or based on "
    "cached data. Only propose an event for a clearly stated unexpected bill or extra credit "
    "card payment; otherwise proposed_event must be null. Return strict JSON only: "
    '{"reply":"...","proposed_event":null or {"kind":"unexpected_bill" or '
    '"extra_credit_card_payment","description":"...","amount":positive number,'
    '"due_date":"YYYY-MM-DD"}}.'
)


class KimiAPIError(Exception):
    pass


def _path(path=None):
    return path or os.getenv("ANALYST_DB_PATH", DB_PATH)


def connect(path=None):
    conn = sqlite3.connect(_path(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
        content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        description TEXT NOT NULL, amount REAL NOT NULL, due_date TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
    """)
    return conn


def history(path=None, limit=20):
    c = connect(path)
    try:
        messages = [dict(r) for r in c.execute(
            "SELECT id, role, content, created_at FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
        events = [dict(r) for r in c.execute(
            "SELECT id, kind, description, amount, due_date, created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
    finally:
        c.close()
    messages.reverse(); events.reverse()
    return {"messages": messages, "confirmed_events": events}


def save_message(role, content, path=None):
    c = connect(path)
    try:
        c.execute("INSERT INTO messages(role, content) VALUES (?, ?)", (role, content))
        c.commit()
    finally:
        c.close()


def validate_event(event):
    if not isinstance(event, dict) or event.get("kind") not in ALLOWED_KINDS:
        raise ValueError("kind must be unexpected_bill or extra_credit_card_payment")
    description = event.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 240:
        raise ValueError("description is required and must be at most 240 characters")
    try:
        amount = Decimal(str(event.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("amount must be a positive finite number")
    if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
        raise ValueError("amount must be a positive finite number")
    due_date = event.get("due_date")
    try:
        parsed = date.fromisoformat(str(due_date))
    except (TypeError, ValueError):
        raise ValueError("due_date must be YYYY-MM-DD")
    if parsed.isoformat() != due_date:
        raise ValueError("due_date must be YYYY-MM-DD")
    return {"kind": event["kind"], "description": description.strip(),
            "amount": float(amount.quantize(Decimal("0.01"))), "due_date": due_date}


def save_event(event, path=None):
    clean = validate_event(event)
    c = connect(path)
    try:
        c.execute("INSERT INTO events(kind, description, amount, due_date) VALUES (?, ?, ?, ?)",
                  (clean["kind"], clean["description"], clean["amount"], clean["due_date"]))
        c.commit()
    finally:
        c.close()
    return clean


def call_kimi(messages, api_key=None, api_url=None, model=None, opener=None):
    key = api_key if api_key is not None else os.getenv("KIMI_API_KEY")
    if not key:
        raise RuntimeError("KIMI_API_KEY is not configured")
    selected_model = model or os.getenv("KIMI_MODEL", "kimi-k3")
    body = {"model": selected_model, "messages": messages,
            "max_completion_tokens": 500,
            "response_format": {"type": "json_object"}}
    if selected_model in {"kimi-k3", "k3", "k3-256k"}:
        body["reasoning_effort"] = os.getenv("KIMI_REASONING_EFFORT", "high")
    else:
        body["temperature"] = 0.2
    platform_url = "https://api.moonshot.ai/v1/chat/completions"
    request_url = api_url or os.getenv("KIMI_API_URL", platform_url)
    opener = opener or urlopen

    def request_completion(url):
        req = Request(url, data=json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + key,
                               "Content-Type": "application/json"}, method="POST")
        with opener(req, timeout=30) as response:
            return response.read().decode("utf-8")

    try:
        raw = request_completion(request_url)
    except HTTPError as error:
        if error.code in {401, 403} and request_url == platform_url and selected_model == "kimi-k3":
            for fallback_url, fallback_model in (
                    ("https://api.moonshot.cn/v1/chat/completions", "kimi-k3"),
                    ("https://api.kimi.com/coding/v1/chat/completions", "k3")):
                body["model"] = fallback_model
                try:
                    raw = request_completion(fallback_url)
                except HTTPError as fallback_error:
                    error = fallback_error
                    if error.code not in {401, 403}:
                        break
                else:
                    error = None
                    break
        if error is None:
            pass
        else:
            labels = {400: "rejected the request", 401: "rejected the API key",
                      403: "denied access", 404: "could not find the endpoint or model",
                      429: "rate limit or balance was reached"}
            raise KimiAPIError(f"Kimi API {labels.get(error.code, 'request failed')} (HTTP {error.code})") from None
    except URLError:
        raise KimiAPIError("Kimi API could not be reached") from None
    try:
        outer = json.loads(raw)
        content = outer["choices"][0]["message"]["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise KimiAPIError("Kimi API returned an unsupported response") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reply"), str) or not parsed["reply"].strip():
        raise ValueError("invalid analyst response")
    proposal = parsed.get("proposed_event")
    if proposal is not None:
        try:
            proposal = validate_event(proposal)
        except ValueError:
            proposal = None
    return {"reply": parsed["reply"].strip(), "proposed_event": proposal}

import re
import sqlite3
from datetime import datetime, timedelta

import os

# Anchored to this file, not the CWD: a relative path silently creates an empty
# database wherever the process happens to be launched from, which looks like data loss.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn):
    schema = """
        CREATE TABLE IF NOT EXISTS transactions (
            item_id TEXT,
            transaction_id TEXT,
            account_id TEXT,
            date TEXT,
            name TEXT,
            amount REAL,
            category TEXT,
            pending INTEGER,
            transaction_type TEXT NOT NULL DEFAULT 'standard',
            type_override TEXT,
            PRIMARY KEY (item_id, transaction_id)
        );

        CREATE TABLE IF NOT EXISTS sync_cursor (
            item_id TEXT PRIMARY KEY,
            cursor TEXT
        );

        CREATE TABLE IF NOT EXISTS balances (
            account_id TEXT PRIMARY KEY,
            item_id TEXT,
            account_name TEXT,
            account_type TEXT,
            account_subtype TEXT,
            available REAL,
            current REAL,
            iso_currency_code TEXT,
            fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bills (
            bill_key TEXT PRIMARY KEY,
            name TEXT,
            amount REAL,
            interval_days INTEGER,
            last_date TEXT,
            next_due_date TEXT,
            item_id TEXT
        );

        CREATE TABLE IF NOT EXISTS income (
            income_key TEXT PRIMARY KEY,
            name TEXT,
            amount REAL,
            interval_days INTEGER,
            last_date TEXT,
            next_pay_date TEXT,
            item_id TEXT
        );

        CREATE TABLE IF NOT EXISTS goals (
            goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            target_amount REAL,
            target_date TEXT,
            current_saved REAL DEFAULT 0,
            created_at TEXT
        );
        """
    # executescript commits any active transaction before running. Execute
    # statements individually so migrations remain atomic on failure.
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("CREATE INDEX IF NOT EXISTS transactions_item_date ON transactions(item_id, date)")


def init_db():
    """Create a fresh schema. Existing databases need explicit migrate_db()."""
    conn = get_conn()
    try:
        create_schema(conn)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_1(conn):
    transaction_columns = _columns(conn, "transactions")
    # Add ownership column before create_schema creates its index. Legacy
    # transactions tables may predate item_id entirely.
    if transaction_columns and "item_id" not in transaction_columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN item_id TEXT")
    create_schema(conn)
    transaction_columns = _columns(conn, "transactions")
    for column, definition in (("transaction_type", "TEXT NOT NULL DEFAULT 'standard'"), ("type_override", "TEXT")):
        if column not in transaction_columns:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
    for table in ("bills", "income"):
        if "item_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN item_id TEXT")
    balance_columns = _columns(conn, "balances")
    for column, definition in (
        ("account_type", "TEXT"),
        ("account_subtype", "TEXT"),
        ("available", "REAL"),
        ("iso_currency_code", "TEXT"),
        ("fetched_at", "TEXT"),
        ("item_id", "TEXT"),
    ):
        if column not in balance_columns:
            conn.execute(f"ALTER TABLE balances ADD COLUMN {column} {definition}")


def _migrate_2(conn):
    info = list(conn.execute("PRAGMA table_info(transactions)"))
    primary_key = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
    if primary_key != ["item_id", "transaction_id"]:
        conn.execute("ALTER TABLE transactions RENAME TO transactions_legacy")
        conn.execute("""CREATE TABLE transactions (
            item_id TEXT, transaction_id TEXT, account_id TEXT, date TEXT, name TEXT,
            amount REAL, category TEXT, pending INTEGER,
            transaction_type TEXT NOT NULL DEFAULT 'standard', type_override TEXT,
            PRIMARY KEY (item_id, transaction_id))""")
        old = _columns(conn, "transactions_legacy")
        def source(column, fallback="NULL"):
            return column if column in old else fallback

        conn.execute(f"""INSERT INTO transactions
            (transaction_id, account_id, item_id, date, name, amount, category, pending, transaction_type, type_override)
            SELECT {source('transaction_id')}, {source('account_id')}, {source('item_id')},
                   {source('date')}, {source('name')}, {source('amount')}, {source('category')},
                   {source('pending')}, COALESCE({source('transaction_type', "'standard'")}, 'standard'),
                   {source('type_override')}
            FROM transactions_legacy""")
        conn.execute("DROP TABLE transactions_legacy")
    # Recreate so an index left by an older release cannot retain wrong column order.
    conn.execute("DROP INDEX IF EXISTS transactions_item_date")
    conn.execute("CREATE INDEX transactions_item_date ON transactions(item_id, date)")

    cursor_columns = _columns(conn, "sync_cursor")
    if "item_id" not in cursor_columns:
        conn.execute("ALTER TABLE sync_cursor RENAME TO sync_cursor_legacy")
        conn.execute("CREATE TABLE sync_cursor (item_id TEXT PRIMARY KEY, cursor TEXT)")
        # NULL ownership is intentional: old global cursor remains evidence, but
        # no new Item can accidentally inherit it.
        if "cursor" in cursor_columns:
            conn.execute("INSERT INTO sync_cursor (item_id, cursor) SELECT NULL, cursor FROM sync_cursor_legacy ORDER BY rowid LIMIT 1")
        conn.execute("DROP TABLE sync_cursor_legacy")

    conn.execute("CREATE INDEX IF NOT EXISTS balances_item_id ON balances(item_id)")
    # These are derived rows with no Item ownership. Never guess ownership.
    conn.execute("DELETE FROM bills WHERE item_id IS NULL")
    conn.execute("DELETE FROM income WHERE item_id IS NULL")


def migrate_db(path):
    """Apply numbered, retry-safe migrations to a named disposable/maintained DB."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                _migrate_1(conn)
                conn.execute("PRAGMA user_version = 1")
            if version < 2:
                _migrate_2(conn)
                conn.execute("PRAGMA user_version = 2")
    finally:
        conn.close()


def get_cursor(item_id):
    conn = get_conn()
    row = conn.execute("SELECT cursor FROM sync_cursor WHERE item_id = ?", (item_id,)).fetchone()
    conn.close()
    return row["cursor"] if row else None


def set_cursor(item_id, cursor):
    conn = get_conn()
    conn.execute(
        "INSERT INTO sync_cursor (item_id, cursor) VALUES (?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET cursor = excluded.cursor",
        (item_id, cursor),
    )
    conn.commit()
    conn.close()


def upsert_transaction(t):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO transactions
            (transaction_id, account_id, item_id, date, name, amount, category, pending, transaction_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, transaction_id) DO UPDATE SET
            account_id = excluded.account_id,
            item_id = excluded.item_id,
            date = excluded.date,
            name = excluded.name,
            amount = excluded.amount,
            category = excluded.category,
            pending = excluded.pending,
            transaction_type = CASE
                WHEN transactions.type_override IS NULL THEN excluded.transaction_type
                ELSE transactions.transaction_type
            END
        """,
        t,
    )
    conn.commit()
    conn.close()


def remove_transaction(item_id, transaction_id):
    conn = get_conn()
    conn.execute("DELETE FROM transactions WHERE item_id = ? AND transaction_id = ?", (item_id, transaction_id))
    conn.commit()
    conn.close()


def monthly_summary(item_id):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', date) AS month,
               category,
               SUM(amount) AS total
        FROM transactions
        WHERE amount > 0 AND pending = 0 AND item_id = ? AND transaction_type = 'standard'
        GROUP BY month, category
        ORDER BY month DESC, total DESC
        """,
        (item_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_balance(b):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO balances
            (account_id, item_id, account_name, account_type, account_subtype,
             available, current, iso_currency_code, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            item_id = excluded.item_id,
            account_name = excluded.account_name,
            account_type = excluded.account_type,
            account_subtype = excluded.account_subtype,
            available = excluded.available,
            current = excluded.current,
            iso_currency_code = excluded.iso_currency_code,
            fetched_at = excluded.fetched_at
        """,
        b,
    )
    conn.commit()
    conn.close()


def get_balances(item_id=None):
    conn = get_conn()
    sql, params = "SELECT * FROM balances", ()
    if item_id:
        sql += " WHERE item_id = ?"
        params = (item_id,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_item_id():
    """Last locally synced Item is a safe read-only fallback when Plaid's
    item_get is temporarily unavailable. It never mixes Items in one query."""
    conn = get_conn()
    row = conn.execute(
        """SELECT item_id FROM balances WHERE item_id IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 1"""
    ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT item_id FROM transactions WHERE item_id IS NOT NULL
               ORDER BY date DESC LIMIT 1"""
        ).fetchone()
    conn.close()
    return row["item_id"] if row else None


def purge_orphans(current_item_id):
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM transactions WHERE item_id IS NOT NULL AND item_id != ?",
        (current_item_id,),
    )
    deleted = cur.rowcount
    conn.execute(
        "DELETE FROM balances WHERE item_id IS NOT NULL AND item_id != ?",
        (current_item_id,),
    )
    conn.execute("DELETE FROM bills WHERE item_id IS NOT NULL AND item_id != ?", (current_item_id,))
    conn.execute("DELETE FROM income WHERE item_id IS NOT NULL AND item_id != ?", (current_item_id,))
    conn.commit()
    conn.close()
    return deleted


# Bank descriptors carry a payment-rail prefix that is identical across unrelated
# merchants. Left in, it dominates the display and weakens name-based grouping.
_DESCRIPTOR_PREFIXES = re.compile(
    r"^(POS Debit|POS Credit|Visa Check Card|ACH Transaction|ACH Debit|ACH Credit|"
    r"Deposit|Withdrawal|Preauthorized (?:Debit|Credit)|Debit Card|Credit Card)\b[\s\-]*",
    re.IGNORECASE,
)


def _normalize_name(name):
    name = re.sub(r"\d{4,}", "", name)
    name = re.sub(r"\b\d{1,2}/\d{1,2}\b", "", name)
    prev = None
    while prev != name:  # descriptors stack, e.g. "POS Debit - Visa Check Card - X"
        prev = name
        name = _DESCRIPTOR_PREFIXES.sub("", name.strip(" -"))
    name = re.sub(r"\s+", " ", name).strip(" -")
    return name


def _canonical_name(name):
    return _normalize_name(name).casefold()


TRANSFER_NAME_PATTERNS = (
    "ONLINE TRANSFER",
    "ONLINE REALTIME TRANSFER",
    "TRF FR OTHER",
    "TRF TO OTHER",
    "TRANSFER FROM CHECKING",
    "TRANSFER TO CHECKING",
    "TRANSFER FROM SHARE",
    "TRANSFER TO SHARE",
)
LIQUIDATION_NAME_PATTERN = re.compile(r"\bBROKERAGE\b.*\bPPD ID\b", re.IGNORECASE)
PAYROLL_NAME_PATTERN = re.compile(
    r"\b(?:PAYROLL|PAY\s*CHECK|PAYCHECK|SALARY|WAGES|DIRECT\s+DEP(?:OSIT)?)\b",
    re.IGNORECASE,
)
P2P_NAME_PATTERN = re.compile(r"\b(?:ACH\s+)?P2P\b", re.IGNORECASE)


def classify_transaction(name, own_name=None):
    """Conservative automatic classification. Ambiguous P2P remains standard
    until the user explicitly overrides it; hiding a real payment is worse than
    counting it as normal activity."""
    return classify_transaction_details(name, own_name)["transaction_type"]


def classify_transaction_details(name, own_name=None):
    """Return conservative type plus confidence/review metadata.

    P2P ownership is never inferred from an unverified name. Callers may
    integrate verified owner identity later through ``own_name``.
    """
    text = name or ""
    upper = text.upper()
    # Payroll descriptors can contain ACH/transfer language. Income evidence
    # wins before any transfer rule, so a paycheck can never be hidden.
    if PAYROLL_NAME_PATTERN.search(text):
        return {"transaction_type": "standard", "confidence": "high", "review_required": False,
                "reason": "payroll descriptor"}
    if LIQUIDATION_NAME_PATTERN.search(text):
        return {"transaction_type": "liquidation", "confidence": "high", "review_required": False,
                "reason": "brokerage liquidation descriptor"}
    # P2P is ambiguous by default. Caller may provide verified account-holder
    # name; only an exact normalized name match is strong enough to call it
    # an own-account transfer.
    if P2P_NAME_PATTERN.search(text):
        if own_name and _canonical_name(text).endswith(_canonical_name(own_name)):
            return {"transaction_type": "transfer", "confidence": "high", "review_required": False,
                    "reason": "verified owner name match"}
        return {"transaction_type": "standard", "confidence": "low", "review_required": True,
                "reason": "ambiguous P2P ownership"}
    if any(pattern in upper for pattern in TRANSFER_NAME_PATTERNS):
        return {"transaction_type": "transfer", "confidence": "high", "review_required": False,
                "reason": "explicit transfer descriptor"}
    return {"transaction_type": "standard", "confidence": "medium", "review_required": False,
            "reason": "no specific classification evidence"}


def backfill_transaction_types(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT item_id, transaction_id, name FROM transactions WHERE transaction_type IS NULL OR transaction_type = 'standard'"
    ).fetchall()
    conn.executemany(
        "UPDATE transactions SET transaction_type = ? WHERE item_id IS ? AND transaction_id = ? AND type_override IS NULL",
        [(classify_transaction(r["name"]), r["item_id"], r["transaction_id"]) for r in rows],
    )
    conn.commit()
    conn.close()


def set_transaction_type(item_id, transaction_id, transaction_type):
    if transaction_type not in {"standard", "transfer", "liquidation"}:
        raise ValueError("invalid transaction type")
    conn = get_conn()
    cur = conn.execute(
        "UPDATE transactions SET transaction_type = ?, type_override = ? WHERE item_id = ? AND transaction_id = ?",
        (transaction_type, transaction_type, item_id, transaction_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def transactions_for_review(item_id, limit=50):
    """Return conservative, unresolved classifications for one Item only."""
    limit = max(1, min(int(limit), 100))
    batch_size = 100
    max_scan = 1000
    conn = get_conn()
    results = []
    last_date = None
    last_transaction_id = None
    scanned = 0
    try:
        while len(results) < limit and scanned < max_scan:
            page_size = min(batch_size, max_scan - scanned)
            params = [item_id]
            keyset = ""
            if last_date is not None:
                keyset = "AND (date < ? OR (date = ? AND transaction_id < ?))"
                params.extend((last_date, last_date, last_transaction_id))
            params.append(page_size)
            rows = conn.execute(
                f"""SELECT transaction_id, date, name, amount, transaction_type
                   FROM transactions
                   WHERE item_id = ? AND pending = 0 AND type_override IS NULL
                     {keyset}
                   ORDER BY date DESC, transaction_id DESC LIMIT ?""",
                params,
            ).fetchall()
            if not rows:
                break
            scanned += len(rows)
            last_date = rows[-1]["date"]
            last_transaction_id = rows[-1]["transaction_id"]
            for row in rows:
                details = classify_transaction_details(row["name"])
                if not details["review_required"]:
                    continue
                results.append({
                    "transaction_id": row["transaction_id"],
                    "date": row["date"],
                    "name": row["name"],
                    "amount": row["amount"],
                    "current_type": row["transaction_type"],
                    "confidence": details["confidence"],
                    "reason": details["reason"],
                })
                if len(results) >= limit:
                    break
            if len(rows) < page_size:
                break
    finally:
        conn.close()
    return results


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


MIN_OCCURRENCES = 3
MAX_INTERVAL_SPREAD = 0.4  # reject streams whose spacing is too irregular to predict
# Nothing billed recurs every day or two. A short cadence means a frequented
# merchant (groceries, coffee, gas), which is a spending habit, not an obligation.
MIN_INTERVAL_DAYS = 5


def _detect_recurring(direction, item_id):
    # direction is validated here rather than interpolated from a caller string.
    comparison = "> 0" if direction == "out" else "< 0"
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT date, name, amount FROM transactions
            WHERE item_id = ? AND pending = 0 AND transaction_type = 'standard' AND amount {comparison}
            ORDER BY date""",
        (item_id,),
    ).fetchall()
    conn.close()

    # Group on the merchant name alone. Including the amount in the key splits a
    # single stream into fragments whenever the amount moves - a pay raise turned
    # one paycheck into four separate "income streams".
    groups = {}
    for r in rows:
        norm = _canonical_name(r["name"])
        display = _normalize_name(r["name"])
        groups.setdefault(norm, []).append((r["date"][:10], r["amount"], display or r["name"]))

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    results = []
    for norm, entries in groups.items():
        if len(entries) < MIN_OCCURRENCES:
            continue
        entries.sort(key=lambda e: e[0])
        dates = [datetime.strptime(e[0], "%Y-%m-%d") for e in entries]
        # Same-day duplicates contribute 0-day gaps that halve the median cadence.
        intervals = [d for d in ((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)) if d > 0]
        if not intervals:
            continue
        interval = int(_median(intervals))
        if interval < MIN_INTERVAL_DAYS:
            continue
        # Irregular spacing means this is a habit, not a scheduled charge.
        spread = sum(abs(d - interval) for d in intervals) / len(intervals) / interval
        if spread > MAX_INTERVAL_SPREAD:
            continue

        amounts = [e[1] for e in entries]
        last_date = dates[-1]
        # Roll forward so a stream that lapsed while unsynced still predicts a
        # future date instead of reporting one permanently in the past.
        next_date = last_date + timedelta(days=interval)
        while next_date < today:
            next_date += timedelta(days=interval)
        results.append({
            "key": norm,
            "name": norm or entries[-1][2],
            # Median, not latest: one anomalous charge should not redefine the stream.
            "amount": round(_median(amounts), 2),
            "interval_days": interval,
            "last_date": last_date.strftime("%Y-%m-%d"),
            "next_date": next_date.strftime("%Y-%m-%d"),
        })
    return results


def detect_bills(item_id):
    results = _detect_recurring("out", item_id)
    conn = get_conn()
    # Full rebuild: upsert-only left rows behind for streams that no longer exist
    # (e.g. after purging a re-linked Item), which kept inflating set-aside totals.
    conn.execute("DELETE FROM bills WHERE item_id = ?", (item_id,))
    for r in results:
        conn.execute(
            """
            INSERT INTO bills (bill_key, name, amount, interval_days, last_date, next_due_date, item_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bill_key) DO UPDATE SET
                name = excluded.name,
                amount = excluded.amount,
                interval_days = excluded.interval_days,
                last_date = excluded.last_date,
                next_due_date = excluded.next_due_date
            """,
            (f"{item_id}:{r['key']}", r["name"], r["amount"], r["interval_days"], r["last_date"], r["next_date"], item_id),
        )
    conn.commit()
    conn.close()


def get_bills_upcoming(item_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bills WHERE item_id = ? ORDER BY next_due_date ASC", (item_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def detect_income(item_id):
    results = _detect_recurring("in", item_id)
    conn = get_conn()
    conn.execute("DELETE FROM income WHERE item_id = ?", (item_id,))
    for r in results:
        conn.execute(
            """
            INSERT INTO income (income_key, name, amount, interval_days, last_date, next_pay_date, item_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(income_key) DO UPDATE SET
                name = excluded.name,
                amount = excluded.amount,
                interval_days = excluded.interval_days,
                last_date = excluded.last_date,
                next_pay_date = excluded.next_pay_date
            """,
            (f"{item_id}:{r['key']}", r["name"], r["amount"], r["interval_days"], r["last_date"], r["next_date"], item_id),
        )
    conn.commit()
    conn.close()


def get_income_upcoming(item_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM income WHERE item_id = ? ORDER BY next_pay_date ASC", (item_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_goal(name, target_amount, target_date, created_at):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO goals (name, target_amount, target_date, current_saved, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (name, target_amount, target_date, created_at),
    )
    conn.commit()
    goal_id = cur.lastrowid
    conn.close()
    return goal_id


def get_goals():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM goals ORDER BY target_date ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_goal(goal_id, fields):
    if not fields:
        return
    conn = get_conn()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE goals SET {set_clause} WHERE goal_id = ?", (*fields.values(), goal_id))
    conn.commit()
    conn.close()
    return cur.rowcount


# --- analytics ---------------------------------------------------------------
def _scoped_where(item_id, days=0):
    where, params = ["item_id = ?", "pending = 0", "transaction_type = 'standard'"], [item_id]
    if days:
        since = (datetime.utcnow().date() - timedelta(days=days - 1)).isoformat()
        where.append("date >= ?")
        params.append(since)
    return " AND ".join(where), params


def category_trend(item_id, months=6, days=0):
    """Spend per category per month, newest month last (chart-ready)."""
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m', date) AS month,
               COALESCE(category, 'UNCATEGORIZED') AS category,
               ROUND(SUM(amount), 2) AS total
        FROM transactions
        WHERE amount > 0 AND {where}
        GROUP BY month, category
        ORDER BY month ASC
        """,
        params,
    ).fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    months_seen = sorted({r["month"] for r in data})[-months:]
    return [r for r in data if r["month"] in months_seen]


def category_totals(item_id, month=None, days=0):
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    where = f"amount > 0 AND {where}"
    if month:
        where += " AND strftime('%Y-%m', date) = ?"
        params.append(month)
    rows = conn.execute(
        f"""SELECT COALESCE(category,'UNCATEGORIZED') AS category,
                   COUNT(*) AS n, ROUND(SUM(amount),2) AS total
            FROM transactions WHERE {where}
            GROUP BY category ORDER BY total DESC""",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def budget_month(item_id, month):
    """Return posted transactions and budget aggregates for one Item/month.

    This is a read-only query. Calendar rows include every posted transaction;
    budget aggregates include only standard transactions.
    """
    start = f"{month}-01"
    year, month_number = (int(part) for part in month.split("-"))
    next_month = (year + 1, 1) if month_number == 12 else (year, month_number + 1)
    end = f"{next_month[0]:04d}-{next_month[1]:02d}-01"
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT date, name, amount, category, transaction_type
               FROM transactions
               WHERE item_id = ? AND pending = 0 AND date >= ? AND date < ?
               ORDER BY date ASC, transaction_id ASC""",
            (item_id, start, end),
        ).fetchall()
        categories = conn.execute(
            """SELECT COALESCE(category, 'UNCATEGORIZED') AS category,
                      COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
               FROM transactions
               WHERE item_id = ? AND pending = 0 AND date >= ? AND date < ?
                 AND amount > 0 AND transaction_type = 'standard'
               GROUP BY category ORDER BY total DESC, category ASC""",
            (item_id, start, end),
        ).fetchall()
        bills = conn.execute(
            """SELECT * FROM bills
               WHERE item_id = ? AND next_due_date >= ? AND next_due_date < ?
               ORDER BY next_due_date ASC, bill_key ASC""",
            (item_id, start, end),
        ).fetchall()
    finally:
        conn.close()

    transactions = [dict(row) for row in rows]
    standard = [row for row in transactions if row["transaction_type"] == "standard"]
    return {
        "month": month,
        "transactions": transactions,
        "categories": [dict(row) for row in categories],
        "totals": {
            "spent": round(sum(row["amount"] for row in standard if row["amount"] > 0), 2),
            "income": round(sum(abs(row["amount"]) for row in standard if row["amount"] < 0), 2),
            "transaction_count": len(transactions),
        },
        "bills": [dict(row) for row in bills],
    }


def merchants(item_id, category=None, limit=25, days=0):
    """Merchant frequency and spend. Frequency changes behaviour; totals alone don't."""
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    where = f"amount > 0 AND {where}"
    if category:
        params.append(category)
        where += " AND COALESCE(category,'UNCATEGORIZED') = ?"
    rows = conn.execute(
        f"""SELECT name, COALESCE(category,'UNCATEGORIZED') AS category,
                   amount, date FROM transactions WHERE {where}""",
        params,
    ).fetchall()
    conn.close()
    grouped = {}
    for r in rows:
        key = (_canonical_name(r["name"]), r["category"])
        group = grouped.setdefault(key, {"name": _normalize_name(r["name"]) or r["name"], "category": r["category"], "visits": 0, "total": 0, "last_seen": r["date"]})
        group["visits"] += 1
        group["total"] += r["amount"]
        group["last_seen"] = max(group["last_seen"], r["date"])
    out = []
    for group in grouped.values():
        group["total"] = round(group["total"], 2)
        group["avg_amount"] = round(group["total"] / group["visits"], 2)
        out.append(group)
    return sorted(out, key=lambda r: r["total"], reverse=True)[:limit]


def daily_flow(item_id, days=90):
    """Per-day spend and income, for the velocity chart."""
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    rows = conn.execute(
        f"""SELECT date,
                   ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2) AS spent,
                   ROUND(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 2) AS received
            FROM transactions
            WHERE {where}
            GROUP BY date ORDER BY date ASC""",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def pending_transactions(item_id, limit=20):
    """Return pending debits and deposits without including them in posted totals."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT transaction_id, date, name, amount, category
           FROM transactions
           WHERE item_id = ? AND pending = 1
           ORDER BY date DESC, transaction_id DESC LIMIT ?""",
        (item_id, limit),
    ).fetchall()
    conn.close()
    return [{**dict(row), "kind": "deposit" if row["amount"] < 0 else "transaction"}
            for row in rows]


def month_totals(item_id, days=0):
    """Income, spend and net per month - the savings-rate series."""
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    rows = conn.execute(
        f"""SELECT strftime('%Y-%m', date) AS month,
                   ROUND(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 2) AS spent,
                   ROUND(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 2) AS income
            FROM transactions
            WHERE {where}
            GROUP BY month ORDER BY month ASC""",
        params,
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["net"] = round(d["income"] - d["spent"], 2)
        d["savings_rate"] = round(d["net"] / d["income"] * 100, 1) if d["income"] else None
        out.append(d)
    return out


def fees_summary(item_id, days=0):
    """Bank fees, annualised. Small per-item, large per-year - that framing is the point."""
    conn = get_conn()
    where, params = _scoped_where(item_id, days)
    rows = conn.execute(
        """SELECT date, name, amount FROM transactions
           WHERE category = 'BANK_FEES' AND amount > 0 AND """ + where + " ORDER BY date DESC",
        params,
    ).fetchall()
    span = conn.execute("SELECT MIN(date) a, MAX(date) b FROM transactions WHERE " + where, params).fetchone()
    conn.close()
    items = [dict(r) for r in rows]
    total = round(sum(i["amount"] for i in items), 2)
    requested_days = int(days or 0)
    if requested_days:
        # The caller selected this calendar window. Do not annualise against
        # the much shorter observed fee span inside it.
        period_days = requested_days
    elif span and span["a"] and span["b"]:
        observed_days = (datetime.strptime(span["b"], "%Y-%m-%d") - datetime.strptime(span["a"], "%Y-%m-%d")).days + 1
        # A single sparse observation is not evidence of a one-day cadence.
        period_days = max(30, observed_days)
    else:
        period_days = 1
    overdraft = round(sum(i["amount"] for i in items if "OVERDRAFT" in i["name"].upper()), 2)
    return {
        "total": total,
        "overdraft_total": overdraft,
        "count": len(items),
        "annualized": round(total / period_days * 365, 2),
        "period_days": period_days,
        "items": items[:20],
    }


def liquidation_summary(item_id, days=0):
    conn = get_conn()
    where, params = ["item_id = ?", "pending = 0", "transaction_type = 'liquidation'"], [item_id]
    if days:
        where.append("date >= ?")
        params.append((datetime.utcnow().date() - timedelta(days=days - 1)).isoformat())
    rows = conn.execute(
        "SELECT date, name, amount FROM transactions WHERE " + " AND ".join(where) + " ORDER BY date DESC",
        params,
    ).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    return {"total": round(sum(-r["amount"] for r in items if r["amount"] < 0), 2), "count": len(items), "items": items[:20]}


def delete_goal(goal_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


def replace_streams(table, item_id, rows):
    """Bulk-replace detected recurring streams. Plaid is authoritative when
    reachable; these rows are derived data and always rebuilt, never merged."""
    key, date_col = ("bill_key", "next_due_date") if table == "bills" else ("income_key", "next_pay_date")
    conn = get_conn()
    conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))
    conn.executemany(
        f"""INSERT INTO {table} ({key}, name, amount, interval_days, last_date, {date_col}, item_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(f"{item_id}:{r['key']}", r["name"], r["amount"], r["interval_days"], r["last_date"], r["next_date"], item_id)
         for r in rows],
    )
    conn.commit()
    conn.close()

import argparse
import hashlib
import os
import sqlite3
import sys

import db


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_backup(db_path, backup_path):
    db_path = os.path.realpath(os.path.abspath(os.fspath(db_path)))
    backup_path = os.path.realpath(os.path.abspath(os.fspath(backup_path)))
    if os.path.normcase(db_path) == os.path.normcase(backup_path):
        raise ValueError("backup must be a different path")
    for path in (db_path, backup_path):
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError("database and backup must exist and be non-empty")
        conn = sqlite3.connect(path)
        try:
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as error:
                raise ValueError("database or backup integrity check failed") from error
            if result != "ok":
                raise ValueError("database or backup integrity check failed")
        finally:
            conn.close()
    if _sha256(db_path) != _sha256(backup_path):
        raise ValueError("backup digest does not match database")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("migrate", "backfill"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    verify_backup(args.db, args.backup)
    if args.command == "migrate":
        db.migrate_db(args.db)
    else:
        db.backfill_transaction_types(args.db)


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"maintenance refused: {error}", file=sys.stderr)
        raise SystemExit(2)

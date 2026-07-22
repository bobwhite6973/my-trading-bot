#!/usr/bin/env python3
"""
issue_license.py — Issue a new license key and add it to keys.json + customer DB.

Usage:
    python scripts/issue_license.py --email user@email.com [--phone +1234567890] [--type full] [--days 365]

Creates a license key in LB-XXXX-XXXX-XXXX format, adds it to keys.json,
and inserts a customer record into customers.db.
"""

import argparse
import json
import os
import secrets
import sqlite3
import string
from datetime import datetime, timezone, timedelta

ALPHABET = string.ascii_uppercase + string.digits
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
KEYS_FILE = os.path.join(ROOT_DIR, "keys.json")
DB_FILE = os.path.join(ROOT_DIR, "customers.db")


def generate_key() -> str:
    """Generate a single key in LB-XXXX-XXXX-XXXX format."""
    parts = []
    for _ in range(3):
        part = ''.join(secrets.choice(ALPHABET) for _ in range(4))
        parts.append(part)
    return "LB-" + "-".join(parts)


def add_to_keys_json(key: str, key_type: str, expires: str | None):
    """Add a key entry to keys.json."""
    entry = {
        "key": key,
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": expires,
        "type": key_type,
    }

    keys = []
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE) as f:
                keys = json.load(f)
        except Exception:
            keys = []

    keys.append(entry)

    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)

    print(f"  → Added to {KEYS_FILE}")


def init_db():
    """Create customers table if it doesn't exist."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            license_key TEXT UNIQUE NOT NULL,
            license_type TEXT DEFAULT 'full',
            purchase_date TEXT NOT NULL,
            expiry_date TEXT,
            stripe_session_id TEXT,
            sol_tx_signature TEXT,
            notes TEXT
        )
    """)
    conn.commit()
    return conn


def insert_customer(conn, email: str, phone: str | None, key: str, key_type: str, expires: str | None):
    """Insert a customer record."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO customers (email, phone, license_key, license_type, purchase_date, expiry_date) VALUES (?, ?, ?, ?, ?, ?)",
        (email, phone, key, key_type, now, expires),
    )
    conn.commit()
    print(f"  → Customer record created for {email}")


def main():
    parser = argparse.ArgumentParser(description="Issue a LeverBot license key")
    parser.add_argument("--email", required=True, help="Customer email address")
    parser.add_argument("--phone", default=None, help="Customer phone number (optional)")
    parser.add_argument("--type", default="full", choices=["full", "trial"], help="License type (default: full)")
    parser.add_argument("--days", type=int, default=365, help="Days until expiry (default: 365, ignored for full)")
    args = parser.parse_args()

    key = generate_key()
    key_type = args.type
    expires = None
    if key_type == "trial":
        expires = (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat()
    elif key_type == "full" and args.days:
        expires = (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat()

    print(f"Issuing {key_type} license:")
    print(f"  Key:     {key}")
    print(f"  Email:   {args.email}")
    print(f"  Expires: {expires[:10] if expires else 'never'}")

    # Add to keys.json
    add_to_keys_json(key, key_type, expires)

    # Add to customer DB
    conn = init_db()
    insert_customer(conn, args.email, args.phone, key, key_type, expires)
    conn.close()

    print(f"\n✓ License issued: {key}")
    print(f"  Send this key to: {args.email}")


if __name__ == "__main__":
    main()

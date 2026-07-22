#!/usr/bin/env python3
"""
process_purchase.py — Process a new Stripe/SOL purchase and issue a license key.

Usage:
    python scripts/process_purchase.py --email user@email.com [--stripe-id cs_xxx] [--sol-tx sig]

Generates a license key, adds it to keys.json, inserts customer record, 
and outputs a pre-formatted email template for the operator to send.
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
    parts = []
    for _ in range(3):
        part = ''.join(secrets.choice(ALPHABET) for _ in range(4))
        parts.append(part)
    return "LB-" + "-".join(parts)


def init_db():
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


EMAIL_TEMPLATE = """Subject: Your LeverBot License Key 🚀

Hi there,

Thank you for purchasing LeverBot! Your license key is:

    {key}

Next steps:
1. Go to https://render.com/deploy?repo=https://github.com/bobwhite6973/my-trading-bot&branch=release
2. Click "Deploy to Render"
3. In the environment variables, paste your license key as LICENSE_KEY
4. Set PAPER_TRADING=true to test first, or false for live trading
5. Your bot will be live in ~2 minutes!

Your license is valid until {expires}.

Dashboard: your-render-url.onrender.com
Docs: https://aitrader.ctonew.app

Questions? Reply to this email.

— The LeverBot Team
"""


def main():
    parser = argparse.ArgumentParser(description="Process a LeverBot purchase and issue a license key")
    parser.add_argument("--email", required=True, help="Customer email address")
    parser.add_argument("--phone", default=None, help="Customer phone number (optional)")
    parser.add_argument("--stripe-id", default=None, help="Stripe session ID")
    parser.add_argument("--sol-tx", default=None, help="Solana transaction signature")
    parser.add_argument("--days", type=int, default=365, help="License validity in days (default: 365)")
    parser.add_argument("--trial", action="store_true", help="Issue a trial key (7 days)")
    args = parser.parse_args()

    key = generate_key()
    now = datetime.now(timezone.utc)
    days = 7 if args.trial else args.days
    expires = (now + timedelta(days=days)).isoformat()
    key_type = "trial" if args.trial else "full"

    # Add to keys.json
    entry = {
        "key": key,
        "created": now.isoformat(),
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

    # Add to customer DB
    conn = init_db()
    conn.execute(
        "INSERT INTO customers (email, phone, license_key, license_type, purchase_date, expiry_date, stripe_session_id, sol_tx_signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (args.email, args.phone, key, key_type, now.isoformat(), expires, args.stripe_id, args.sol_tx),
    )
    conn.commit()
    conn.close()

    # Print results
    print("=" * 60)
    print(f"LICENSE ISSUED: {key}")
    print(f"Type: {key_type} | Expires: {expires[:10]}")
    print(f"Customer: {args.email}")
    print("=" * 60)

    # Print email template
    print("\n--- COPY-PASTE EMAIL TEMPLATE ---\n")
    print(EMAIL_TEMPLATE.format(
        key=key,
        expires=expires[:10] if expires else "never",
    ))
    print("--- END EMAIL TEMPLATE ---")


if __name__ == "__main__":
    main()

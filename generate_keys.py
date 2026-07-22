#!/usr/bin/env python3
"""
generate_keys.py — Generate license keys for LeverBot.

Format: LB-XXXX-XXXX-XXXX (4 groups of 4 uppercase alphanumeric characters)
Output: JSON array of key objects with created/expires/type fields.

Usage:
    python generate_keys.py --count 10 --trial-days 7 --output keys.json
    python generate_keys.py --count 5 --full --output keys.json   # full (non-expiring) keys
"""

import argparse
import json
import secrets
import string
from datetime import datetime, timezone, timedelta

ALPHABET = string.ascii_uppercase + string.digits


def generate_key() -> str:
    """Generate a single key in LB-XXXX-XXXX-XXXX format."""
    parts = []
    for _ in range(3):
        part = ''.join(secrets.choice(ALPHABET) for _ in range(4))
        parts.append(part)
    return "LB-" + "-".join(parts)


def generate_keys(count: int, trial_days: int | None, full: bool = False) -> list[dict]:
    """
    Generate `count` license keys.

    Args:
        count: Number of keys to generate.
        trial_days: Days until expiry for trial keys. None for full keys.
        full: If True, generate full (non-expiring) keys.

    Returns:
        List of dicts with key, created, expires, type.
    """
    now = datetime.now(timezone.utc)
    keys = []

    for _ in range(count):
        key = generate_key()
        key_type = "full" if full else "trial"
        expires = None if full else (now + timedelta(days=trial_days or 7)).isoformat()

        keys.append({
            "key": key,
            "created": now.isoformat(),
            "expires": expires,
            "type": key_type,
        })

    return keys


def main():
    parser = argparse.ArgumentParser(description="Generate LeverBot license keys")
    parser.add_argument("--count", type=int, default=1, help="Number of keys to generate (default: 1)")
    parser.add_argument("--trial-days", type=int, default=7, help="Trial duration in days (default: 7)")
    parser.add_argument("--full", action="store_true", help="Generate full (non-expiring) keys")
    parser.add_argument("--output", type=str, default="keys.json", help="Output JSON file (default: keys.json)")
    args = parser.parse_args()

    keys = generate_keys(args.count, args.trial_days, args.full)

    with open(args.output, "w") as f:
        json.dump(keys, f, indent=2)

    print(f"Generated {len(keys)} key(s) → {args.output}")
    for k in keys:
        expires_str = k["expires"][:10] if k["expires"] else "never"
        print(f"  {k['key']}  ({k['type']}, expires {expires_str})")


if __name__ == "__main__":
    main()

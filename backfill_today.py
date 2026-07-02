"""
One-time backfill script: fetches today's calls from the Aircall REST API
and inserts them into the database, exactly as the webhook would have.

Run this once to catch up on calls that happened before the webhook was
active, or after any interruption. Safe to run multiple times - skips any
call already in the database by aircall_call_id.

Usage:
    python backfill_today.py            # dry run - shows what would be imported
    python backfill_today.py --write    # actually writes to the database
    python backfill_today.py --write --hubspot  # also runs HubSpot lookups (slower)

Requirements:
    AIRCALL_API_ID and AIRCALL_API_TOKEN must be set in .env
    Rate limit: Aircall allows 60 requests/min. This script adds a small
    delay between pages to stay well under that.
"""
import asyncio
import os
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal, init_db
from models import CallEvent
from call_tags import serialize_tags
from timezone_utils import today_pst_bounds_utc, PACIFIC

AIRCALL_BASE = "https://api.aircall.io/v1"
DRY_RUN = "--write" not in sys.argv
RUN_HUBSPOT = "--hubspot" in sys.argv


def aircall_auth_header() -> str:
    api_id = os.environ.get("AIRCALL_API_ID", "").strip()
    api_token = os.environ.get("AIRCALL_API_TOKEN", "").strip()
    if not api_id or not api_token:
        raise SystemExit("AIRCALL_API_ID and AIRCALL_API_TOKEN must be set in .env")
    creds = b64encode(f"{api_id}:{api_token}".encode()).decode()
    return f"Basic {creds}"


def map_aircall_call(call: dict) -> dict | None:
    """
    Map a single Aircall call object to our internal schema.
    Returns None only for internal (agent-to-agent) calls.
    Both inbound and outbound are now included.
    """
    # Skip internal calls only — inbound is now tracked
    if call.get("direction") == "internal":
        return None

    aircall_id = str(call["id"])
    direction = call.get("direction") or "outbound"
    user = call.get("user") or {}
    sdr_name = user.get("name") or "Unknown SDR"

    raw_digits = call.get("raw_digits") or call.get("number") or ""

    # Customer contact name from Aircall (populated if number is a saved contact)
    contact = call.get("contact") or {}
    customer_first_name = (contact.get("first_name") or "").strip() or None
    customer_last_name  = (contact.get("last_name") or "").strip() or None

    started_at_ts = call.get("started_at")
    ended_at_ts = call.get("ended_at")
    answered_at_ts = call.get("answered_at")

    started_at = datetime.fromtimestamp(started_at_ts, tz=timezone.utc).replace(tzinfo=None) if started_at_ts else datetime.utcnow()
    ended_at = datetime.fromtimestamp(ended_at_ts, tz=timezone.utc).replace(tzinfo=None) if ended_at_ts else None

    # Talk time = answered_at to ended_at (excludes ring time, per Aircall docs)
    talk_seconds = None
    if answered_at_ts and ended_at_ts:
        talk_seconds = ended_at_ts - answered_at_ts

    # Determine outcome
    has_voicemail = bool(call.get("voicemail_url") or call.get("voicemail"))
    was_answered = answered_at_ts is not None

    if has_voicemail:
        outcome = "voicemail"
        status = "voicemail"
    elif was_answered:
        outcome = "answered"
        status = "ended"
    else:
        outcome = "missed"
        status = "ended"

    # Tags
    raw_tags = call.get("tags") or []
    tag_names = [t.get("name", "").strip() for t in raw_tags if t.get("name")]
    tags = serialize_tags(tag_names) if tag_names else None

    return {
        "aircall_call_id": aircall_id,
        "sdr_name": sdr_name,
        "direction": direction,
        "raw_digits": raw_digits,
        "customer_first_name": customer_first_name,
        "customer_last_name": customer_last_name,
        "status": status,
        "outcome": outcome,
        "tags": tags,
        "is_active": False,
        "started_at": started_at,
        "ended_at": ended_at,
        "talk_seconds": talk_seconds,
    }


async def fetch_todays_calls() -> list[dict]:
    """Fetch all calls from today (PST) via the Aircall REST API."""
    start_utc, end_utc = today_pst_bounds_utc()
    # Aircall uses UNIX timestamps
    from_ts = int(start_utc.replace(tzinfo=timezone.utc).timestamp())
    to_ts = int(end_utc.replace(tzinfo=timezone.utc).timestamp())

    auth = aircall_auth_header()
    calls = []
    url = f"{AIRCALL_BASE}/calls?from={from_ts}&to={to_ts}&per_page=50&order=asc"

    async with httpx.AsyncClient(timeout=10.0) as client:
        page = 1
        while url:
            print(f"  Fetching page {page}...", end=" ")
            resp = await client.get(url, headers={"Authorization": auth})
            if resp.status_code == 401:
                raise SystemExit("Aircall returned 401 - check your AIRCALL_API_ID and AIRCALL_API_TOKEN in .env")
            if resp.status_code != 200:
                print(f"Error: HTTP {resp.status_code}")
                break

            data = resp.json()
            page_calls = data.get("calls", [])
            calls.extend(page_calls)
            print(f"got {len(page_calls)} calls (total so far: {len(calls)})")

            meta = data.get("meta", {})
            url = meta.get("next_page_link")
            page += 1

            if url:
                time.sleep(0.5)  # stay well under the 60 req/min rate limit

    return calls


async def run():
    init_db()
    db = SessionLocal()

    start_utc, _ = today_pst_bounds_utc()
    today_pst = start_utc.replace(tzinfo=timezone.utc).astimezone(PACIFIC).date()
    print(f"\nBackfill for {today_pst} PST")
    print(f"Mode: {'DRY RUN (add --write to actually save)' if DRY_RUN else 'WRITE'}")
    print(f"HubSpot enrichment: {'yes' if RUN_HUBSPOT else 'no (add --hubspot to enable)'}")
    print()

    print("Fetching calls from Aircall API...")
    raw_calls = await fetch_todays_calls()
    print(f"\nTotal calls returned by Aircall: {len(raw_calls)}")

    # Check which aircall_call_ids already exist
    existing_ids = {
        row[0] for row in
        db.query(CallEvent.aircall_call_id)
        .filter(CallEvent.aircall_call_id.isnot(None))
        .all()
    }

    to_insert = []
    skipped = 0
    for call in raw_calls:
        mapped = map_aircall_call(call)
        if mapped is None:
            continue
        if mapped["aircall_call_id"] in existing_ids:
            skipped += 1
            continue
        to_insert.append(mapped)

    print(f"Already in DB (skipping): {skipped}")
    print(f"New calls to import: {len(to_insert)}")

    if not to_insert:
        print("\nNothing to import.")
        return

    # Print a preview
    print("\nPreview (first 5):")
    for c in to_insert[:5]:
        tags_display = c['tags'] or '(no tags)'
        print(f"  [{c['direction']}] {c['sdr_name']} | {c['outcome']} | tags: {tags_display} | {c['started_at'].strftime('%H:%M:%S')}")

    if DRY_RUN:
        print(f"\nDry run complete. Run with --write to import {len(to_insert)} calls.")
        return

    # Run HubSpot lookups if requested
    if RUN_HUBSPOT:
        from hubspot_lookup import lookup_company
        print(f"\nRunning HubSpot lookups for {len(to_insert)} calls (this may take a minute)...")
        for i, c in enumerate(to_insert):
            result = await lookup_company(
                c["raw_digits"],
                customer_first_name=c.get("customer_first_name"),
                customer_last_name=c.get("customer_last_name"),
            )
            c["company_name"] = result["company_name"]
            c["state"] = result["state"]
            c["industry"] = result["industry"]
            c["hubspot_company_id"] = result.get("hubspot_company_id")
            # For inbound calls, store the resolved contact name
            if c["direction"] == "inbound":
                resolved = result.get("contact_name")
                if not resolved and c["raw_digits"]:
                    digits = "".join(d for d in c["raw_digits"] if d.isdigit())
                    if len(digits) == 11 and digits.startswith("1"):
                        digits = digits[1:]
                    resolved = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else c["raw_digits"]
                c["contact_name"] = resolved
            else:
                c["contact_name"] = None
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(to_insert)} lookups done...")
    else:
        for c in to_insert:
            c["company_name"] = "Unknown Company"
            c["state"] = None
            c["industry"] = None
            c["hubspot_company_id"] = None
            c["contact_name"] = None

    # Write to DB
    inserted = 0
    for c in to_insert:
        db.add(CallEvent(
            aircall_call_id=c["aircall_call_id"],
            sdr_name=c["sdr_name"],
            company_name=c["company_name"],
            state=c["state"],
            industry=c["industry"],
            hubspot_company_id=c.get("hubspot_company_id"),
            direction=c["direction"],
            contact_name=c.get("contact_name"),
            status=c["status"],
            outcome=c["outcome"],
            tags=c["tags"],
            is_active=False,
            started_at=c["started_at"],
            ended_at=c["ended_at"],
            talk_seconds=c["talk_seconds"],
        ))
        inserted += 1

    db.commit()
    db.close()
    print(f"\nDone. Imported {inserted} calls.")
    print("Check /api/dashboard to confirm they show up correctly.")


if __name__ == "__main__":
    asyncio.run(run())

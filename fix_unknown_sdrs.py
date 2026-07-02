"""
Fixes rows in Supabase where sdr_name = 'Unknown SDR' by:
  1. Re-fetching the full call record from Aircall (gets real SDR name,
     direction, and customer contact info)
  2. Running the HubSpot lookup to resolve company name, state, and
     contact name (for inbound calls)

Usage:
    python fix_unknown_sdrs.py            # dry run - shows what would change
    python fix_unknown_sdrs.py --write    # actually updates Supabase
"""
import asyncio
import os
import sys
import time
from base64 import b64encode

import httpx
from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal
from models import CallEvent
from hubspot_lookup import lookup_company

DRY_RUN = "--write" not in sys.argv
AIRCALL_BASE = "https://api.aircall.io/v1"


def aircall_auth_header() -> str:
    api_id = os.environ.get("AIRCALL_API_ID", "").strip()
    api_token = os.environ.get("AIRCALL_API_TOKEN", "").strip()
    if not api_id or not api_token:
        raise SystemExit("AIRCALL_API_ID and AIRCALL_API_TOKEN must be set in .env")
    creds = b64encode(f"{api_id}:{api_token}".encode()).decode()
    return f"Basic {creds}"


async def fetch_call_from_aircall(
    client: httpx.AsyncClient, aircall_call_id: str
) -> dict:
    """Fetch the full call record from Aircall by ID."""
    try:
        resp = await client.get(
            f"{AIRCALL_BASE}/calls/{aircall_call_id}",
            headers={"Authorization": aircall_auth_header()},
            timeout=8.0,
        )
        if resp.status_code != 200:
            print(f"  Aircall HTTP {resp.status_code} for call {aircall_call_id}")
            return {}
        return resp.json().get("call", {})
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        print(f"  Aircall fetch failed: {exc}")
        return {}


async def run():
    db = SessionLocal()

    unknown_rows = (
        db.query(CallEvent)
        .filter(
            CallEvent.aircall_call_id.isnot(None),
            CallEvent.sdr_name == "Unknown SDR",
        )
        .all()
    )

    print(f"\nFix Unknown SDRs")
    print(f"Mode: {'DRY RUN (add --write to save)' if DRY_RUN else 'WRITE'}")
    print(f"Rows to fix: {len(unknown_rows)}")

    if not unknown_rows:
        print("\nNo Unknown SDR rows found — nothing to fix.")
        return

    hubspot_token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
    if not hubspot_token:
        print("\nWARNING: HUBSPOT_PRIVATE_APP_TOKEN not set — will fix SDR names")
        print("and direction from Aircall, but cannot resolve company/contact.\n")

    fixed = 0
    failed = 0

    async with httpx.AsyncClient() as client:
        for i, row in enumerate(unknown_rows):
            print(f"\n[{i+1}/{len(unknown_rows)}] Call {row.aircall_call_id}...")

            # Step 1: fetch from Aircall
            call = await fetch_call_from_aircall(client, row.aircall_call_id)
            if not call:
                print(f"  ✗ Could not fetch from Aircall")
                failed += 1
                time.sleep(0.3)
                continue

            # Extract fields from Aircall record
            user = call.get("user") or {}
            real_sdr_name = user.get("name") or "Unknown SDR"
            direction = call.get("direction") or "outbound"
            raw_digits = call.get("raw_digits") or call.get("number") or ""

            contact = call.get("contact") or {}
            first_name = (contact.get("first_name") or "").strip() or None
            last_name  = (contact.get("last_name") or "").strip() or None

            print(f"  SDR: {real_sdr_name} | direction: {direction} | phone: {raw_digits}")

            # Step 2: HubSpot lookup
            company_name = row.company_name
            state = row.state
            industry = row.industry
            hubspot_company_id = row.hubspot_company_id
            contact_name = row.contact_name

            if hubspot_token and raw_digits:
                result = await lookup_company(
                    raw_digits,
                    customer_first_name=first_name,
                    customer_last_name=last_name,
                )
                company_name = result["company_name"]
                state = result["state"]
                industry = result["industry"]
                hubspot_company_id = result.get("hubspot_company_id")
                if direction == "inbound":
                    resolved = result.get("contact_name")
                    if not resolved and raw_digits:
                        digits = "".join(d for d in raw_digits if d.isdigit())
                        if len(digits) == 11 and digits.startswith("1"):
                            digits = digits[1:]
                        resolved = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else raw_digits
                    contact_name = resolved

            print(f"  company: {company_name} | state: {state} | contact: {contact_name}")

            if not DRY_RUN:
                row.sdr_name = real_sdr_name
                row.direction = direction
                row.company_name = company_name
                row.state = state
                row.industry = industry
                row.hubspot_company_id = hubspot_company_id
                row.contact_name = contact_name

            fixed += 1
            time.sleep(0.3)  # gentle rate limiting

    if not DRY_RUN:
        db.commit()

    db.close()

    print(f"\n{'--- DRY RUN ---' if DRY_RUN else '--- DONE ---'}")
    print(f"  Fixed: {fixed}")
    print(f"  Failed (Aircall unreachable): {failed}")
    if DRY_RUN and fixed > 0:
        print(f"\n  Run with --write to apply {fixed} updates.")


if __name__ == "__main__":
    asyncio.run(run())

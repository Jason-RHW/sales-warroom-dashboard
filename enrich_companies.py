"""
Enriches existing real calls that still show "Unknown Company" by:
  1. Fetching each call's phone number from the Aircall API
  2. Running the HubSpot lookup to resolve company name, state, industry
  3. Updating the row in the database

Safe to run multiple times - skips rows that already have a real company name.

Usage:
    python enrich_companies.py          # dry run, shows what would change
    python enrich_companies.py --write  # actually updates the database
"""
import asyncio
import os
import sys
import time
from base64 import b64encode

import httpx
from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal, init_db
from models import CallEvent

DRY_RUN = "--write" not in sys.argv
AIRCALL_BASE = "https://api.aircall.io/v1"


def aircall_auth_header() -> str:
    api_id = os.environ.get("AIRCALL_API_ID", "").strip()
    api_token = os.environ.get("AIRCALL_API_TOKEN", "").strip()
    if not api_id or not api_token:
        raise SystemExit("AIRCALL_API_ID and AIRCALL_API_TOKEN must be set in .env")
    creds = b64encode(f"{api_id}:{api_token}".encode()).decode()
    return f"Basic {creds}"


async def fetch_call_details(client: httpx.AsyncClient, aircall_call_id: str) -> dict:
    """Fetch full call object from Aircall — includes contact name if the
    number is saved as an Aircall contact, plus raw_digits."""
    try:
        resp = await client.get(
            f"{AIRCALL_BASE}/calls/{aircall_call_id}",
            headers={"Authorization": aircall_auth_header()},
            timeout=8.0,
        )
        if resp.status_code != 200:
            return {}
        return resp.json().get("call", {})
    except (httpx.HTTPError, httpx.TimeoutException):
        return {}


async def run():
    init_db()
    db = SessionLocal()

    # Find all real calls still showing Unknown Company
    unknown_rows = (
        db.query(CallEvent)
        .filter(
            CallEvent.aircall_call_id.isnot(None),
            CallEvent.company_name == "Unknown Company",
        )
        .all()
    )

    print(f"\nEnrich Companies")
    print(f"Mode: {'DRY RUN (add --write to save)' if DRY_RUN else 'WRITE'}")
    print(f"Calls to enrich: {len(unknown_rows)}")

    if not unknown_rows:
        print("\nNothing to enrich - all real calls already have company names.")
        return

    # First check HubSpot token
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
    if not token:
        raise SystemExit("\nHUBSPOT_PRIVATE_APP_TOKEN not set in .env - cannot run enrichment.")
    print(f"HubSpot token: {token[:12]}...{token[-4:]} (loaded OK)\n")

    resolved = 0
    failed = 0
    no_phone = 0

    from hubspot_lookup import lookup_company

    async with httpx.AsyncClient() as client:
        for i, row in enumerate(unknown_rows):
            print(f"[{i+1}/{len(unknown_rows)}] Call {row.aircall_call_id} ({row.sdr_name})...", end=" ")

            # Step 1: get full call details from Aircall (includes contact name + phone)
            call_data = await fetch_call_details(client, row.aircall_call_id)
            phone = call_data.get("raw_digits") or call_data.get("number")
            if not phone:
                print("no phone number found in Aircall - skipping")
                no_phone += 1
                time.sleep(0.3)
                continue

            # Extract customer name if Aircall has it
            contact = call_data.get("contact") or {}
            first_name = (contact.get("first_name") or "").strip() or None
            last_name = (contact.get("last_name") or "").strip() or None
            strategy = "A (name+phone)" if (first_name or last_name) else "B (phone only)"
            name_display = f"{first_name or ''} {last_name or ''}".strip() or "(no name)"
            print(f"phone={phone} name={name_display} strategy={strategy}...", end=" ")

            # Step 2: HubSpot lookup using whichever strategy applies
            result = await lookup_company(
                phone,
                customer_first_name=first_name,
                customer_last_name=last_name,
            )

            if result["company_name"] != "Unknown Company":
                print(f"✓ {result['company_name']} ({result['state']})")
                if not DRY_RUN:
                    row.company_name = result["company_name"]
                    row.state = result["state"]
                    row.industry = result["industry"]
                    row.hubspot_company_id = result.get("hubspot_company_id")
                resolved += 1
            else:
                state_display = result["state"] or "NULL"
                print(f"✗ no HubSpot match; area-code state={state_display}")
                if not DRY_RUN:
                    row.company_name = result["company_name"]
                    row.state = result["state"]
                    row.industry = result["industry"]
                    row.hubspot_company_id = result.get("hubspot_company_id")
                failed += 1

            if not DRY_RUN and (i + 1) % 10 == 0:
                db.commit()

            time.sleep(0.3)

    if not DRY_RUN:
        db.commit()

    db.close()

    print(f"\n{'--- DRY RUN SUMMARY ---' if DRY_RUN else '--- DONE ---'}")
    print(f"  Resolved:     {resolved}")
    print(f"  No HubSpot match: {failed}")
    print(f"  No phone found:   {no_phone}")

    if failed > 0:
        print(f"\n  {failed} calls had a phone number but no HubSpot match.")
        print("  Most likely cause: phone number format in HubSpot doesn't match")
        print("  what Aircall sends. Check a specific call above and compare")
        print("  the phone shown vs. how it's stored in your HubSpot company records.")

    if DRY_RUN and resolved > 0:
        print(f"\n  Run with --write to save {resolved} company resolutions.")


if __name__ == "__main__":
    asyncio.run(run())

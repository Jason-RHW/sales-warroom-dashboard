"""
Receiver for Aircall webhook events.

Events to enable on the Aircall webhook (Integration page -> Webhook ->
toggle these on, leave the rest off):
    call.created            -> dialing
    call.ringing_on_agent   -> ringing
    call.answered           -> connected, outcome = answered
    call.hungup             -> call just ended (fires immediately)
    call.ended              -> final call data ready (fires ~30s after hungup,
                               includes duration/recording)
    call.voicemail_left     -> voicemail, outcome = voicemail

THREE LAYERS OF CLOSING A CALL OUT, so a single dropped webhook can't strand
a call as "active" forever on the wallboard:
  1. call.hungup   - fast path, closes the call the instant it ends
  2. call.ended    - fallback closer, ~30s later, in case hungup was dropped
  3. aggregator.py's stale-call sweep - hard backstop after 20 minutes

HUBSPOT LOOKUP RUNS IN THE BACKGROUND, not inline in this handler. Aircall
times out webhook deliveries after 5 seconds and disables a webhook after
10 consecutive failures - a multi-step HubSpot lookup (Company search, then
possibly a Contact search + association + company fetch) is exactly the
"time-consuming task" Aircall's own docs say to offload. So: the row is
created/updated with a placeholder immediately, the response goes back to
Aircall right away, and the real company/state/industry get filled in a
moment later once the background task resolves.

IMPORTANT: `status` reflects the call's current/live lifecycle stage and
gets overwritten as the call progresses - it is NOT used for KPI counting.
`outcome` is set once (on answer or voicemail) and never overwritten by a
later hangup, which is what answered_calls/connect_rate actually count from.
"""
from fastapi import APIRouter, Request, BackgroundTasks
import os
from base64 import b64encode
from datetime import datetime, timezone
from typing import Optional
import httpx
from sqlalchemy.orm import Session

from database import SessionLocal
from models import CallEvent
from hubspot_lookup import lookup_company
from call_tags import serialize_tags

router = APIRouter()
AIRCALL_BASE = "https://api.aircall.io/v1"

# Aircall event names -> our internal status. Must match Aircall's actual
# webhook event names exactly (see developer.aircall.io/api-references).
# NOTE: also enable call.tagged in your Aircall webhook settings - it is
# handled separately below, not through this map.
AIRCALL_STATUS_MAP = {
    "call.created": "dialing",
    "call.ringing_on_agent": "ringing",
    "call.answered": "connected",
    "call.hungup": "ended",
    "call.voicemail_left": "voicemail",
}


def _format_phone_number(phone_number: str) -> str:
    digits = "".join(c for c in phone_number if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone_number


def _refresh_aircall_metadata(
    existing: CallEvent,
    sdr_name: Optional[str],
    direction: Optional[str],
) -> None:
    """Patch placeholder values when later Aircall events contain more data."""
    if sdr_name and (
        not existing.sdr_name or existing.sdr_name == "Unknown SDR"
    ):
        existing.sdr_name = sdr_name

    if not sdr_name and existing.sdr_name == "Unknown SDR":
        existing.direction = "inbound"
        return

    if direction and existing.direction != direction:
        # Never overwrite 'inbound' — once classified as inbound, it stays inbound.
        # Later Aircall events can have direction=None or wrong values.
        if existing.direction != "inbound":
            existing.direction = direction


def _aircall_auth_header() -> Optional[str]:
    api_id = os.environ.get("AIRCALL_API_ID", "").strip()
    api_token = os.environ.get("AIRCALL_API_TOKEN", "").strip()
    if not api_id or not api_token:
        return None
    creds = b64encode(f"{api_id}:{api_token}".encode()).decode()
    return f"Basic {creds}"


async def _fetch_call_from_aircall(aircall_call_id: str) -> dict:
    auth = _aircall_auth_header()
    if not auth:
        print("[webhook] AIRCALL_API_ID/AIRCALL_API_TOKEN missing; cannot refresh call")
        return {}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{AIRCALL_BASE}/calls/{aircall_call_id}",
                headers={"Authorization": auth},
                timeout=8.0,
            )
            if resp.status_code != 200:
                print(
                    f"[webhook] Aircall refresh HTTP {resp.status_code} "
                    f"for call {aircall_call_id}"
                )
                return {}
            return resp.json().get("call", {})
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        print(f"[webhook] Aircall refresh failed for call {aircall_call_id}: {exc}")
        return {}


async def _refresh_call_from_aircall_in_background(aircall_call_id: str) -> None:
    """Fetch canonical call details when webhook payloads are incomplete."""
    call = await _fetch_call_from_aircall(aircall_call_id)
    if not call:
        return

    user = call.get("user") or {}
    sdr_name = (user.get("name") or "").strip() or None
    raw_direction = (call.get("direction") or "").strip() or None
    direction = "inbound" if (not sdr_name or sdr_name == "Unknown SDR") else raw_direction
    phone_number = call.get("raw_digits") or call.get("number") or ""

    contact = call.get("contact") or {}
    customer_first_name = (contact.get("first_name") or "").strip() or None
    customer_last_name = (contact.get("last_name") or "").strip() or None

    company = None
    if phone_number:
        company = await lookup_company(
            phone_number,
            customer_first_name=customer_first_name,
            customer_last_name=customer_last_name,
        )

    db: Session = SessionLocal()
    try:
        row = (
            db.query(CallEvent)
            .filter(CallEvent.aircall_call_id == aircall_call_id)
            .first()
        )
        if not row:
            return

        _refresh_aircall_metadata(row, sdr_name, direction)

        if company:
            row.company_name = company["company_name"]
            row.state = company["state"]
            row.industry = company["industry"]
            row.hubspot_company_id = company.get("hubspot_company_id")

            if row.direction == "inbound":
                resolved = company.get("contact_name")
                if not resolved and phone_number:
                    resolved = _format_phone_number(phone_number)
                row.contact_name = resolved

        db.commit()
        print(
            f"[webhook] Aircall refreshed call_id={aircall_call_id} "
            f"sdr={row.sdr_name!r} direction={row.direction!r}"
        )
    finally:
        db.close()


def _close_call(existing: CallEvent, reason_event: str) -> None:
    """Mark a call no-longer-active and fill in ended_at/outcome if not
    already set. Safe to call from multiple events - only acts on fields
    that are still empty, so hungup and ended can both call this without
    overwriting whichever one got there first."""
    existing.is_active = False
    if existing.status not in ("voicemail",):
        existing.status = "ended"
    if existing.ended_at is None:
        existing.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if existing.started_at and existing.talk_seconds is None:
            existing.talk_seconds = (
                existing.ended_at - existing.started_at
            ).total_seconds()
    if existing.outcome is None:
        existing.outcome = "missed"


async def _enrich_company_in_background(
    aircall_call_id: str,
    phone_number: str,
    customer_first_name: str = None,
    customer_last_name: str = None,
    direction: str = "outbound",
) -> None:
    """Runs AFTER the webhook response has already been sent to Aircall.
    For outbound: resolves company name and state.
    For inbound: resolves the caller's contact name, company, and state."""
    company = await lookup_company(
        phone_number,
        customer_first_name=customer_first_name,
        customer_last_name=customer_last_name,
    )
    db: Session = SessionLocal()
    try:
        row = (
            db.query(CallEvent)
            .filter(CallEvent.aircall_call_id == aircall_call_id)
            .first()
        )
        if row:
            row.company_name = company["company_name"]
            row.state = company["state"]
            row.industry = company["industry"]
            row.hubspot_company_id = company.get("hubspot_company_id")

            # The first webhook for a call sometimes lacks direction. If a
            # later event fixed it before this task runs, trust the row.
            effective_direction = row.direction or direction

            # For inbound calls: use resolved contact name, or fall back to
            # the formatted phone number as the caller identifier
            if effective_direction == "inbound":
                resolved = company.get("contact_name")
                if not resolved and phone_number:
                    resolved = _format_phone_number(phone_number)
                row.contact_name = resolved
            db.commit()
    finally:
        db.close()


@router.post("/webhooks/aircall")
async def aircall_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    event_type = payload.get("event")  # e.g. "call.answered"
    data = payload.get("data", {})
    aircall_call_id = str(data.get("id"))

    db: Session = SessionLocal()
    try:
        existing = (
            db.query(CallEvent)
            .filter(CallEvent.aircall_call_id == aircall_call_id)
            .first()
        )

        user = data.get("user") or {}
        sdr_name = (user.get("name") or "").strip() or None
        phone_number = (data.get("raw_digits") or data.get("number") or "")

        # In this team's workflow, real outbound calls carry the SDR user in
        # the Aircall payload. If there is no SDR, classify it as inbound now
        # so it never pollutes outbound KPIs while enrichment catches up.
        direction = (data.get("direction") or "").strip() or None
        direction_for_insert = "inbound" if (not sdr_name or sdr_name == "Unknown SDR") else (direction or "outbound")
        print(
            f"[webhook] event={event_type} "
            f"direction_raw={data.get('direction')!r} "
            f"direction_used={direction_for_insert} "
            f"call_id={aircall_call_id}"
        )
        needs_aircall_refresh = not sdr_name or not direction

        if existing:
            _refresh_aircall_metadata(existing, sdr_name, direction)

        # call.ended arrives ~30s after call.hungup with the final duration.
        # Normally the call is already closed by hungup by this point, so
        # this just backfills the real talk time. But if hungup was dropped,
        # this is the fallback that closes the call out instead.
        if event_type == "call.ended":
            if existing:
                if data.get("duration") is not None:
                    existing.talk_seconds = data.get("duration")
                if existing.is_active:
                    _close_call(existing, "call.ended")
                db.commit()
                if needs_aircall_refresh:
                    background_tasks.add_task(
                        _refresh_call_from_aircall_in_background,
                        aircall_call_id,
                    )
            return {"received": True, "event": event_type}

        # call.tagged fires when an SDR tags a call (during or after the call).
        # Aircall sends the full current tag array each time, so we replace
        # whatever's stored with the latest complete set.
        if event_type == "call.tagged":
            if existing:
                raw_tags = data.get("tags") or []
                tag_names = [t.get("name", "").strip() for t in raw_tags if t.get("name")]
                existing.tags = serialize_tags(tag_names) if tag_names else None
                db.commit()
                if needs_aircall_refresh:
                    background_tasks.add_task(
                        _refresh_call_from_aircall_in_background,
                        aircall_call_id,
                    )
            return {"received": True, "event": event_type}

        internal_status = AIRCALL_STATUS_MAP.get(event_type)
        if internal_status is None:
            if existing:
                db.commit()
                if needs_aircall_refresh:
                    background_tasks.add_task(
                        _refresh_call_from_aircall_in_background,
                        aircall_call_id,
                    )
            return {"received": True, "ignored": True, "event": event_type}

        # Both inbound and outbound are now tracked.
        # Inbound calls are excluded from KPI/SDR counts in the aggregator
        # but shown in the live activity feed.

        # Customer name — only present if this number is saved as a contact
        # in Aircall's contact book. Null for cold/unknown numbers.
        contact = data.get("contact") or {}
        customer_first_name = (contact.get("first_name") or "").strip() or None
        customer_last_name = (contact.get("last_name") or "").strip() or None
        if customer_first_name or customer_last_name:
            print(f"[webhook] Contact name from Aircall: {customer_first_name} {customer_last_name} — will use Strategy A")
        else:
            print(f"[webhook] No contact name in payload — will use Strategy B (phone only)")

        is_active = internal_status not in ("ended", "voicemail")

        if existing:
            if event_type != "call.hungup":
                existing.status = internal_status
                existing.is_active = is_active

            if event_type == "call.answered" and existing.outcome is None:
                existing.outcome = "answered"
            elif event_type == "call.voicemail_left" and existing.outcome is None:
                existing.outcome = "voicemail"

            if event_type == "call.hungup":
                _close_call(existing, "call.hungup")
        else:
            # Insert with a placeholder immediately - never block the
            # response to Aircall on a HubSpot lookup. The background task
            # below fills in the real company/state/industry a moment later.
            db.add(CallEvent(
                aircall_call_id=aircall_call_id,
                sdr_name=sdr_name or "Unknown SDR",
                company_name="Unknown Company",
                state=None,
                industry=None,
                direction=direction_for_insert,
                status=internal_status,
                is_active=is_active,
                outcome="answered" if event_type == "call.answered" else (
                    "voicemail" if event_type == "call.voicemail_left" else None
                ),
                started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))
            db.commit()
            background_tasks.add_task(
                _enrich_company_in_background,
                aircall_call_id,
                phone_number,
                customer_first_name,
                customer_last_name,
                direction_for_insert,
            )
            background_tasks.add_task(
                _refresh_call_from_aircall_in_background,
                aircall_call_id,
            )
            return {"received": True, "event": event_type}

        db.commit()
        if needs_aircall_refresh:
            background_tasks.add_task(
                _refresh_call_from_aircall_in_background,
                aircall_call_id,
            )
    finally:
        db.close()

    return {"received": True, "event": event_type}

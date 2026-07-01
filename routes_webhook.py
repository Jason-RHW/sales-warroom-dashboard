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
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from database import SessionLocal
from models import CallEvent
from hubspot_lookup import lookup_company
from call_tags import serialize_tags

router = APIRouter()

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
) -> None:
    """Runs AFTER the webhook response has already been sent to Aircall.
    Uses name+phone (Strategy A) when name is available, phone-only (Strategy B) otherwise."""
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
            return {"received": True, "event": event_type}

        internal_status = AIRCALL_STATUS_MAP.get(event_type)
        if internal_status is None:
            return {"received": True, "ignored": True, "event": event_type}

        user = data.get("user") or {}
        sdr_name = user.get("name", "Unknown SDR")
        phone_number = (data.get("raw_digits") or data.get("number") or "")

        # Only track outbound calls — inbound calls are customer-initiated
        # and shouldn't appear on the SDR outreach dashboard
        direction = data.get("direction", "outbound")
        if direction == "inbound":
            return {"received": True, "ignored": True, "reason": "inbound call"}

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
                sdr_name=sdr_name,
                company_name="Unknown Company",
                state=None,   # set by background task after HubSpot/area code lookup
                industry=None,
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
            )
            return {"received": True, "event": event_type}

        db.commit()
    finally:
        db.close()

    return {"received": True, "event": event_type}

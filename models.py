"""
One table, intentionally. Every call event - dialing, ringing, connected,
voicemail, ended - is a row here. The dashboard never reads anything except
this table (via aggregator.py), so when the real Aircall webhook gets wired
up, nothing downstream needs to change.
"""
from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean
from sqlalchemy.sql import func
from database import Base


class CallEvent(Base):
    __tablename__ = "call_events"

    id = Column(Integer, primary_key=True, index=True)

    # Who / what
    sdr_name = Column(String, nullable=False, index=True)
    company_name = Column(String, nullable=False)
    state = Column(String(2), nullable=True, index=True)    # two-letter USPS code, or NULL when unresolved
    industry = Column(String, nullable=True)

    # Source linkage (filled in once Aircall/HubSpot are connected)
    aircall_call_id = Column(String, nullable=True, index=True)
    hubspot_company_id = Column(String, nullable=True)

    # Call lifecycle - the CURRENT/live stage, used for the map and feed.
    # This gets overwritten as a call progresses (dialing -> ringing ->
    # connected -> ended), which is exactly why it's NOT safe to use for
    # counting answered calls (see `outcome` below).
    status = Column(String, nullable=False, index=True)
    # one of: dialing | ringing | connected | voicemail | ended
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    # True while the call is still in progress (no terminal status yet)

    # What the call's RESULT was, set once and never overwritten by later
    # lifecycle events (e.g. a hangup after an answer doesn't erase that it
    # was answered). This is what KPIs/connect rate should count from.
    outcome = Column(String, nullable=True, index=True)
    # one of: answered | voicemail | missed | None (still in progress)

    direction = Column(String, nullable=True, default="outbound", index=True)
    # "outbound" (SDR dialing out) or "inbound" (customer calling in)
    # NULL treated as "outbound" for backward compat with existing rows.

    contact_name = Column(String, nullable=True)
    # For inbound calls: the caller's resolved name from HubSpot.
    # For outbound calls: NULL (company_name is used instead).

    # Tags applied by the SDR after the call (via call.tagged webhook event).
    tags = Column(String, nullable=True)

    started_at = Column(DateTime(), nullable=False, index=True)  # naive, always UTC
    ended_at = Column(DateTime(), nullable=True)                 # naive, always UTC
    talk_seconds = Column(Float, nullable=True)  # set once a connected call ends

    created_at = Column(DateTime(), server_default=func.now())

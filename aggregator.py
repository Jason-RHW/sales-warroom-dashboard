"""
All the dashboard's numbers are derived here, from raw rows in call_events.
Nothing is pre-computed or cached in a separate table - at this call volume
(a few hundred rows/day) querying live is simpler and can never drift out of
sync with the source data.

IMPORTANT: KPI/connect-rate counting uses `outcome`, not `status`. `status`
is the call's current/live lifecycle stage and gets overwritten as a call
progresses (e.g. connected -> ended on hangup) - using it for "was this call
answered" would silently undercount every call the moment it finishes.
`outcome` is set once by the webhook handler and never overwritten.
"""
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from models import CallEvent
from timezone_utils import today_pst_bounds_utc, PACIFIC
from call_tags import is_answered, is_sample

# Safety net: if a call's hangup event never arrives (dropped webhook,
# Two stale-call thresholds:
# - dialing/ringing: 1 minute — if it hasn't connected or hung up in 60s,
#   the webhook chain almost certainly broke (unanswered calls typically
#   resolve in under 30s)
# - connected: 20 minutes — generous for even a long discovery call
STALE_DIALING_MINUTES = 1
STALE_ACTIVE_CALL_MINUTES = 20


def _auto_heal_stale_calls(db: Session) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Short threshold: dialing or ringing calls older than 1 minute
    dialing_cutoff = now - timedelta(minutes=STALE_DIALING_MINUTES)
    stale_dialing = (
        db.query(CallEvent)
        .filter(
            CallEvent.is_active == True,  # noqa: E712
            CallEvent.status.in_(["dialing", "ringing"]),
            CallEvent.started_at < dialing_cutoff,
        )
        .all()
    )

    # Long threshold: connected calls older than 20 minutes
    connected_cutoff = now - timedelta(minutes=STALE_ACTIVE_CALL_MINUTES)
    stale_connected = (
        db.query(CallEvent)
        .filter(
            CallEvent.is_active == True,  # noqa: E712
            CallEvent.status == "connected",
            CallEvent.started_at < connected_cutoff,
        )
        .all()
    )

    stale = stale_dialing + stale_connected
    if not stale:
        return

    for r in stale:
        r.is_active = False
        r.status = "ended"
        if r.ended_at is None:
            r.ended_at = now
        if r.outcome is None:
            r.outcome = "missed"
    db.commit()


def build_dashboard_payload(db: Session) -> dict:
    _auto_heal_stale_calls(db)

    start_utc, end_utc = today_pst_bounds_utc()

    rows = (
        db.query(CallEvent)
        .filter(CallEvent.started_at >= start_utc, CallEvent.started_at <= end_utc)
        .order_by(CallEvent.started_at.desc())
        .all()
    )

    calls_today = len(rows)
    active_rows = [r for r in rows if r.is_active]
    answered_rows = [r for r in rows if is_answered(r.tags)]
    voicemail_rows = [r for r in rows if r.outcome == "voicemail"]
    sample_rows = [r for r in rows if is_sample(r.tags)]

    connect_rate = round((len(answered_rows) / calls_today) * 100, 1) if calls_today else 0.0
    talk_seconds = [r.talk_seconds for r in answered_rows if r.talk_seconds]
    avg_talk_seconds = round(sum(talk_seconds) / len(talk_seconds)) if talk_seconds else 0

    active_states = {r.state for r in active_rows}

    # ---- state tiles ----
    state_map: dict[str, dict] = {}
    state_totals: dict[str, int] = {}   # total calls today for ALL states (active + inactive)
    by_state_all = defaultdict(list)
    for r in rows:
        by_state_all[r.state].append(r)

    for state, state_rows in by_state_all.items():
        active_in_state = [r for r in state_rows if r.is_active]
        state_totals[state] = len(state_rows)

        if not active_in_state:
            continue  # only active states go in state_map (used for green glow + badge)
        answered_in_state = [r for r in state_rows if is_answered(r.tags)]
        connect = round((len(answered_in_state) / len(state_rows)) * 100) if state_rows else 0
        # Unique SDR names currently active in this state (for future use)
        active_sdrs = list({r.sdr_name for r in active_in_state})
        state_map[state] = {
            "live": len(active_in_state),
            "total_today": len(state_rows),
            "connect_rate": connect,
            "active_sdrs": active_sdrs,
        }

    # ---- live activity feed ----
    active_sorted = sorted(active_rows, key=lambda r: r.started_at, reverse=True)
    recent_ended = [r for r in rows if not r.is_active]
    recent_ended.sort(key=lambda r: r.started_at, reverse=True)

    feed = []
    for r in (active_sorted + recent_ended)[:8]:
        ref_time = r.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)
        elapsed = max(0, int((ref_time - r.started_at).total_seconds()))
        # Format start time as HH:MM AM/PM in PST, stripping leading zero
        dt_pst = r.started_at.replace(tzinfo=timezone.utc).astimezone(PACIFIC)
        hour = dt_pst.strftime("%I").lstrip("0") or "12"
        started_at_time = f"{hour}:{dt_pst.strftime('%M')} {dt_pst.strftime('%p')}"
        feed.append({
            "sdr": r.sdr_name,
            "company": r.company_name,
            "state": r.state,
            "status": r.status,
            "timer_seconds": elapsed,
            "started_at_time": started_at_time,
        })

    # ---- SDR leaderboard, ranked by total calls ----
    by_sdr = defaultdict(list)
    for r in rows:
        by_sdr[r.sdr_name].append(r)

    sdr_table = []
    for name, sdr_rows in by_sdr.items():
        answered = [r for r in sdr_rows if is_answered(r.tags)]
        samples = [r for r in sdr_rows if is_sample(r.tags)]
        rate = round((len(answered) / len(sdr_rows)) * 100) if sdr_rows else 0
        sdr_table.append({
            "name": name,
            "calls": len(sdr_rows),
            "connect_rate": rate,
            "samples": len(samples),
        })
    sdr_table.sort(key=lambda s: s["calls"], reverse=True)
    sdr_table = sdr_table[:6]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": {
            "active_calls_now": len(active_rows),
            "active_states_count": len(active_states),
            "calls_today": calls_today,
            "answered_calls": len(answered_rows),
            "voicemails": len(voicemail_rows),
            "connect_rate": connect_rate,
            "avg_talk_seconds": avg_talk_seconds,
            "samples_today": len(sample_rows),
        },
        "states": state_map,
        "state_totals": state_totals,
        "feed": feed,
        "sdr_table": sdr_table,
    }

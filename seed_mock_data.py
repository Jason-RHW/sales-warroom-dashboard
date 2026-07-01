"""
Populates call_events with a realistic day's worth of activity, scoped to
'today in PST' so it shows up correctly on the dashboard right away.

This is what you run instead of having a live Aircall feed. When the real
webhook is connected later, this script becomes optional - useful for demos
or testing, but no longer the primary data source.

SAFE BY DESIGN: this script only ever deletes/regenerates rows it created
itself - identifiable because mock rows always have aircall_call_id = NULL,
while every real webhook-sourced row always has one set. Real call data is
never touched, no matter how many times this is run.

Usage:
    python seed_mock_data.py            # wipes today's MOCK rows and reseeds
    python seed_mock_data.py --keep     # adds to whatever mock data exists
"""
import sys
import random
from datetime import timedelta, timezone

from database import SessionLocal, init_db
from models import CallEvent
from timezone_utils import now_pst
from call_tags import serialize_tags, ANSWERED_TAGS, SAMPLE_TAG

random.seed(42)

SDRS = ["Jason", "Amy", "John", "Grace", "Maria", "Basilio", "Henry", "Lhoreto"]

# (company, state, industry) - same accounts used in the dashboard's original
# mock data, so the visual stays consistent
ACCOUNTS = [
    ("ABC Medical", "CA", "Healthcare"),
    ("Pacific Crest Dental", "CA", "Healthcare"),
    ("Golden Gate Foods", "CA", "Food & Beverage"),
    ("Dallas Auto Supply", "TX", "Automotive"),
    ("Lone Star Freight Co", "TX", "Logistics"),
    ("Midwest Logistics", "IL", "Logistics"),
    ("Lakeshore Manufacturing", "IL", "Manufacturing"),
    ("Empire Builders Co", "NY", "Construction"),
    ("Coastal Seafood Distributors", "FL", "Food & Beverage"),
    ("Rocky Mountain Medical Supply", "CO", "Healthcare"),
    ("Sunbelt Restaurant Group", "GA", "Food Service"),
    ("Steel City Fabrication", "OH", "Manufacturing"),
    ("Cascade Health Partners", "WA", "Healthcare"),
]

# Tag weights for calls that actually get answered (outcome = answered)
ANSWERED_TAG_CHOICES = [
    "Spoke with Contact",
    "Interested New Lead",
    "Not Interested",
    "Reception/Gatekeeper",
    "Customer hang up",
    "Callback/Follow up",
    "Send Sample",
    "DNC",
]
ANSWERED_TAG_WEIGHTS = [0.25, 0.12, 0.20, 0.18, 0.08, 0.08, 0.06, 0.03]


def seed(keep_existing: bool = False):
    init_db()
    db = SessionLocal()
    try:
        day_start_pst = now_pst().replace(hour=7, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_pst.astimezone(timezone.utc).replace(tzinfo=None)

        if not keep_existing:
            # Only clear MOCK rows from today (aircall_call_id is NULL) -
            # real webhook-sourced rows always have that field set, and are
            # never touched by this script, no matter how many times it runs.
            mock_rows_today = db.query(CallEvent).filter(
                CallEvent.started_at >= day_start_utc,
                CallEvent.aircall_call_id.is_(None),
            ).all()
            for r in mock_rows_today:
                db.delete(r)
            db.commit()

        now = now_pst()
        total_calls = random.randint(180, 260)

        for _ in range(total_calls):
            sdr = random.choice(SDRS)
            company, state, industry = random.choice(ACCOUNTS)

            minutes_ago = random.randint(0, max(1, int((now - day_start_pst).total_seconds() / 60)))
            started = now - timedelta(minutes=minutes_ago)

            result = random.choices(
                ["answered", "voicemail", "missed"],
                weights=[0.40, 0.45, 0.15],
            )[0]
            talk_seconds = random.randint(45, 540) if result == "answered" else None
            ended = started + timedelta(seconds=talk_seconds) if talk_seconds else started + timedelta(seconds=12)

            status = "voicemail" if result == "voicemail" else "ended"

            # Only answered calls get tags - voicemail/missed are never tagged
            # by SDRs. ~85% of answered calls get tagged (realistic tagging discipline).
            tags = None
            if result == "answered" and random.random() < 0.85:
                tag = random.choices(ANSWERED_TAG_CHOICES, weights=ANSWERED_TAG_WEIGHTS)[0]
                tags = serialize_tags([tag])

            db.add(CallEvent(
                sdr_name=sdr,
                company_name=company,
                state=state,
                industry=industry,
                status=status,
                outcome=result,
                tags=tags,
                is_active=False,
                started_at=started.astimezone(timezone.utc).replace(tzinfo=None),
                ended_at=ended.astimezone(timezone.utc).replace(tzinfo=None),
                talk_seconds=talk_seconds,
                # aircall_call_id left NULL - this is what marks it as mock data
            ))

        # a handful of calls still in progress right now
        live_statuses = ["connected", "ringing", "dialing"]
        for _ in range(random.randint(10, 14)):
            sdr = random.choice(SDRS)
            company, state, industry = random.choice(ACCOUNTS)
            seconds_ago = random.randint(5, 280)
            started = now - timedelta(seconds=seconds_ago)
            live_status = random.choice(live_statuses)
            # Connected live calls might already have a tag if the SDR
            # tagged mid-call - small chance to keep it realistic
            live_tags = None
            if live_status == "connected" and random.random() < 0.3:
                tag = random.choices(ANSWERED_TAG_CHOICES, weights=ANSWERED_TAG_WEIGHTS)[0]
                live_tags = serialize_tags([tag])
            db.add(CallEvent(
                sdr_name=sdr,
                company_name=company,
                state=state,
                industry=industry,
                status=live_status,
                outcome="answered" if live_status == "connected" else None,
                tags=live_tags,
                is_active=True,
                started_at=started.astimezone(timezone.utc).replace(tzinfo=None),
                # aircall_call_id left NULL - this is what marks it as mock data
            ))

        db.commit()
        total = db.query(CallEvent).count()
        real = db.query(CallEvent).filter(CallEvent.aircall_call_id.isnot(None)).count()
        mock = total - real
        print(f"Seeded {total_calls} completed + active mock calls.")
        print(f"DB now has {total} total rows ({mock} mock, {real} real from Aircall).")
    finally:
        db.close()


if __name__ == "__main__":
    seed(keep_existing="--keep" in sys.argv)

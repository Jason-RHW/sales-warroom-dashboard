"""
This is the piece that makes the "resets at midnight PST" requirement work
without a cron job that deletes anything.

Instead of zeroing data out at midnight, every dashboard query is scoped to
"today in America/Los_Angeles". When the PST calendar date rolls over, the
window naturally shifts and yesterday's rows fall outside it - the dashboard
reads zero without anything being deleted. All historical rows stay in the
database for later reporting.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def today_pst_bounds_utc() -> tuple[datetime, datetime]:
    """
    Returns (start_utc, end_utc) for the current calendar day in
    America/Los_Angeles, converted to UTC. These come back as naive
    datetimes (tzinfo stripped) because every timestamp in the database is
    stored as naive UTC - this keeps comparisons correct on both SQLite
    (which discards tzinfo on storage) and Postgres/Supabase.
    """
    now_pst_dt = datetime.now(PACIFIC)
    start_pst = datetime.combine(now_pst_dt.date(), time.min, tzinfo=PACIFIC)
    end_pst = datetime.combine(now_pst_dt.date(), time.max, tzinfo=PACIFIC)
    start_utc = start_pst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_pst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def now_pst() -> datetime:
    return datetime.now(PACIFIC)

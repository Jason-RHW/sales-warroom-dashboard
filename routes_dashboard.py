from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from aggregator import build_dashboard_payload

router = APIRouter()


@router.get("/api/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """
    Single endpoint the dashboard polls. Always scoped to 'today in
    America/Los_Angeles' - see timezone_utils.today_pst_bounds_utc().
    """
    return build_dashboard_payload(db)

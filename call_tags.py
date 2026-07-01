"""
Aircall tag names that drive KPI categorization.

These must exactly match the tag names configured in your Aircall account
(case-sensitive). If a tag is renamed in Aircall, update it here too.

Only calls with at least one ANSWERED_TAGS tag count toward "Answered Calls"
and "Connect Rate". This is intentionally stricter than the call.answered
webhook event, which fires for voicemail machine pickups too — here, a real
conversation only counts if the SDR confirms it by tagging the call.
"""

# Any of these tags on a call = it was a real answered conversation
ANSWERED_TAGS = {
    "Spoke with Contact",
    "Interested New Lead",
    "Not Interested",
    "Reception/Gatekeeper",
    "Customer hang up",
    "Callback/Follow up",
    "Send Sample",
    "DNC",
}

# This specific tag = a sample was sent
SAMPLE_TAG = "Send Sample"


def parse_tags(tags_str: str | None) -> set[str]:
    """Parse the comma-separated tags string stored in the DB back into a set."""
    if not tags_str:
        return set()
    return {t.strip() for t in tags_str.split(",") if t.strip()}


def serialize_tags(tags: list[str] | set[str]) -> str:
    """Serialize a list/set of tag names to the comma-separated format for DB storage."""
    return ",".join(sorted(tags))


def is_answered(tags_str: str | None) -> bool:
    return bool(parse_tags(tags_str) & ANSWERED_TAGS)


def is_sample(tags_str: str | None) -> bool:
    return SAMPLE_TAG in parse_tags(tags_str)

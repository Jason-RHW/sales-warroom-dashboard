"""
Resolves a call to a HubSpot company (name, state, industry).

TWO LOOKUP STRATEGIES depending on what Aircall provides:

  STRATEGY A — Name + phone (when Aircall knows the customer name):
    1. Search HubSpot Contacts by first_name + last_name
    2. Among results, prefer the one whose stored phone matches the call number
    3. Resolve to that contact's primary Company via the v4 associations API
    4. If name search finds nothing, fall back to Strategy B

  STRATEGY B — Phone only (when Aircall only has raw_digits):
    1. Search Companies directly by phone number
    2. If no company match, search Contacts by phone/mobilephone
    3. Resolve that contact to its primary Company via v4 associations API
    4. If still nothing, return the "Unknown Company" placeholder

PHONE FORMAT RISK: HubSpot search uses exact-match (EQ) only. If a stored
number format doesn't match any of the variants we generate, the lookup
will quietly return "Unknown Company". If match rates are low in practice,
check what format numbers are actually stored in HubSpot and add that
variant to _phone_variants().

PERFORMANCE: called from a FastAPI BackgroundTask AFTER the webhook has
already responded to Aircall. Never inline in the request path.
"""
import os
import httpx

HUBSPOT_BASE = "https://api.hubapi.com"
REQUEST_TIMEOUT_SECONDS = 4.0

# In-memory cache keyed by digits-only phone number. Resets on restart.
_phone_lookup_cache: dict[str, dict] = {}

PLACEHOLDER = {"company_name": "Unknown Company", "state": "CA", "industry": None, "hubspot_company_id": None}

US_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
}
VALID_STATE_CODES = set(US_STATE_NAME_TO_ABBR.values())


def _normalize_state(raw_state: str | None) -> str:
    if not raw_state:
        return "CA"
    raw_state = raw_state.strip()
    if len(raw_state) == 2 and raw_state.upper() in VALID_STATE_CODES:
        return raw_state.upper()
    mapped = US_STATE_NAME_TO_ABBR.get(raw_state.lower())
    if mapped:
        return mapped
    print(f"[hubspot_lookup] Unrecognized state value '{raw_state}' - defaulting to CA")
    return "CA"


def _humanize_industry(raw_industry: str | None) -> str | None:
    if not raw_industry:
        return None
    return raw_industry.replace("_", " ").title()


def _phone_variants(raw_digits: str) -> list[str]:
    digits = "".join(c for c in (raw_digits or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return [raw_digits] if raw_digits else []
    area, mid, last = digits[:3], digits[3:6], digits[6:]
    return [
        raw_digits,
        digits,
        f"+1{digits}",
        f"1{digits}",
        f"({area}) {mid}-{last}",
        f"{area}-{mid}-{last}",
    ]


def _digits_only(phone: str | None) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


async def _hubspot_search(
    client: httpx.AsyncClient, token: str, object_type: str, filter_groups: list,
    properties: list = None,
) -> list:
    props = properties or ["name", "state", "industry", "phone", "mobilephone", "firstname", "lastname"]
    try:
        resp = await client.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"filterGroups": filter_groups, "limit": 5, "properties": props},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("results", [])
    except (httpx.HTTPError, httpx.TimeoutException):
        return []


async def _get_associated_company_id(client: httpx.AsyncClient, token: str, contact_id: str):
    try:
        resp = await client.get(
            f"{HUBSPOT_BASE}/crm/v4/objects/contacts/{contact_id}/associations/companies",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        return str(results[0]["toObjectId"]) if results else None
    except (httpx.HTTPError, httpx.TimeoutException, KeyError, IndexError):
        return None


async def _get_company_by_id(client: httpx.AsyncClient, token: str, company_id: str):
    try:
        resp = await client.get(
            f"{HUBSPOT_BASE}/crm/v3/objects/companies/{company_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"properties": "name,state,industry"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


async def _resolve_contact_to_company(client: httpx.AsyncClient, token: str, contact: dict):
    contact_id = contact.get("id")
    if not contact_id:
        return None
    company_id = await _get_associated_company_id(client, token, contact_id)
    if not company_id:
        return None
    company = await _get_company_by_id(client, token, company_id)
    if not company:
        return None
    props = company.get("properties", {})
    return {
        "company_name": props.get("name") or "Unknown Company",
        "state": _normalize_state(props.get("state")),
        "industry": _humanize_industry(props.get("industry")),
        "hubspot_company_id": company_id,
    }


async def _strategy_a_name_plus_phone(
    client: httpx.AsyncClient, token: str,
    first_name: str, last_name: str, phone_number: str,
):
    """
    Search contacts by name. If multiple results, prefer the one whose
    phone matches the call number. Then resolve to primary company.
    """
    filters = []
    if first_name:
        filters.append({"propertyName": "firstname", "operator": "EQ", "value": first_name})
    if last_name:
        filters.append({"propertyName": "lastname", "operator": "EQ", "value": last_name})
    if not filters:
        return None

    hits = await _hubspot_search(
        client, token, "contacts",
        [{"filters": filters}],
        properties=["firstname", "lastname", "phone", "mobilephone"],
    )
    if not hits:
        return None

    # Prefer the hit whose phone number matches the call number
    call_digits = _digits_only(phone_number)
    # Strip leading country code for comparison
    if len(call_digits) == 11 and call_digits.startswith("1"):
        call_digits = call_digits[1:]

    best = hits[0]
    if call_digits and len(hits) > 1:
        for hit in hits:
            props = hit.get("properties", {})
            stored = _digits_only(props.get("phone") or props.get("mobilephone") or "")
            if len(stored) == 11 and stored.startswith("1"):
                stored = stored[1:]
            if stored and stored == call_digits:
                best = hit
                break

    return await _resolve_contact_to_company(client, token, best)


async def _strategy_b_phone_only(client: httpx.AsyncClient, token: str, phone_number: str):
    """
    Search companies then contacts by phone number variants.
    """
    variants = _phone_variants(phone_number)
    if not variants:
        return None

    # Companies by phone
    for variant in variants:
        hits = await _hubspot_search(
            client, token, "companies",
            [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": variant}]}],
            properties=["name", "state", "industry"],
        )
        if hits:
            props = hits[0].get("properties", {})
            return {
                "company_name": props.get("name") or "Unknown Company",
                "state": _normalize_state(props.get("state")),
                "industry": _humanize_industry(props.get("industry")),
                "hubspot_company_id": hits[0].get("id"),
            }

    # Contacts by phone -> Company
    for variant in variants:
        hits = await _hubspot_search(
            client, token, "contacts",
            [
                {"filters": [{"propertyName": "phone", "operator": "EQ", "value": variant}]},
                {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": variant}]},
            ],
            properties=["firstname", "lastname", "phone", "mobilephone"],
        )
        if hits:
            result = await _resolve_contact_to_company(client, token, hits[0])
            if result:
                return result
            break

    return None


async def lookup_company(
    phone_number: str,
    customer_first_name: str = None,
    customer_last_name: str = None,
) -> dict:
    """
    Main entry point.
    Uses Strategy A (name+phone) when customer name is known from Aircall.
    Uses Strategy B (phone only) otherwise, or as fallback if A finds nothing.
    """
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        return dict(PLACEHOLDER)

    cache_key = _digits_only(phone_number)
    if cache_key and cache_key in _phone_lookup_cache:
        return _phone_lookup_cache[cache_key]

    first_name = (customer_first_name or "").strip()
    last_name = (customer_last_name or "").strip()
    has_name = bool(first_name or last_name)

    result = None
    async with httpx.AsyncClient() as client:
        if has_name:
            result = await _strategy_a_name_plus_phone(
                client, token, first_name, last_name, phone_number
            )
            if result:
                print(f"[hubspot_lookup] Strategy A (name) → {result['company_name']}")
            else:
                print(f"[hubspot_lookup] Strategy A found nothing for '{first_name} {last_name}' — falling back to phone")

        if not result:
            result = await _strategy_b_phone_only(client, token, phone_number)
            if result:
                print(f"[hubspot_lookup] Strategy B (phone) → {result['company_name']}")
            else:
                print(f"[hubspot_lookup] No match found for {phone_number}")

    final = result or dict(PLACEHOLDER)
    if cache_key:
        _phone_lookup_cache[cache_key] = final
    return final


# Backward-compatible alias used by enrich_companies.py and backfill_today.py
async def lookup_company_by_phone(phone_number: str) -> dict:
    return await lookup_company(phone_number)

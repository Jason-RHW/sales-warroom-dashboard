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

PLACEHOLDER = {"company_name": "Unknown Company", "state": None, "industry": None, "hubspot_company_id": None}

# US area code → state mapping. Used as a fallback when HubSpot returns no
# match — much better than defaulting everything to CA. Not perfect (VoIP
# numbers can have misleading area codes, and some codes span state lines)
# but correct for the vast majority of real outbound calls.
AREA_CODE_TO_STATE: dict[str, str] = {
    "201":"NJ","202":"DC","203":"CT","204":"MB","205":"AL","206":"WA","207":"ME",
    "208":"ID","209":"CA","210":"TX","212":"NY","213":"CA","214":"TX","215":"PA",
    "216":"OH","217":"IL","218":"MN","219":"IN","220":"OH","223":"PA","224":"IL",
    "225":"LA","228":"MS","229":"GA","231":"MI","234":"OH","239":"FL","240":"MD",
    "248":"MI","251":"AL","252":"NC","253":"WA","254":"TX","256":"AL","260":"IN",
    "262":"WI","267":"PA","269":"MI","270":"KY","272":"PA","276":"VA","281":"TX",
    "301":"MD","302":"DE","303":"CO","304":"WV","305":"FL","307":"WY","308":"NE",
    "309":"IL","310":"CA","312":"IL","313":"MI","314":"MO","315":"NY","316":"KS",
    "317":"IN","318":"LA","319":"IA","320":"MN","321":"FL","323":"CA","325":"TX",
    "330":"OH","331":"IL","332":"NY","334":"AL","336":"NC","337":"LA","339":"MA",
    "340":"VI","341":"CA","346":"TX","347":"NY","351":"MA","352":"FL","360":"WA",
    "361":"TX","364":"KY","380":"OH","385":"UT","386":"FL","401":"RI","402":"NE",
    "404":"GA","405":"OK","406":"MT","407":"FL","408":"CA","409":"TX","410":"MD",
    "412":"PA","413":"MA","414":"WI","415":"CA","417":"MO","419":"OH","423":"TN",
    "424":"CA","425":"WA","430":"TX","432":"TX","434":"VA","435":"UT","440":"OH",
    "442":"CA","443":"MD","447":"IL","458":"OR","463":"IN","469":"TX","470":"GA",
    "472":"NC","475":"CT","478":"GA","479":"AR","480":"AZ","484":"PA","501":"AR",
    "502":"KY","503":"OR","504":"LA","505":"NM","507":"MN","508":"MA","509":"WA",
    "510":"CA","512":"TX","513":"OH","515":"IA","516":"NY","517":"MI","518":"NY",
    "520":"AZ","530":"CA","531":"NE","534":"WI","539":"OK","540":"VA","541":"OR",
    "551":"NJ","559":"CA","561":"FL","562":"CA","563":"IA","564":"WA","567":"OH",
    "570":"PA","571":"VA","573":"MO","574":"IN","575":"NM","580":"OK","585":"NY",
    "586":"MI","601":"MS","602":"AZ","603":"NH","605":"SD","606":"KY","607":"NY",
    "608":"WI","609":"NJ","610":"PA","612":"MN","614":"OH","615":"TN","616":"MI",
    "617":"MA","618":"IL","619":"CA","620":"KS","623":"AZ","626":"CA","628":"CA",
    "629":"TN","630":"IL","631":"NY","636":"MO","641":"IA","646":"NY","650":"CA",
    "651":"MN","657":"CA","659":"AL","660":"MO","661":"CA","662":"MS","667":"MD",
    "669":"CA","671":"GU","678":"GA","681":"WV","682":"TX","689":"FL","701":"ND",
    "702":"NV","703":"VA","704":"NC","706":"GA","707":"CA","708":"IL","712":"IA",
    "713":"TX","714":"CA","715":"WI","716":"NY","717":"PA","718":"NY","719":"CO",
    "720":"CO","724":"PA","725":"NV","726":"TX","727":"FL","730":"IL","731":"TN",
    "732":"NJ","734":"MI","737":"TX","740":"OH","743":"NC","747":"CA","754":"FL",
    "757":"VA","760":"CA","762":"GA","763":"MN","764":"CA","765":"IN","769":"MS",
    "770":"GA","771":"MD","772":"FL","773":"IL","774":"MA","775":"NV","779":"IL",
    "781":"MA","785":"KS","786":"FL","801":"UT","802":"VT","803":"SC","804":"VA",
    "805":"CA","806":"TX","808":"HI","810":"MI","812":"IN","813":"FL","814":"PA",
    "815":"IL","816":"MO","817":"TX","818":"CA","820":"CA","826":"VA","828":"NC",
    "830":"TX","831":"CA","832":"TX","835":"PA","838":"NY","839":"SC","840":"CA",
    "843":"SC","845":"NY","847":"IL","848":"NJ","850":"FL","854":"SC","856":"NJ",
    "857":"MA","858":"CA","859":"KY","860":"CT","861":"CA","862":"NJ","863":"FL",
    "864":"SC","865":"TN","870":"AR","872":"IL","878":"PA","901":"TN","903":"TX",
    "904":"FL","906":"MI","907":"AK","908":"NJ","909":"CA","910":"NC","912":"GA",
    "913":"KS","914":"NY","915":"TX","916":"CA","917":"NY","918":"OK","919":"NC",
    "920":"WI","925":"CA","928":"AZ","929":"NY","930":"IN","931":"TN","934":"NY",
    "936":"TX","937":"OH","938":"AL","940":"TX","941":"FL","945":"TX","947":"MI",
    "949":"CA","951":"CA","952":"MN","954":"FL","956":"TX","959":"CT","970":"CO",
    "971":"OR","972":"TX","973":"NJ","975":"MO","978":"MA","979":"TX","980":"NC",
    "983":"CO","984":"NC","985":"LA","989":"MI",
}


def _state_from_phone(phone_number: str) -> str | None:
    """Derive US state from area code as a last-resort fallback.
    Returns None if the area code is unrecognized — caller should handle
    this by not assigning any state rather than defaulting to CA."""
    digits = "".join(c for c in (phone_number or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        area_code = digits[:3]
        return AREA_CODE_TO_STATE.get(area_code)  # None if not found
    return None

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


def _normalize_state(raw_state: str | None) -> str | None:
    if not raw_state:
        return None
    raw_state = raw_state.strip()
    if len(raw_state) == 2 and raw_state.upper() in VALID_STATE_CODES:
        return raw_state.upper()
    mapped = US_STATE_NAME_TO_ABBR.get(raw_state.lower())
    if mapped:
        return mapped
    print(f"[hubspot_lookup] Unrecognized state value '{raw_state}' - leaving state unresolved")
    return None


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

    print(f"[strategy_a] Searching HubSpot contacts: firstname={first_name!r} lastname={last_name!r}")
    hits = await _hubspot_search(
        client, token, "contacts",
        [{"filters": filters}],
        properties=["firstname", "lastname", "phone", "mobilephone"],
    )
    print(f"[strategy_a] HubSpot returned {len(hits)} contact(s)")
    if not hits:
        return None

    # Prefer the hit whose phone number matches the call number
    call_digits = _digits_only(phone_number)
    # Strip leading country code for comparison
    if len(call_digits) == 11 and call_digits.startswith("1"):
        call_digits = call_digits[1:]

    best = hits[0]
    if call_digits:
        best = None
        for hit in hits:
            props = hit.get("properties", {})
            stored_numbers = [props.get("phone"), props.get("mobilephone")]
            for stored_number in stored_numbers:
                stored = _digits_only(stored_number or "")
                if len(stored) == 11 and stored.startswith("1"):
                    stored = stored[1:]
                if stored and stored == call_digits:
                    best = hit
                    break
            if best:
                break
        if not best:
            print("[strategy_a] Name match found, but phone did not match call number")
            return None

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
                print(f"[hubspot_lookup] Strategy A (name) → {result['company_name']} ({result['state']})")
            else:
                print(f"[hubspot_lookup] Strategy A found nothing for '{first_name} {last_name}' — falling back to phone")

        if not result:
            print(f"[hubspot_lookup] Strategy B — trying phone variants for {phone_number}")
            result = await _strategy_b_phone_only(client, token, phone_number)
            if result:
                print(f"[hubspot_lookup] Strategy B (phone) → {result['company_name']} ({result['state']})")
            else:
                state_guess = _state_from_phone(phone_number)
                print(f"[hubspot_lookup] No HubSpot match — area code state: {state_guess}")

    state_fallback = _state_from_phone(phone_number)  # None if area code unrecognized
    final = result if result else {
        "company_name": "Unknown Company",
        "state": state_fallback,
        "industry": None,
        "hubspot_company_id": None,
    }
    if cache_key:
        _phone_lookup_cache[cache_key] = final
    return final


# Backward-compatible alias used by enrich_companies.py and backfill_today.py
async def lookup_company_by_phone(phone_number: str) -> dict:
    return await lookup_company(phone_number)

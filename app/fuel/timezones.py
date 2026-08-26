"""US pickup-location to timezone resolution.

A pickup time on a load confirmation is local to the pickup city. Treating it as
UTC or as the VPS clock would put the fuel deadline hours out, so the timezone
has to be resolved before any arithmetic happens.

The rule is deliberately conservative, because the spec is explicit that a wrong
time is worse than no time:

* **Single-zone states** resolve outright.
* **States with a small, well-defined exception** (Texas is Central except two
  far-west counties) resolve to the dominant zone, with the exception cities
  listed. These are stable facts, not guesses.
* **Genuinely split states** (Tennessee, Kentucky, Indiana, the Dakotas, Idaho)
  resolve only if the city is recognised. Otherwise the caller gets ``None`` and
  the load is marked NEED TO CHECK rather than assigned a guessed zone.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"  # no daylight saving
PACIFIC = "America/Los_Angeles"
ALASKA = "America/Anchorage"
HAWAII = "Pacific/Honolulu"

#: States that sit entirely in one zone.
SINGLE_ZONE: dict[str, str] = {
    # Eastern
    "CT": EASTERN, "DE": EASTERN, "DC": EASTERN, "GA": EASTERN, "ME": EASTERN,
    "MD": EASTERN, "MA": EASTERN, "NH": EASTERN, "NJ": EASTERN, "NY": EASTERN,
    "NC": EASTERN, "OH": EASTERN, "PA": EASTERN, "RI": EASTERN, "SC": EASTERN,
    "VT": EASTERN, "VA": EASTERN, "WV": EASTERN,
    # Central
    "AL": CENTRAL, "AR": CENTRAL, "IL": CENTRAL, "IA": CENTRAL, "LA": CENTRAL,
    "MN": CENTRAL, "MS": CENTRAL, "MO": CENTRAL, "OK": CENTRAL, "WI": CENTRAL,
    # Mountain
    "CO": MOUNTAIN, "MT": MOUNTAIN, "NM": MOUNTAIN, "UT": MOUNTAIN, "WY": MOUNTAIN,
    "AZ": ARIZONA,
    # Pacific and beyond
    "CA": PACIFIC, "NV": PACIFIC, "WA": PACIFIC,
    "AK": ALASKA, "HI": HAWAII,
}

#: States that are overwhelmingly one zone, with a short exception list.
#: ``{state: (dominant zone, {city: zone})}``
DOMINANT_ZONE: dict[str, tuple[str, dict[str, str]]] = {
    # Central except El Paso and Hudspeth counties.
    "TX": (CENTRAL, {
        "el paso": MOUNTAIN, "fabens": MOUNTAIN, "tornillo": MOUNTAIN,
        "socorro": MOUNTAIN, "san elizario": MOUNTAIN, "clint": MOUNTAIN,
        "anthony": MOUNTAIN, "fort hancock": MOUNTAIN, "sierra blanca": MOUNTAIN,
        "dell city": MOUNTAIN,
    }),
    # Eastern except the western panhandle.
    "FL": (EASTERN, {
        "pensacola": CENTRAL, "milton": CENTRAL, "crestview": CENTRAL,
        "fort walton beach": CENTRAL, "destin": CENTRAL, "niceville": CENTRAL,
        "navarre": CENTRAL, "gulf breeze": CENTRAL, "defuniak springs": CENTRAL,
        "panama city": CENTRAL, "panama city beach": CENTRAL, "lynn haven": CENTRAL,
        "marianna": CENTRAL, "chipley": CENTRAL, "bonifay": CENTRAL,
    }),
    # Eastern except four Upper Peninsula counties bordering Wisconsin.
    "MI": (EASTERN, {
        "menominee": CENTRAL, "iron mountain": CENTRAL, "kingsford": CENTRAL,
        "ironwood": CENTRAL, "bessemer": CENTRAL, "wakefield": CENTRAL,
        "iron river": CENTRAL, "crystal falls": CENTRAL, "norway": CENTRAL,
    }),
    # Central except four far-western counties.
    "KS": (CENTRAL, {
        "goodland": MOUNTAIN, "sharon springs": MOUNTAIN, "tribune": MOUNTAIN,
        "syracuse": MOUNTAIN,
    }),
    # Central except the western panhandle.
    "NE": (CENTRAL, {
        "scottsbluff": MOUNTAIN, "gering": MOUNTAIN, "alliance": MOUNTAIN,
        "sidney": MOUNTAIN, "chadron": MOUNTAIN, "kimball": MOUNTAIN,
        "ogallala": MOUNTAIN,
    }),
    # Pacific except most of Malheur County.
    "OR": (PACIFIC, {
        "ontario": MOUNTAIN, "nyssa": MOUNTAIN, "vale": MOUNTAIN,
        "adrian": MOUNTAIN,
    }),
}

#: States genuinely split down the middle. Resolved only by known city.
SPLIT_STATE_CITIES: dict[str, dict[str, str]] = {
    "TN": {
        "nashville": CENTRAL, "memphis": CENTRAL, "clarksville": CENTRAL,
        "murfreesboro": CENTRAL, "franklin": CENTRAL, "jackson": CENTRAL,
        "columbia": CENTRAL, "lawrenceburg": CENTRAL, "lebanon": CENTRAL,
        "smyrna": CENTRAL, "gallatin": CENTRAL, "dickson": CENTRAL,
        "cookeville": CENTRAL, "shelbyville": CENTRAL, "tullahoma": CENTRAL,
        "knoxville": EASTERN, "chattanooga": EASTERN, "kingsport": EASTERN,
        "johnson city": EASTERN, "bristol": EASTERN, "cleveland": EASTERN,
        "morristown": EASTERN, "sevierville": EASTERN, "maryville": EASTERN,
        "oak ridge": EASTERN, "athens": EASTERN, "greeneville": EASTERN,
    },
    "KY": {
        "louisville": EASTERN, "lexington": EASTERN, "frankfort": EASTERN,
        "richmond": EASTERN, "georgetown": EASTERN, "florence": EASTERN,
        "covington": EASTERN, "nicholasville": EASTERN, "elizabethtown": EASTERN,
        "danville": EASTERN, "somerset": EASTERN, "london": EASTERN,
        "bowling green": CENTRAL, "owensboro": CENTRAL, "paducah": CENTRAL,
        "henderson": CENTRAL, "hopkinsville": CENTRAL, "madisonville": CENTRAL,
        "murray": CENTRAL, "central city": CENTRAL,
    },
    "IN": {
        "indianapolis": EASTERN, "fort wayne": EASTERN, "south bend": EASTERN,
        "bloomington": EASTERN, "carmel": EASTERN, "fishers": EASTERN,
        "muncie": EASTERN, "lafayette": EASTERN, "terre haute": EASTERN,
        "columbus": EASTERN, "elkhart": EASTERN, "kokomo": EASTERN,
        "anderson": EASTERN, "greenwood": EASTERN, "noblesville": EASTERN,
        "gary": CENTRAL, "hammond": CENTRAL, "east chicago": CENTRAL,
        "merrillville": CENTRAL, "michigan city": CENTRAL, "valparaiso": CENTRAL,
        "portage": CENTRAL, "crown point": CENTRAL, "schererville": CENTRAL,
        "evansville": CENTRAL, "jasper": CENTRAL, "vincennes": CENTRAL,
    },
    "ND": {
        "fargo": CENTRAL, "grand forks": CENTRAL, "bismarck": CENTRAL,
        "minot": CENTRAL, "jamestown": CENTRAL, "devils lake": CENTRAL,
        "wahpeton": CENTRAL, "valley city": CENTRAL,
        "dickinson": MOUNTAIN, "williston": MOUNTAIN, "beach": MOUNTAIN,
        "bowman": MOUNTAIN, "watford city": MOUNTAIN,
    },
    "SD": {
        "sioux falls": CENTRAL, "brookings": CENTRAL, "watertown": CENTRAL,
        "aberdeen": CENTRAL, "mitchell": CENTRAL, "yankton": CENTRAL,
        "huron": CENTRAL, "pierre": CENTRAL, "vermillion": CENTRAL,
        "rapid city": MOUNTAIN, "spearfish": MOUNTAIN, "sturgis": MOUNTAIN,
        "belle fourche": MOUNTAIN, "hot springs": MOUNTAIN, "custer": MOUNTAIN,
        "deadwood": MOUNTAIN, "lead": MOUNTAIN,
    },
    "ID": {
        "boise": MOUNTAIN, "meridian": MOUNTAIN, "nampa": MOUNTAIN,
        "idaho falls": MOUNTAIN, "pocatello": MOUNTAIN, "twin falls": MOUNTAIN,
        "caldwell": MOUNTAIN, "rexburg": MOUNTAIN, "burley": MOUNTAIN,
        "mountain home": MOUNTAIN, "blackfoot": MOUNTAIN,
        "coeur d'alene": PACIFIC, "coeur dalene": PACIFIC, "post falls": PACIFIC,
        "moscow": PACIFIC, "lewiston": PACIFIC, "sandpoint": PACIFIC,
        "hayden": PACIFIC, "rathdrum": PACIFIC,
    },
}

#: Why a lookup failed, for the log and for the NEED TO CHECK reason.
UNKNOWN_STATE = "unknown_state"
AMBIGUOUS_SPLIT_STATE = "ambiguous_split_state"


def resolve_timezone(state: str | None, city: str | None = None) -> tuple[str | None, str]:
    """Return ``(IANA timezone, reason)``.

    ``reason`` is the resolution path on success, or why it failed. The caller
    marks the load NEED TO CHECK whenever the timezone is ``None``.
    """
    code = (state or "").strip().upper()
    town = (city or "").strip().lower()

    if not code:
        return None, UNKNOWN_STATE

    if code in SINGLE_ZONE:
        return SINGLE_ZONE[code], "single_zone_state"

    if code in DOMINANT_ZONE:
        dominant, exceptions = DOMINANT_ZONE[code]
        if town in exceptions:
            return exceptions[town], "dominant_zone_exception"
        return dominant, "dominant_zone_state"

    if code in SPLIT_STATE_CITIES:
        cities = SPLIT_STATE_CITIES[code]
        if town in cities:
            return cities[town], "split_state_known_city"
        return None, AMBIGUOUS_SPLIT_STATE

    return None, UNKNOWN_STATE


def load_zone(name: str) -> ZoneInfo | None:
    """Load an IANA zone, or ``None`` if the container has no tz database."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None

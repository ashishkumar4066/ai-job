"""Country / region / timezone resolution for the eligibility filter.

The two aggregator boards describe candidate eligibility in different shapes,
both of them human-facing rather than machine-facing:

  * Himalayas -> `locationRestrictions: ["United States"]` (ISO country *names*,
    empty list means unrestricted) and `timezoneRestrictions: [-8, 5.5]`
    (numeric UTC offsets).
  * Remotive  -> `candidate_required_location: "Northern America, LATAM, Europe, APAC"`
    — one free-text string mixing countries, regions and timezone phrases.

Both are funnelled through here into a single vocabulary: ISO-3166-1 alpha-2
codes plus the sentinel `worldwide`.

Scope note: `REGION_MEMBERS` is deliberately *not* a complete gazetteer. It
lists the members that matter for deciding eligibility for this tool. Every
region containing India is enumerated carefully, because that is the membership
the filter actually keys off; other regions carry a representative list. Region
expansion is also recorded in the eligibility reasons, so an over-broad match
is always visible rather than silent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

WORLDWIDE = "worldwide"

# Phrases that mean "no location restriction at all".
_WORLDWIDE_TOKENS = {
    "worldwide", "world wide", "world-wide", "global", "globally", "international",
    "anywhere", "anywhere in the world", "any location", "any country", "everywhere",
    "remote worldwide", "remote - worldwide", "fully remote worldwide", "no restrictions",
    "unrestricted", "all locations", "all countries",
}

# ISO-3166-1 alpha-2 for the countries these boards actually emit, plus the
# aliases they emit them under.
_COUNTRY_CODES: dict[str, str] = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "argentina": "AR",
    "armenia": "AM", "australia": "AU", "austria": "AT", "azerbaijan": "AZ",
    "bahrain": "BH", "bangladesh": "BD", "belarus": "BY", "belgium": "BE",
    "bolivia": "BO", "bosnia and herzegovina": "BA", "brazil": "BR", "bulgaria": "BG",
    "cambodia": "KH", "cameroon": "CM", "canada": "CA", "chile": "CL",
    "china": "CN", "colombia": "CO", "costa rica": "CR", "croatia": "HR",
    "cyprus": "CY", "czechia": "CZ", "czech republic": "CZ", "denmark": "DK",
    "dominican republic": "DO", "ecuador": "EC", "egypt": "EG", "el salvador": "SV",
    "estonia": "EE", "ethiopia": "ET", "finland": "FI", "france": "FR",
    "georgia": "GE", "germany": "DE", "ghana": "GH", "greece": "GR",
    "guatemala": "GT", "honduras": "HN", "hong kong": "HK", "hungary": "HU",
    "iceland": "IS", "india": "IN", "indonesia": "ID", "iran": "IR",
    "iraq": "IQ", "ireland": "IE", "israel": "IL", "italy": "IT",
    "jamaica": "JM", "japan": "JP", "jordan": "JO", "kazakhstan": "KZ",
    "kenya": "KE", "kuwait": "KW", "latvia": "LV", "lebanon": "LB",
    "lithuania": "LT", "luxembourg": "LU", "malaysia": "MY", "malta": "MT",
    "mexico": "MX", "moldova": "MD", "morocco": "MA", "nepal": "NP",
    "netherlands": "NL", "new zealand": "NZ", "nigeria": "NG", "north macedonia": "MK",
    "norway": "NO", "oman": "OM", "pakistan": "PK", "panama": "PA",
    "paraguay": "PY", "peru": "PE", "philippines": "PH", "poland": "PL",
    "portugal": "PT", "qatar": "QA", "romania": "RO", "russia": "RU",
    "russian federation": "RU", "saudi arabia": "SA", "serbia": "RS",
    "singapore": "SG", "slovakia": "SK", "slovenia": "SI", "south africa": "ZA",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR", "spain": "ES",
    "sri lanka": "LK", "sweden": "SE", "switzerland": "CH", "taiwan": "TW",
    "tanzania": "TZ", "thailand": "TH", "tunisia": "TN", "turkey": "TR",
    "turkiye": "TR", "uganda": "UG", "ukraine": "UA",
    "united arab emirates": "AE", "uae": "AE",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
    "u.s.": "US", "u.s.a.": "US", "us": "US", "america": "US",
    "uruguay": "UY", "venezuela": "VE", "vietnam": "VN", "viet nam": "VN",
    "zimbabwe": "ZW",
}

_KNOWN_CODES = frozenset(_COUNTRY_CODES.values())

# City -> country, for the ATS boards that name a city without its country.
# Deliberately India-heavy: `IN` is the code the filter actually keys off, and
# live boards write "Remote, Bangalore" (GitLab) with no country anywhere in
# the string. Other cities are left unresolved rather than half-covered — an
# unresolved token is visible in the reasons, a wrong one is not.
_CITY_COUNTRIES: dict[str, str] = {
    "bangalore": "IN", "bengaluru": "IN", "mumbai": "IN", "bombay": "IN",
    "delhi": "IN", "new delhi": "IN", "gurugram": "IN", "gurgaon": "IN",
    "noida": "IN", "hyderabad": "IN", "chennai": "IN", "pune": "IN",
    "kolkata": "IN", "ahmedabad": "IN", "jaipur": "IN", "kochi": "IN",
    "chandigarh": "IN", "indore": "IN", "coimbatore": "IN", "thiruvananthapuram": "IN",
    # US metro shorthand. Present because Greenhouse packs several cities into
    # one `location.name` in abbreviated form — Stripe posts "SF, NYC, SEA,
    # CHI" — and without these the whole string resolves to nothing, so the
    # posting carries no candidate-location data at all. 613 open rows have a
    # location string with three or more commas.
    #
    # `sea` in particular USED to resolve to South-East Asia via a bare
    # three-letter region alias, so "SF, NYC, SEA, CHI" came back as
    # {ID, KH, MY, PH, SG, TH, VN} — a US-only multi-city role tagged as
    # ASEAN-eligible. Same failure as the lone-qualifier role patterns: a
    # short token is an abbreviation for whichever thing the writer meant, and
    # a US job board means the airport.
    "sf": "US", "sfo": "US", "nyc": "US", "sea": "US", "chi": "US",
    "la": "US", "lax": "US", "dc": "US", "atl": "US", "bos": "US",
    "pdx": "US", "phl": "US", "aus": "US", "den": "US",
}

# Region -> member countries. See the scope note in the module docstring.
_ASIA = frozenset({
    "IN", "CN", "JP", "KR", "SG", "MY", "TH", "VN", "PH", "ID", "HK", "TW",
    "PK", "BD", "LK", "NP", "KH", "AE", "SA", "IL", "TR", "KZ",
})
_SOUTH_ASIA = frozenset({"IN", "PK", "BD", "LK", "NP", "AF"})
# APAC adds Oceania to Asia.
_APAC = _ASIA | {"AU", "NZ"}
_EUROPE = frozenset({
    "GB", "IE", "FR", "DE", "ES", "PT", "IT", "NL", "BE", "LU", "CH", "AT",
    "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "SI", "RS", "BA", "MK",
    "SE", "NO", "DK", "FI", "IS", "EE", "LV", "LT", "UA", "MD", "BY", "MT", "CY",
})
_NORTH_AMERICA = frozenset({"US", "CA", "MX"})
_LATAM = frozenset({
    "MX", "BR", "AR", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE",
    "CR", "PA", "GT", "HN", "SV", "DO",
})
_SOUTH_AMERICA = frozenset({"BR", "AR", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE"})
_AFRICA = frozenset({"ZA", "NG", "KE", "EG", "GH", "MA", "TN", "DZ", "ET", "UG", "TZ", "CM", "ZW"})
_OCEANIA = frozenset({"AU", "NZ"})
_MIDDLE_EAST = frozenset({"AE", "SA", "IL", "QA", "KW", "BH", "OM", "JO", "LB", "TR"})

REGION_MEMBERS: dict[str, frozenset[str]] = {
    "asia": _ASIA,
    "asia pacific": _APAC,
    "asia-pacific": _APAC,
    "apac": _APAC,
    "south asia": _SOUTH_ASIA,
    "southern asia": _SOUTH_ASIA,
    "indian subcontinent": _SOUTH_ASIA,
    "southeast asia": frozenset({"SG", "MY", "TH", "VN", "PH", "ID", "KH"}),
    "south east asia": frozenset({"SG", "MY", "TH", "VN", "PH", "ID", "KH"}),
    # NO bare "sea" alias. It used to be here and it was wrong: on a US job
    # board "SEA" is Seattle, so Stripe's "SF, NYC, SEA, CHI" resolved to
    # {ID, KH, MY, PH, SG, TH, VN} and a US-only role claimed ASEAN
    # eligibility. `sea -> US` now lives in `_CITY_COUNTRIES`, which is
    # checked first. Spell the region out to mean the region.
    "europe": _EUROPE,
    "eu": _EUROPE,
    "eea": _EUROPE,
    "european union": _EUROPE,
    "emea": _EUROPE | _MIDDLE_EAST | _AFRICA,
    "middle east": _MIDDLE_EAST,
    "africa": _AFRICA,
    "oceania": _OCEANIA,
    "anz": _OCEANIA,
    "australia and new zealand": _OCEANIA,
    "americas": _NORTH_AMERICA | _LATAM,
    "the americas": _NORTH_AMERICA | _LATAM,
    "amer": _NORTH_AMERICA | _LATAM,
    "north america": _NORTH_AMERICA,
    "northern america": _NORTH_AMERICA,
    # Zapier's Ashby board writes regions as NAMER / APAC / EMEA.
    "namer": _NORTH_AMERICA,
    "namer region": _NORTH_AMERICA,
    "central america": frozenset({"CR", "PA", "GT", "HN", "SV"}),
    "south america": _SOUTH_AMERICA,
    "latam": _LATAM,
    "latin america": _LATAM,
    "caribbean": frozenset({"DO", "JM"}),
}

# A token is a timezone constraint, not a place, if it looks like any of these.
# The bare-abbreviation branch is deliberately CASE-SENSITIVE, via a scoped
# `(?-i:)` inside an otherwise case-insensitive pattern. It exists to catch
# "EST" / "PST" / "IST" / "CEST", and under a blanket IGNORECASE it matched any
# three-to-five letter word ending in T — including "East". That made
# `looks_like_timezone("South East Asia")` true, so a named REGION was filed as
# a timezone restriction and its member countries were never resolved. A real
# timezone abbreviation is written in capitals; an English word is not.
_TIMEZONE_RE = re.compile(
    r"(utc|gmt|time\s*zones?|timezones?|(?-i:\b[A-Z]{2,4}T\b)|\butc[+-]|\bgmt[+-])",
    re.IGNORECASE,
)
# Splits "USA, Canada, USA timezones" / "Europe / UK" / "US | Canada", and the
# spaced dash ATS boards use for "India - Remote" / "Remote - US".
_SPLIT_RE = re.compile(r"[,;/|]|\s[-–—]\s| and | or |\bplus\b", re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"\(([^)]*)\)")


# US state / territory abbreviations. These must be resolved *before* the bare
# alpha-2 fallback, or a Greenhouse location like "San Francisco, CA" would
# resolve to Canada.
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr",
}

_STRIP_CHARS = " \t-–—.,;:"


def _clean_token(value: str) -> str:
    token = value.strip(_STRIP_CHARS)
    token = re.sub(
        r"^(only|remote|hybrid|onsite|on-site|based in|located in|residing in|must be in)\b",
        "",
        token,
        flags=re.I,
    )
    token = re.sub(r"\b(only|based|residents?|citizens?)$", "", token, flags=re.I)
    return token.strip(_STRIP_CHARS)


def is_worldwide(token: str) -> bool:
    return _clean_token(token).casefold() in _WORLDWIDE_TOKENS


def looks_like_timezone(token: str) -> bool:
    return bool(_TIMEZONE_RE.search(token))


def country_code(name: str) -> str | None:
    """Resolve one country name/alias to ISO-3166-1 alpha-2, else None."""
    key = _clean_token(name).casefold()
    if not key:
        return None
    # States first: "CA" is California far more often than Canada in the ATS
    # location strings this runs over, and "IN" is Indiana, not India.
    if key in _US_STATES:
        return "US"
    if key in _COUNTRY_CODES:
        return _COUNTRY_CODES[key]
    if key in _CITY_COUNTRIES:
        return _CITY_COUNTRIES[key]
    # Already a bare alpha-2 code — accept only if we know it.
    upper = key.upper()
    if len(upper) == 2 and upper in _KNOWN_CODES:
        return upper
    return None


def is_country_code(value: str) -> bool:
    """True for a known ISO alpha-2 code, read as a COUNTRY.

    `country_code` resolves "CA" and "IN" as US states on purpose. Boards that
    write locations as ISO codes throughout (YC's `"CA / Remote (CA)"` is
    Canada) need the other reading, and this is it.
    """
    return len(value.strip()) == 2 and value.strip().upper() in _KNOWN_CODES


def is_us_state(value: str) -> bool:
    return value.strip().lower() in _US_STATES


def region_members(name: str) -> frozenset[str] | None:
    return REGION_MEMBERS.get(_clean_token(name).casefold())


def split_location_text(value: str) -> list[str]:
    """Break one free-text eligibility string into candidate tokens.

    Parentheticals are pulled out first so `"USA, CST (UTC-6)"` yields the
    timezone hint as its own token instead of corrupting the country token.
    """
    if not value:
        return []
    extras = [m.group(1) for m in _PARENTHETICAL_RE.finditer(value)]
    stripped = _PARENTHETICAL_RE.sub(" ", value)
    tokens = [_clean_token(t) for t in _SPLIT_RE.split(stripped)]
    tokens.extend(_clean_token(t) for t in extras)
    return [t for t in tokens if t]


def resolve_eligibility(
    tokens: Iterable[str],
    *,
    empty_means_worldwide: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Resolve location tokens into (eligibility, timezone hints, unresolved).

    `eligibility` holds ISO alpha-2 codes and/or the `worldwide` sentinel,
    de-duplicated with order preserved. Anything we could not classify is
    returned in `unresolved` rather than dropped, so the reason trail can say
    so out loud.
    """
    eligibility: list[str] = []
    timezones: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    saw_any = False

    def add(code: str) -> None:
        if code not in seen:
            seen.add(code)
            eligibility.append(code)

    for token in tokens:
        token = _clean_token(token)
        if not token:
            continue
        saw_any = True

        if is_worldwide(token):
            add(WORLDWIDE)
            continue
        # Region BEFORE timezone: a token that names a region we know is a
        # region, whatever else it might look like. "South East Asia" and
        # "Central - United States" both carry timezone-ish words, and
        # guessing timezone first throws away membership we can actually
        # resolve. Belt and braces alongside the case-sensitivity fix above.
        members = region_members(token)
        if members:
            for member in sorted(members):
                add(member)
            continue

        if looks_like_timezone(token):
            timezones.append(token)
            continue

        code = country_code(token)
        if code:
            add(code)
            continue

        unresolved.append(token)

    if not saw_any and empty_means_worldwide:
        add(WORLDWIDE)

    return eligibility, timezones, unresolved


def format_utc_offset(offset: float) -> str:
    """Render a numeric UTC offset the way Himalayas emits it (`5.5` -> `UTC+05:30`)."""
    sign = "-" if offset < 0 else "+"
    magnitude = abs(offset)
    hours = int(magnitude)
    minutes = round((magnitude - hours) * 60)
    if minutes == 60:  # tolerate float noise, e.g. 5.999999
        hours += 1
        minutes = 0
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


# --------------------------------------------------------------------------
# Prose that narrows a "worldwide" claim
# --------------------------------------------------------------------------
# Several boards call a role worldwide when the employer simply stated nothing:
# Wellfound renders it "Everywhere", We Work Remotely files 97% of its rows
# under "Anywhere in the World", Remote OK leaves `location` blank. The JD text
# is then the only place a restriction shows up. Ordered: first match wins.
RESTRICTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:with)?in\s+the\s+(United States|USA|US)\b", re.I), "United States"),
    (re.compile(r"\bUS[- ]based\b|\bU\.S\.[- ]based\b", re.I), "United States"),
    (re.compile(r"\b(?:must|required to)\s+(?:be\s+)?(?:located|reside|live)\s+in\s+the\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\b(?:must|required to)\s+(?:be\s+)?(?:located|reside|live)\s+in\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\bauthoriz(?:ed|ation) to work in the\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\bonly\s+(?:accepting|considering)\s+.{0,40}?\bin\s+([A-Z][A-Za-z .]+)", re.I), ""),
]

# A pay sentence names a country without restricting anyone: Reddit's "we share
# base salary ranges for all US-based job postings", Canonical's "compensation
# for US based candidates". This is the false positive that kept the check off
# Jobicy.
_PAY_SENTENCE = re.compile(
    r"salar|compensat|\bpay\b|base\s+range|\$\s?\d|benefits?\s+(?:vary|outside)", re.I
)
# Same splitter as `app/blockers.py`: a full stop ends a sentence only after a
# lowercase word, so "U.S." does not.
_SENTENCE_SPLIT = re.compile(r"(?<=[a-z]{2}[.!?])\s+|\n+|•")


def _match_restriction(text: str) -> str | None:
    for pattern, fixed in RESTRICTION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = fixed or (match.group(1) if match.groups() else "")
        name = name.strip().rstrip(".,;")
        if name and country_code(name):
            return name
    return None


def restriction_from_prose(text: str | None, *, skip_pay_sentences: bool = False) -> str | None:
    """Find a country restriction stated in free text, or None.

    Deliberately conservative: it only reports a restriction it can name, so an
    unparsed sentence leaves eligibility as-is rather than inventing a limit.

    `skip_pay_sentences` reads sentence by sentence and ignores pay sentences.
    Wellfound predates it and reads the whole text at once.
    """
    if not text:
        return None
    if not skip_pay_sentences:
        return _match_restriction(text)
    for sentence in _SENTENCE_SPLIT.split(text):
        if sentence and not _PAY_SENTENCE.search(sentence):
            found = _match_restriction(sentence)
            if found:
                return found
    return None

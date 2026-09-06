"""Pure helpers shared by adapters and the ingest pipeline.

No I/O here — everything is deterministic and directly unit-testable.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

# Tokens that mean "this role is remote" when found in a location string.
_REMOTE_PATTERNS = re.compile(
    r"\b(remote|work from home|wfh|distributed|anywhere|virtual)\b", re.IGNORECASE
)
# "Remote-first office in NYC" style strings are still remote; but a location
# like "Fremont, CA" obviously is not. Hybrid is treated as *not* remote.
_NON_REMOTE_HINTS = re.compile(r"\bnon[- ]remote\b|\bno remote\b", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Minimal HTML → text. Block elements become newlines, `<li>` gets a bullet."""

    _BLOCK = {
        "p", "div", "br", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote",
    }
    _SKIP = {"script", "style", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "li":
            self._parts.append("\n• ")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def maybe_unescape_html(value: str | None) -> str | None:
    """Undo entity-escaping applied to an HTML payload.

    Greenhouse returns `content` double-encoded (`&lt;p&gt;...`). Other boards
    return real HTML. Detect rather than assume, since this drifts per board.
    """
    if not value:
        return value
    if "<" not in value and ("&lt;" in value or "&gt;" in value):
        return html_module.unescape(value)
    return value


def html_to_text(value: str | None) -> str | None:
    """Flatten an HTML description to readable plain text."""
    if not value:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        raw = parser.text()
    except Exception:  # malformed markup: fall back to a tag strip
        raw = re.sub(r"<[^>]+>", " ", value)
        raw = html_module.unescape(raw)

    raw = raw.replace("\xa0", " ")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in raw.split("\n")]
    text = "\n".join(lines)
    text = _BLANKLINES_RE.sub("\n\n", text).strip()
    return text or None


def clean_locations(values: list[str | None] | None) -> list[str]:
    """Trim, drop empties, and de-duplicate case-insensitively (order kept)."""
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        cleaned = _WHITESPACE_RE.sub(" ", str(value)).strip(" ,;|")
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def detect_remote(
    locations: list[str] | None = None,
    *,
    title: str | None = None,
    workplace_type: str | None = None,
    explicit: bool | None = None,
) -> bool:
    """Best-effort remote flag.

    Precedence: a recognized `workplace_type`, then an ATS-provided boolean,
    then a keyword scan of locations and title.

    `workplace_type` outranks `explicit` because the two genuinely disagree in
    live data — Ashby returns `isRemote: true` alongside `workplaceType:
    "Hybrid"` — and "hybrid" is the more specific claim. A hybrid role is not
    remote for someone filtering on remote-only.
    """
    if workplace_type:
        normalized = workplace_type.strip().lower().replace("-", "").replace("_", "")
        if normalized == "remote":
            return True
        if normalized in {"onsite", "hybrid", "inperson", "office"}:
            return False

    if explicit is not None:
        return bool(explicit)

    haystack = " | ".join([*(locations or []), title or ""])
    if _NON_REMOTE_HINTS.search(haystack):
        return False
    return bool(_REMOTE_PATTERNS.search(haystack))


def build_source_key(ats: str, company_key: str, native_id: str) -> str:
    """Identity per the spec: `{ats}:{company}:{job_id}`."""
    return f"{ats}:{company_key}:{native_id}"


# Symbol -> ISO 4217. A bare `$` is read as USD: both aggregator boards are
# US-centric and quote unqualified dollars in USD. The prefixed variants are
# listed first so `CA$` never falls through to plain `$`.
_CURRENCY_SYMBOLS: list[tuple[str, str]] = [
    ("CA$", "CAD"), ("C$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"),
    ("NZ$", "NZD"), ("R$", "BRL"), ("S$", "SGD"), ("HK$", "HKD"),
    ("US$", "USD"), ("$", "USD"),
    ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("¥", "JPY"), ("₽", "RUB"),
    ("zł", "PLN"), ("R$", "BRL"), ("₪", "ILS"), ("₩", "KRW"),
]

_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "INR", "CAD", "AUD", "NZD", "SGD", "CHF", "SEK",
    "NOK", "DKK", "PLN", "BRL", "MXN", "ZAR", "JPY", "CNY", "HKD", "ILS",
    "AED", "KRW", "RUB", "TRY", "CZK", "HUF", "RON", "UAH", "PHP", "IDR",
    "MYR", "THB", "VND", "NGN", "KES", "EGP", "ARS", "CLP", "COP", "PEN",
}

_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
# A number with optional thousands/decimal separators and an optional k suffix.
_AMOUNT_RE = re.compile(r"(\d[\d.,]*)\s*([kK])?\b")


def _parse_amount(digits: str, k_suffix: bool) -> float | None:
    """Turn one matched amount into a number.

    Separator handling is the fiddly part, because live data mixes conventions
    inside a single feed: Remotive returns both `$45,000 - $50,000` (comma as
    thousands) and `$31,2k- $52k` (comma as a decimal point).
    """
    text = digits.strip().rstrip(".,")
    if not text:
        return None

    if "," in text:
        tail = text.rsplit(",", 1)[1]
        if len(tail) == 3 and "." not in tail:
            text = text.replace(",", "")          # 45,000 -> 45000
        else:
            text = text.replace(".", "").replace(",", ".")  # 31,2 -> 31.2
    try:
        value = float(text)
    except ValueError:
        return None

    if k_suffix:
        value *= 1000
    return value


def detect_currency(text: str | None) -> str | None:
    """Find an ISO 4217 code in free text, via an explicit code or a symbol."""
    if not text:
        return None
    for match in _CURRENCY_CODE_RE.finditer(text.upper()):
        if match.group(1) in _CURRENCY_CODES:
            return match.group(1)
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return None


def parse_salary_text(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse a free-text pay string into `(min, max, currency)`.

    Remotive has no structured salary at all — only strings like `$150k - $230k`,
    `$18 - $22/hr` or `OTE $25k - $35k`. Values are returned exactly as quoted:
    an hourly range stays hourly, because nothing downstream needs the rate
    normalized and inventing an annualization factor would be a guess.
    """
    if not text or not text.strip():
        return None, None, None

    currency = detect_currency(text)

    amounts: list[float] = []
    for match in _AMOUNT_RE.finditer(text):
        value = _parse_amount(match.group(1), bool(match.group(2)))
        if value is not None and value > 0:
            amounts.append(value)
        if len(amounts) == 2:
            break

    if not amounts:
        return None, None, currency
    if len(amounts) == 1:
        return amounts[0], None, currency

    low, high = sorted(amounts[:2])
    return low, high, currency


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to aware UTC. Naive input is assumed UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_epoch_millis(value: Any) -> datetime | None:
    """Lever timestamps are epoch milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    # Tolerate seconds-precision values from a drifting API.
    if millis < 10_000_000_000:
        millis *= 1000
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def content_hash(
    *,
    title: str,
    locations: list[str],
    department: str | None,
    remote: bool,
    apply_url: str,
    description_text: str | None,
    posted_at: datetime | None,
    updated_at: datetime | None,
) -> str:
    """Stable hash over the fields that make a posting meaningfully different.

    Used to skip no-op writes and, in Phase 2, to cache LLM validation so an
    unchanged job never re-bills.
    """
    payload = json.dumps(
        {
            "title": title.strip(),
            "locations": sorted(loc.casefold() for loc in locations),
            "department": (department or "").strip().casefold(),
            "remote": remote,
            "apply_url": apply_url.strip(),
            "description": (description_text or "").strip(),
            "posted_at": posted_at.isoformat() if posted_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

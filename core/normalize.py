"""Parsers that turn messy page text into typed values.

Everything here is pure and side-effect free, which makes it the part of the
project that is cheapest to test and hardest to get subtly wrong.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------- text


# Tags that mark a line break in prose; everything else just disappears.
_BLOCK_TAG = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\s*/?\s*>")
# A real tag starts with a letter, a slash or a bang - so "under < 50k" and
# "2 < 3" in ad text survive untouched.
_HTML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")


def strip_html(value: str) -> str:
    """Turn markup into plain text.

    Zameen stores its descriptions with `<br />` between lines, so without
    this the tags end up in the database and the spreadsheet.
    """
    value = _BLOCK_TAG.sub("\n", value)
    value = _HTML_TAG.sub("", value)
    # Unescape last: doing it first would turn `&lt;br&gt;` into a tag that
    # the rules above would then strip out.
    return html.unescape(value)


def clean_text(value: str | None) -> str:
    """Strip markup, collapse whitespace, drop zero-width junk."""
    if not value:
        return ""
    if "<" in value or "&" in value:
        value = strip_html(value)
    value = value.replace("​", "").replace("\xa0", " ")
    # Keep paragraph breaks, collapse everything else.
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n\s*", "\n\n", value)
    return value.strip()


# --------------------------------------------------------------------- price

_SOUTH_ASIAN_MULTIPLIERS = {
    "thousand": Decimal(1_000),
    "k": Decimal(1_000),
    "lakh": Decimal(100_000),
    "lac": Decimal(100_000),
    "lacs": Decimal(100_000),
    "lakhs": Decimal(100_000),
    "crore": Decimal(10_000_000),
    "crores": Decimal(10_000_000),
    "cr": Decimal(10_000_000),
    "arab": Decimal(1_000_000_000),
    "million": Decimal(1_000_000),
    "mn": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}

_NO_PRICE_MARKERS = (
    "call for price",
    "price on call",
    "contact for price",
    "ask for price",
    "negotiable price",
    "on demand",
    "poa",
)

_PRICE_NUMBER = re.compile(r"(\d[\d,\.]*)")


def parse_price(raw: str | None) -> tuple[Decimal | None, str, str]:
    """Parse a price string into (amount, currency, original_text).

    Handles plain numbers ("4,500,000"), currency prefixes ("Rs 4.5 lakh",
    "PKR 85,00,000") and South Asian magnitude words ("4.5 Crore", "85 Lakh").
    Returns (None, currency, raw) when no usable number is present.
    """
    original = clean_text(raw)
    if not original:
        return None, "PKR", ""

    lowered = original.lower()
    if any(marker in lowered for marker in _NO_PRICE_MARKERS):
        return None, "PKR", original

    currency = "USD" if re.search(r"\bUSD\b|\$", original, re.I) else "PKR"

    match = _PRICE_NUMBER.search(lowered)
    if not match:
        return None, currency, original

    number_text = match.group(1)
    # "85,00,000" (Indian grouping) and "4,500,000" both just lose their commas.
    # A trailing ".00" is a decimal; commas are never decimal separators here.
    number_text = number_text.replace(",", "")
    if number_text.count(".") > 1:
        number_text = number_text.replace(".", "")

    try:
        amount = Decimal(number_text)
    except InvalidOperation:
        return None, currency, original

    # Apply a magnitude word if one follows the number.
    tail = lowered[match.end() :]
    word = re.match(r"\s*([a-z]+)", tail)
    if word:
        multiplier = _SOUTH_ASIAN_MULTIPLIERS.get(word.group(1))
        if multiplier is not None:
            amount *= multiplier

    if amount <= 0:
        return None, currency, original

    return amount, currency, original


# ---------------------------------------------------------------------- date

_RELATIVE = re.compile(
    r"(?:(?P<num>\d+)|\b(?P<article>an?)\b)\s*"
    r"(?P<unit>second|sec|minute|min|hour|hr|day|week|month|year)s?\s*(?:ago|old|before)",
    re.I,
)

_UNIT_DELTA = {
    "second": timedelta(seconds=1),
    "sec": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def parse_ad_date(raw, *, now: datetime | None = None) -> date | None:
    """Parse a listing date from whatever the site shows.

    Accepts relative phrases ("3 days ago", "an hour ago", "Updated 2 weeks
    ago"), the words Today/Yesterday, epoch seconds or milliseconds, and
    absolute date strings. Returns None when nothing parses.
    """
    now = now or datetime.now(timezone.utc)

    if raw is None or raw == "":
        return None

    # Numeric epoch (OLX hands these out as ms).
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if number > 1e11:  # milliseconds
            number /= 1000.0
        if number <= 0:
            return None
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = clean_text(str(raw))
    if not text:
        return None

    lowered = text.lower()

    if "just now" in lowered or "moments ago" in lowered or "today" in lowered:
        return now.date()
    if "yesterday" in lowered:
        return (now - timedelta(days=1)).date()

    relative = _RELATIVE.search(lowered)
    if relative:
        count = int(relative.group("num")) if relative.group("num") else 1
        delta = _UNIT_DELTA.get(relative.group("unit").lower())
        if delta is not None:
            return (now - delta * count).date()

    # Absolute date, ISO first then anything dateutil recognises.
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    try:
        from dateutil import parser as dateutil_parser

        # Parse twice against two different defaults. Any field dateutil had to
        # invent differs between the two runs, which is how we tell a real date
        # from a fuzzy match on a string that contains no date at all.
        # (Checking against `now` instead would silently discard every listing
        # posted today, since dateutil fills the missing time from the default.)
        first = dateutil_parser.parse(text, fuzzy=True, default=datetime(2000, 1, 1))
        second = dateutil_parser.parse(text, fuzzy=True, default=datetime(2001, 2, 2))
    except (ValueError, OverflowError, ImportError):
        return None

    if (first.month, first.day) != (second.month, second.day):
        return None  # no real month/day in the text

    if first.year != second.year:
        # A date with no year - "Sep 19". Assume the most recent occurrence.
        candidate = date(now.year, first.month, first.day)
        if candidate > now.date():
            candidate = date(now.year - 1, first.month, first.day)
        return candidate

    return first.date()


# --------------------------------------------------------------------- phone

# A Pakistani number always carries a trunk/country prefix: 0, +92, 92 or 0092.
# Requiring it is what stops unrelated digit runs in ad text - mileage next to
# a model year, for instance - from being read as a phone number.
#
# Separators deliberately exclude newlines: "Mileage 41500\n2019 import" must
# not join into one 9-digit "number".
_PHONE_SEP = r"[ \t\-.() ]?"
_PHONE_CANDIDATE = re.compile(
    rf"(?<!\d)(?:\+92|0092|0){_PHONE_SEP}(?:\d{_PHONE_SEP}){{8,11}}(?!\d)"
)

_DIGITS = re.compile(r"\D+")

# Urdu/Arabic listings often carry the number in Arabic-Indic digits.
_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def normalize_digits(text: str) -> str:
    """Fold Arabic-Indic digit forms down to ASCII 0-9."""
    return text.translate(_DIGIT_TRANSLATION)


def normalize_phone(raw: str | None) -> str | None:
    """Normalize a Pakistani number to +92XXXXXXXXXX, or None if implausible."""
    if not raw:
        return None

    digits = _DIGITS.sub("", normalize_digits(str(raw)))
    if not digits:
        return None

    # Strip country code / trunk prefix down to the national significant number.
    if digits.startswith("0092"):
        national = digits[4:]
    elif digits.startswith("92") and len(digits) >= 12:
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits.lstrip("0")
    else:
        national = digits

    if not national:
        return None

    # Mobile: 10 digits starting with 3. Landline: 9-10 digits.
    if national.startswith("3"):
        if len(national) != 10:
            return None
    elif not 9 <= len(national) <= 10:
        return None

    if len(set(national)) <= 2:  # 0000000000, 1111111111 - placeholder junk
        return None

    return f"+92{national}"


def extract_phone(text: str | None) -> str | None:
    """Pull the first plausible phone number out of free text.

    Used only against publicly rendered ad content - a number the seller typed
    into their own description. It does not unmask anything the site hides.
    """
    if not text:
        return None

    for match in _PHONE_CANDIDATE.finditer(normalize_digits(str(text))):
        normalized = normalize_phone(match.group(0))
        if normalized:
            return normalized
    return None


# --------------------------------------------------------------------- slugs


def city_slug(name: str) -> str:
    """'Dera Ghazi Khan' -> 'dera-ghazi-khan'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")

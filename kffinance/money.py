"""Money handling: integer minor units with an explicit ISO currency code.

Amounts of different currencies are never added implicitly. Conversion needs an
explicit FxRate carrying its source and as-of date.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "IDR": 2, "BHD": 3, "KWD": 3}
_CCY = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    pass


def exponent(currency: str) -> int:
    check_currency(currency)
    return EXPONENTS.get(currency, 2)


def check_currency(currency: str) -> str:
    if not isinstance(currency, str) or not _CCY.match(currency):
        raise MoneyError(f"invalid currency code: {currency!r}")
    return currency


def parse_amount(text, currency: str) -> int:
    """Parse a decimal string like '1,234.50' or '(12.00)' into signed minor units.

    Rejects values with more decimal places than the currency allows rather than rounding.
    """
    exp = exponent(currency)
    s = str(text).strip().replace(",", "").replace(" ", "")
    if not s:
        raise MoneyError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    for sym in ("S$", "$", "SGD", "USD"):
        s = s.replace(sym, "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise MoneyError(f"not a number: {text!r}") from None
    if not d.is_finite():
        raise MoneyError(f"not finite: {text!r}")
    if neg:
        d = -d
    scaled = d * (Decimal(10) ** exp)
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{text!r} has more than {exp} decimal places for {currency}")
    return int(scaled)


def format_minor(minor: int, currency: str) -> str:
    exp = exponent(currency)
    sign = "-" if minor < 0 else ""
    q = Decimal(abs(minor)) / (Decimal(10) ** exp)
    return f"{sign}{currency} {q:,.{exp}f}"


def obj(minor, currency):
    """API representation. None means *unavailable*, which is different from 0."""
    if minor is None:
        return {"available": False, "minor": None, "currency": currency, "display": "Unavailable"}
    return {"available": True, "minor": int(minor), "currency": currency, "display": format_minor(int(minor), currency)}


@dataclass(frozen=True)
class FxRate:
    base: str
    quote: str
    rate: str  # decimal string: 1 base = rate quote
    source: str
    as_of: str  # ISO date


def convert(minor: int, frm: str, to: str, fx: FxRate | None) -> int:
    check_currency(frm), check_currency(to)
    if frm == to:
        return minor
    if fx is None or not fx.source or not fx.as_of:
        raise MoneyError(f"conversion {frm}->{to} needs an FxRate with source and as-of date")
    if (fx.base, fx.quote) != (frm, to):
        raise MoneyError(f"FxRate {fx.base}/{fx.quote} does not match {frm}->{to}")
    major = Decimal(minor) / (Decimal(10) ** exponent(frm))
    out = major * Decimal(fx.rate) * (Decimal(10) ** exponent(to))
    return int(out.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def add(a: int, ca: str, b: int, cb: str) -> int:
    if ca != cb:
        raise MoneyError(f"refusing to add {ca} and {cb} without explicit conversion")
    return a + b

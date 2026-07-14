"""
Normalization + SHA-256 hashing of PII for Meta Conversions API.

Meta matches on the hash of a NORMALIZED value. Hashing a raw string that
differs by case, whitespace or formatting produces a hash that will never
match — silently. The API returns 200, the event is accepted, and Event Match
Quality quietly drops. That failure mode is why normalization lives in its own
module with its own tests, rather than inline in the client.
"""

import hashlib
import re

_NON_DIGITS = re.compile(r"\D")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def norm_email(value: str | None) -> str | None:
    """Lowercase, strip whitespace. Returns None for empty input."""
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def norm_phone(value: str | None) -> str | None:
    """
    Strip everything that is not a digit. Meta expects country code included
    and no leading '+', no spaces, no dashes.
    E.g. '+380 (67) 123-45-67' -> '380671234567'
    """
    if not value:
        return None
    digits = _NON_DIGITS.sub("", value)
    return digits or None


def norm_name(value: str | None) -> str | None:
    """Lowercase, strip. Used for first/last name."""
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def norm_zip(value: str | None) -> str | None:
    """Lowercase, strip, take the part before any space or dash (US ZIP+4)."""
    if not value:
        return None
    cleaned = value.strip().lower()
    cleaned = re.split(r"[\s-]", cleaned)[0]
    return cleaned or None


def hash_email(value: str | None) -> str | None:
    normalized = norm_email(value)
    return _sha256(normalized) if normalized else None


def hash_phone(value: str | None) -> str | None:
    normalized = norm_phone(value)
    return _sha256(normalized) if normalized else None


def hash_name(value: str | None) -> str | None:
    normalized = norm_name(value)
    return _sha256(normalized) if normalized else None


def hash_zip(value: str | None) -> str | None:
    normalized = norm_zip(value)
    return _sha256(normalized) if normalized else None


def build_user_data(
    *,
    email: str | None = None,
    phone: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    zip_code: str | None = None,
    client_ip: str | None = None,
    client_user_agent: str | None = None,
    fbc: str | None = None,
    fbp: str | None = None,
) -> dict:
    """
    Assemble Meta's user_data block.

    Hashed:     em, ph, fn, ln, zp
    NOT hashed: client_ip_address, client_user_agent, fbc, fbp
                (Meta explicitly requires these in plaintext — hashing them
                 destroys the match instead of protecting it.)

    Keys with no value are omitted entirely. Meta treats an empty string as a
    present-but-invalid identifier, which drags EMQ down; an absent key is
    simply absent.
    """
    candidates = {
        "em": hash_email(email),
        "ph": hash_phone(phone),
        "fn": hash_name(first_name),
        "ln": hash_name(last_name),
        "zp": hash_zip(zip_code),
        "client_ip_address": client_ip,
        "client_user_agent": client_user_agent,
        "fbc": fbc,
        "fbp": fbp,
    }
    return {k: v for k, v in candidates.items() if v}

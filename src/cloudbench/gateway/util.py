from __future__ import annotations

import secrets
from datetime import UTC, datetime

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase


def new_id(prefix: str, length: int = 14) -> str:
    """Return a random, URL-safe ID such as ``run_7x2k9q...`` with the given prefix."""
    return f"{prefix}_" + "".join(secrets.choice(_ALPHABET) for _ in range(length))


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; values we store are always UTC, so reattach it."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

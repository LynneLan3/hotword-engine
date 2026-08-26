"""Deterministic identity primitives for the universal candidate schema.

Identity text is normalized with Unicode NFKC, case-folding, apostrophe
removal, and non-alphanumeric separator collapsing.  Consequently, for
example, ``Soul's Remnant`` and ``Souls Remnant`` have the same game identity.
This is intentionally normalization only; it is not fuzzy entity matching.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from enum import Enum
from typing import Any


_APOSTROPHES = frozenset(("'", "’", "`", "´"))


def normalize_identity_text(value: str) -> str:
    """Return the stable identity form for a human-readable text value."""

    if not isinstance(value, str):
        raise TypeError("identity text must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    chars: list[str] = []
    for char in normalized:
        if char in _APOSTROPHES:
            continue
        chars.append(char if char.isalnum() else " ")
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def normalize_aliases(aliases: Any) -> tuple[str, ...]:
    """Normalize, de-duplicate, and stably order optional aliases."""

    if aliases is None:
        return ()
    if isinstance(aliases, (str, bytes)):
        raise TypeError("aliases must be an iterable of strings")
    result = {normalize_identity_text(alias) for alias in aliases}
    return tuple(sorted(alias for alias in result if alias))


def _stable_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _stable_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return value


def _text_component(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _make_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(
        [_stable_value(part) for part in parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{prefix}_{digest}"


def make_game_entity_id(canonical_name: str) -> str:
    """Make the canonical identity for a game name.

    Aliases are deliberately excluded: resolving an alias to an entity is a
    later concern, while this helper provides only deterministic primitives.
    """

    return _make_id("ge", normalize_identity_text(canonical_name))


def make_platform_listing_id(
    game_entity_id: str,
    platform: Any,
    platform_game_id: str,
) -> str:
    return _make_id(
        "pl",
        _text_component(game_entity_id),
        _text_component(platform),
        _text_component(platform_game_id),
    )


def make_event_id(
    game_entity_id: str,
    event_type: Any,
    event_date: Any,
    source: str,
    source_ref: str = "",
    platform_listing_id: str | None = None,
) -> str:
    return _make_id(
        "ev",
        _text_component(game_entity_id),
        _text_component(event_type),
        _text_component(event_date),
        _text_component(source),
        _text_component(source_ref),
        _text_component(platform_listing_id),
    )


def make_signal_id(
    game_entity_id: str,
    observed_at: Any,
    source: str,
    signal_type: str,
    raw_value: Any,
    event_id: str | None = None,
    platform_listing_id: str | None = None,
) -> str:
    return _make_id(
        "sig",
        _text_component(game_entity_id),
        _text_component(event_id),
        _text_component(platform_listing_id),
        _text_component(observed_at),
        _text_component(source),
        _text_component(signal_type),
        raw_value,
    )

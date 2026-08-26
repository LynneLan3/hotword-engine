"""Reusable, platform-neutral Game x Event candidate contracts."""

from .adapters import CanonicalRecords, SourceAdapter, steam_candidate_to_records
from .ids import (
    make_event_id,
    make_game_entity_id,
    make_platform_listing_id,
    make_signal_id,
    normalize_identity_text,
    normalize_aliases,
)
from .schema import (
    CandidateSignal,
    EventType,
    GameEntity,
    GameEvent,
    Platform,
    PlatformListing,
    SchemaValidationError,
)

__all__ = [
    "CandidateSignal",
    "CanonicalRecords",
    "EventType",
    "GameEntity",
    "GameEvent",
    "Platform",
    "PlatformListing",
    "SchemaValidationError",
    "SourceAdapter",
    "make_event_id",
    "make_game_entity_id",
    "make_platform_listing_id",
    "make_signal_id",
    "normalize_aliases",
    "normalize_identity_text",
    "steam_candidate_to_records",
]

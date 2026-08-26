"""Thin source adapter contracts and an offline Steam compatibility bridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping, Protocol

from .schema import CandidateSignal, EventType, GameEntity, GameEvent, Platform, PlatformListing


@dataclass(frozen=True)
class CanonicalRecords:
    """The small output envelope shared by future source adapters."""

    game_entities: tuple[GameEntity, ...] = ()
    platform_listings: tuple[PlatformListing, ...] = ()
    events: tuple[GameEvent, ...] = ()
    signals: tuple[CandidateSignal, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_entities": [item.to_dict() for item in self.game_entities],
            "platform_listings": [item.to_dict() for item in self.platform_listings],
            "events": [item.to_dict() for item in self.events],
            "signals": [item.to_dict() for item in self.signals],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SourceAdapter(Protocol):
    """Minimal contract: collect raw records, then normalize each record."""

    source_name: str

    def collect(self) -> Iterable[Mapping[str, Any]]:
        """Return source observations; implementations may be offline or remote."""

    def normalize(self, raw_record: Mapping[str, Any]) -> CanonicalRecords:
        """Convert one source observation into canonical records."""


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def steam_candidate_to_records(record: Mapping[str, Any]) -> CanonicalRecords:
    """Convert one existing Steam Candidate-shaped job without side effects.

    The helper accepts the current nested ``steam_signals`` shape and emits
    only canonical records.  It does not call Steam, SearchApi, Sheets, or any
    production runner.
    """

    if not isinstance(record, Mapping):
        raise ValueError("Steam Candidate record must be an object")
    app_id = _first(record, "steam_app_id")
    game_name = _first(record, "game_name")
    signals = record.get("steam_signals") or {}
    if not isinstance(signals, Mapping):
        raise ValueError("steam_signals must be an object")
    if app_id is None or not str(app_id).strip():
        raise ValueError("Steam Candidate record missing steam_app_id")
    if game_name is None or not str(game_name).strip():
        raise ValueError("Steam Candidate record missing game_name")

    app_id = str(app_id).strip()
    entity = GameEntity.create(str(game_name), aliases=())
    listing = PlatformListing.create(
        entity.game_entity_id,
        Platform.STEAM,
        app_id,
        store_url=_first(record, "steam_url"),
        release_date=_first(signals, "release_date"),
        region="US",
        listing_status="ACTIVE",
    )
    release_date = _first(signals, "release_date")
    if release_date is None:
        raise ValueError("Steam Candidate record missing steam_signals.release_date")
    source_ref = str(_first(record, "job_id") or app_id).strip()
    event = GameEvent.create(
        entity.game_entity_id,
        EventType.NEW_RELEASE,
        release_date,
        "STEAM_CANDIDATE",
        source_ref,
        listing.platform_listing_id,
    )
    observed_at = _first(record, "created_at") or release_date
    signal_values = (
        ("STEAM_FOLLOWERS", _first(signals, "followers")),
        ("STEAM_7D_GAIN", _first(signals, "followers_gain_7d", "7d_gain")),
    )
    canonical_signals = tuple(
        CandidateSignal.create(
            entity.game_entity_id,
            observed_at,
            "STEAM_CANDIDATE",
            signal_type,
            raw_value,
            event_id=event.event_id,
            platform_listing_id=listing.platform_listing_id,
            normalized_value=raw_value if isinstance(raw_value, (int, float)) else None,
            metadata={"steam_app_id": app_id, "job_id": source_ref},
        )
        for signal_type, raw_value in signal_values
        if raw_value is not None
    )
    return CanonicalRecords(
        game_entities=(entity,),
        platform_listings=(listing,),
        events=(event,),
        signals=canonical_signals,
    )

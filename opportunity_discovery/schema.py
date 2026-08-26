"""Small JSON-safe canonical objects for Game x Event discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

from .ids import (
    make_event_id,
    make_game_entity_id,
    make_platform_listing_id,
    make_signal_id,
    normalize_aliases,
)


class SchemaValidationError(ValueError):
    """Raised when a canonical object cannot satisfy the contract."""


class Platform(str, Enum):
    STEAM = "STEAM"
    NINTENDO_SWITCH = "NINTENDO_SWITCH"
    NINTENDO_SWITCH_2 = "NINTENDO_SWITCH_2"
    XBOX = "XBOX"
    PLAYSTATION = "PLAYSTATION"
    ROBLOX = "ROBLOX"
    IOS = "IOS"
    ANDROID = "ANDROID"
    OTHER = "OTHER"


class EventType(str, Enum):
    NEW_RELEASE = "NEW_RELEASE"
    EARLY_ACCESS = "EARLY_ACCESS"
    DEMO = "DEMO"
    PORT = "PORT"
    DLC = "DLC"
    EXPANSION = "EXPANSION"
    MAJOR_UPDATE = "MAJOR_UPDATE"
    VERSION_1_0 = "VERSION_1_0"
    GAME_PASS = "GAME_PASS"
    PS_PLUS = "PS_PLUS"
    CROSSPLAY = "CROSSPLAY"
    FREE_WEEKEND = "FREE_WEEKEND"
    RESURGENCE = "RESURGENCE"
    OTHER = "OTHER"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaValidationError(f"{field_name} is required")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _iso_datetime(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise SchemaValidationError(f"{field_name} is required")
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise SchemaValidationError(f"{field_name} must be an ISO 8601 string, date, or datetime")


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise SchemaValidationError(f"invalid {field_name}; expected one of: {allowed}") from exc


def _json_safe(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_safe(item, field_name) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item, field_name) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise SchemaValidationError(f"{field_name} keys must be strings")
        return {key: _json_safe(item, field_name) for key, item in value.items()}
    raise SchemaValidationError(f"{field_name} must contain JSON-safe values")


def _text_tuple(values: Any, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise SchemaValidationError(f"{field_name} must be a list of strings")
    try:
        result = tuple(_required_text(value, field_name) for value in values)
    except TypeError as exc:
        raise SchemaValidationError(f"{field_name} must be a list of strings") from exc
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class GameEntity:
    """The game itself, independent of any store or platform metrics."""

    game_entity_id: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    developer: str | None = None
    publisher: str | None = None
    genres: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_entity_id", _required_text(self.game_entity_id, "game_entity_id"))
        object.__setattr__(self, "canonical_name", _required_text(self.canonical_name, "canonical_name"))
        object.__setattr__(self, "aliases", normalize_aliases(self.aliases))
        object.__setattr__(self, "developer", _optional_text(self.developer, "developer"))
        object.__setattr__(self, "publisher", _optional_text(self.publisher, "publisher"))
        object.__setattr__(self, "genres", _text_tuple(self.genres, "genres"))

    @classmethod
    def create(cls, canonical_name: str, **kwargs: Any) -> "GameEntity":
        return cls(game_entity_id=make_game_entity_id(canonical_name), canonical_name=canonical_name, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_entity_id": self.game_entity_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "developer": self.developer,
            "publisher": self.publisher,
            "genres": list(self.genres),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameEntity":
        return cls(**dict(data))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PlatformListing:
    platform_listing_id: str
    game_entity_id: str
    platform: Platform
    platform_game_id: str
    store_url: str | None = None
    release_date: str | date | datetime | None = None
    region: str | None = None
    listing_status: str = "ACTIVE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform_listing_id", _required_text(self.platform_listing_id, "platform_listing_id"))
        object.__setattr__(self, "game_entity_id", _required_text(self.game_entity_id, "game_entity_id"))
        object.__setattr__(self, "platform", _enum(self.platform, Platform, "platform"))
        object.__setattr__(self, "platform_game_id", _required_text(self.platform_game_id, "platform_game_id"))
        object.__setattr__(self, "store_url", _optional_text(self.store_url, "store_url"))
        object.__setattr__(self, "release_date", _iso_datetime(self.release_date, "release_date", required=False))
        object.__setattr__(self, "region", _optional_text(self.region, "region"))
        object.__setattr__(self, "listing_status", _required_text(self.listing_status, "listing_status"))

    @classmethod
    def create(cls, game_entity_id: str, platform: Platform, platform_game_id: str, **kwargs: Any) -> "PlatformListing":
        return cls(
            platform_listing_id=make_platform_listing_id(game_entity_id, platform, platform_game_id),
            game_entity_id=game_entity_id,
            platform=platform,
            platform_game_id=platform_game_id,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_listing_id": self.platform_listing_id,
            "game_entity_id": self.game_entity_id,
            "platform": self.platform.value,
            "platform_game_id": self.platform_game_id,
            "store_url": self.store_url,
            "release_date": self.release_date,
            "region": self.region,
            "listing_status": self.listing_status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlatformListing":
        return cls(**dict(data))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class GameEvent:
    event_id: str
    game_entity_id: str
    event_type: EventType
    event_date: str | date | datetime
    source: str
    source_ref: str
    platform_listing_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(self, "game_entity_id", _required_text(self.game_entity_id, "game_entity_id"))
        object.__setattr__(self, "event_type", _enum(self.event_type, EventType, "event_type"))
        object.__setattr__(self, "event_date", _iso_datetime(self.event_date, "event_date"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "source_ref", _required_text(self.source_ref, "source_ref"))
        object.__setattr__(self, "platform_listing_id", _optional_text(self.platform_listing_id, "platform_listing_id"))

    @classmethod
    def create(
        cls,
        game_entity_id: str,
        event_type: EventType,
        event_date: str | date | datetime,
        source: str,
        source_ref: str,
        platform_listing_id: str | None = None,
    ) -> "GameEvent":
        return cls(
            event_id=make_event_id(game_entity_id, event_type, event_date, source, source_ref, platform_listing_id),
            game_entity_id=game_entity_id,
            event_type=event_type,
            event_date=event_date,
            source=source,
            source_ref=source_ref,
            platform_listing_id=platform_listing_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "game_entity_id": self.game_entity_id,
            "platform_listing_id": self.platform_listing_id,
            "event_type": self.event_type.value,
            "event_date": self.event_date,
            "source": self.source,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameEvent":
        return cls(**dict(data))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CandidateSignal:
    signal_id: str
    game_entity_id: str
    observed_at: str | date | datetime
    source: str
    signal_type: str
    raw_value: Any
    event_id: str | None = None
    platform_listing_id: str | None = None
    normalized_value: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _required_text(self.signal_id, "signal_id"))
        object.__setattr__(self, "game_entity_id", _required_text(self.game_entity_id, "game_entity_id"))
        object.__setattr__(self, "event_id", _optional_text(self.event_id, "event_id"))
        object.__setattr__(self, "platform_listing_id", _optional_text(self.platform_listing_id, "platform_listing_id"))
        object.__setattr__(self, "observed_at", _iso_datetime(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "signal_type", _required_text(self.signal_type, "signal_type"))
        object.__setattr__(self, "raw_value", _json_safe(self.raw_value, "raw_value"))
        object.__setattr__(self, "normalized_value", _json_safe(self.normalized_value, "normalized_value"))
        if not isinstance(self.metadata, Mapping):
            raise SchemaValidationError("metadata must be an object")
        object.__setattr__(self, "metadata", _json_safe(dict(self.metadata), "metadata"))

    @classmethod
    def create(
        cls,
        game_entity_id: str,
        observed_at: str | date | datetime,
        source: str,
        signal_type: str,
        raw_value: Any,
        event_id: str | None = None,
        platform_listing_id: str | None = None,
        normalized_value: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "CandidateSignal":
        return cls(
            signal_id=make_signal_id(
                game_entity_id,
                observed_at,
                source,
                signal_type,
                raw_value,
                event_id,
                platform_listing_id,
            ),
            game_entity_id=game_entity_id,
            event_id=event_id,
            platform_listing_id=platform_listing_id,
            observed_at=observed_at,
            source=source,
            signal_type=signal_type,
            raw_value=raw_value,
            normalized_value=normalized_value,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "game_entity_id": self.game_entity_id,
            "event_id": self.event_id,
            "platform_listing_id": self.platform_listing_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "signal_type": self.signal_type,
            "raw_value": self.raw_value,
            "normalized_value": self.normalized_value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateSignal":
        return cls(**dict(data))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

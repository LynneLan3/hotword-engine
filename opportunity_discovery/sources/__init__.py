"""Source adapters used by shadow discovery experiments."""

from .steam import (
    POPULAR_NEW_RELEASES,
    POPULAR_UPCOMING,
    SteamDiscoveryObservation,
    SteamPageResult,
    SteamShadowAdapter,
    build_steam_page_url,
    parse_steam_search_results,
)
from .twitch import (
    TwitchRawObservation,
    TwitchTopGamesAdapter,
    append_twitch_run,
    write_twitch_artifact,
)

__all__ = [
    "POPULAR_NEW_RELEASES",
    "POPULAR_UPCOMING",
    "SteamDiscoveryObservation",
    "SteamPageResult",
    "SteamShadowAdapter",
    "build_steam_page_url",
    "parse_steam_search_results",
    "TwitchRawObservation",
    "TwitchTopGamesAdapter",
    "append_twitch_run",
    "write_twitch_artifact",
]

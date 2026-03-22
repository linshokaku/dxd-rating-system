from __future__ import annotations

from dxd_rating.shared.constants import is_dummy_discord_user_id


def build_dummy_player_display_name(discord_user_id: int) -> str:
    return f"<dummy_{discord_user_id}>"


def format_player_display_name(*, discord_user_id: int, display_name: str | None) -> str:
    if display_name is not None:
        return display_name
    return str(discord_user_id)


def resolve_player_display_name(
    *,
    discord_user_id: int,
    guild_display_name: str | None = None,
    global_display_name: str | None = None,
    username: str | None = None,
) -> str | None:
    if is_dummy_discord_user_id(discord_user_id):
        return build_dummy_player_display_name(discord_user_id)

    for candidate in (guild_display_name, global_display_name, username):
        normalized_candidate = _normalize_display_name(candidate)
        if normalized_candidate is not None:
            return normalized_candidate

    return None


def resolve_registered_display_name(
    *,
    discord_user_id: int,
    display_name: str | None,
) -> str | None:
    if display_name is not None:
        return display_name
    if is_dummy_discord_user_id(discord_user_id):
        return build_dummy_player_display_name(discord_user_id)
    return None


def _normalize_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    normalized_value = value.strip()
    if normalized_value == "":
        return None
    return normalized_value

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

MAX_PUBLIC_EMBED_DESCRIPTION_LENGTH = 4096
PUBLIC_MESSAGE_ALLOWED_MENTIONS = discord.AllowedMentions(
    users=True,
    roles=False,
    everyone=False,
    replied_user=False,
)


@dataclass(frozen=True, slots=True)
class PublicMessagePayload:
    content: str | None
    embed: discord.Embed


def build_public_message_payload(
    notification_content: str | None,
    embed_body: str,
) -> PublicMessagePayload:
    return PublicMessagePayload(
        content=_normalize_message_content(notification_content),
        embed=discord.Embed(description=embed_body),
    )


def build_body_only_public_message_send_kwargs(
    embed_body: str,
    *,
    allowed_mentions: discord.AllowedMentions | None = PUBLIC_MESSAGE_ALLOWED_MENTIONS,
    view: discord.ui.View | None = None,
    suppress_embeds_for_fallback: bool | None = None,
) -> dict[str, object]:
    return build_public_message_send_kwargs(
        notification_content=None,
        embed_body=embed_body,
        allowed_mentions=allowed_mentions,
        view=view,
        suppress_embeds_for_fallback=suppress_embeds_for_fallback,
    )


def build_public_message_send_kwargs(
    notification_content: str | None,
    embed_body: str,
    *,
    allowed_mentions: discord.AllowedMentions | None = PUBLIC_MESSAGE_ALLOWED_MENTIONS,
    view: discord.ui.View | None = None,
    suppress_embeds_for_fallback: bool | None = None,
) -> dict[str, object]:
    kwargs: dict[str, object]
    if _should_fallback_to_plain_text(embed_body):
        kwargs = {
            "content": build_public_message_fallback_text(notification_content, embed_body),
        }
        if suppress_embeds_for_fallback is not None:
            kwargs["suppress_embeds"] = suppress_embeds_for_fallback
    else:
        payload = build_public_message_payload(notification_content, embed_body)
        kwargs = {
            "content": payload.content,
            "embed": payload.embed,
        }

    if allowed_mentions is not None:
        kwargs["allowed_mentions"] = allowed_mentions
    if view is not None:
        kwargs["view"] = view
    return kwargs


def build_body_only_public_message_edit_kwargs(
    embed_body: str,
    *,
    suppress_embeds_for_fallback: bool | None = None,
) -> dict[str, Any]:
    return build_public_message_edit_kwargs(
        notification_content=None,
        embed_body=embed_body,
        suppress_embeds_for_fallback=suppress_embeds_for_fallback,
    )


def build_public_message_edit_kwargs(
    notification_content: str | None,
    embed_body: str,
    *,
    suppress_embeds_for_fallback: bool | None = None,
) -> dict[str, Any]:
    if _should_fallback_to_plain_text(embed_body):
        kwargs: dict[str, Any] = {
            "content": build_public_message_fallback_text(notification_content, embed_body),
            "embed": None,
        }
        if suppress_embeds_for_fallback is not None:
            kwargs["suppress_embeds"] = suppress_embeds_for_fallback
        return kwargs

    payload = build_public_message_payload(notification_content, embed_body)
    return {
        "content": payload.content,
        "embed": payload.embed,
    }


def build_public_message_fallback_text(
    notification_content: str | None,
    embed_body: str,
) -> str:
    normalized_content = _normalize_message_content(notification_content)
    if normalized_content is None:
        return embed_body
    if embed_body == "":
        return normalized_content
    return "\n".join((normalized_content, embed_body))


def _should_fallback_to_plain_text(embed_body: str) -> bool:
    return len(embed_body) > MAX_PUBLIC_EMBED_DESCRIPTION_LENGTH


def _normalize_message_content(content: str | None) -> str | None:
    if content is None:
        return None
    if content.strip() == "":
        return None
    return content

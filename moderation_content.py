"""Extract content supplied by the sender without scanning an entire reply chain."""

import re
from dataclasses import dataclass

from aiogram.types import Message


LINK_RE = re.compile(r"(?:https?://|tg://|www\.|(?:t|telegram)\.me/)", re.IGNORECASE)
MEDIA_TYPES = {"photo", "video", "audio", "animation", "voice_note", "document", "map"}
CONTENT_FIELDS = {
    "text", "caption", "credit", "summary", "expression", "alternative_text", "label",
    "blocks", "items", "cells", "button", "buttons", "copy_text", "web_app", "login_url",
}


@dataclass
class ModerationContent:
    text: str = ""
    has_link: bool = False
    has_mention: bool = False
    has_media: bool = False


def inspect_content(message: Message) -> ModerationContent:
    content = ModerationContent()

    def scan_rich(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            # Formatting can split a word or URL into multiple inline fragments.
            return "".join(scan_rich(item) for item in value)
        if not isinstance(value, dict):
            return ""
        kind = value.get("type")
        if kind in MEDIA_TYPES:
            content.has_media = True
        if kind in {"mention", "text_mention"}:
            content.has_mention = True
        parts = []
        if value.get("url"):
            content.has_link = True
            parts.append(value["url"])
        for key, child in value.items():
            if key in CONTENT_FIELDS:
                if key in {"blocks", "items", "cells", "buttons"} and isinstance(child, list):
                    parts.append("\n".join(scan_rich(item) for item in child))
                else:
                    parts.append(scan_rich(child))
        return "\n".join(part for part in parts if part)

    parts = [message.text or "", message.caption or ""]
    entities = [*(message.entities or []), *(message.caption_entities or [])]
    if message.quote:
        parts.append(message.quote.text)
        entities.extend(message.quote.entities or [])
    for entity in entities:
        if entity.type in {"url", "text_link"}:
            content.has_link = True
            if entity.url:
                parts.append(entity.url)
        if entity.type in {"mention", "text_mention"}:
            content.has_mention = True
    if message.rich_message:
        parts.append(scan_rich(message.rich_message.model_dump(exclude_none=True)))
    content.text = "\n".join(part for part in parts if part)
    content.has_link = content.has_link or bool(LINK_RE.search(content.text))
    content.has_mention = content.has_mention or "@" in content.text
    content.has_media = content.has_media or any((
        message.photo, message.video, message.document, message.animation,
        message.voice, message.video_note, message.story, message.audio,
        message.live_photo, message.paid_media,
    ))
    return content


def has_untrusted_source(message: Message, trusted_chat_ids: set[int]) -> bool:
    """Unknown/hidden origins are untrusted; ordinary local replies have no origin."""
    external = message.external_reply
    if external:
        # chat is the location of the replied-to message, not its forward origin.
        if external.chat:
            if external.chat.id not in trusted_chat_ids:
                return True
        else:
            origin_chat = (getattr(external.origin, "chat", None)
                           or getattr(external.origin, "sender_chat", None))
            if not origin_chat or origin_chat.id not in trusted_chat_ids:
                return True
    if message.forward_origin:
        origin_chat = (getattr(message.forward_origin, "chat", None)
                       or getattr(message.forward_origin, "sender_chat", None))
        if not origin_chat or origin_chat.id not in trusted_chat_ids:
            return True
    return False

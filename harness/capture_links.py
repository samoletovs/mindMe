"""Translate Telegram link entities into the plain-text capture wire format."""

from __future__ import annotations

from urllib.parse import urlsplit


def normalize_message_links(message: dict) -> dict:
    """Preserve commentary and message identity while making link targets explicit."""
    field = "text" if message.get("text") else "caption"
    text = message.get(field, "")
    if not isinstance(text, str):
        raise ValueError("invalid_capture_text")
    entity_field = "entities" if field == "text" else "caption_entities"
    entities = message.get(entity_field, [])
    if not isinstance(entities, list):
        raise ValueError("invalid_capture_entities")

    # Telegram offsets count UTF-16 code units, not Python Unicode characters.
    encoded = text.encode("utf-16-le")
    replacements: list[tuple[int, int, str]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError("invalid_capture_entity")
        if entity.get("type") not in ("url", "text_link"):
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if (
            type(offset) is not int or type(length) is not int
            or offset < 0 or length <= 0 or (offset + length) * 2 > len(encoded)
        ):
            raise ValueError("invalid_capture_entity_range")
        start, end = offset * 2, (offset + length) * 2
        label = encoded[start:end].decode("utf-16-le")
        # Validate that neither boundary splits a surrogate pair.
        encoded[:start].decode("utf-16-le")
        encoded[end:].decode("utf-16-le")
        target = entity.get("url") if entity["type"] == "text_link" else label
        if not isinstance(target, str) or not target:
            raise ValueError("invalid_capture_link")
        if entity["type"] == "url" and "://" not in target:
            target = "https://" + target
        parsed = urlsplit(target)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        if (
            not parsed.hostname or any(c.isspace() or ord(c) < 32 for c in target)
            or "\\" in target
        ):
            raise ValueError("invalid_capture_link")
        replacements.append((start, end, target))

    replacements.sort()
    if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:])):
        raise ValueError("overlapping_capture_links")
    for start, end, target in reversed(replacements):
        encoded = encoded[:start] + target.encode("utf-16-le") + encoded[end:]
    normalized = encoded.decode("utf-16-le")
    if normalized == text:
        return message
    # Offsets no longer describe the rewritten text; never forward stale entities.
    return {key: value for key, value in message.items() if key != entity_field} | {
        field: normalized,
    }

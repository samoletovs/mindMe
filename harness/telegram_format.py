"""Small, escaped Telegram HTML fragments shared by daily and weekly briefings."""

from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlsplit

MAX_LINK = 400


def escape(value: str) -> str:
    return html.escape(value, quote=False)


def units(value: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def escaped_chunks(value: str, limit: int = 3000) -> list[str]:
    """Split untrusted text without cutting an escaped entity or a Unicode character."""
    if limit < 6:
        raise ValueError("escaped_chunk_limit_too_small")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in value:
        encoded = escape(char)
        width = units(encoded)
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(encoded)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


def pack_html_blocks(blocks: list[str], limit: int = 3900) -> list[str]:
    """Pack complete HTML blocks; callers must close all tags within each block."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        if units(block) > limit:
            raise ValueError("telegram_html_block_too_long")
        combined = current + "\n\n" + block if current else block
        if units(combined) > limit:
            messages.append(current)
            current = block
        else:
            current = combined
    if current:
        messages.append(current)
    return messages


def word_count(value: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def excerpt(value: object, limit: int) -> tuple[str, bool]:
    text = value.strip() if isinstance(value, str) else ""
    escaped = escape(text)
    if units(escaped) <= limit:
        return escaped, False
    chars: list[str] = []
    length = 0
    for char in text:
        part = escape(char)
        if length + units(part) > limit - 1:
            break
        chars.append(part)
        length += units(part)
    return "".join(chars).rstrip() + "\u2026", True


def safe_url(value: object) -> bool:
    if not isinstance(value, str) or units(value) > MAX_LINK:
        return False
    try:
        parsed = urlsplit(value)
        decoded = unquote(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password
            and not parsed.query
            and not any(char.isspace() or ord(char) < 32 or char in '<>"\\' for char in decoded)
        )
    except ValueError:
        return False


def github_link(value: object, label: str = "Source") -> str:
    if not isinstance(value, str) or not safe_url(value):
        return ""
    parsed = urlsplit(value)
    parts = parsed.path.split("/")
    if (
        parsed.netloc != "github.com" or parsed.fragment or len(parts) < 5
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1])
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[2])
        or parts[3] not in {"blob", "tree", "issues", "pull", "commit"}
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        return ""
    link = f'<a href="{html.escape(value, quote=True)}">{escape(label)}</a>'
    return link if units(link) <= MAX_LINK else ""


def inline_text(value: str) -> str:
    """Convert source Markdown links, never source HTML, into safe named links."""
    parts: list[str] = []
    end = 0
    for match in re.finditer(r"\[([^\]\n]+)\]\(([^)\s]+)\)", value):
        parts.append(escape(value[end:match.start()]))
        label, url = match.groups()
        parts.append(
            f'<a href="{html.escape(url, quote=True)}">{escape(label)}</a>'
            if safe_url(url) else escape(label) + " (link unavailable)"
        )
        end = match.end()
    parts.append(escape(value[end:]))
    return "".join(parts)

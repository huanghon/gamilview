from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in payload.get("headers", []):
        name = item.get("name", "")
        value = item.get("value", "")
        if name:
            result[name.lower()] = value
    return result


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [payload]
    for child in payload.get("parts", []) or []:
        parts.extend(_walk_parts(child))
    return parts


def _html_to_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _normalize_date(value: str) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def parse_gmail_message(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload", {})
    headers = _headers(payload)
    text_chunks: list[str] = []
    html_chunks: list[str] = []

    for part in _walk_parts(payload):
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        decoded = _decode_body(body.get("data"))
        if not decoded:
            continue
        if mime_type == "text/plain":
            text_chunks.append(decoded.strip())
        elif mime_type == "text/html":
            html_chunks.append(decoded.strip())

    body_html = "\n".join(chunk for chunk in html_chunks if chunk)
    body_text = "\n".join(chunk for chunk in text_chunks if chunk)
    if not body_text and body_html:
        body_text = _html_to_text(body_html)

    return {
        "message_id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "subject": headers.get("subject", ""),
        "sender": headers.get("from", ""),
        "received_at": _normalize_date(headers.get("date", "")),
        "body_text": body_text,
        "body_html": body_html,
        "snippet": message.get("snippet", ""),
    }

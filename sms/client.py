from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from gmail.code_extractor import extract_verification_code

from .time_parser import parse_kr_received_time

DEFAULT_SMS_SCRIPT_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbySwklPb6Cupzz8SayfcVAwZvho3Cl5CGBAGySDZeacfOUvQsH5tQSifQHy3QcFvLBEvw/exec"
)

# Korean column headers from the Apps Script HTML table
COL_RECEIVED = "받은시간"
COL_PHONE = "전화번호"
COL_BODY = "인증번호"
COL_SIM = "SIM"


class SmsClientError(RuntimeError):
    """Raised when the SMS Apps Script source cannot be read or parsed."""


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "url": "",
    "fetched_at": 0.0,
    "records": [],
}


def clear_cache() -> None:
    with _cache_lock:
        _cache["url"] = ""
        _cache["fetched_at"] = 0.0
        _cache["records"] = []


def _decode_goog_script_payload(html: str) -> dict[str, Any]:
    match = re.search(
        r'goog\.script\.init\("((?:\\.|[^"\\])*)"',
        html,
        re.S,
    )
    if not match:
        raise SmsClientError("Could not find embedded userHtml data")

    js_string = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        r"\\u00\1",
        match.group(1),
    )
    try:
        config = json.loads(json.loads('"' + js_string + '"'))
    except json.JSONDecodeError as exc:
        raise SmsClientError(f"Failed to decode Apps Script payload: {exc}") from exc
    if not isinstance(config, dict):
        raise SmsClientError("Apps Script payload is not an object")
    return config


def extract_records_from_html(html: str, *, tz_name: str = "Asia/Seoul") -> list[dict[str, Any]]:
    """Parse the Apps Script HTML page into normalized SMS records."""
    config = _decode_goog_script_payload(html)
    user_html = config.get("userHtml")
    if not user_html:
        raise SmsClientError("Apps Script payload missing userHtml")

    soup = BeautifulSoup(str(user_html), "html.parser")
    table = soup.find("table")
    if table is None:
        raise SmsClientError("Could not find the embedded table")

    table_rows = table.find_all("tr")
    if not table_rows:
        return []

    headers = [
        cell.get_text(" ", strip=True)
        for cell in table_rows[0].find_all(["th", "td"])
    ]
    records: list[dict[str, Any]] = []
    now = datetime.now(ZoneInfo(tz_name))

    for row_index, row in enumerate(table_rows[1:]):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"])
        ]
        if len(cells) != len(headers):
            continue
        raw = dict(zip(headers, cells))
        phone = str(raw.get(COL_PHONE, "")).strip()
        body = str(raw.get(COL_BODY, "")).strip()
        sim = str(raw.get(COL_SIM, "")).strip()
        received_raw = str(raw.get(COL_RECEIVED, "")).strip()
        if not phone or not body:
            continue

        try:
            received_at = parse_kr_received_time(received_raw, now=now, tz_name=tz_name)
        except ValueError:
            # Keep unparseable rows out of ranking rather than failing the whole fetch.
            continue

        records.append(
            {
                "phone": phone,
                "body": body,
                "sim": sim,
                "received_at": received_at,
                "received_at_raw": received_raw,
                "received_at_iso": received_at.isoformat(),
                "row_index": row_index,
            }
        )

    return records


def fetch_records(
    *,
    url: str = DEFAULT_SMS_SCRIPT_URL,
    timeout: float = 30.0,
    cache_ttl_seconds: int = 15,
    tz_name: str = "Asia/Seoul",
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and cache normalized SMS records from the Apps Script endpoint."""
    url = (url or DEFAULT_SMS_SCRIPT_URL).strip()
    ttl = max(0, int(cache_ttl_seconds or 0))
    now_ts = time.monotonic()

    with _cache_lock:
        if (
            not force_refresh
            and _cache["url"] == url
            and _cache["records"]
            and ttl > 0
            and (now_ts - float(_cache["fetched_at"])) < ttl
        ):
            return list(_cache["records"])

    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SmsClientError(f"Failed to fetch SMS source: {exc}") from exc

    records = extract_records_from_html(response.text, tz_name=tz_name)

    with _cache_lock:
        _cache["url"] = url
        _cache["fetched_at"] = time.monotonic()
        _cache["records"] = records

    return list(records)


def fetch_latest_for_phone(
    phone: str,
    *,
    window_seconds: int = 0,
    url: str = DEFAULT_SMS_SCRIPT_URL,
    timeout: float = 30.0,
    cache_ttl_seconds: int = 15,
    tz_name: str = "Asia/Seoul",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the latest SMS verification code for an exact phone match."""
    phone = (phone or "").strip()
    if not phone:
        raise SmsClientError("Phone is required")

    records = fetch_records(
        url=url,
        timeout=timeout,
        cache_ttl_seconds=cache_ttl_seconds,
        tz_name=tz_name,
    )
    matched = [item for item in records if item["phone"] == phone]
    if not matched:
        raise LookupError(f"No SMS found for {phone}")

    matched.sort(
        key=lambda item: (item["received_at"], item.get("row_index", 0)),
        reverse=True,
    )

    tz = ZoneInfo(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    window = int(window_seconds or 0)
    if window > 0:
        cutoff = current - timedelta(seconds=window)
        matched = [item for item in matched if item["received_at"] >= cutoff]
        if not matched:
            raise LookupError(f"No SMS found for {phone} within last {window}s")

    latest = matched[0]
    code = extract_verification_code(str(latest["body"]))
    if not code:
        raise LookupError("Code not found")

    return {
        "phone": phone,
        "source": "sms",
        "sim": latest.get("sim") or "",
        "received_at": latest["received_at_iso"],
        "received_at_raw": latest.get("received_at_raw") or "",
        "code": code,
        "body": latest.get("body") or "",
    }

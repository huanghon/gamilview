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
    "AKfycbzcqdDdQdma6ikRFDSJFPuPALPcwmGoSwuIKr8q1KUYyKynqAUi_9t0ARVPl2EKWod3/exec"
)

# Column aliases: old headers + new headers + common English variants.
COLS_RECEIVED = (
    "시간",
    "받은시간",
    "수신시간",
    "시간/날짜",
    "날짜",
    "수신",
    "Time",
    "Received",
    "Received At",
    "received_at",
    "Date",
    "Datetime",
)
COLS_PHONE = (
    "전화번호",
    "번호",
    "휴대폰",
    "핸드폰",
    "전화",
    "Phone",
    "Phone Number",
    "Tel",
    "Mobile",
    "phone",
)
COLS_BODY = (
    "문자내용",
    "인증번호",
    "내용",
    "메시지",
    "메세지",
    "문자",
    "본문",
    "Message",
    "Body",
    "Content",
    "SMS",
    "Text",
)
COLS_SIM = (
    "SIM",
    "sim",
    "유심",
    "이름",
    "성명",
    "Name",
    "Label",
    "Owner",
)


class SmsClientError(RuntimeError):
    """Raised when the SMS Apps Script source cannot be read or parsed."""


_cache_lock = threading.Lock()
# Per-URL cache so multiple sources do not overwrite each other.
_cache_by_url: dict[str, dict[str, Any]] = {}


def clear_cache(url: str | None = None) -> None:
    with _cache_lock:
        if url is None:
            _cache_by_url.clear()
            return
        _cache_by_url.pop((url or "").strip(), None)


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


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _header_matches(header: str, aliases: tuple[str, ...]) -> bool:
    h = _normalize_header(header)
    if not h:
        return False
    for alias in aliases:
        a = _normalize_header(alias)
        if not a:
            continue
        if h == a or a in h or h in a:
            return True
    return False


def _pick_col(raw: dict[str, Any], names: tuple[str, ...]) -> str:
    """Return the first non-empty cell among candidate header names / fuzzy matches."""
    for name in names:
        if name in raw:
            value = str(raw[name]).strip()
            if value:
                return value
    for key, cell in raw.items():
        if _header_matches(str(key), names):
            value = str(cell).strip()
            if value:
                return value
    return ""


def map_headers(headers: list[str]) -> dict[str, str | None]:
    """Map logical fields to the first matching real table header."""
    mapping: dict[str, str | None] = {
        "received": None,
        "phone": None,
        "body": None,
        "sim": None,
    }
    alias_groups = {
        "received": COLS_RECEIVED,
        "phone": COLS_PHONE,
        "body": COLS_BODY,
        "sim": COLS_SIM,
    }
    used: set[str] = set()
    for field, aliases in alias_groups.items():
        for header in headers:
            if header in used:
                continue
            if _header_matches(header, aliases):
                mapping[field] = header
                used.add(header)
                break
    return mapping


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
        phone = _pick_col(raw, COLS_PHONE)
        body = _pick_col(raw, COLS_BODY)
        sim = _pick_col(raw, COLS_SIM)
        received_raw = _pick_col(raw, COLS_RECEIVED)
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


def inspect_html(html: str, *, tz_name: str = "Asia/Seoul") -> dict[str, Any]:
    """Inspect table headers and row count without raising on empty tables."""
    config = _decode_goog_script_payload(html)
    user_html = config.get("userHtml")
    if not user_html:
        raise SmsClientError("Apps Script payload missing userHtml")

    soup = BeautifulSoup(str(user_html), "html.parser")
    table = soup.find("table")
    if table is None:
        raise SmsClientError("Could not find the embedded table")

    table_rows = table.find_all("tr")
    headers = [
        cell.get_text(" ", strip=True)
        for cell in table_rows[0].find_all(["th", "td"])
    ] if table_rows else []
    mapping = map_headers(headers)
    records = extract_records_from_html(html, tz_name=tz_name)
    return {
        "headers": headers,
        "mapped_headers": mapping,
        "record_count": len(records),
        "ok": bool(mapping.get("phone") and mapping.get("body")),
    }


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
    if not url:
        raise SmsClientError("SMS source URL is required")
    ttl = max(0, int(cache_ttl_seconds or 0))
    now_ts = time.monotonic()

    with _cache_lock:
        cached = _cache_by_url.get(url)
        if (
            not force_refresh
            and cached
            and cached.get("records") is not None
            and ttl > 0
            and (now_ts - float(cached.get("fetched_at") or 0)) < ttl
        ):
            return list(cached["records"])

    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SmsClientError(f"Failed to fetch SMS source: {exc}") from exc

    records = extract_records_from_html(response.text, tz_name=tz_name)

    with _cache_lock:
        _cache_by_url[url] = {
            "fetched_at": time.monotonic(),
            "records": records,
        }

    return list(records)


def inspect_source(
    *,
    url: str,
    timeout: float = 30.0,
    tz_name: str = "Asia/Seoul",
) -> dict[str, Any]:
    url = (url or "").strip()
    if not url:
        raise SmsClientError("SMS source URL is required")
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SmsClientError(f"Failed to fetch SMS source: {exc}") from exc
    info = inspect_html(response.text, tz_name=tz_name)
    info["url"] = url
    return info


def _pick_latest_match(
    phone: str,
    records: list[dict[str, Any]],
    *,
    window_seconds: int = 0,
    tz_name: str = "Asia/Seoul",
    now: datetime | None = None,
) -> dict[str, Any]:
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
        "source_name": latest.get("source_name") or "",
        "source_url": latest.get("source_url") or "",
    }


def fetch_latest_for_phone(
    phone: str,
    *,
    window_seconds: int = 0,
    url: str = DEFAULT_SMS_SCRIPT_URL,
    timeout: float = 30.0,
    cache_ttl_seconds: int = 15,
    tz_name: str = "Asia/Seoul",
    now: datetime | None = None,
    source_name: str = "",
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
    tagged = []
    for item in records:
        row = dict(item)
        row["source_name"] = source_name
        row["source_url"] = url
        tagged.append(row)
    return _pick_latest_match(
        phone,
        tagged,
        window_seconds=window_seconds,
        tz_name=tz_name,
        now=now,
    )


def fetch_latest_for_phone_multi(
    phone: str,
    sources: list[dict[str, Any]],
    *,
    window_seconds: int = 0,
    timeout: float = 30.0,
    cache_ttl_seconds: int = 15,
    tz_name: str = "Asia/Seoul",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Query multiple SMS pages and return the newest matching verification code."""
    phone = (phone or "").strip()
    if not phone:
        raise SmsClientError("Phone is required")
    if not sources:
        raise SmsClientError("No SMS sources configured")

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []

    for source in sources:
        url = str(source.get("url") or "").strip()
        name = str(source.get("name") or "").strip()
        if not url:
            continue
        try:
            records = fetch_records(
                url=url,
                timeout=timeout,
                cache_ttl_seconds=cache_ttl_seconds,
                tz_name=tz_name,
            )
        except SmsClientError as exc:
            errors.append(f"{name or url}: {exc}")
            continue
        for item in records:
            if item["phone"] != phone:
                continue
            row = dict(item)
            row["source_name"] = name
            row["source_url"] = url
            candidates.append(row)

    if not candidates:
        if errors:
            raise SmsClientError("; ".join(errors))
        raise LookupError(f"No SMS found for {phone}")

    try:
        result = _pick_latest_match(
            phone,
            candidates,
            window_seconds=window_seconds,
            tz_name=tz_name,
            now=now,
        )
    except LookupError:
        if errors:
            raise LookupError(
                f"No SMS found for {phone}; some sources failed: {'; '.join(errors)}"
            ) from None
        raise

    result["queried_sources"] = len(sources)
    result["source_errors"] = errors
    return result

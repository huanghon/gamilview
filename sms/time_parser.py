from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# 07. 14. 오후 3:37 / 07. 14. 오전 9:05 / 7.14. 오후 3:37
_KR_TIME_RE = re.compile(
    r"(?P<month>\d{1,2})\s*\.\s*(?P<day>\d{1,2})\s*\.\s*"
    r"(?P<ampm>오전|오후)\s*"
    r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{2})"
)


def parse_kr_received_time(
    text: str,
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Seoul",
) -> datetime:
    """Parse Korean Apps Script table time into timezone-aware datetime.

    Input examples:
      - 07. 14. 오후 3:37
      - 07. 14. 오전 12:05
    Year is inferred from *now* (default current time in the given timezone).
    If the resulting local date is more than 1 day in the future, roll back one year
    to handle Dec/Jan cross-year cases.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty Korean time text")

    match = _KR_TIME_RE.search(raw)
    if not match:
        raise ValueError(f"unsupported Korean time format: {raw!r}")

    tz = ZoneInfo(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)

    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = match.group("ampm")

    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"invalid month/day in Korean time: {raw!r}")
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        raise ValueError(f"invalid hour/minute in Korean time: {raw!r}")

    if ampm == "오후":
        if hour != 12:
            hour += 12
    else:  # 오전
        if hour == 12:
            hour = 0

    year = current.year
    try:
        result = datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError as exc:
        raise ValueError(f"invalid Korean datetime: {raw!r}") from exc

    # Cross-year: data from Dec viewed in Jan next year.
    if result > current + timedelta(days=1):
        try:
            result = result.replace(year=year - 1)
        except ValueError:
            # e.g. Feb 29 on non-leap previous year — keep original year
            pass

    return result

from __future__ import annotations

import re


KEYWORD_PATTERN = (
    r"(?:"
    r"인증번호|"
    r"验证码|驗證碼|"
    r"verification\s*code|"
    r"otp|"
    r"code"
    r")"
)

_FLAGS = re.IGNORECASE | re.DOTALL


def _search_first(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, _FLAGS)
        if match:
            return match.group(1)
    return None


def _pick_best_candidate(text: str, patterns: list[str]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for pattern in patterns:
        for index, match in enumerate(re.finditer(pattern, text, _FLAGS)):
            candidates.append((index, match.group(1)))
    if not candidates:
        return None
    _, best = min(
        candidates,
        key=lambda item: (abs(len(item[1]) - 6), 0 if len(item[1]) == 6 else 1, item[0]),
    )
    return best


def extract_verification_code(text: str) -> str | None:
    if not text:
        return None

    value = str(text)

    keyword_patterns = [
        rf"{KEYWORD_PATTERN}(?:[^\d\[]{{0,20}})\[\s*(\d{{6}})\s*\]",
        rf"{KEYWORD_PATTERN}(?:[^\d\[]{{0,20}})\[\s*(\d{{4,8}})\s*\]",
        rf"{KEYWORD_PATTERN}(?:[^\d]{{0,20}})(\d{{6}})(?!\d)",
        rf"{KEYWORD_PATTERN}(?:[^\d]{{0,20}})(\d{{4,8}})(?!\d)",
    ]
    code = _search_first(value, keyword_patterns)
    if code:
        return code

    fallback_patterns = [
        [r"\[\s*(\d{6})\s*\]"],
        [r"\[\s*(\d{4,8})\s*\]"],
        [r"(?<!\d)(\d{6})(?!\d)"],
        [r"(?<!\d)(\d{4,8})(?!\d)"],
    ]
    for patterns in fallback_patterns:
        code = _pick_best_candidate(value, patterns)
        if code:
            return code

    return None

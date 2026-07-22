from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gmail.client import GmailAccountClient, GmailClientError
from gmail.code_extractor import extract_verification_code
from gmail.parser import parse_gmail_message
from sms.client import (
    DEFAULT_SMS_SCRIPT_URL,
    SmsClientError,
    clear_cache as clear_sms_cache,
    fetch_latest_for_phone as fetch_latest_sms_for_phone,
    fetch_latest_for_phone_multi as fetch_latest_sms_for_phone_multi,
    inspect_source as inspect_sms_source,
)


# 兼容 PyInstaller 打包后的运行时路径
if getattr(sys, "frozen", False):
    # frozen 下 templates/static 被释放到 _MEIPASS（只读临时目录）
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", "."))
    # 用户可见数据/配置目录跟 exe 同级
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    BASE_DIR = BUNDLE_DIR

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
CONFIG_PATH = BASE_DIR / "config" / "phones.json"

load_dotenv(BASE_DIR / ".env")


class Settings:
    app_access_token: str = os.getenv("APP_ACCESS_TOKEN", "change-me")
    gmail_credentials_dir: Path = Path(
        os.getenv("GMAIL_CREDENTIALS_DIR", str(BASE_DIR / "gmail_credentials"))
    )
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    # 可选：强制指定生成链接的域名。不配也能用，默认按当前访问 Host 生成 https 链接
    public_base_url: str = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RECORD_LINK_BASE_URL")
        or os.getenv("BASE_URL")
        or ""
    ).strip().rstrip("/")
    # 默认开启：生成的专用链接一律使用 https（线上 SSL 场景）
    force_https_links: bool = os.getenv("FORCE_HTTPS_LINKS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "0"))
    default_gmail_query: str = os.getenv("DEFAULT_GMAIL_QUERY", "newer_than:30d")
    max_results_per_account: int = int(os.getenv("MAX_RESULTS_PER_ACCOUNT", "20"))
    # 专用链接（/api/v1/smpp/record）只拉取最近这么多秒内的邮件/短信，默认 3 分钟
    record_link_window_seconds: int = int(os.getenv("RECORD_LINK_WINDOW_SECONDS", "180"))
    # 短信源（Google Apps Script 表格）
    sms_script_url: str = os.getenv("SMS_SCRIPT_URL", DEFAULT_SMS_SCRIPT_URL)
    sms_cache_ttl_seconds: int = int(os.getenv("SMS_CACHE_TTL_SECONDS", "15"))
    sms_request_timeout: float = float(os.getenv("SMS_REQUEST_TIMEOUT", "30"))
    sms_timezone: str = os.getenv("SMS_TIMEZONE", "Asia/Seoul")


settings = Settings()

GMAIL_ACCOUNT_ALIASES = {
    "ehdqja9179@gmail.com": "gmail1",
    "magic22dan@gmail.com": "gmail2",
    "chlqlrkfdl@gmail.com": "gmail3",
}
ALL_GMAIL_ACCOUNTS = "all"
ALL_GMAIL_ACCOUNT_TOKENS = {"", "-", "*", ALL_GMAIL_ACCOUNTS}
SOURCE_GMAIL = "gmail"
SOURCE_SMS = "sms"
VALID_SOURCES = {SOURCE_GMAIL, SOURCE_SMS}
SMS_SOURCE_TOKENS = {"sms", "sms_kr", "sms-source", "script"}
SMS_REF_ALL = "all"
SMS_REF_DEFAULT = "default"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    sync_phone_configs()
    poll_task = None
    if settings.poll_interval_seconds > 0:
        poll_task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        if poll_task:
            poll_task.cancel()


app = FastAPI(title="Gmail Mail Viewer", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BUNDLE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BUNDLE_DIR / "static")), name="static")


def build_public_base_url(request: Request) -> str:
    """Build absolute base URL for generated record links.

    Default: always use https + current Host.
    Optional override: PUBLIC_BASE_URL / RECORD_LINK_BASE_URL / BASE_URL.
    Set FORCE_HTTPS_LINKS=0 only if you explicitly need http links.
    """
    configured = (settings.public_base_url or "").strip().rstrip("/")
    if configured:
        if configured.startswith("http://") and settings.force_https_links:
            configured = "https://" + configured[len("http://") :]
        elif (
            not configured.startswith("http://")
            and not configured.startswith("https://")
        ):
            scheme = "https" if settings.force_https_links else "http"
            configured = f"{scheme}://{configured}"
        return configured

    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or (request.headers.get("host") or "").strip()
    if not host:
        # Fallback: host from request.base_url
        base = str(request.base_url).rstrip("/")
        if "://" in base:
            host = base.split("://", 1)[1]
        else:
            host = base

    scheme = "https" if settings.force_https_links else (
        (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        or request.url.scheme
        or "http"
    )
    return f"{scheme}://{host}".rstrip("/")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db() -> Any:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS phones (
                phone TEXT PRIMARY KEY,
                record_url TEXT,
                gmail_accounts TEXT NOT NULL,
                keywords TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                gmail_account TEXT NOT NULL,
                message_id TEXT NOT NULL,
                thread_id TEXT,
                subject TEXT,
                sender TEXT,
                received_at TEXT,
                body_text TEXT,
                body_html TEXT,
                snippet TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(phone, gmail_account, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                gmail_account TEXT,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS record_links (
                token TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                gmail_account TEXT NOT NULL,
                sender TEXT NOT NULL DEFAULT '',
                window_seconds INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'gmail',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 老库迁移：补 sender / window_seconds / source 列
        cols = {row[1] for row in conn.execute("PRAGMA table_info(record_links)").fetchall()}
        if "sender" not in cols:
            conn.execute("ALTER TABLE record_links ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        if "window_seconds" not in cols:
            conn.execute("ALTER TABLE record_links ADD COLUMN window_seconds INTEGER NOT NULL DEFAULT 0")
        if "source" not in cols:
            conn.execute("ALTER TABLE record_links ADD COLUMN source TEXT NOT NULL DEFAULT 'gmail'")
        if "sms_source_ref" not in cols:
            conn.execute(
                "ALTER TABLE record_links ADD COLUMN sms_source_ref TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_phone_received ON emails(phone, received_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_message ON emails(message_id)"
        )
    seed_sms_sources_from_env()


def load_phone_configs() -> list[dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return []

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        raise RuntimeError("config/phones.json must be a JSON array")

    configs: list[dict[str, Any]] = []
    for item in raw:
        if not item.get("phone"):
            continue
        configs.append(
            {
                "phone": str(item["phone"]),
                "record_url": item.get("record_url", ""),
                "gmail_accounts": item.get("gmail_accounts", []),
                "keywords": item.get("keywords") or [str(item["phone"])],
                "enabled": bool(item.get("enabled", True)),
                "gmail_query": item.get("gmail_query", ""),
            }
        )
    return configs


def sync_phone_configs() -> None:
    with db() as conn:
        for item in load_phone_configs():
            conn.execute(
                """
                INSERT INTO phones (phone, record_url, gmail_accounts, keywords, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    record_url=excluded.record_url,
                    gmail_accounts=excluded.gmail_accounts,
                    keywords=excluded.keywords,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    item["phone"],
                    item["record_url"],
                    json.dumps(item["gmail_accounts"], ensure_ascii=False),
                    json.dumps(item["keywords"], ensure_ascii=False),
                    1 if item["enabled"] else 0,
                    now_iso(),
                ),
            )


def require_token(token: str = Query(default="")) -> str:
    if not settings.app_access_token or settings.app_access_token == "change-me":
        raise HTTPException(status_code=500, detail="APP_ACCESS_TOKEN is not configured")
    if token != settings.app_access_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def get_phone_config(phone: str) -> dict[str, Any] | None:
    for item in load_phone_configs():
        if item["phone"] == phone and item["enabled"]:
            return item
    return None


def build_gmail_query(config: dict[str, Any]) -> str:
    if config.get("gmail_query"):
        return str(config["gmail_query"])

    keywords = [str(k).strip() for k in config.get("keywords", []) if str(k).strip()]
    if not keywords:
        keywords = [str(config["phone"])]

    if len(keywords) == 1:
        keyword_query = keywords[0]
    else:
        keyword_query = "(" + " OR ".join(keywords) + ")"

    return f"{settings.default_gmail_query} {keyword_query}".strip()


def build_phone_query(phone: str, sender: str = "", window_seconds: int | None = None) -> str:
    parts: list[str] = []
    if window_seconds and window_seconds > 0:
        # Gmail 支持 after:<epoch_seconds>，可做到分钟级过滤
        after_epoch = int((datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).timestamp())
        parts.append(f"after:{after_epoch}")
    else:
        parts.append(settings.default_gmail_query)
    if sender:
        # 指定了发件人 → 只按发件人过滤，不再限定标题里必须含手机号
        parts.append(f"from:{sender}")
    else:
        parts.append(f"subject:{phone}")
    return " ".join(p for p in parts if p).strip()


def natural_sort_key(value: str) -> list[tuple[int, Any]]:
    parts: list[tuple[int, Any]] = []
    current = ""
    is_digit = False
    for char in value:
        char_is_digit = char.isdigit()
        if current and char_is_digit != is_digit:
            parts.append((0, int(current)) if is_digit else (1, current.lower()))
            current = ""
        current += char
        is_digit = char_is_digit
    if current:
        parts.append((0, int(current)) if is_digit else (1, current.lower()))
    return parts


def discover_gmail_accounts(credentials_dir: str | Path | None = None) -> list[str]:
    base = Path(credentials_dir or settings.gmail_credentials_dir)
    if not base.exists():
        return []

    accounts: list[str] = []
    seen: set[str] = set()

    def add(account: str) -> None:
        account = account.strip()
        key = account.lower()
        if account and key not in seen:
            accounts.append(account)
            seen.add(key)

    for path in base.iterdir():
        if path.is_dir() and (path / "token.json").is_file():
            add(path.name)
        elif path.is_file() and path.name.endswith("_token.json"):
            add(path.name[: -len("_token.json")])
        elif path.is_file() and path.name.startswith("token_") and path.suffix == ".json":
            add(path.stem[len("token_") :])

    return sorted(accounts, key=natural_sort_key)


def resolve_gmail_accounts(
    configured_accounts: Any,
    credentials_dir: str | Path | None = None,
) -> list[str]:
    if isinstance(configured_accounts, str):
        configured_accounts = [configured_accounts]
    accounts = [
        normalize_gmail_account(str(account))
        for account in (configured_accounts or [])
    ]
    accounts = [account for account in accounts if account]
    if not accounts or any(account.lower() in ALL_GMAIL_ACCOUNT_TOKENS for account in accounts):
        return discover_gmail_accounts(credentials_dir)
    return accounts


def normalize_gmail_account(value: str) -> str:
    account = value.strip()
    if account.lower() in ALL_GMAIL_ACCOUNT_TOKENS:
        return ""
    return GMAIL_ACCOUNT_ALIASES.get(account.lower(), account)


def save_email(phone: str, gmail_account: str, parsed: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO emails (
                phone, gmail_account, message_id, thread_id, subject, sender,
                received_at, body_text, body_html, snippet, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone, gmail_account, message_id) DO UPDATE SET
                thread_id=excluded.thread_id,
                subject=excluded.subject,
                sender=excluded.sender,
                received_at=excluded.received_at,
                body_text=excluded.body_text,
                body_html=excluded.body_html,
                snippet=excluded.snippet
            """,
            (
                phone,
                gmail_account,
                parsed["message_id"],
                parsed.get("thread_id"),
                parsed.get("subject"),
                parsed.get("sender"),
                parsed.get("received_at"),
                parsed.get("body_text"),
                parsed.get("body_html"),
                parsed.get("snippet"),
                now_iso(),
            ),
        )


def add_fetch_log(
    status: str,
    message: str,
    phone: str | None = None,
    gmail_account: str | None = None,
) -> None:
    safe_message = message[:1000]
    with db() as conn:
        conn.execute(
            """
            INSERT INTO fetch_logs (phone, gmail_account, status, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (phone, gmail_account, status, safe_message, now_iso()),
        )


def refresh_phone(phone: str) -> dict[str, Any]:
    config = get_phone_config(phone)
    if not config:
        raise HTTPException(status_code=404, detail=f"Phone config not found: {phone}")

    query = build_gmail_query(config)
    accounts = resolve_gmail_accounts(config.get("gmail_accounts"))
    if not accounts:
        raise HTTPException(status_code=400, detail=f"No Gmail accounts configured for {phone}")

    result = {"phone": phone, "query": query, "accounts": [], "saved": 0, "errors": []}

    for account in accounts:
        account_name = str(account)
        try:
            client = GmailAccountClient(account_name, settings.gmail_credentials_dir)
            messages = client.search_messages(query, max_results=settings.max_results_per_account)
            saved_count = 0
            for msg in messages:
                full_msg = client.get_message(msg["id"])
                parsed = parse_gmail_message(full_msg)
                save_email(phone, account_name, parsed)
                saved_count += 1
            add_fetch_log("ok", f"Fetched {saved_count} messages", phone, account_name)
            result["accounts"].append({"account": account_name, "saved": saved_count})
            result["saved"] += saved_count
        except GmailClientError as exc:
            add_fetch_log("error", str(exc), phone, account_name)
            result["errors"].append({"account": account_name, "message": str(exc)})
        except Exception as exc:  # Keep refresh resilient across accounts.
            add_fetch_log("error", f"Unexpected error: {exc}", phone, account_name)
            result["errors"].append({"account": account_name, "message": f"Unexpected error: {exc}"})

    return result


def refresh_all() -> dict[str, Any]:
    sync_phone_configs()
    results = []
    for item in load_phone_configs():
        if item["enabled"]:
            results.append(refresh_phone(item["phone"]))
    return {"results": results}



def row_sms_source(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = row_to_dict(row)
    if not item:
        return None
    item["enabled"] = bool(item.get("enabled"))
    item["is_default"] = bool(item.get("is_default"))
    return item


def seed_sms_sources_from_env() -> None:
    """If sms_sources is empty, import the legacy SMS_SCRIPT_URL as default source."""
    url = (settings.sms_script_url or "").strip()
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM sms_sources").fetchone()["c"]
        if count:
            return
        if not url:
            return
        now = now_iso()
        conn.execute(
            """
            INSERT INTO sms_sources (name, url, enabled, is_default, note, created_at, updated_at)
            VALUES (?, ?, 1, 1, ?, ?, ?)
            """,
            ("default", url, "migrated from SMS_SCRIPT_URL", now, now),
        )


def list_sms_sources(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sms_sources"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY is_default DESC, id ASC"
    with db() as conn:
        rows = conn.execute(sql).fetchall()
    return [row_sms_source(row) for row in rows]  # type: ignore[misc]


def get_sms_source_by_id(source_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM sms_sources WHERE id = ?", (source_id,)).fetchone()
    return row_sms_source(row)


def get_sms_source_by_name(name: str) -> dict[str, Any] | None:
    name = (name or "").strip()
    if not name:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_sources WHERE lower(name) = lower(?)",
            (name,),
        ).fetchone()
    return row_sms_source(row)


def get_default_sms_source() -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM sms_sources
            WHERE enabled = 1 AND is_default = 1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if row:
            return row_sms_source(row)
        row = conn.execute(
            """
            SELECT * FROM sms_sources
            WHERE enabled = 1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
    return row_sms_source(row)


def ensure_single_default(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE sms_sources SET is_default = 0 WHERE id != ?", (source_id,))
    conn.execute("UPDATE sms_sources SET is_default = 1 WHERE id = ?", (source_id,))


def parse_sms_source_token(value: str | None) -> tuple[str, str]:
    """Return (source, sms_source_ref).

    Accepts:
      sms / sms_kr / script
      sms:all / sms:default / sms:源名
    """
    raw = str(value or "").strip()
    if not raw:
        return SOURCE_GMAIL, ""
    lower = raw.lower()
    if lower in SMS_SOURCE_TOKENS:
        return SOURCE_SMS, ""
    if lower.startswith("sms:"):
        ref = raw.split(":", 1)[1].strip()
        return SOURCE_SMS, ref
    if lower.startswith("sms_"):
        ref = raw[4:].strip(":_- ")
        if ref:
            return SOURCE_SMS, ref
    return SOURCE_GMAIL, ""


def normalize_sms_source_ref(value: str | None) -> str:
    ref = str(value or "").strip()
    if not ref:
        return ""
    lower = ref.lower()
    if lower in {SMS_REF_DEFAULT, "def", "*"}:
        return ""
    if lower == SMS_REF_ALL:
        return SMS_REF_ALL
    return ref


def resolve_sms_targets(sms_source_ref: str) -> tuple[str, list[dict[str, Any]]]:
    """Resolve link ref into mode + source list.

    mode: all | named | default | env
    """
    ref = normalize_sms_source_ref(sms_source_ref)
    if ref and ref.lower() == SMS_REF_ALL:
        sources = list_sms_sources(enabled_only=True)
        if not sources:
            url = (settings.sms_script_url or "").strip()
            if url:
                return "all", [{"id": 0, "name": "env", "url": url, "enabled": True, "is_default": True}]
            raise HTTPException(status_code=400, detail="No enabled SMS sources configured")
        return "all", sources

    if ref:
        source = get_sms_source_by_name(ref)
        if not source and ref.isdigit():
            source = get_sms_source_by_id(int(ref))
        if not source:
            raise HTTPException(status_code=400, detail=f"SMS source not found: {ref}")
        if not source.get("enabled"):
            raise HTTPException(status_code=400, detail=f"SMS source disabled: {ref}")
        return "named", [source]

    source = get_default_sms_source()
    if source:
        return "default", [source]
    url = (settings.sms_script_url or "").strip()
    if url:
        return "env", [{"id": 0, "name": "env", "url": url, "enabled": True, "is_default": True}]
    raise HTTPException(status_code=400, detail="No default SMS source configured")


def normalize_source(value: str | None) -> str:
    raw = str(value or SOURCE_GMAIL).strip()
    lower = raw.lower()
    if lower in SMS_SOURCE_TOKENS or lower.startswith("sms:") or lower.startswith("sms_"):
        return SOURCE_SMS
    if lower in ("", SOURCE_GMAIL, "mail", "email"):
        return SOURCE_GMAIL
    if lower in VALID_SOURCES:
        return lower
    raise HTTPException(status_code=400, detail=f"Unsupported source: {value}")


def create_record_link(
    phone: str,
    gmail_account: str = "",
    sender: str = "",
    window_seconds: int = 0,
    source: str = SOURCE_GMAIL,
    sms_source_ref: str = "",
) -> dict[str, Any]:
    phone = phone.strip()
    sender = sender.strip()
    try:
        window_seconds = int(window_seconds or 0)
    except (TypeError, ValueError):
        window_seconds = 0
    if window_seconds < 0:
        window_seconds = 0
    if not phone:
        raise HTTPException(status_code=400, detail="Phone is required")

    # Allow source="sms:all" / gmail_account="sms:源名" style tokens.
    parsed_source, parsed_ref = parse_sms_source_token(source)
    if parsed_source == SOURCE_SMS:
        source = SOURCE_SMS
        if not sms_source_ref:
            sms_source_ref = parsed_ref
    else:
        ga_source, ga_ref = parse_sms_source_token(gmail_account)
        if ga_source == SOURCE_SMS:
            source = SOURCE_SMS
            gmail_account = ""
            if not sms_source_ref:
                sms_source_ref = ga_ref
        else:
            source = normalize_source(source)

    sms_source_ref = normalize_sms_source_ref(sms_source_ref)

    if source == SOURCE_SMS:
        # 短信源不依赖 Gmail 账号；发件人字段忽略
        gmail_account = ""
        sender = ""
        # Validate ref early so bad names fail at link creation time.
        resolve_sms_targets(sms_source_ref)
    else:
        gmail_account = normalize_gmail_account(gmail_account)
        if not gmail_account:
            gmail_account = ALL_GMAIL_ACCOUNTS
        sms_source_ref = ""

    token = secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO record_links (
                token, phone, gmail_account, sender, window_seconds, source,
                sms_source_ref, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                phone,
                gmail_account,
                sender,
                window_seconds,
                source,
                sms_source_ref,
                now_iso(),
                now_iso(),
            ),
        )
    return {
        "phone": phone,
        "source": source,
        "sms_source_ref": sms_source_ref or SMS_REF_DEFAULT,
        "gmail_account": gmail_account,
        "sender": sender,
        "window_seconds": window_seconds,
        "token": token,
    }


def get_record_link(token: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM record_links WHERE token = ?",
            (token,),
        ).fetchone()
    return row_to_dict(row)


def resolve_link_window_seconds(item: dict[str, Any]) -> int:
    """每个链接独立的时间窗口（秒）；为 0 时回退到全局默认 RECORD_LINK_WINDOW_SECONDS。"""
    try:
        link_window = int(item.get("window_seconds") or 0)
    except (TypeError, ValueError):
        link_window = 0
    return link_window if link_window > 0 else settings.record_link_window_seconds


def fetch_latest_sms_body_for_link(item: dict[str, Any]) -> dict[str, Any]:
    phone = str(item["phone"]).strip()
    window_seconds = resolve_link_window_seconds(item)
    # Legacy links have no sms_source_ref column value → default / env fallback.
    sms_source_ref = str(item.get("sms_source_ref") or "").strip()
    mode, sources = resolve_sms_targets(sms_source_ref)
    try:
        if mode == "all" or len(sources) > 1:
            result = fetch_latest_sms_for_phone_multi(
                phone,
                sources,
                window_seconds=window_seconds,
                timeout=settings.sms_request_timeout,
                cache_ttl_seconds=settings.sms_cache_ttl_seconds,
                tz_name=settings.sms_timezone,
            )
        else:
            source = sources[0]
            result = fetch_latest_sms_for_phone(
                phone,
                window_seconds=window_seconds,
                url=str(source.get("url") or settings.sms_script_url),
                timeout=settings.sms_request_timeout,
                cache_ttl_seconds=settings.sms_cache_ttl_seconds,
                tz_name=settings.sms_timezone,
                source_name=str(source.get("name") or ""),
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SmsClientError as exc:
        add_fetch_log("error", str(exc), phone, SOURCE_SMS)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result["sms_query_mode"] = mode
    return result


def fetch_latest_body_for_link(item: dict[str, Any]) -> dict[str, Any]:
    phone = str(item["phone"])
    gmail_account = str(item["gmail_account"])
    sender = str(item.get("sender") or "").strip()
    window_seconds = resolve_link_window_seconds(item)
    query = build_phone_query(phone, sender, window_seconds=window_seconds)
    accounts = resolve_gmail_accounts([] if gmail_account == ALL_GMAIL_ACCOUNTS else [gmail_account])
    if not accounts:
        raise HTTPException(status_code=400, detail="No Gmail accounts found")
    errors: list[str] = []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        if window_seconds and window_seconds > 0
        else None
    )

    for account in accounts:
        try:
            client = GmailAccountClient(account, settings.gmail_credentials_dir)
            messages = client.search_messages(query, max_results=settings.max_results_per_account)
        except GmailClientError as exc:
            errors.append(f"{account}: {exc}")
            add_fetch_log("error", str(exc), phone, account)
            continue

        for msg in messages:
            full_msg = client.get_message(msg["id"])
            parsed = parse_gmail_message(full_msg)
            subject = str(parsed.get("subject") or "")
            if sender:
                # 指定了发件人：只校验发件人，不再要求标题包含手机号
                msg_sender = str(parsed.get("sender") or "").lower()
                if sender.lower() not in msg_sender:
                    continue
            else:
                # 未指定发件人：保持原行为，按手机号匹配标题
                if phone not in subject:
                    continue
            # 二次校验：邮件接收时间必须落在窗口内
            if cutoff is not None:
                received_at_raw = str(parsed.get("received_at") or "")
                try:
                    received_at = datetime.fromisoformat(received_at_raw)
                except ValueError:
                    continue
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
                if received_at < cutoff:
                    continue

            save_email(phone, account, parsed)
            body_text = str(parsed.get("body_text") or "")
            code = extract_verification_code(body_text)
            if not code:
                raise HTTPException(status_code=404, detail="Code not found")
            return {
                "phone": phone,
                "source": SOURCE_GMAIL,
                "gmail_account": account,
                "sender_filter": sender,
                "message_id": str(parsed.get("message_id") or ""),
                "subject": subject,
                "sender": str(parsed.get("sender") or ""),
                "received_at": str(parsed.get("received_at") or ""),
                "code": code,
            }

    if errors and len(errors) == len(accounts):
        raise GmailClientError("; ".join(errors))
    if sender:
        detail = f"No email found from {sender}"
    else:
        detail = f"No email subject found for {phone}"
    if window_seconds and window_seconds > 0:
        detail += f" within last {window_seconds}s"
    raise HTTPException(status_code=404, detail=detail)


def fetch_record_for_link(item: dict[str, Any]) -> dict[str, Any]:
    source = str(item.get("source") or SOURCE_GMAIL).strip().lower()
    if source == SOURCE_SMS:
        return fetch_latest_sms_body_for_link(item)
    return fetch_latest_body_for_link(item)


async def poll_loop() -> None:
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        try:
            await asyncio.to_thread(refresh_all)
        except Exception as exc:
            add_fetch_log("error", f"Polling refresh failed: {exc}")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, token: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "token": token, "app_token_is_default": settings.app_access_token == "change-me"},
    )


@app.get("/mail/{phone}", response_class=HTMLResponse)
def mail_page(request: Request, phone: str, token: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        "mail.html",
        {
            "request": request,
            "phone": phone,
            "token": token,
            "app_token_is_default": settings.app_access_token == "change-me",
        },
    )


@app.get("/api/phones")
def api_phones(_: str = Depends(require_token)) -> dict[str, Any]:
    sync_phone_configs()
    with db() as conn:
        rows = conn.execute("SELECT * FROM phones WHERE enabled=1 ORDER BY phone").fetchall()
    phones = []
    for row in rows:
        item = dict(row)
        item["gmail_accounts"] = json.loads(item["gmail_accounts"])
        item["resolved_gmail_accounts"] = resolve_gmail_accounts(item["gmail_accounts"])
        item["keywords"] = json.loads(item["keywords"])
        phones.append(item)
    return {"phones": phones}


@app.post("/api/record-links")
async def api_create_record_link(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    raw_source = payload.get("source")
    gmail_account = str(payload.get("gmail_account", ""))
    sms_source_ref = str(payload.get("sms_source") or payload.get("sms_source_ref") or "")

    # 兼容前端把 sms / sms:all / sms:源名 写在 gmail_account 第二列
    if raw_source is None:
        parsed, ref = parse_sms_source_token(gmail_account)
        if parsed == SOURCE_SMS:
            raw_source = SOURCE_SMS
            gmail_account = ""
            if not sms_source_ref:
                sms_source_ref = ref
    else:
        parsed, ref = parse_sms_source_token(str(raw_source))
        if parsed == SOURCE_SMS:
            raw_source = SOURCE_SMS
            if not sms_source_ref:
                sms_source_ref = ref

    item = create_record_link(
        phone=str(payload.get("phone", "")),
        gmail_account=gmail_account,
        sender=str(payload.get("sender", "")),
        window_seconds=int(payload.get("window_seconds") or 0),
        source=str(raw_source or SOURCE_GMAIL),
        sms_source_ref=sms_source_ref,
    )
    base = build_public_base_url(request)
    item["url"] = f"{base}/api/v1/smpp/record?token={item['token']}&format=txt2"
    return item


@app.get("/api/sms-sources")
def api_list_sms_sources() -> dict[str, Any]:
    return {"sources": list_sms_sources()}


@app.post("/api/sms-sources")
async def api_create_sms_source(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    note = str(payload.get("note") or "").strip()
    enabled = 1 if payload.get("enabled", True) else 0
    is_default = 1 if payload.get("is_default", False) else 0
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if name.lower() == SMS_REF_ALL:
        raise HTTPException(status_code=400, detail=f"Reserved source name: {name}")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    now = now_iso()
    try:
        with db() as conn:
            if is_default:
                conn.execute("UPDATE sms_sources SET is_default = 0")
            # If this is the first source, force default.
            count = conn.execute("SELECT COUNT(*) AS c FROM sms_sources").fetchone()["c"]
            if count == 0:
                is_default = 1
            cur = conn.execute(
                """
                INSERT INTO sms_sources (name, url, enabled, is_default, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, url, enabled, is_default, note, now, now),
            )
            source_id = int(cur.lastrowid)
            if is_default:
                ensure_single_default(conn, source_id)
            row = conn.execute("SELECT * FROM sms_sources WHERE id = ?", (source_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail=f"Source name already exists: {name}") from exc

    clear_sms_cache(url)
    return row_sms_source(row)  # type: ignore[return-value]


@app.put("/api/sms-sources/{source_id}")
async def api_update_sms_source(source_id: int, request: Request) -> dict[str, Any]:
    existing = get_sms_source_by_id(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="SMS source not found")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    name = str(payload.get("name", existing["name"])).strip()
    url = str(payload.get("url", existing["url"])).strip()
    note = str(payload.get("note", existing.get("note") or "")).strip()
    enabled = 1 if payload.get("enabled", existing["enabled"]) else 0
    is_default = 1 if payload.get("is_default", existing["is_default"]) else 0
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if name.lower() == SMS_REF_ALL:
        raise HTTPException(status_code=400, detail=f"Reserved source name: {name}")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not enabled and existing.get("is_default") and is_default:
        raise HTTPException(status_code=400, detail="Cannot disable the default SMS source")

    now = now_iso()
    try:
        with db() as conn:
            if existing.get("is_default") and not is_default:
                others = conn.execute(
                    "SELECT id FROM sms_sources WHERE enabled = 1 AND id != ? ORDER BY id ASC LIMIT 1",
                    (source_id,),
                ).fetchone()
                if not others:
                    raise HTTPException(status_code=400, detail="At least one default SMS source is required")
            if is_default:
                conn.execute("UPDATE sms_sources SET is_default = 0")
                enabled = 1
            conn.execute(
                """
                UPDATE sms_sources
                SET name = ?, url = ?, enabled = ?, is_default = ?, note = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, url, enabled, is_default, note, now, source_id),
            )
            if is_default:
                ensure_single_default(conn, source_id)
            row = conn.execute("SELECT * FROM sms_sources WHERE id = ?", (source_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail=f"Source name already exists: {name}") from exc

    clear_sms_cache(str(existing.get("url") or ""))
    clear_sms_cache(url)
    return row_sms_source(row)  # type: ignore[return-value]


@app.delete("/api/sms-sources/{source_id}")
def api_delete_sms_source(source_id: int) -> dict[str, Any]:
    existing = get_sms_source_by_id(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="SMS source not found")

    with db() as conn:
        enabled_count = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_sources WHERE enabled = 1"
        ).fetchone()["c"]
        if existing.get("is_default") and enabled_count <= 1:
            if not (settings.sms_script_url or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail="Cannot delete the only default SMS source without SMS_SCRIPT_URL fallback",
                )
        conn.execute("DELETE FROM sms_sources WHERE id = ?", (source_id,))
        default_row = conn.execute(
            "SELECT id FROM sms_sources WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if not default_row:
            other = conn.execute(
                "SELECT id FROM sms_sources WHERE enabled = 1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if other:
                ensure_single_default(conn, int(other["id"]))

    clear_sms_cache(str(existing.get("url") or ""))
    return {"ok": True, "deleted_id": source_id}


@app.post("/api/sms-sources/test")
async def api_test_sms_source(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    url = str(payload.get("url") or "").strip()
    source_id = payload.get("id")
    if not url and source_id is not None:
        source = get_sms_source_by_id(int(source_id))
        if not source:
            raise HTTPException(status_code=404, detail="SMS source not found")
        url = str(source.get("url") or "")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    try:
        info = inspect_sms_source(
            url=url,
            timeout=settings.sms_request_timeout,
            tz_name=settings.sms_timezone,
        )
    except SmsClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return info


@app.get("/api/v1/smpp/record", name="api_record", response_model=None)
def api_record(token: str = "", format: str = "txt2"):
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")

    item = get_record_link(token)
    if not item:
        raise HTTPException(status_code=404, detail="Record link not found")

    try:
        result = fetch_record_for_link(item)
    except GmailClientError as exc:
        add_fetch_log("error", str(exc), str(item["phone"]), str(item.get("gmail_account") or ""))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if format == "json":
        return JSONResponse(result)

    return PlainTextResponse(result["code"], media_type="text/plain; charset=utf-8")


@app.get("/api/mail/{phone}")
def api_latest_mail(phone: str, _: str = Depends(require_token)) -> JSONResponse:
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM emails
            WHERE phone = ?
            ORDER BY datetime(received_at) DESC, id DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No email found for {phone}")
    return JSONResponse(row_to_dict(row))


@app.get("/api/mails/{phone}")
def api_mails(phone: str, limit: int = 50, _: str = Depends(require_token)) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, phone, gmail_account, message_id, thread_id, subject, sender,
                   received_at, snippet, created_at
            FROM emails
            WHERE phone = ?
            ORDER BY datetime(received_at) DESC, id DESC
            LIMIT ?
            """,
            (phone, limit),
        ).fetchall()
    return {"phone": phone, "mails": [dict(row) for row in rows]}


@app.get("/api/mail-detail/{message_id}")
def api_mail_detail(message_id: str, phone: str = "", _: str = Depends(require_token)) -> JSONResponse:
    params: list[Any] = [message_id]
    where = "message_id = ?"
    if phone:
        where += " AND phone = ?"
        params.append(phone)
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM emails
            WHERE {where}
            ORDER BY datetime(received_at) DESC, id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Email not found: {message_id}")
    return JSONResponse(row_to_dict(row))


@app.post("/api/refresh")
def api_refresh(
    background_tasks: BackgroundTasks,
    phone: str = "",
    background: bool = False,
    _: str = Depends(require_token),
) -> dict[str, Any]:
    if background:
        background_tasks.add_task(refresh_phone, phone) if phone else background_tasks.add_task(refresh_all)
        return {"status": "queued", "phone": phone or None}
    if phone:
        return refresh_phone(phone)
    return refresh_all()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": now_iso()}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)

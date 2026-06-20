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
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "0"))
    default_gmail_query: str = os.getenv("DEFAULT_GMAIL_QUERY", "newer_than:30d")
    max_results_per_account: int = int(os.getenv("MAX_RESULTS_PER_ACCOUNT", "20"))
    # 专用链接（/api/v1/smpp/record）只拉取最近这么多秒内的邮件，默认 3 分钟
    record_link_window_seconds: int = int(os.getenv("RECORD_LINK_WINDOW_SECONDS", "180"))


settings = Settings()

GMAIL_ACCOUNT_ALIASES = {
    "ehdqja9179@gmail.com": "gmail1",
    "magic22dan@gmail.com": "gmail2",
    "chlqlrkfdl@gmail.com": "gmail3",
}
ALL_GMAIL_ACCOUNTS = "all"
ALL_GMAIL_ACCOUNT_TOKENS = {"", "-", "*", ALL_GMAIL_ACCOUNTS}


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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 老库迁移：补 sender / window_seconds 列
        cols = {row[1] for row in conn.execute("PRAGMA table_info(record_links)").fetchall()}
        if "sender" not in cols:
            conn.execute("ALTER TABLE record_links ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        if "window_seconds" not in cols:
            conn.execute("ALTER TABLE record_links ADD COLUMN window_seconds INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_phone_received ON emails(phone, received_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_message ON emails(message_id)"
        )


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


def create_record_link(
    phone: str,
    gmail_account: str,
    sender: str = "",
    window_seconds: int = 0,
) -> dict[str, Any]:
    phone = phone.strip()
    gmail_account = normalize_gmail_account(gmail_account)
    sender = sender.strip()
    try:
        window_seconds = int(window_seconds or 0)
    except (TypeError, ValueError):
        window_seconds = 0
    if window_seconds < 0:
        window_seconds = 0
    if not phone:
        raise HTTPException(status_code=400, detail="Phone is required")
    if not gmail_account:
        gmail_account = ALL_GMAIL_ACCOUNTS

    token = secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO record_links (token, phone, gmail_account, sender, window_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (token, phone, gmail_account, sender, window_seconds, now_iso(), now_iso()),
        )
    return {
        "phone": phone,
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


def fetch_latest_body_for_link(item: dict[str, Any]) -> dict[str, Any]:
    phone = str(item["phone"])
    gmail_account = str(item["gmail_account"])
    sender = str(item.get("sender") or "").strip()
    # 每个链接独立的时间窗口（秒）；为 0 时回退到全局默认
    try:
        link_window = int(item.get("window_seconds") or 0)
    except (TypeError, ValueError):
        link_window = 0
    window_seconds = link_window if link_window > 0 else settings.record_link_window_seconds
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

    item = create_record_link(
        phone=str(payload.get("phone", "")),
        gmail_account=str(payload.get("gmail_account", "")),
        sender=str(payload.get("sender", "")),
        window_seconds=int(payload.get("window_seconds") or 0),
    )
    base = str(request.base_url).rstrip("/")
    item["url"] = f"{base}/api/v1/smpp/record?token={item['token']}&format=txt2"
    return item


@app.get("/api/v1/smpp/record", name="api_record", response_model=None)
def api_record(token: str = "", format: str = "txt2"):
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")

    item = get_record_link(token)
    if not item:
        raise HTTPException(status_code=404, detail="Record link not found")

    try:
        result = fetch_latest_body_for_link(item)
    except GmailClientError as exc:
        add_fetch_log("error", str(exc), str(item["phone"]), str(item["gmail_account"]))
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

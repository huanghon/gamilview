from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

import app


class FakeGmailSuccess:
    def __init__(self, account: str, credentials_dir: str) -> None:
        self.account = account

    def search_messages(self, query: str, max_results: int = 20) -> list[dict[str, str]]:
        return [{"id": "message-1"}]

    def get_message(self, message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "threadId": "thread-1",
            "snippet": "인증번호는 [111111]입니다",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "[SMS자동전달] 16006329로부터 새로운 메시지"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Date", "value": "Wed, 18 Jun 2026 07:31:00 +0000"},
                ],
                "body": {"data": "67O066C06IKs656MOyA6IDE2MDA2MzI5W05DXSDsnbjsp4vrspjtmLjripQgWzExMTExMV3snoXri4jri6Qu"},
            },
        }


class FakeGmailMissingCode:
    def __init__(self, account: str, credentials_dir: str) -> None:
        self.account = account

    def search_messages(self, query: str, max_results: int = 20) -> list[dict[str, str]]:
        return [{"id": "message-2"}]

    def get_message(self, message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "threadId": "thread-2",
            "snippet": "plain text without code",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "[SMS자동전달] 16006329로부터 새로운 메시지"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Date", "value": "Wed, 18 Jun 2026 07:31:00 +0000"},
                ],
                "body": {"data": "cGxhaW4gdGV4dCB3aXRob3V0IGNvZGU="},
            },
        }


class RecordApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.init_db()
        self.client = TestClient(app.app)

    def tearDown(self) -> None:
        with app.db() as conn:
            conn.execute("DELETE FROM record_links WHERE token LIKE 'test-token-%'")
            conn.execute("DELETE FROM emails WHERE message_id IN ('message-1', 'message-2')")

    def test_txt2_returns_extracted_code(self) -> None:
        original = app.GmailAccountClient
        app.GmailAccountClient = FakeGmailSuccess
        try:
            with app.db() as conn:
                conn.execute(
                    """
                    INSERT INTO record_links (token, phone, gmail_account, sender, window_seconds, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("test-token-success", "16006329", "gmail1", "", 999999999, app.now_iso(), app.now_iso()),
                )
            response = self.client.get("/api/v1/smpp/record?token=test-token-success&format=txt2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "111111")
        finally:
            app.GmailAccountClient = original

    def test_returns_code_not_found_when_body_has_no_code(self) -> None:
        original = app.GmailAccountClient
        app.GmailAccountClient = FakeGmailMissingCode
        try:
            with app.db() as conn:
                conn.execute(
                    """
                    INSERT INTO record_links (token, phone, gmail_account, sender, window_seconds, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("test-token-missing", "16006329", "gmail1", "", 999999999, app.now_iso(), app.now_iso()),
                )
            response = self.client.get("/api/v1/smpp/record?token=test-token-missing&format=txt2")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"], "Code not found")
        finally:
            app.GmailAccountClient = original


class GmailAccountDiscoveryTests(unittest.TestCase):
    def test_all_accounts_are_discovered_from_token_folders(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for account in ("gmail10", "gamil880", "gmail2"):
                account_dir = base / account
                account_dir.mkdir()
                (account_dir / "token.json").write_text("{}", encoding="utf-8")
            (base / "not-ready").mkdir()

            self.assertEqual(
                app.resolve_gmail_accounts(["all"], base),
                ["gamil880", "gmail2", "gmail10"],
            )


if __name__ == "__main__":
    unittest.main()

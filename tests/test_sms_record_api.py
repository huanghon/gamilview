from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app
from sms.client import clear_cache


class SmsRecordApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.init_db()
        clear_cache()
        self.client = TestClient(app.app)

    def tearDown(self) -> None:
        with app.db() as conn:
            conn.execute("DELETE FROM record_links WHERE token LIKE 'test-sms-%'")
        clear_cache()

    def test_create_record_link_with_source_sms(self) -> None:
        response = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "source": "sms", "window_seconds": 0},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["phone"], "01080792425")
        self.assertEqual(data["source"], "sms")
        self.assertIn("token=", data["url"])
        self.assertEqual(data["gmail_account"], "")

    def test_create_record_link_gmail_account_sms_token(self) -> None:
        # 前端兼容：第二列写 sms 时也可识别
        response = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "gmail_account": "sms"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "sms")

    def test_txt2_returns_latest_sms_code(self) -> None:
        now = datetime(2026, 7, 14, 15, 50, tzinfo=ZoneInfo("Asia/Seoul"))
        fake_result = {
            "phone": "01080792425",
            "source": "sms",
            "sim": "김지수",
            "received_at": "2026-07-14T15:37:00+09:00",
            "received_at_raw": "07. 14. 오후 3:37",
            "code": "436501",
            "body": "본인확인 인증번호(436501)입력시 정상처리 됩니다.",
        }
        with app.db() as conn:
            conn.execute(
                """
                INSERT INTO record_links (
                    token, phone, gmail_account, sender, window_seconds, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-sms-success",
                    "01080792425",
                    "",
                    "",
                    999999999,
                    "sms",
                    app.now_iso(),
                    app.now_iso(),
                ),
            )

        with patch("app.fetch_latest_sms_for_phone", return_value=fake_result) as mocked:
            response = self.client.get("/api/v1/smpp/record?token=test-sms-success&format=txt2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "436501")
            mocked.assert_called_once()
            kwargs = mocked.call_args.kwargs
            self.assertEqual(kwargs["window_seconds"], 999999999)

        with patch("app.fetch_latest_sms_for_phone", return_value=fake_result):
            response = self.client.get("/api/v1/smpp/record?token=test-sms-success&format=json")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["code"], "436501")
            self.assertEqual(data["source"], "sms")
            self.assertEqual(data["phone"], "01080792425")

        # silence unused var warning in some linters
        self.assertIsNotNone(now)

    def test_sms_not_found(self) -> None:
        with app.db() as conn:
            conn.execute(
                """
                INSERT INTO record_links (
                    token, phone, gmail_account, sender, window_seconds, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-sms-missing",
                    "01000000000",
                    "",
                    "",
                    180,
                    "sms",
                    app.now_iso(),
                    app.now_iso(),
                ),
            )
        with patch("app.fetch_latest_sms_for_phone", side_effect=LookupError("No SMS found for 01000000000")):
            response = self.client.get("/api/v1/smpp/record?token=test-sms-missing&format=txt2")
            self.assertEqual(response.status_code, 404)
            self.assertIn("No SMS found", response.json()["detail"])


class SmsClientMatchTests(unittest.TestCase):
    def test_exact_phone_match_and_latest(self) -> None:
        from sms.client import extract_records_from_html, fetch_latest_for_phone

        # Minimal HTML-like payload is complex; unit-test ranking via mock records path
        # by calling extract on a synthetic goog.script.init wrapper is heavy.
        # Here we test fetch_latest_for_phone against patched fetch_records.
        records = [
            {
                "phone": "01080792425",
                "body": "본인확인 인증번호(111111)입력시 정상처리 됩니다.",
                "sim": "A",
                "received_at": datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Seoul")),
                "received_at_raw": "07. 14. 오후 3:00",
                "received_at_iso": "2026-07-14T15:00:00+09:00",
                "row_index": 0,
            },
            {
                "phone": "01080792425",
                "body": "본인확인 인증번호(222222)입력시 정상처리 됩니다.",
                "sim": "A",
                "received_at": datetime(2026, 7, 14, 15, 30, tzinfo=ZoneInfo("Asia/Seoul")),
                "received_at_iso": "2026-07-14T15:30:00+09:00",
                "received_at_raw": "07. 14. 오후 3:30",
                "row_index": 1,
            },
            {
                "phone": "01052322496",
                "body": "본인확인 인증번호(333333)입력시 정상처리 됩니다.",
                "sim": "B",
                "received_at": datetime(2026, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Seoul")),
                "received_at_iso": "2026-07-14T16:00:00+09:00",
                "received_at_raw": "07. 14. 오후 4:00",
                "row_index": 2,
            },
        ]
        now = datetime(2026, 7, 14, 15, 40, tzinfo=ZoneInfo("Asia/Seoul"))
        with patch("sms.client.fetch_records", return_value=records):
            latest = fetch_latest_for_phone(
                "01080792425",
                window_seconds=0,
                now=now,
            )
            self.assertEqual(latest["code"], "222222")

            # 严格全等：不匹配去掉 0 的号码
            with self.assertRaises(LookupError):
                fetch_latest_for_phone("1080792425", window_seconds=0, now=now)

            # 窗口过滤：只保留最近 20 分钟 → 只有 15:30 一条
            latest_window = fetch_latest_for_phone(
                "01080792425",
                window_seconds=20 * 60,
                now=now,
            )
            self.assertEqual(latest_window["code"], "222222")

            with self.assertRaises(LookupError):
                fetch_latest_for_phone(
                    "01080792425",
                    window_seconds=5 * 60,
                    now=now,
                )

        # keep import used for static analyzers
        self.assertTrue(callable(extract_records_from_html))


if __name__ == "__main__":
    unittest.main()

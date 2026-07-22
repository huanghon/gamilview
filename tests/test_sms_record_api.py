from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app
from sms.client import clear_cache, extract_records_from_html, fetch_latest_for_phone_multi


def _wrap_table_html(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    table = f"<table><tr>{head}</tr>{body}</table>"
    # Mimic Apps Script payload nesting used by sms.client
    import json

    config = json.dumps({"userHtml": table})
    # Double-encoded JS string form expected by decoder
    js_string = json.dumps(config)[1:-1]
    return f'goog.script.init("{js_string}");'


class SmsSourceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.init_db()
        clear_cache()
        with app.db() as conn:
            conn.execute("DELETE FROM sms_sources")
            conn.execute("DELETE FROM record_links WHERE token LIKE 'test-sms-%'")
        # re-seed from env after wipe
        app.seed_sms_sources_from_env()
        self.client = TestClient(app.app)

    def tearDown(self) -> None:
        clear_cache()

    def test_list_and_crud_sources(self) -> None:
        listed = self.client.get("/api/sms-sources")
        self.assertEqual(listed.status_code, 200)
        sources = listed.json()["sources"]
        self.assertGreaterEqual(len(sources), 1)

        created = self.client.post(
            "/api/sms-sources",
            json={
                "name": "page2",
                "url": "https://example.com/script2",
                "enabled": True,
                "is_default": False,
                "note": "second",
            },
        )
        self.assertEqual(created.status_code, 200)
        source_id = created.json()["id"]
        self.assertEqual(created.json()["name"], "page2")

        updated = self.client.put(
            f"/api/sms-sources/{source_id}",
            json={
                "name": "page2b",
                "url": "https://example.com/script2b",
                "enabled": True,
                "is_default": True,
                "note": "now default",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["is_default"])
        self.assertEqual(updated.json()["name"], "page2b")

        listed2 = self.client.get("/api/sms-sources").json()["sources"]
        defaults = [s for s in listed2 if s["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["name"], "page2b")

        deleted = self.client.delete(f"/api/sms-sources/{source_id}")
        self.assertEqual(deleted.status_code, 200)

    def test_create_record_link_sms_all_and_named(self) -> None:
        self.client.post(
            "/api/sms-sources",
            json={"name": "alpha", "url": "https://example.com/a", "enabled": True},
        )
        resp_all = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "source": "sms:all", "window_seconds": 0},
        )
        self.assertEqual(resp_all.status_code, 200)
        self.assertEqual(resp_all.json()["source"], "sms")
        self.assertEqual(resp_all.json()["sms_source_ref"], "all")

        resp_named = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "source": "sms", "sms_source_ref": "alpha"},
        )
        self.assertEqual(resp_named.status_code, 200)
        self.assertEqual(resp_named.json()["sms_source_ref"], "alpha")

        # gmail_account compatibility: second column style
        resp_compat = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "gmail_account": "sms:all"},
        )
        self.assertEqual(resp_compat.status_code, 200)
        self.assertEqual(resp_compat.json()["source"], "sms")
        self.assertEqual(resp_compat.json()["sms_source_ref"], "all")

    def test_legacy_link_without_sms_source_ref_uses_default(self) -> None:
        fake_result = {
            "phone": "01080792425",
            "source": "sms",
            "sim": "A",
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
                    "test-sms-legacy",
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
            response = self.client.get("/api/v1/smpp/record?token=test-sms-legacy&format=txt2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "436501")
            mocked.assert_called_once()

    def test_all_mode_picks_latest_across_sources(self) -> None:
        with app.db() as conn:
            conn.execute("DELETE FROM sms_sources")
            now = app.now_iso()
            conn.execute(
                """
                INSERT INTO sms_sources (name, url, enabled, is_default, note, created_at, updated_at)
                VALUES (?, ?, 1, 1, '', ?, ?), (?, ?, 1, 0, '', ?, ?)
                """,
                (
                    "s1",
                    "https://example.com/1",
                    now,
                    now,
                    "s2",
                    "https://example.com/2",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO record_links (
                    token, phone, gmail_account, sender, window_seconds, source,
                    sms_source_ref, created_at, updated_at
                )
                VALUES (?, ?, '', '', ?, 'sms', 'all', ?, ?)
                """,
                ("test-sms-all", "01080792425", 999999999, app.now_iso(), app.now_iso()),
            )

        newer = {
            "phone": "01080792425",
            "source": "sms",
            "sim": "B",
            "received_at": "2026-07-14T16:00:00+09:00",
            "received_at_raw": "07. 14. 오후 4:00",
            "code": "999999",
            "body": "code 999999",
            "source_name": "s2",
        }
        with patch("app.fetch_latest_sms_for_phone_multi", return_value=newer) as mocked:
            response = self.client.get("/api/v1/smpp/record?token=test-sms-all&format=json")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["code"], "999999")
            self.assertEqual(data["sms_query_mode"], "all")
            mocked.assert_called_once()


class SmsHeaderCompatTests(unittest.TestCase):
    def test_old_and_new_headers(self) -> None:
        old_html = _wrap_table_html(
            ["시간", "전화번호", "문자내용", "SIM"],
            [["07. 14. 오후 3:37", "01080792425", "인증번호(111111)입니다", "A"]],
        )
        new_html = _wrap_table_html(
            ["받은시간", "전화번호", "인증번호", "SIM"],
            [["07. 14. 오후 4:10", "01080792425", "인증번호(222222)입니다", "B"]],
        )
        old_records = extract_records_from_html(old_html)
        new_records = extract_records_from_html(new_html)
        self.assertEqual(len(old_records), 1)
        self.assertEqual(old_records[0]["phone"], "01080792425")
        self.assertIn("111111", old_records[0]["body"])
        self.assertEqual(len(new_records), 1)
        self.assertIn("222222", new_records[0]["body"])

    def test_multi_source_latest(self) -> None:
        now = datetime(2026, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        records_a = [
            {
                "phone": "01080792425",
                "body": "인증번호(111111)",
                "sim": "A",
                "received_at": datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Seoul")),
                "received_at_raw": "x",
                "received_at_iso": "2026-07-14T15:00:00+09:00",
                "row_index": 0,
            }
        ]
        records_b = [
            {
                "phone": "01080792425",
                "body": "인증번호(222222)",
                "sim": "B",
                "received_at": datetime(2026, 7, 14, 15, 50, tzinfo=ZoneInfo("Asia/Seoul")),
                "received_at_raw": "y",
                "received_at_iso": "2026-07-14T15:50:00+09:00",
                "row_index": 0,
            }
        ]

        def fake_fetch_records(*, url: str, **kwargs):
            if url.endswith("/1"):
                return records_a
            return records_b

        with patch("sms.client.fetch_records", side_effect=fake_fetch_records):
            latest = fetch_latest_for_phone_multi(
                "01080792425",
                [
                    {"name": "s1", "url": "https://example.com/1"},
                    {"name": "s2", "url": "https://example.com/2"},
                ],
                window_seconds=0,
                now=now,
            )
        self.assertEqual(latest["code"], "222222")
        self.assertEqual(latest["source_name"], "s2")


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
        response = self.client.post(
            "/api/record-links",
            json={"phone": "01080792425", "gmail_account": "sms"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "sms")

    def test_txt2_returns_latest_sms_code(self) -> None:
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

        with patch("app.fetch_latest_sms_for_phone", return_value=fake_result):
            response = self.client.get("/api/v1/smpp/record?token=test-sms-success&format=json")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["code"], "436501")
            self.assertEqual(data["source"], "sms")


if __name__ == "__main__":
    unittest.main()

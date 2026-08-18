# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from core import (
    generic_api_mail_client,
    gmx_imap_client,
    imap_mail_client,
    mailcom_client,
    outlook_client,
    qqmail_client,
    recent_mail_service,
)
from webui.app import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecentMailServiceTests(unittest.TestCase):
    def test_mailcom_alias_dispatches_and_normalizes_plain_text_without_secrets(self):
        password = "MAIL-PASSWORD-SECRET"
        pickup_key = "alias_SUPER_SECRET_PICKUP_KEY"
        pickup_url = f"https://mail.example/messages?key={pickup_key}"
        row = {
            "email": "fixture@gmx.com",
            "password": password,
            "code_url": pickup_url,
            "refresh_token": "REFRESH-TOKEN-SECRET",
            "copy_line": f"fixture@gmx.com----{password}",
        }
        raw = [{
            "subject": f"Latest {password}",
            "from": {"emailAddress": {"name": "Sender", "address": "sender@example.com"}},
            "to": ["fixture@gmx.com"],
            "ts": 1_700_000_000,
            "bodyPreview": "Short preview",
            "html": (
                "<style>.hidden{display:none}</style><script>alert('x')</script>"
                f"<p>Hello {password}</p><p>{pickup_key}</p>"
            ),
        }]
        with mock.patch.object(
            recent_mail_service.db,
            "get_mailcom_email_by_email",
            return_value=row,
        ), mock.patch.object(mailcom_client, "list_recent_messages", return_value=raw) as fetch:
            result = recent_mail_service.fetch_recent_messages(
                "fixture@gmx.com", "gmx", limit=7
            )

        fetch.assert_called_once_with("fixture@gmx.com", limit=7)
        self.assertEqual(result["source"], "mailcom")
        self.assertEqual(result["count"], 1)
        message = result["messages"][0]
        self.assertEqual(
            set(message),
            {"subject", "from", "to", "received_at", "preview", "text"},
        )
        serialized = repr(result)
        for secret in (password, pickup_key, pickup_url, "REFRESH-TOKEN-SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("<script", message["text"])
        self.assertNotIn("alert('x')", message["text"])
        self.assertIn("Sender <sender@example.com>", message["from"])
        self.assertIn("Hello [REDACTED]", message["text"])

    def test_every_supported_source_uses_its_pool_lookup_and_provider(self):
        cases = (
            ("outlook", "get_outlook_by_email", outlook_client),
            ("generic_api", "get_generic_api_email_by_email", generic_api_mail_client),
            ("imap_pass", "get_imap_email_by_email", imap_mail_client),
            ("mailcom", "get_mailcom_email_by_email", mailcom_client),
            ("cloudflare_domain", "get_domain_email_by_email", qqmail_client),
        )
        for source, getter_name, provider in cases:
            with self.subTest(source=source), mock.patch.object(
                recent_mail_service.db,
                getter_name,
                return_value={"email": "box@example.com"},
            ), mock.patch.object(
                provider,
                "list_recent_messages",
                return_value=[{"subject": source, "text": "body"}],
            ) as fetch:
                result = recent_mail_service.fetch_recent_messages(
                    "box@example.com", source, limit=2
                )
                self.assertEqual(result["messages"][0]["subject"], source)
                fetch.assert_called_once_with("box@example.com", limit=2)

    def test_missing_pool_row_is_not_found_without_calling_provider(self):
        with mock.patch.object(
            recent_mail_service.db,
            "get_imap_email_by_email",
            return_value=None,
        ), mock.patch.object(imap_mail_client, "list_recent_messages") as fetch:
            with self.assertRaises(recent_mail_service.RecentMailNotFoundError):
                recent_mail_service.fetch_recent_messages(
                    "missing@example.com", "imap_pass"
                )
        fetch.assert_not_called()

    def test_provider_error_removes_password_token_and_pickup_query(self):
        password = "PASSWORD-SENTINEL"
        token = "TOKEN-SENTINEL-123456"
        pickup_url = f"https://pickup.example/messages?token={token}"
        row = {
            "email": "box@example.com",
            "password": password,
            "code_url": pickup_url,
        }
        error = RuntimeError(
            f"login failed password={password}; url={pickup_url}; Bearer {token}"
        )
        with mock.patch.object(
            recent_mail_service.db,
            "get_generic_api_email_by_email",
            return_value=row,
        ), mock.patch.object(
            generic_api_mail_client,
            "list_recent_messages",
            side_effect=error,
        ):
            with self.assertRaises(recent_mail_service.RecentMailFetchError) as caught:
                recent_mail_service.fetch_recent_messages(
                    "box@example.com", "generic_api"
                )
        detail = str(caught.exception)
        self.assertNotIn(password, detail)
        self.assertNotIn(token, detail)
        self.assertNotIn(pickup_url, detail)

    def test_limit_is_bounded_and_invalid_source_is_rejected(self):
        self.assertEqual(
            recent_mail_service.normalize_limit(999),
            recent_mail_service.MAX_LIMIT,
        )
        with self.assertRaises(recent_mail_service.RecentMailValidationError):
            recent_mail_service.normalize_limit(0)
        with self.assertRaises(recent_mail_service.RecentMailValidationError):
            recent_mail_service.normalize_source("unknown")

    def test_fetch_clamps_count_and_truncates_large_fields(self):
        raw = [{
            "subject": "s" * 900,
            "text": "<p>hello</p>" + ("x" * 25_000),
        } for _ in range(25)]
        with mock.patch.object(
            recent_mail_service.db,
            "get_imap_email_by_email",
            return_value={"email": "box@example.com", "password": "secret"},
        ), mock.patch.object(
            imap_mail_client,
            "list_recent_messages",
            return_value=raw,
        ) as fetch:
            result = recent_mail_service.fetch_recent_messages(
                "box@example.com", "imap_pass", limit=999
            )

        fetch.assert_called_once_with("box@example.com", limit=20)
        self.assertEqual(result["count"], 20)
        self.assertEqual(len(result["messages"][0]["subject"]), 500)
        self.assertEqual(len(result["messages"][0]["text"]), 20_000)
        self.assertNotIn("<p>", result["messages"][0]["text"])


class ProviderRecentMailTests(unittest.TestCase):
    def test_mailcom_recent_reader_requests_all_bodies_and_per_call_limit(self):
        account = mailcom_client.MailComAccount("fixture@mail.com", "secret")
        with mock.patch.object(
            mailcom_client,
            "get_account_context",
            return_value=account,
        ), mock.patch.object(
            mailcom_client,
            "_list_messages",
            return_value=[{"subject": "one"}],
        ) as fetch:
            result = mailcom_client.list_recent_messages("fixture@mail.com", limit=4)
        self.assertEqual(result, [{"subject": "one"}])
        fetch.assert_called_once_with(
            "fixture@mail.com",
            "secret",
            0.0,
            message_limit=4,
            include_all_bodies=True,
        )

    def test_imap_zero_cutoff_uses_all_and_limits_ids(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b"1 2 3 4"])
        conn.fetch.return_value = ("NO", [])
        rows = imap_mail_client._fetch_messages_since(
            conn, "INBOX", 0.0, message_limit=2
        )
        self.assertEqual(rows, [])
        conn.search.assert_called_once_with(None, "ALL")
        self.assertEqual(conn.fetch.call_count, 2)

    def test_gmx_zero_cutoff_uses_all(self):
        conn = mock.Mock()
        conn.select.return_value = ("OK", [])
        conn.search.return_value = ("OK", [b""])
        self.assertEqual(
            gmx_imap_client._fetch_messages_since(
                conn, "INBOX", 0.0, message_limit=5
            ),
            [],
        )
        conn.search.assert_called_once_with(None, "ALL")

    def test_qq_recent_reader_filters_shared_inbox_by_recipient(self):
        conn = mock.Mock()
        messages = [
            {"subject": "mine", "to": "Alias <mine@example.com>", "date": "2026-08-18T10:00:00Z"},
            {"subject": "other", "to": "other@example.com", "date": "2026-08-18T11:00:00Z"},
        ]
        with mock.patch.object(qqmail_client, "_connect_imap", return_value=conn), mock.patch.object(
            qqmail_client,
            "_search_messages",
            return_value=messages,
        ) as search:
            result = qqmail_client.list_recent_messages("mine@example.com", limit=3)
        self.assertEqual([item["subject"] for item in result], ["mine"])
        search.assert_called_once_with(
            conn,
            after_dt=None,
            message_limit=3,
            recipient="mine@example.com",
        )
        conn.logout.assert_called_once()

    def test_qq_recipient_matching_rejects_alias_prefix_collision(self):
        self.assertTrue(
            qqmail_client._recipient_matches(
                "Display <Mine@Example.com>, other@example.com",
                "mine@example.com",
            )
        )
        self.assertFalse(
            qqmail_client._recipient_matches(
                "Display <notmine@example.com>",
                "mine@example.com",
            )
        )

    def test_qq_search_asks_imap_for_target_recipient_first(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b""])
        self.assertEqual(
            qqmail_client._search_messages(
                conn,
                after_dt=None,
                message_limit=10,
                recipient="mine@example.com",
            ),
            [],
        )
        conn.search.assert_called_once_with(None, '(TO "mine@example.com")')

    def test_qq_search_peeks_without_marking_message_read(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b"7"])
        conn.fetch.return_value = (
            "OK",
            [(b"7 (BODY[] {55}", b"Subject: Hello\r\nTo: mine@example.com\r\n\r\nBody")],
        )
        messages = qqmail_client._search_messages(conn, message_limit=1)
        self.assertEqual(messages[0]["subject"], "Hello")
        conn.fetch.assert_called_once_with(b"7", "(BODY.PEEK[])")

    def test_outlook_recent_reader_uses_imap_when_graph_is_empty(self):
        account = outlook_client.OutlookAccount(
            "fixture@outlook.com", "password", "client", "refresh"
        )
        session = mock.Mock()
        imap_item = {
            "subject": "fallback",
            "date": "2026-08-18T10:00:00Z",
            "text": "body",
        }
        with mock.patch.object(
            outlook_client,
            "get_account_context",
            return_value=account,
        ), mock.patch.object(
            outlook_client,
            "_http_session",
            return_value=session,
        ), mock.patch.object(
            outlook_client,
            "_fetch_via",
            side_effect=[[], [imap_item]],
        ) as fetch:
            result = outlook_client.list_recent_messages(
                "fixture@outlook.com", limit=5
            )
        self.assertEqual(result, [imap_item])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["graph", "imap"])
        session.close.assert_called_once()


class RecentMailApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_api_returns_no_store_plain_message_payload(self):
        service_result = {
            "source": "mailcom",
            "email": "box@mail.com",
            "count": 1,
            "messages": [{
                "subject": "Latest",
                "from": "sender@example.com",
                "to": "box@mail.com",
                "received_at": "2026-08-18T10:00:00Z",
                "preview": "preview",
                "text": "body",
            }],
        }
        with mock.patch.object(
            recent_mail_service,
            "fetch_recent_messages",
            return_value=service_result,
        ) as fetch:
            response = self.client.get(
                "/api/email-pool/recent-messages"
                "?email=box%40mail.com&source=mailcom&limit=6"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        fetch.assert_called_once_with(
            email="box@mail.com", source="mailcom", limit="6"
        )

    def test_api_maps_validation_not_found_and_fetch_errors(self):
        cases = (
            (recent_mail_service.RecentMailValidationError("bad request"), 400),
            (recent_mail_service.RecentMailNotFoundError("missing"), 404),
            (recent_mail_service.RecentMailFetchError("upstream"), 502),
        )
        for error, expected_status in cases:
            with self.subTest(status=expected_status), mock.patch.object(
                recent_mail_service,
                "fetch_recent_messages",
                side_effect=error,
            ):
                response = self.client.get(
                    "/api/email-pool/recent-messages"
                    "?email=box%40mail.com&source=mailcom"
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertFalse(response.get_json()["ok"])
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))


class RecentMailUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (PROJECT_ROOT / "webui" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_pool_has_recent_mail_button_and_dialog_states(self):
        for marker in (
            "data-pool-recent-mail",
            'id="recentMailModal"',
            'id="recentMailList"',
            'id="recentMailDetailBody"',
            "正在读取最近 10 封邮件",
            "该邮箱暂无最近邮件",
            "读取失败",
        ):
            self.assertIn(marker, self.html)

    def test_message_fields_render_as_text_and_each_row_is_selectable(self):
        self.assertIn("body.textContent = mail.body", self.html)
        self.assertIn("subject.textContent = mail.subject", self.html)
        self.assertIn("button.addEventListener('click', () => selectRecentMail(index))", self.html)
        self.assertNotIn("recentMailDetailBody').innerHTML", self.html)


if __name__ == "__main__":
    unittest.main()

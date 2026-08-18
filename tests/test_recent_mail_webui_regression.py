# -*- coding: utf-8 -*-
"""邮箱池最近邮件的跨层与 WebUI 回归测试。"""
from __future__ import annotations

import re
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from core import mailcom_client, qqmail_client, recent_mail_service
from webui.app import create_app


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_INDEX_TEMPLATE = _PROJECT_ROOT / "webui" / "templates" / "index.html"


def _client():
    """创建不触碰工作区任务恢复状态的已鉴权 Flask 测试客户端。"""
    with ExitStack() as stack:
        for name in (
            "recover_interrupted_plan_checks",
            "recover_interrupted_subscription_cancels",
            "recover_interrupted_extract_links",
            "recover_interrupted_live_checks",
            "recover_interrupted_twofa",
            "recover_interrupted_codex_agents",
        ):
            stack.enter_context(
                mock.patch(f"webui.app.db.{name}", return_value=0)
            )
        client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def _template_source() -> str:
    return _INDEX_TEMPLATE.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


class RecentMailCrossLayerTests(unittest.TestCase):
    def test_selected_pool_source_must_contain_the_email(self):
        """同地址即使存在于其他池，也不能绕过请求中明确选择的来源。"""
        with mock.patch.object(
            recent_mail_service.db,
            "get_mailcom_email_by_email",
            return_value=None,
        ) as selected_getter, mock.patch.object(
            recent_mail_service.db,
            "get_generic_api_email_by_email",
            return_value={"email": "same@example.com", "code_url": "https://pickup.invalid"},
        ) as other_getter, mock.patch.object(
            mailcom_client,
            "list_recent_messages",
        ) as provider:
            with self.assertRaises(recent_mail_service.RecentMailNotFoundError):
                recent_mail_service.fetch_recent_messages(
                    "same@example.com", "mailcom", limit=10
                )

        selected_getter.assert_called_once_with("same@example.com")
        other_getter.assert_not_called()
        provider.assert_not_called()

    def test_limit_is_clamped_before_provider_call_and_result_slicing(self):
        rows = [
            {"subject": f"mail-{index}", "text": f"body-{index}"}
            for index in range(30)
        ]
        with mock.patch.object(
            recent_mail_service.db,
            "get_mailcom_email_by_email",
            return_value={"email": "box@mail.com", "password": "secret"},
        ), mock.patch.object(
            mailcom_client,
            "list_recent_messages",
            return_value=rows,
        ) as provider:
            result = recent_mail_service.fetch_recent_messages(
                "box@mail.com", "mailcom", limit="999"
            )

        provider.assert_called_once_with(
            "box@mail.com", limit=recent_mail_service.MAX_LIMIT
        )
        self.assertEqual(result["count"], recent_mail_service.MAX_LIMIT)
        self.assertEqual(len(result["messages"]), recent_mail_service.MAX_LIMIT)

    def test_html_only_body_becomes_plain_text_and_ignores_active_content(self):
        raw = [{
            "subject": "<b>Hello &amp; welcome</b>",
            "from": "sender@example.com",
            "html": (
                "<!doctype html><style>.secret{display:none}</style>"
                "<script>window.evil = true</script>"
                "<p>Hello &amp; <strong>world</strong></p><br><div>Line two</div>"
            ),
        }]
        with mock.patch.object(
            recent_mail_service.db,
            "get_mailcom_email_by_email",
            return_value={"email": "box@mail.com", "password": "secret"},
        ), mock.patch.object(
            mailcom_client,
            "list_recent_messages",
            return_value=raw,
        ):
            result = recent_mail_service.fetch_recent_messages(
                "box@mail.com", "mailcom"
            )

        message = result["messages"][0]
        self.assertEqual(message["subject"], "Hello & welcome")
        self.assertIn("Hello & world", message["text"])
        self.assertIn("Line two", message["text"])
        self.assertNotRegex(message["text"], r"(?i)<script|window\.evil|display:none")

    def test_flask_response_is_no_store_and_does_not_serialize_pool_secrets(self):
        password = "MAILBOX-PASSWORD-SENTINEL"
        refresh_token = "REFRESH-TOKEN-SENTINEL"
        pickup_key = "PICKUP-KEY-SENTINEL-123456"
        row = {
            "email": "box@mail.com",
            "password": password,
            "refresh_token": refresh_token,
            "code_url": f"https://pickup.invalid/messages?key={pickup_key}",
            "copy_line": f"box@mail.com----{password}",
        }
        provider_rows = [{
            "subject": f"subject {password}",
            "text": f"body {refresh_token} {pickup_key}",
            "html": "<p>safe body</p>",
        }]
        with mock.patch.object(
            recent_mail_service.db,
            "get_mailcom_email_by_email",
            return_value=row,
        ), mock.patch.object(
            mailcom_client,
            "list_recent_messages",
            return_value=provider_rows,
        ):
            response = _client().get(
                "/api/email-pool/recent-messages"
                "?source=mailcom&email=box%40mail.com&limit=10"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")
        serialized = response.get_data(as_text=True)
        for secret in (password, refresh_token, pickup_key, row["code_url"], row["copy_line"]):
            self.assertNotIn(secret, serialized)
        payload = response.get_json()
        self.assertEqual(
            set(payload["messages"][0]),
            {"subject", "from", "to", "received_at", "preview", "text"},
        )


class QqRecentMailRecipientTests(unittest.TestCase):
    def test_shared_inbox_search_uses_server_side_to_filter(self):
        conn = mock.Mock()
        conn.search.return_value = ("OK", [b""])

        result = qqmail_client._search_messages(
            conn,
            after_dt=None,
            message_limit=10,
            recipient="mine@example.com",
        )

        self.assertEqual(result, [])
        conn.search.assert_called_once_with(None, '(TO "mine@example.com")')

    def test_recipient_filter_uses_exact_address_not_substring(self):
        """mine@example.com 不得命中 notmine@example.com。"""
        conn = mock.Mock()
        messages = [
            {
                "subject": "prefix collision",
                "to": "Not Mine <notmine@example.com>",
                "date": "2026-08-18T12:00:00Z",
            },
            {
                "subject": "exact recipient",
                "to": "Owner <MINE@example.com>, secondary@example.com",
                "date": "2026-08-18T11:00:00Z",
            },
        ]
        with mock.patch.object(
            qqmail_client, "_connect_imap", return_value=conn
        ), mock.patch.object(
            qqmail_client, "_search_messages", return_value=messages
        ):
            result = qqmail_client.list_recent_messages(
                "mine@example.com", limit=10
            )

        self.assertEqual(
            [item["subject"] for item in result],
            ["exact recipient"],
        )
        conn.logout.assert_called_once_with()


class RecentMailTemplateTests(unittest.TestCase):
    def test_pool_rows_open_recent_mail_modal_with_encoded_request(self):
        source = _template_source()
        render = _between(source, "function renderOutlook()", "function updateOutlookSelectionUi")
        click = _between(source, "async function onOutlookBodyClick", "(function bindOutlookV2")
        loader = _between(source, "async function loadRecentMails()", "function openRecentMailModal")

        self.assertIn("data-pool-recent-mail", render)
        self.assertIn("data-email=\"${email}\"", render)
        self.assertIn("data-source=\"${src}\"", render)
        self.assertIn("openRecentMailModal(", click)
        self.assertIn("encodeURIComponent(source)", loader)
        self.assertIn("encodeURIComponent(email)", loader)
        self.assertIn("limit=10", loader)

    def test_modal_has_loading_empty_error_refresh_and_stale_response_states(self):
        source = _template_source()
        recent = _between(source, "let RECENT_MAIL_REQUEST_SEQ", "async function loadOutlook()")

        for expected in (
            "正在读取最近 10 封邮件…",
            "该邮箱暂无最近邮件",
            "读取失败：${RECENT_MAIL_CONTEXT.error}",
            "refresh.disabled = RECENT_MAIL_CONTEXT.loading",
            "refresh.textContent = RECENT_MAIL_CONTEXT.loading ? '刷新中…' : '刷新'",
            "if (requestSeq !== RECENT_MAIL_REQUEST_SEQ) return",
            "RECENT_MAIL_REQUEST_SEQ += 1",
        ):
            self.assertIn(expected, recent)
        self.assertIn("RECENT_MAIL_CONTEXT.messages.forEach", recent)
        self.assertIn("button.addEventListener('click', () => selectRecentMail(index))", recent)
        self.assertIn("RECENT_MAIL_CONTEXT.selected = RECENT_MAIL_CONTEXT.messages.length ? 0 : -1", recent)

    def test_mail_content_is_rendered_only_through_text_nodes(self):
        source = _template_source()
        recent = _between(source, "let RECENT_MAIL_REQUEST_SEQ", "async function loadOutlook()")

        self.assertIn("if (body) body.textContent = mail.body", recent)
        self.assertIn("subject.textContent = mail.subject", recent)
        self.assertIn("preview.textContent =", recent)
        self.assertIn("list.replaceChildren()", recent)
        self.assertNotIn("innerHTML", recent)
        self.assertNotRegex(recent, re.compile(r"\.insertAdjacentHTML\s*\("))

    def test_modal_is_in_scroll_lock_and_has_all_close_paths(self):
        source = _template_source()
        self.assertIn("|| !$('#recentMailModal').classList.contains('hidden')", source)
        for element_id in (
            "recentMailModal",
            "recentMailList",
            "recentMailDetailBody",
            "btnCloseRecentMail",
            "btnCloseRecentMail2",
            "btnRefreshRecentMail",
        ):
            self.assertIn(f'id="{element_id}"', source)
        bindings = _between(source, "function bindRecentMailModal()", "async function loadOutlook()")
        self.assertIn("bind('btnCloseRecentMail', closeRecentMailModal)", bindings)
        self.assertIn("bind('btnCloseRecentMail2', closeRecentMailModal)", bindings)
        self.assertIn("bind('btnRefreshRecentMail', loadRecentMails)", bindings)
        self.assertIn("event.target === modal", bindings)
        self.assertIn("event.key === 'Escape'", bindings)


if __name__ == "__main__":
    unittest.main()

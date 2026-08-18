# -*- coding: utf-8 -*-
"""回归：Codex 手机验证在异常/停止时必须释放已取出的号码。"""

from types import SimpleNamespace
from unittest import mock

import pytest

from config import codex as cfg
from core import codex_oauth
from core import sms_provider


class _Http:
    def __init__(self):
        self.close = mock.Mock()


def _response(status=200, body=None):
    body = {} if body is None else body
    return SimpleNamespace(status_code=status, text="", json=lambda: body)


def _run_with_common_patches(*, post_side_effect=None, post_return=None, wait_return="123456", **overrides):
    """执行手机号流程并返回 fake provider mocks。"""
    http = _Http()
    values = {
        "SMS_MAX_RETRIES": 1,
        "SMS_CODE_WAIT": 1,
        "SMS_POLL_INTERVAL": 1,
        "SMS_PROVIDER": "grizzly",
    }
    values.update(overrides)
    patches = [
        mock.patch.object(cfg, key, value) for key, value in values.items()
    ]
    for item in patches:
        item.start()

    acquire = mock.patch.object(sms_provider, "_http", return_value=http)
    acquire_mock = acquire.start()
    acquire_number = mock.patch.object(sms_provider, "acquire_number", return_value=("activation-1", "15551234567"))
    acquire_number_mock = acquire_number.start()
    wait = mock.patch.object(sms_provider, "wait_for_sms_code", return_value=wait_return)
    wait_mock = wait.start()
    mark = mock.patch.object(sms_provider, "mark_sms_sent")
    mark_mock = mark.start()
    cancel = mock.patch.object(sms_provider, "cancel")
    cancel_mock = cancel.start()
    complete = mock.patch.object(sms_provider, "complete")
    complete_mock = complete.start()
    post = mock.patch.object(
        codex_oauth,
        "_post_json",
        side_effect=post_side_effect,
        return_value=post_return or _response(),
    )
    post_mock = post.start()
    sleep = mock.patch.object(codex_oauth, "_sleep_before_phone_retry")
    sleep_mock = sleep.start()
    return (
        http,
        cancel_mock,
        complete_mock,
        post_mock,
        patches + [acquire, acquire_number, wait, mark, cancel, complete, post, sleep],
    )


def _stop_all(items):
    for item in reversed(items):
        item.stop()


def test_send_exception_releases_number_with_normal_cancel_and_preserves_exception():
    error = RuntimeError("send transport failed")
    http, cancel, _complete, _post, patches = _run_with_common_patches(post_side_effect=error)
    try:
        with pytest.raises(RuntimeError) as caught:
            codex_oauth._do_phone_verification(object())
        assert caught.value is error
        cancel.assert_called_once_with("activation-1", http)
    finally:
        _stop_all(patches)


def test_validate_exception_marks_number_bad_after_otp_received():
    error = RuntimeError("validate transport failed")
    # First POST is add-phone/send; second is phone-otp/validate.
    calls = [_response(200), error]
    http, cancel, _complete, _post, patches = _run_with_common_patches(
        post_side_effect=calls,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            codex_oauth._do_phone_verification(object())
        assert caught.value is error
        cancel.assert_called_once_with("activation-1", http, bad=True)
    finally:
        _stop_all(patches)


def test_stop_exception_is_re_raised_unchanged_and_still_releases_number():
    class CodexRetryStopped(Exception):
        pass

    stop = CodexRetryStopped("用户手动停止 Codex 补跑")
    http, cancel, _complete, _post, patches = _run_with_common_patches(post_side_effect=stop)
    try:
        with pytest.raises(CodexRetryStopped) as caught:
            codex_oauth._do_phone_verification(object())
        assert caught.value is stop
        cancel.assert_called_once_with("activation-1", http)
    finally:
        _stop_all(patches)


def test_cleanup_exception_does_not_replace_openai_exception():
    error = RuntimeError("OpenAI request failed")
    http, cancel, _complete, _post, patches = _run_with_common_patches(post_side_effect=error)
    cancel.side_effect = RuntimeError("cancel transport failed")
    try:
        with pytest.raises(RuntimeError) as caught:
            codex_oauth._do_phone_verification(object())
        assert caught.value is error
    finally:
        _stop_all(patches)

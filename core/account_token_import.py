# -*- coding: utf-8 -*-
"""Import ChatGPT accounts from OpenAI access/refresh tokens.

This module deliberately keeps OpenAI refresh tokens out of the account row:
``core.db`` stores them in ``codex_accounts`` while the account table keeps its
existing Outlook mail ``refresh_token`` semantics.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable

from core import db, plan_check_service
from core.openai_token_refresh import (
    extract_openai_token_metadata,
    refresh_openai_token,
)


logger = logging.getLogger(__name__)

_MAX_IMPORT_BYTES = 2 * 1024 * 1024
_MAX_IMPORT_RECORDS = 500
_MIN_TOKEN_LENGTH = 16
_PAIR_SEPARATORS = ("----", "====")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_TOKEN_LABEL_RE = re.compile(
    r"^(?P<label>AT|RT|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*(?P<value>.+)$",
    re.IGNORECASE,
)

_ACCESS_KEYS = ("access_token", "accessToken", "at", "AT")
_REFRESH_KEYS = ("refresh_token", "refreshToken", "rt", "RT")
_ID_KEYS = ("id_token", "idToken")


class TokenImportError(ValueError):
    """A user-facing import error that never contains credential text."""


def _first_value(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def _normalize_token(value: Any) -> str:
    token = str(value or "").strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _validate_token(token: str, label: str) -> str:
    value = _normalize_token(token)
    if len(value) < _MIN_TOKEN_LENGTH or any(ch.isspace() for ch in value):
        raise TokenImportError(f"{label} 格式不正确")
    return value


def _looks_like_jwt(token: str) -> bool:
    return bool(_JWT_RE.fullmatch(_normalize_token(token)))


def _labeled_token(value: str) -> tuple[str | None, str]:
    text = str(value or "").strip()
    match = _TOKEN_LABEL_RE.fullmatch(text)
    if not match:
        return None, _normalize_token(text)
    label = match.group("label").lower().replace(" ", "").replace("-", "").replace("_", "")
    kind = "rt" if label in {"rt", "refreshtoken"} else "at"
    return kind, _normalize_token(match.group("value"))


def _record_from_json(value: Any, *, line_no: int) -> dict[str, Any]:
    if isinstance(value, str):
        return _record_from_line(value, line_no=line_no)
    if not isinstance(value, dict):
        raise TokenImportError("JSON 条目必须是对象或 Token 字符串")

    access_token = _normalize_token(_first_value(value, _ACCESS_KEYS))
    refresh_token = _normalize_token(_first_value(value, _REFRESH_KEYS))
    id_token = _normalize_token(_first_value(value, _ID_KEYS))
    if not access_token and not refresh_token:
        raise TokenImportError("JSON 条目缺少 access_token 或 refresh_token")
    if access_token:
        access_token = _validate_token(access_token, "AT")
    if refresh_token:
        refresh_token = _validate_token(refresh_token, "RT")

    record: dict[str, Any] = {
        "line": line_no,
        "source": "at_rt" if access_token and refresh_token else "at" if access_token else "rt",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
    }
    aliases = {
        "email": ("email",),
        "account_id": ("account_id", "accountId", "chatgpt_account_id"),
        "plan_type": ("plan_type", "planType", "chatgpt_plan_type"),
        "user_id": ("user_id", "userId", "chatgpt_user_id"),
        "user_name": ("user_name", "userName", "name"),
        "token_expires_at": ("token_expires_at", "expired", "expires_at"),
        "oauth_client_id": ("oauth_client_id", "token_client_id", "client_id"),
    }
    for target, keys in aliases.items():
        item = _first_value(value, keys)
        if item not in (None, ""):
            record[target] = str(item).strip()
    if isinstance(value.get("token_expired"), bool):
        record["token_expired"] = value["token_expired"]
    return record


def _record_from_pair(left: str, right: str, *, line_no: int) -> dict[str, Any]:
    left_kind, left_value = _labeled_token(left)
    right_kind, right_value = _labeled_token(right)
    if not left_value or not right_value:
        raise TokenImportError("AT----RT 两段都必须有值")

    if left_kind and right_kind and left_kind == right_kind:
        raise TokenImportError("AT----RT 中出现了两个相同类型的 Token")
    if left_kind == "rt" or right_kind == "at":
        access_token, refresh_token = right_value, left_value
    elif left_kind == "at" or right_kind == "rt":
        access_token, refresh_token = left_value, right_value
    elif _looks_like_jwt(right_value) and not _looks_like_jwt(left_value):
        access_token, refresh_token = right_value, left_value
    else:
        # 无标签且无法从 JWT 外形区分时，遵循界面声明的 AT----RT 顺序。
        access_token, refresh_token = left_value, right_value

    return {
        "line": line_no,
        "source": "at_rt",
        "access_token": _validate_token(access_token, "AT"),
        "refresh_token": _validate_token(refresh_token, "RT"),
        "id_token": "",
    }


def _record_from_line(line: str, *, line_no: int) -> dict[str, Any]:
    text = str(line or "").strip()
    if not text:
        raise TokenImportError("Token 为空")
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            raise TokenImportError("JSON 格式不正确") from None
        return _record_from_json(parsed, line_no=line_no)

    for separator in _PAIR_SEPARATORS:
        if separator in text:
            left, right = text.split(separator, 1)
            return _record_from_pair(left, right, line_no=line_no)

    kind, token = _labeled_token(text)
    if kind is None:
        kind = "at" if _looks_like_jwt(token) else "rt"
    token = _validate_token(token, kind.upper())
    return {
        "line": line_no,
        "source": kind,
        "access_token": token if kind == "at" else "",
        "refresh_token": token if kind == "rt" else "",
        "id_token": "",
    }


def _whole_json_entries(text: str) -> list[Any] | None:
    stripped = text.strip()
    if not stripped.startswith(("{", "[", '"')):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("accounts", "tokens", "credentials", "items"):
            nested = parsed.get(key)
            if isinstance(nested, list):
                return nested
        return [parsed]
    if isinstance(parsed, str):
        return [parsed]
    return [parsed]


def _record_fingerprint(record: dict[str, Any]) -> str:
    material = "\0".join(
        str(record.get(key) or "").strip()
        for key in ("access_token", "refresh_token", "id_token")
    )
    return hashlib.sha256(("account-token-import\0" + material).encode("utf-8")).hexdigest()


def parse_token_import_text(text: str) -> dict[str, Any]:
    """Parse supported import formats without returning secrets in errors."""
    if not isinstance(text, str):
        raise TokenImportError("text 必须是字符串")
    if len(text.encode("utf-8")) > _MAX_IMPORT_BYTES:
        raise TokenImportError("导入内容过大，单次最多 2 MB")
    if not text.strip():
        raise TokenImportError("请粘贴 AT、RT 或 Token JSON")

    entries = _whole_json_entries(text)
    candidates: list[tuple[int, Any, bool]] = []
    if entries is not None:
        candidates = [(index, value, True) for index, value in enumerate(entries, start=1)]
    else:
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            candidates.append((line_no, line, False))

    if len(candidates) > _MAX_IMPORT_RECORDS:
        raise TokenImportError(f"单次最多导入 {_MAX_IMPORT_RECORDS} 条 Token")

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    seen: set[str] = set()
    for line_no, value, from_json in candidates:
        try:
            record = (
                _record_from_json(value, line_no=line_no)
                if from_json
                else _record_from_line(str(value), line_no=line_no)
            )
            fingerprint = _record_fingerprint(record)
            if fingerprint in seen:
                skipped += 1
                continue
            seen.add(fingerprint)
            records.append(record)
        except TokenImportError as exc:
            errors.append({"line": line_no, "error": str(exc)})

    return {
        "records": records,
        "errors": errors,
        "parsed": len(candidates),
        "skipped": skipped,
    }


def _valid_email(value: Any) -> str:
    email = str(value or "").strip()
    if not email or "@" not in email or any(ch.isspace() for ch in email):
        raise TokenImportError("未能从 Token 识别邮箱")
    return email


def _merge_identity(record: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    incoming_email = str(record.get("email") or "").strip()
    token_email = str(metadata.get("email") or "").strip()
    if incoming_email and token_email and incoming_email.casefold() != token_email.casefold():
        raise TokenImportError("JSON 邮箱与 Token 邮箱不一致")
    incoming_account_id = str(record.get("account_id") or "").strip()
    token_account_id = str(metadata.get("account_id") or "").strip()
    if incoming_account_id and token_account_id and incoming_account_id != token_account_id:
        raise TokenImportError("JSON account_id 与 Token account_id 不一致")

    merged["email"] = _valid_email(token_email or incoming_email)
    for key in (
        "account_id", "plan_type", "user_id", "user_name",
        "token_client_id", "token_expires_at", "token_expired",
    ):
        if metadata.get(key) not in (None, ""):
            target = "oauth_client_id" if key == "token_client_id" else key
            merged[target] = metadata[key]
    return merged


def _refresh_record(
    record: dict[str, Any],
    *,
    refresh_fn: Callable[..., dict[str, Any]],
    max_attempts: int,
) -> dict[str, Any]:
    current = dict(record)
    last_result: dict[str, Any] = {}
    for _attempt in range(1, max(1, min(4, int(max_attempts))) + 1):
        last_result = refresh_fn(str(current.get("refresh_token") or ""))
        if last_result.get("ok"):
            for key in (
                "access_token", "refresh_token", "id_token", "email",
                "account_id", "plan_type", "user_id", "user_name",
                "token_expires_at", "token_expired", "oauth_client_id",
            ):
                if last_result.get(key) not in (None, ""):
                    current[key] = last_result[key]
            return current
        if not last_result.get("retryable"):
            break
        # The built-in refresher owns the complete four-route proxy budget.
        # Keep this loop for injected/custom refresh functions without
        # multiplying a built-in network retry into sixteen requests.
        if last_result.get("retry_exhausted"):
            break

    reason = str(last_result.get("reason") or "refresh_failed")[:80]
    detail = str(last_result.get("error") or "RT 刷新失败")[:240]
    raise TokenImportError(f"RT 刷新失败（{reason}）：{detail}")


def _prepare_record(
    record: dict[str, Any],
    *,
    refresh_fn: Callable[..., dict[str, Any]],
    refresh_attempts: int,
) -> dict[str, Any]:
    prepared = dict(record)
    access_token = str(prepared.get("access_token") or "").strip()
    metadata = extract_openai_token_metadata(
        access_token,
        id_token=str(prepared.get("id_token") or ""),
    ) if access_token else {}

    if prepared.get("refresh_token") and (
        not access_token or metadata.get("token_expired") is True
    ):
        prepared = _refresh_record(
            prepared,
            refresh_fn=refresh_fn,
            max_attempts=refresh_attempts,
        )
        access_token = str(prepared.get("access_token") or "").strip()
        metadata = extract_openai_token_metadata(
            access_token,
            id_token=str(prepared.get("id_token") or ""),
        )

    if not access_token:
        raise TokenImportError("未获得可导入的 AT")
    return _merge_identity(prepared, metadata)


def import_account_tokens(
    text: str,
    *,
    refresh_fn: Callable[..., dict[str, Any]] = refresh_openai_token,
    enqueue_fn: Callable[..., dict[str, Any]] = plan_check_service.enqueue_account_plan_check,
    refresh_attempts: int = 4,
) -> dict[str, Any]:
    """Import tokens and queue a full account-plan refresh for every success."""
    parsed = parse_token_import_text(text)
    errors = list(parsed["errors"])
    items: list[dict[str, Any]] = []
    inserted = 0
    updated = 0
    plan_queued = 0

    for raw_record in parsed["records"]:
        line_no = int(raw_record.get("line") or 0)
        source = str(raw_record.get("source") or "token")
        try:
            record = _prepare_record(
                raw_record,
                refresh_fn=refresh_fn,
                refresh_attempts=refresh_attempts,
            )
            saved = db.upsert_token_account(record)
            action = str(saved.get("action") or "updated")
            if action == "inserted":
                inserted += 1
            else:
                updated += 1

            query_state = "not_queued"
            try:
                queued = enqueue_fn(
                    account_id=int(saved["id"]),
                    email=str(saved["email"]),
                    access_token=str(record.get("access_token") or ""),
                    trigger="token_import",
                )
                if queued.get("accepted"):
                    query_state = "queued"
                    plan_queued += 1
                elif queued.get("busy"):
                    query_state = "busy"
                else:
                    query_state = "not_queued"
            except Exception as exc:
                query_state = "not_queued"
                logger.warning(
                    "Token 导入后套餐查询入队异常 line=%s email=%s type=%s",
                    line_no,
                    saved.get("email"),
                    type(exc).__name__,
                )

            items.append({
                "line": line_no,
                "email": saved.get("email"),
                "source": source,
                "action": action,
                "plan": record.get("plan_type") or "unknown",
                "plan_query": query_state,
            })
        except TokenImportError as exc:
            errors.append({"line": line_no, "error": str(exc)})
        except (TypeError, ValueError) as exc:
            # DB validation errors contain field names only, never credentials.
            errors.append({"line": line_no, "error": str(exc)[:240]})
        except Exception as exc:
            logger.warning(
                "Token 导入处理异常 line=%s type=%s",
                line_no,
                type(exc).__name__,
            )
            errors.append({"line": line_no, "error": "账号写入失败，请查看服务日志"})

    return {
        "ok": not errors,
        "parsed": int(parsed["parsed"]),
        "inserted": inserted,
        "updated": updated,
        "skipped": int(parsed["skipped"]),
        "failed": len(errors),
        "plan_queued": plan_queued,
        "items": items,
        "errors": errors,
    }


__all__ = [
    "TokenImportError",
    "import_account_tokens",
    "parse_token_import_text",
]

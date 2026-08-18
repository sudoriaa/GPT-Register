# -*- coding: utf-8 -*-
"""持久化的 1K50 CDK 池。

CDK 是可消耗的凭据。这个模块把完整值只保存在本地池文件和工作线程的
短生命周期对象中，列表/日志接口只返回脱敏值。所有状态变更都在进程内
加锁并以替换文件方式落盘，WebUI 重启后仍可继续轮换。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CDK_POOL_PATH = _ROOT / "data" / "cdk_pool.json"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CdkPoolError(RuntimeError):
    """Base error for pool state transitions."""


class CdkPoolEmptyError(CdkPoolError):
    """Raised by strict lease helpers when no code is available."""


class CdkLeaseError(CdkPoolError):
    """Raised when a lease operation references a missing/active entry."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve()).casefold()


def _lock_for(path: Path) -> threading.RLock:
    key = _path_key(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def normalize_code(value: object) -> str:
    """清理一行 CDK；允许复制文本中的空白，但不改变大小写。"""
    return "".join(str(value or "").strip().split())


def fingerprint(value: object) -> str:
    return hashlib.sha256(normalize_code(value).encode("utf-8")).hexdigest()[:24]


def mask_code(value: object) -> str:
    code = normalize_code(value)
    if not code:
        return ""
    if len(code) <= 8:
        return code[:2] + "***" + code[-2:]
    return f"{code[:4]}…{code[-4:]}"


# Compatibility names used by the first client draft and by integrations.
redact_cdk = mask_code


@dataclass(frozen=True)
class CdkLease:
    code: str = field(repr=False)
    record_id: str
    fingerprint: str = ""
    remaining_uses: int | None = None

    @property
    def lease_id(self) -> str:
        return self.record_id


class CdkPool:
    """Thread-safe JSON-backed CDK pool.

    ``lease`` 返回内部使用的完整 code；WebUI 应使用 ``list_public``，该
    方法永远不会返回完整 CDK。测试和上层服务可以传入临时 path 隔离数据。
    """

    def __init__(self, path: str | os.PathLike | None = None, *, lease_ttl: float = 30 * 60):
        self.path = Path(path or DEFAULT_CDK_POOL_PATH).expanduser()
        self._lock = _lock_for(self.path)
        self.lease_ttl = max(1.0, float(lease_ttl))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _recover_stale_locked(self, rows: list[dict]) -> int:
        """Return abandoned leases to the queue after a process restart."""
        now = datetime.now()
        recovered = 0
        for row in rows:
            if row.get("status") != "leased":
                continue
            raw = row.get("updated_at") or row.get("last_used_at")
            try:
                age = (now - datetime.fromisoformat(str(raw))).total_seconds()
                remaining = row.get("remaining_uses")
                has_remaining = remaining in (None, "") or int(remaining or 0) > 0
            except (TypeError, ValueError):
                continue
            if age < self.lease_ttl:
                continue
            row["status"] = "available" if has_remaining else "exhausted"
            row["leased_task_id"] = None
            row["last_error"] = "CDK 租约超时，已自动回收"
            row["updated_at"] = _now()
            recovered += 1
        return recovered

    @staticmethod
    def _safe_error(row: dict, error: object) -> str:
        text = str(error or "")
        code = str(row.get("code") or "")
        if code:
            text = text.replace(code, "[REDACTED]")
        return text[:500]

    def _read(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw or "[]")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if isinstance(data, dict):
            data = data.get("items") or data.get("cdks") or []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = normalize_code(item.get("code"))
            if not code:
                continue
            row = dict(item)
            row["id"] = str(row.get("id") or uuid.uuid4().hex)
            row["code"] = code
            row["fingerprint"] = str(row.get("fingerprint") or fingerprint(code))
            row["status"] = str(row.get("status") or "available").lower()
            if row["status"] not in {"available", "leased", "exhausted", "invalid", "error"}:
                row["status"] = "available"
            out.append(row)
        return out

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
        try:
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _public(row: dict) -> dict:
        last_error = str(row.get("last_error") or "")
        raw_code = str(row.get("code") or "")
        if raw_code:
            last_error = last_error.replace(raw_code, "[REDACTED]")
        return {
            "id": row.get("id"),
            "fingerprint": row.get("fingerprint"),
            "display_code": mask_code(row.get("code")),
            "status": row.get("status") or "available",
            "remaining_uses": row.get("remaining_uses"),
            "leased_task_id": row.get("leased_task_id"),
            "last_error": last_error[:500],
            "last_used_at": row.get("last_used_at"),
            "updated_at": row.get("updated_at"),
        }

    def list(self, *, include_code: bool = False) -> list[dict]:
        with self._lock:
            rows = self._read()
            if self._recover_stale_locked(rows):
                self._write(rows)
            if include_code:
                return [dict(row) for row in rows]
            return [self._public(row) for row in rows]

    def list_public(self) -> list[dict]:
        return self.list(include_code=False)

    def import_codes(self, values: str | Iterable[str], *, replace: bool = False) -> dict:
        if isinstance(values, str):
            incoming = values.splitlines()
        else:
            incoming = list(values or [])
        normalized: list[str] = []
        seen: set[str] = set()
        for value in incoming:
            code = normalize_code(value)
            if not code or code in seen:
                continue
            seen.add(code)
            normalized.append(code)
        with self._lock:
            old = [] if replace else self._read()
            by_fp = {str(row.get("fingerprint") or fingerprint(row.get("code"))): row for row in old}
            added = 0
            for code in normalized:
                fp = fingerprint(code)
                if fp in by_fp:
                    # A failed/invalid CDK can be deliberately re-imported to
                    # make it available again without creating a duplicate.
                    continue
                row = {
                    "id": uuid.uuid4().hex,
                    "code": code,
                    "fingerprint": fp,
                    "status": "available",
                    "remaining_uses": None,
                    "leased_task_id": None,
                    "last_error": "",
                    "last_used_at": None,
                    "updated_at": _now(),
                }
                old.append(row)
                by_fp[fp] = row
                added += 1
            self._write(old)
            return {"added": added, "total": len(old), "items": [self._public(x) for x in old]}

    def delete(self, ids: Iterable[object]) -> dict:
        wanted = {str(x).strip() for x in (ids or []) if str(x).strip()}
        with self._lock:
            rows = self._read()
            kept, deleted, skipped = [], [], []
            for row in rows:
                if str(row.get("id")) not in wanted and str(row.get("fingerprint")) not in wanted:
                    kept.append(row)
                    continue
                if row.get("status") == "leased":
                    kept.append(row)
                    skipped.append({"id": row.get("id"), "reason": "CDK 正在使用"})
                else:
                    deleted.append(self._public(row))
            if len(kept) != len(rows):
                self._write(kept)
            return {"deleted": deleted, "skipped": skipped, "deleted_count": len(deleted)}

    def _find(self, rows: list[dict], key: object) -> dict | None:
        if isinstance(key, CdkLease):
            key = key.record_id or key.fingerprint or key.code
        elif isinstance(key, dict):
            key = key.get("id") or key.get("record_id") or key.get("lease_id") or key.get("fingerprint") or key.get("code")
        else:
            key = getattr(key, "record_id", None) or getattr(key, "fingerprint", None) or getattr(key, "code", None) or key
        text = str(key or "").strip()
        if not text:
            return None
        for row in rows:
            if text in {str(row.get("id")), str(row.get("fingerprint")), str(row.get("code"))}:
                return row
        return None

    def lease(self, *, task_id: str | None = None) -> dict | None:
        """租用下一条可用 CDK，并返回包含完整 code 的内部记录。"""
        with self._lock:
            rows = self._read()
            if self._recover_stale_locked(rows):
                self._write(rows)
            # Prefer entries with known remaining uses and the greatest count.
            candidates = [r for r in rows if r.get("status") == "available"]
            candidates.sort(key=lambda r: (r.get("remaining_uses") is None, -(int(r.get("remaining_uses") or 0))))
            if not candidates:
                return None
            row = candidates[0]
            row["status"] = "leased"
            row["leased_task_id"] = str(task_id or uuid.uuid4().hex)
            row["updated_at"] = _now()
            self._write(rows)
            return dict(row)

    # Explicit aliases for new integrations; retain ``lease`` for existing
    # callers and persisted state.
    acquire = lease

    def lease_required(self, *, task_id: str | None = None) -> dict:
        row = self.lease(task_id=task_id)
        if row is None:
            raise CdkPoolEmptyError("没有可用 CDK")
        return row

    def release(self, key: object, *, status: str = "available", error: str = "", remaining_uses: object = None) -> bool:
        with self._lock:
            rows = self._read()
            row = self._find(rows, key)
            if not row:
                return False
            desired = str(status or "available").lower()
            if desired not in {"available", "leased", "exhausted", "invalid", "error"}:
                desired = "error"
            row["status"] = desired
            row["leased_task_id"] = None if desired != "leased" else row.get("leased_task_id")
            if remaining_uses is not None:
                try:
                    row["remaining_uses"] = max(0, int(remaining_uses))
                    if row["remaining_uses"] <= 0 and desired == "available":
                        row["status"] = "exhausted"
                except (TypeError, ValueError):
                    pass
            if error:
                row["last_error"] = self._safe_error(row, error)
            row["last_used_at"] = _now()
            row["updated_at"] = _now()
            self._write(rows)
            return True

    def rotate(self, key: object, *, error: str = "CDK 轮换", consumed: bool = True, remaining_uses: object = None) -> bool:
        """End a lease and move the code out of the current attempt.

        A transient failure keeps the code available for a later manual retry;
        callers can pass ``remaining_uses=0`` (or use ``mark_invalid``) for a
        terminal CDK error. ``consumed`` is accepted for a uniform worker
        interface; the remote remaining count is authoritative when supplied.
        """
        status = "available"
        if remaining_uses is not None:
            try:
                status = "exhausted" if int(remaining_uses) <= 0 else "available"
            except (TypeError, ValueError):
                pass
        return self.release(key, status=status, error=error, remaining_uses=remaining_uses)

    def recover_stale(self) -> int:
        with self._lock:
            rows = self._read()
            count = self._recover_stale_locked(rows)
            if count:
                self._write(rows)
            return count

    def recover_orphans(self) -> int:
        """回收上一个 WebUI 进程留下的全部租约。

        进程重启后没有存活的 worker 可以继续持有旧 lease，因此启动恢复
        阶段可以安全地把它们放回池中；已知剩余次数为 0 的条目标记耗尽。
        """
        with self._lock:
            rows = self._read()
            count = 0
            for row in rows:
                if row.get("status") != "leased":
                    continue
                try:
                    remaining = row.get("remaining_uses")
                    row["status"] = "exhausted" if remaining not in (None, "") and int(remaining or 0) <= 0 else "available"
                except (TypeError, ValueError):
                    row["status"] = "available"
                row["leased_task_id"] = None
                row["last_error"] = "WebUI 重启，已回收 CDK 租约"
                row["updated_at"] = _now()
                count += 1
            if count:
                self._write(rows)
            return count

    def mark_used(self, key: object, *, remaining_uses: object = None, error: str = "") -> bool:
        desired = "available"
        try:
            if remaining_uses is not None and int(remaining_uses) <= 0:
                desired = "exhausted"
        except (TypeError, ValueError):
            pass
        return self.release(key, status=desired, error=error, remaining_uses=remaining_uses)

    def mark_invalid(self, key: object, error: str = "CDK 无效") -> bool:
        return self.release(key, status="invalid", error=error)

    def reset(self, ids: Iterable[object] | None = None) -> dict:
        wanted = None if ids is None else {str(x).strip() for x in ids if str(x).strip()}
        with self._lock:
            rows = self._read()
            count = 0
            for row in rows:
                if wanted is not None and not (str(row.get("id")) in wanted or str(row.get("fingerprint")) in wanted):
                    continue
                if row.get("status") == "leased":
                    continue
                row["status"] = "available"
                row["last_error"] = ""
                row["leased_task_id"] = None
                row["updated_at"] = _now()
                count += 1
            if count:
                self._write(rows)
            return {"reset_count": count, "items": [self._public(x) for x in rows]}

    def available_count(self) -> int:
        return sum(1 for row in self.list(include_code=True) if row.get("status") == "available")

    # Friendly aliases for callers that use pool terminology rather than the
    # WebUI action names.
    add_codes = import_codes
    add = import_codes

    def remove(self, key: object) -> dict:
        value = key if isinstance(key, (list, tuple, set)) else [key]
        return self.delete(value)

    reset_failed = reset

    def lease_next(self, *, task_id: str | None = None) -> CdkLease | None:
        row = self.lease(task_id=task_id)
        if not row:
            return None
        return CdkLease(
            code=str(row.get("code") or ""),
            record_id=str(row.get("id") or ""),
            fingerprint=str(row.get("fingerprint") or ""),
            remaining_uses=row.get("remaining_uses"),
        )


_DEFAULT_POOL = CdkPool()


def get_pool() -> CdkPool:
    return _DEFAULT_POOL


def import_codes(values: str | Iterable[str], *, replace: bool = False) -> dict:
    return _DEFAULT_POOL.import_codes(values, replace=replace)


def public_items() -> list[dict]:
    return _DEFAULT_POOL.list_public()

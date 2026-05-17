"""Shared atomic I/O helpers for kodawari artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Iterator


class CorruptArtifactError(ValueError):
    """Raised when a JSON/JSONL artifact is corrupt and quarantined."""

    def __init__(
        self,
        path: Path,
        *,
        reason: str,
        quarantine_path: Path | None = None,
    ) -> None:
        self.path = path
        self.reason = reason
        self.quarantine_path = quarantine_path
        message = f"{reason}: {path}"
        if quarantine_path is not None:
            message += f" (quarantined to {quarantine_path})"
        super().__init__(message)


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _artifact_lock_path(path: Path) -> Path:
    lock_name = f"{path.name.lstrip('.') or 'artifact'}.lock"
    return path.with_name(lock_name)


def acquire_file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 5.0,
    retry_seconds: float = 0.05,
    stale_after_seconds: float | None = None,
) -> int:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            _write_lock_metadata(lock_fd)
            return lock_fd
        except (FileExistsError, PermissionError):
            if _lock_looks_stale(lock_path, stale_after_seconds=stale_after_seconds):
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise ValueError(f"timed out waiting for lock: {lock_path}")
            time.sleep(max(0.01, float(retry_seconds)))


def release_file_lock(lock_fd: int, lock_path: Path) -> None:
    try:
        os.close(lock_fd)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def path_lock(
    path: Path,
    *,
    timeout_seconds: float = 5.0,
    retry_seconds: float = 0.05,
    stale_after_seconds: float | None = None,
) -> Iterator[None]:
    lock_path = _artifact_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = acquire_file_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        retry_seconds=retry_seconds,
        stale_after_seconds=stale_after_seconds,
    )
    try:
        yield
    finally:
        release_file_lock(lock_fd, lock_path)


def _write_lock_metadata(lock_fd: int) -> None:
    payload = {
        "schema_version": "workflow.file_lock.v1",
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        os.write(lock_fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        os.fsync(lock_fd)
    except OSError:
        pass


def _lock_looks_stale(lock_path: Path, *, stale_after_seconds: float | None) -> bool:
    if stale_after_seconds is None:
        return False
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return age >= max(1.0, float(stale_after_seconds))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return age >= max(1.0, float(stale_after_seconds))
    if not isinstance(payload, dict):
        return age >= max(1.0, float(stale_after_seconds))
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return age >= max(1.0, float(stale_after_seconds))
    return pid <= 0 or not _pid_exists(pid)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _atomic_write_text_unlocked(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{_utc_now_stamp()}.tmp")
    with temp_path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", use_lock: bool = True) -> None:
    if use_lock:
        with path_lock(path):
            _atomic_write_text_unlocked(path, text, encoding=encoding)
        return
    _atomic_write_text_unlocked(path, text, encoding=encoding)


def atomic_write_json(path: Path, payload: dict[str, Any], *, use_lock: bool = True) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
        use_lock=use_lock,
    )


def canonical_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write_canonical_json(path: Path, payload: dict[str, Any], *, use_lock: bool = True) -> None:
    atomic_write_text(path, canonical_json_text(payload), use_lock=use_lock)


def append_jsonl_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def quarantine_corrupt_artifact(
    path: Path,
    *,
    raw_text: str | None = None,
    suffix: str = "corrupt",
) -> Path:
    quarantine_dir = path.parent / ".quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / f"{path.name}.{_utc_now_stamp()}.{suffix}"
    if raw_text is None and path.exists():
        os.replace(path, target)
        return target
    target.write_text(raw_text or "", encoding="utf-8")
    return target


def quarantine_corrupt_jsonl_lines(path: Path, lines: list[str]) -> Path | None:
    cleaned = [str(line) for line in lines if str(line).strip()]
    if not cleaned:
        return None
    quarantine_dir = path.parent / ".quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / f"{path.name}.{_utc_now_stamp()}.bad-lines.jsonl"
    target.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    return target


def load_json_dict(
    path: Path,
    *,
    required: bool = False,
    encoding: str = "utf-8-sig",
    quarantine_on_error: bool = False,
) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ValueError(f"required file not found: {path}")
        return None
    try:
        payload = json.loads(path.read_text(encoding=encoding))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        quarantine_path = quarantine_corrupt_artifact(path) if quarantine_on_error else None
        raise CorruptArtifactError(
            path,
            reason=f"invalid JSON ({exc.__class__.__name__})",
            quarantine_path=quarantine_path,
        ) from exc
    if not isinstance(payload, dict):
        if quarantine_on_error:
            quarantine_path = quarantine_corrupt_artifact(path, raw_text=json.dumps(payload, ensure_ascii=False))
            raise CorruptArtifactError(
                path,
                reason="expected JSON object",
                quarantine_path=quarantine_path,
            )
        raise ValueError(f"expected JSON object at: {path}")
    return payload


def load_jsonl_rows(
    path: Path,
    *,
    quarantine_bad_lines: bool = False,
) -> tuple[list[dict[str, Any]], int, Path | None]:
    if not path.exists():
        return [], 0, None
    rows: list[dict[str, Any]] = []
    bad_lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                bad_lines.append(line)
                continue
            if isinstance(payload, dict):
                rows.append(payload)
            else:
                bad_lines.append(line)
    quarantine_path = quarantine_corrupt_jsonl_lines(path, bad_lines) if quarantine_bad_lines else None
    return rows, len(bad_lines), quarantine_path

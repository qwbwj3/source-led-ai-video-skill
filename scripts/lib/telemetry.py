from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_TYPES = {"stage_started", "stage_finished", "stage_failed", "cache_hit"}
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class TelemetryError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _valid_hashes(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if type(value) is not dict:
        raise TelemetryError(f"{field} must be an object")
    clean: dict[str, str] = {}
    for name, digest in value.items():
        if (
            type(name) is not str
            or not name
            or len(name) > 100
            or type(digest) is not str
            or HASH_PATTERN.fullmatch(digest) is None
        ):
            raise TelemetryError(f"{field} contains an invalid hash")
        clean[name] = digest
    return dict(sorted(clean.items()))


def _open_regular_leaf(
    path: Path,
    flags: int,
    *,
    mode: int = 0o600,
    allow_missing: bool = False,
) -> int | None:
    """Open one log leaf without following symlinks and verify its inode.

    Callers are responsible for validating/creating the parent directory.  The
    descriptor is checked both before and after ``open`` so platforms without
    ``O_NOFOLLOW`` still reject a leaf swapped to a symlink before any bytes are
    read or written.
    """
    path = Path(path)
    before: os.stat_result | None
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise TelemetryError("log leaf cannot be inspected") from exc
    if before is not None and (
        stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
    ):
        raise TelemetryError("log leaf must be a regular file")

    safe_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    # Avoid blocking if an attacker races a FIFO into place. This flag has no
    # effect on normal regular-file I/O.
    safe_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, safe_flags, mode)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise TelemetryError("log leaf is missing") from None
    except OSError as exc:
        raise TelemetryError("log leaf cannot be opened safely") from exc

    try:
        descriptor_stat = os.fstat(descriptor)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (after.st_dev, after.st_ino)
            or (
                before is not None
                and (descriptor_stat.st_dev, descriptor_stat.st_ino)
                != (before.st_dev, before.st_ino)
            )
        ):
            raise TelemetryError("log leaf must be a regular file")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise TelemetryError("event log write did not make progress")
        view = view[written:]


def _decode_complete_lines(raw: bytes) -> tuple[list[dict[str, Any]], bytes, bool]:
    if not raw:
        return [], b"", False
    repaired = False
    complete_end = len(raw) if raw.endswith(b"\n") else raw.rfind(b"\n") + 1
    complete = raw[:complete_end]
    if complete_end != len(raw):
        repaired = True
    events: list[dict[str, Any]] = []
    cursor = 0
    for line in complete.splitlines(keepends=True):
        cursor += len(line)
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Only a corrupt final complete line is recoverable. Earlier damage
            # would make sequence and idempotency claims unsafe.
            if cursor != len(complete):
                raise TelemetryError("event log contains interior corruption") from exc
            complete = complete[: cursor - len(line)]
            repaired = True
            break
        if type(value) is not dict:
            raise TelemetryError("event log entries must be objects")
        events.append(value)
    return events, complete, repaired


def _read_descriptor_events(
    descriptor: int, *, repair_tail: bool
) -> tuple[list[dict[str, Any]], bool]:
    events, complete, repaired = _decode_complete_lines(_read_all(descriptor))
    if repaired and repair_tail:
        os.ftruncate(descriptor, len(complete))
        os.fsync(descriptor)
    return events, repaired


def read_events(path: Path, *, repair_tail: bool = False) -> list[dict[str, Any]]:
    path = Path(os.path.abspath(os.fspath(path)))
    if repair_tail:
        # Tail truncation is a write and must participate in the same lock as
        # appends. Probe first so reading a missing log remains side-effect free.
        probe = _open_regular_leaf(path, os.O_RDONLY, allow_missing=True)
        if probe is None:
            return []
        os.close(probe)
        lock_descriptor = _open_regular_leaf(
            path.with_suffix(path.suffix + ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_APPEND,
        )
        assert lock_descriptor is not None
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    else:
        lock_descriptor = None
    try:
        descriptor = _open_regular_leaf(
            path,
            os.O_RDWR if repair_tail else os.O_RDONLY,
            allow_missing=True,
        )
        if descriptor is None:
            return []
        try:
            events, _ = _read_descriptor_events(descriptor, repair_tail=repair_tail)
            return events
        finally:
            os.close(descriptor)
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def append_identity_record(
    path: Path,
    identity: dict[str, Any],
    *,
    include_monotonic: bool = False,
) -> dict[str, Any]:
    """Append a canonical identity once under a safe per-log leaf lock."""
    if type(identity) is not dict or any(
        reserved in identity
        for reserved in ("event_id", "sequence", "recorded_at", "monotonic_ns")
    ):
        raise TelemetryError("event identity contains reserved fields")
    try:
        event_id = hashlib.sha256(_canonical(identity)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise TelemetryError("event identity is not finite canonical JSON") from exc

    path = Path(os.path.abspath(os.fspath(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_descriptor = _open_regular_leaf(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND
    )
    assert lock_descriptor is not None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        log_descriptor = _open_regular_leaf(
            path, os.O_RDWR | os.O_CREAT | os.O_APPEND
        )
        assert log_descriptor is not None
        try:
            events, _ = _read_descriptor_events(log_descriptor, repair_tail=True)
            for existing in events:
                if existing.get("event_id") == event_id:
                    return existing
            record: dict[str, Any] = identity | {
                "sequence": len(events) + 1,
                "event_id": event_id,
                "recorded_at": _utc_now(),
            }
            if include_monotonic:
                # Persist the raw clock value. It is monotonic only inside one
                # OS boot; forcing it above a pre-reboot value would turn a
                # cross-boot stage into a near-zero duration.
                record["monotonic_ns"] = time.monotonic_ns()
            os.lseek(log_descriptor, 0, os.SEEK_END)
            _write_all(log_descriptor, _canonical(record) + b"\n")
            os.fsync(log_descriptor)
            return record
        finally:
            os.close(log_descriptor)
    finally:
        os.close(lock_descriptor)


@contextmanager
def identity_log_lock(path: Path):
    """Hold the same exclusive leaf lock used by append_identity_record."""
    path = Path(os.path.abspath(os.fspath(path)))
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_descriptor = _open_regular_leaf(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND
    )
    assert lock_descriptor is not None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_descriptor)


def append_event(
    path: Path,
    *,
    event_type: str,
    stage: str,
    attempt: int,
    build_key: str | None = None,
    input_hashes: dict[str, str] | None = None,
    output_hashes: dict[str, str] | None = None,
    error_code: str | None = None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """Append a secret-free, idempotent production event.

    The schema intentionally accepts hashes and bounded identifiers only. It
    cannot accidentally persist environment values, prompts, URLs, or error
    messages. A process crash may leave one partial tail line; the next writer
    truncates only that tail while holding the log lock.
    """
    if event_type not in EVENT_TYPES:
        raise TelemetryError("unsupported event type")
    if type(stage) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stage) is None:
        raise TelemetryError("invalid stage")
    if type(attempt) is not int or isinstance(attempt, bool) or attempt < 1:
        raise TelemetryError("attempt must be a positive integer")
    if build_key is not None and (
        type(build_key) is not str or re.fullmatch(r"[0-9a-f]{20,64}", build_key) is None
    ):
        raise TelemetryError("invalid build key")
    if error_code is not None and (
        type(error_code) is not str
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code) is None
    ):
        raise TelemetryError("invalid error code")
    if cache_key is not None and (
        type(cache_key) is not str
        or re.fullmatch(r"[0-9a-f]{20,64}", cache_key) is None
    ):
        raise TelemetryError("invalid cache key")
    if event_type == "stage_failed" and not error_code:
        raise TelemetryError("failed events require an error code")
    if event_type != "stage_failed" and error_code is not None:
        raise TelemetryError("error code is only valid for failed events")
    if event_type == "cache_hit" and not cache_key:
        raise TelemetryError("cache-hit events require a cache key")

    inputs = _valid_hashes(input_hashes, "input_hashes")
    outputs = _valid_hashes(output_hashes, "output_hashes")
    identity = {
        "schema": 1,
        "event": event_type,
        "stage": stage,
        "attempt": attempt,
        "build_key": build_key,
        "input_hashes": inputs,
        "output_hashes": outputs,
        "error_code": error_code,
        "cache_key": cache_key,
    }
    return append_identity_record(path, identity, include_monotonic=True)


def next_attempt(events: list[dict[str, Any]], stage: str, build_key: str | None) -> int:
    attempts = [
        event.get("attempt")
        for event in events
        if event.get("stage") == stage
        and event.get("build_key") == build_key
        and type(event.get("attempt")) is int
    ]
    return max(attempts, default=0) + 1


def stage_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str | None], tuple[dict[str, Any], float | None]] = {}
    starts: dict[tuple[str, str | None, int], dict[str, Any]] = {}
    for event in events:
        stage = event.get("stage")
        if type(stage) is str:
            build_key = event.get("build_key")
            attempt = event.get("attempt")
            elapsed_seconds: float | None = None
            if type(attempt) is int:
                attempt_key = (stage, build_key, attempt)
                if event.get("event") == "stage_started":
                    starts[attempt_key] = event
                elif event.get("event") in {"stage_finished", "stage_failed"}:
                    started = starts.get(attempt_key)
                    start_ns = started.get("monotonic_ns") if started else None
                    end_ns = event.get("monotonic_ns")
                    if (
                        type(start_ns) is int
                        and type(end_ns) is int
                        and end_ns >= start_ns
                    ):
                        elapsed_seconds = round((end_ns - start_ns) / 1_000_000_000, 6)
                    elif started is not None:
                        # A lower monotonic value indicates an OS reboot. Fall
                        # back to the persisted UTC timestamps for that attempt.
                        try:
                            start_raw = started.get("recorded_at")
                            end_raw = event.get("recorded_at")
                            if type(start_raw) is not str or type(end_raw) is not str:
                                raise ValueError
                            start_time = datetime.fromisoformat(
                                start_raw[:-1] + "+00:00"
                                if start_raw.endswith("Z")
                                else start_raw
                            )
                            end_time = datetime.fromisoformat(
                                end_raw[:-1] + "+00:00"
                                if end_raw.endswith("Z")
                                else end_raw
                            )
                            wall_elapsed = (end_time - start_time).total_seconds()
                            if wall_elapsed >= 0:
                                elapsed_seconds = round(wall_elapsed, 6)
                        except (TypeError, ValueError):
                            elapsed_seconds = None
            latest[(stage, build_key)] = (event, elapsed_seconds)
    return [
        {
            "stage": stage,
            "build_key": build_key,
            "status": event.get("event"),
            "attempt": event.get("attempt"),
            "error_code": event.get("error_code"),
            "recorded_at": event.get("recorded_at"),
            "elapsed_seconds": elapsed_seconds,
        }
        for (stage, build_key), (event, elapsed_seconds) in sorted(
            latest.items(), key=lambda item: int(item[1][0].get("sequence", 0))
        )
    ]

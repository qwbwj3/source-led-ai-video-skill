from __future__ import annotations

import base64
import fcntl
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .common import (
    REQUIRED_VOLC_ENV,
    WorkflowError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    load_and_validate_config,
    narration_hash,
    narration_text,
    project_atomic_copy_file,
    project_output_path,
    redact,
    resolve_media_tools,
    run_process,
    sha256_bytes,
    sha256_file,
)


OFFICIAL_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"
ADAPTER_VERSION = "volc-http-query-v2"
CACHE_SCHEMA = 1
MIN_AUDIO_BYTES = 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_SUCCESS_BODY_BYTES = 32 * 1024 * 1024
MAX_CACHE_METADATA_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_request(request: urllib.request.Request, timeout: int) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def canonical_endpoint(value: str) -> str:
    raw = str(value).strip()
    if "?" in raw or "#" in raw:
        raise WorkflowError("TTS_ENDPOINT", "Volcengine endpoint is not the supported sync API")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise WorkflowError(
            "TTS_ENDPOINT", "Volcengine endpoint is not the supported sync API"
        ) from exc
    host = "openspeech.bytedance.com"
    allowed_netlocs = {host, f"{host}:443"}
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() not in allowed_netlocs
        or parsed.hostname is None
        or parsed.hostname.lower() != host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != "/api/v1/tts"
        or parsed.query
        or parsed.fragment
    ):
        raise WorkflowError("TTS_ENDPOINT", "Volcengine endpoint is not the supported sync API")
    return OFFICIAL_ENDPOINT


def cache_provenance(text: str, env: dict[str, str]) -> dict[str, Any]:
    endpoint = canonical_endpoint(env["VOLC_TTS_ENDPOINT"])
    return {
        "schema": 1,
        "adapter_version": ADAPTER_VERSION,
        "endpoint": endpoint,
        "narration_sha256": sha256_bytes(text.encode("utf-8")),
        "app_id_sha256": sha256_bytes(env["VOLC_TTS_APP_ID"].encode("utf-8")),
        "cluster_sha256": sha256_bytes(env["VOLC_TTS_CLUSTER"].encode("utf-8")),
        "voice_sha256": sha256_bytes(env["VOLC_TTS_VOICE"].encode("utf-8")),
        "encoding": env["VOLC_TTS_ENCODING"].strip().lower(),
        "speed_ratio": 1.0,
    }


def _cache_key(text: str, env: dict[str, str]) -> str:
    return sha256_bytes(canonical_json(cache_provenance(text, env)))


def _extract_audio(payload: Any) -> bytes | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for candidate in (
        payload.get("data"),
        result.get("data"),
        payload.get("audio"),
        result.get("audio"),
    ):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            audio = base64.b64decode("".join(candidate.split()), validate=True)
        except (ValueError, TypeError):
            continue
        if audio:
            return audio
    return None


def _has_mp3_signature(content: bytes) -> bool:
    if content.startswith(b"ID3"):
        return True
    return len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0


def _error_details(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "unavailable", "response did not contain JSON error details"
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    code = "unavailable"
    for container in (payload, result):
        for key in ("code", "status_code", "error_code"):
            if container.get(key) not in (None, ""):
                code = str(container[key])
                break
        if code != "unavailable":
            break
    message = "response did not contain audio data"
    for container in (payload, result):
        for key in ("message", "msg", "status_text", "error"):
            value = container.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                message = str(value)
                break
        if message != "response did not contain audio data":
            break
    return code, message


def _declared_length(headers: Any) -> int | None:
    try:
        raw = headers.get("Content-Length")
    except AttributeError:
        return None
    try:
        length = int(raw)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _read_success_body(response: Any) -> bytes:
    declared = _declared_length(response.headers)
    if declared is not None and declared > MAX_SUCCESS_BODY_BYTES:
        raise WorkflowError("TTS_RESPONSE_SIZE", "Volcengine response exceeds the size limit")
    body = response.read(MAX_SUCCESS_BODY_BYTES + 1)
    if len(body) > MAX_SUCCESS_BODY_BYTES:
        raise WorkflowError("TTS_RESPONSE_SIZE", "Volcengine response exceeds the size limit")
    return body


def _read_error_body(response: Any) -> bytes:
    body = response.read(MAX_ERROR_BODY_BYTES)
    return body[:MAX_ERROR_BODY_BYTES]


def _cache_metadata(
    audio: bytes,
    key: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": CACHE_SCHEMA,
        "cache_key": key,
        "adapter_version": provenance["adapter_version"],
        "endpoint": provenance["endpoint"],
        "encoding": provenance["encoding"],
        "provenance_sha256": sha256_bytes(canonical_json(provenance)),
        "audio_sha256": sha256_bytes(audio),
        "size": len(audio),
    }


def _validated_cache_metadata(
    cache_file: Path,
    metadata_file: Path,
    key: str,
    provenance: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        if not cache_file.is_file() or not metadata_file.is_file():
            return None
        if metadata_file.stat().st_size > MAX_CACHE_METADATA_BYTES:
            return None
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        size = cache_file.stat().st_size
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema") != CACHE_SCHEMA
            or metadata.get("cache_key") != key
            or metadata.get("adapter_version") != provenance["adapter_version"]
            or metadata.get("endpoint") != provenance["endpoint"]
            or metadata.get("encoding") != "mp3"
            or metadata.get("provenance_sha256")
            != sha256_bytes(canonical_json(provenance))
            or not isinstance(metadata.get("size"), int)
            or isinstance(metadata.get("size"), bool)
            or metadata.get("size") != size
            or not MIN_AUDIO_BYTES < size <= MAX_SUCCESS_BODY_BYTES
        ):
            return None
        with cache_file.open("rb") as handle:
            signature = handle.read(3)
        if not _has_mp3_signature(signature):
            return None
        if metadata.get("audio_sha256") != sha256_file(cache_file):
            return None
        return metadata
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _is_decodable_mp3(ffprobe: Path, audio_path: Path) -> bool:
    """Read every frame with the locked FFprobe and require one real MP3 stream."""
    try:
        result = run_process(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=codec_type,codec_name,duration,nb_read_frames:format=duration",
                "-of",
                "json",
                audio_path,
            ],
            timeout_seconds=120,
        )
    except WorkflowError:
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams")
        if not isinstance(streams, list) or len(streams) != 1:
            return False
        audio = streams[0]
        if (
            not isinstance(audio, dict)
            or audio.get("codec_type") != "audio"
            or audio.get("codec_name") != "mp3"
        ):
            return False
        frame_count = int(audio.get("nb_read_frames", 0))
        raw_duration = audio.get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = (payload.get("format") or {}).get("duration")
        duration = float(raw_duration)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return frame_count > 0 and math.isfinite(duration) and duration > 0


def _clear_cache_pair(cache_file: Path, metadata_file: Path) -> None:
    try:
        cache_file.unlink(missing_ok=True)
        metadata_file.unlink(missing_ok=True)
    except OSError as exc:
        raise WorkflowError("TTS_CACHE", "Could not clear an invalid TTS cache entry") from exc


def _cache_result(
    status: str,
    key: str,
    provenance: dict[str, Any],
    metadata: dict[str, Any],
    request_id: str | None = None,
) -> dict[str, Any]:
    result = {
        "cache": status,
        "cache_key": key,
        "adapter_version": str(provenance["adapter_version"]),
        "endpoint": str(provenance["endpoint"]),
        "audio_sha256": str(metadata["audio_sha256"]),
        "audio_size": metadata["size"],
    }
    if request_id is not None:
        result["request_id"] = request_id
    return result


def _synthesize_from_config(
    config_path: Path,
    output_path: Path,
    env: dict[str, str],
    cache_root: Path,
) -> dict[str, Any]:
    try:
        import review_gate
    except ImportError as exc:
        raise WorkflowError("REVIEW_GATE", "Publish review gate is unavailable") from exc
    resolved = Path(config_path).resolve()
    approval = review_gate.verify_approval(resolved)
    config, project_root = load_and_validate_config(resolved)
    if approval.get("narration_sha256") != narration_hash(config):
        raise WorkflowError("REVIEW_STALE", "TTS blocked: narration changed after approval")
    output_path = project_output_path(project_root, output_path)
    text = narration_text(config)
    missing = [name for name in REQUIRED_VOLC_ENV if not env.get(name)]
    if missing:
        raise WorkflowError("TTS_CONFIG", "Missing Volcengine settings: " + ", ".join(missing))
    for name in REQUIRED_VOLC_ENV:
        value = env[name]
        if type(value) is not str or not value.strip():
            raise WorkflowError("TTS_CONFIG", f"Invalid Volcengine setting: {name}")
        if len(value) > 8192 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise WorkflowError("TTS_CONFIG", f"Invalid Volcengine setting: {name}")
    if not env["VOLC_TTS_ACCESS_TOKEN"].isascii():
        raise WorkflowError("TTS_CONFIG", "Invalid Volcengine access token")
    endpoint = canonical_endpoint(env["VOLC_TTS_ENDPOINT"])
    encoding = env["VOLC_TTS_ENCODING"].strip().lower()
    if encoding != "mp3":
        raise WorkflowError("TTS_ENCODING", "This reproducible workflow requires MP3 encoding")
    clean_text = text.strip()
    if not clean_text:
        raise WorkflowError("TTS_TEXT", "Narration text is empty")
    _, ffprobe = resolve_media_tools()

    provenance = cache_provenance(clean_text, env)
    key = sha256_bytes(canonical_json(provenance))
    cache_dir = cache_root / "tts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{key}.mp3"
    metadata_file = cache_dir / f"{key}.json"
    lock_path = cache_dir / f"{key}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        cached = _validated_cache_metadata(
            cache_file, metadata_file, key, provenance
        )
        if cached is not None:
            if _is_decodable_mp3(ffprobe, cache_file):
                project_atomic_copy_file(project_root, cache_file, output_path)
                return _cache_result("hit", key, provenance, cached)
            _clear_cache_pair(cache_file, metadata_file)

        _clear_cache_pair(cache_file, metadata_file)
        reqid = str(uuid.uuid4())
        payload = {
            "app": {
                "appid": env["VOLC_TTS_APP_ID"],
                "token": env["VOLC_TTS_ACCESS_TOKEN"],
                "cluster": env["VOLC_TTS_CLUSTER"],
            },
            "user": {"uid": "source-led-ai-video"},
            "audio": {
                "voice_type": env["VOLC_TTS_VOICE"],
                "encoding": encoding,
                "speed_ratio": 1.0,
            },
            "request": {"reqid": reqid, "text": clean_text, "operation": "query"},
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer;{env['VOLC_TTS_ACCESS_TOKEN']}",
            },
        )

        status = 0
        headers: dict[str, str] = {}
        body = b""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with _open_request(request, timeout=60) as response:
                    status = int(response.status)
                    headers = dict(response.headers.items())
                    if 200 <= status < 300:
                        body = _read_success_body(response)
                    else:
                        body = _read_error_body(response)
                last_error = None
                break
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                headers = dict(exc.headers.items()) if exc.headers else {}
                body = _read_error_body(exc)
                last_error = exc
                if status != 429 and not 500 <= status < 600:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(1 << attempt)

        response_payload = None
        try:
            response_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not 200 <= status < 300:
            code, summary = _error_details(response_payload)
            if last_error is not None and status == 0:
                summary = type(last_error).__name__
            raise WorkflowError(
                "TTS_HTTP",
                redact(
                    f"Volcengine request failed (HTTP {status or 'unavailable'}, code {code}): {summary}",
                    env,
                ),
            )

        audio = _extract_audio(response_payload)
        if audio is None and _has_mp3_signature(body):
            audio = body
        if (
            not audio
            or len(audio) <= MIN_AUDIO_BYTES
            or len(audio) > MAX_SUCCESS_BODY_BYTES
            or not _has_mp3_signature(audio)
        ):
            code, summary = _error_details(response_payload)
            raise WorkflowError(
                "TTS_AUDIO", redact(f"No valid MP3 audio (code {code}): {summary}", env)
            )

        metadata = _cache_metadata(audio, key, provenance)
        atomic_write_bytes(cache_file, audio)
        if not _is_decodable_mp3(ffprobe, cache_file):
            _clear_cache_pair(cache_file, metadata_file)
            raise WorkflowError("TTS_AUDIO", "Volcengine returned undecodable MP3 audio")
        atomic_write_json(metadata_file, metadata)
        project_atomic_copy_file(project_root, cache_file, output_path)
        return _cache_result("miss", key, provenance, metadata, reqid)


def synthesize(
    config_path: Path,
    output_path: Path,
    env: dict[str, str],
    cache_root: Path,
) -> dict[str, Any]:
    """Synthesize only the exact narration bound to a current review approval."""
    try:
        return _synthesize_from_config(config_path, output_path, env, cache_root)
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(
            "TTS_INTERNAL",
            redact(f"Volcengine transport failed: {type(exc).__name__}", env),
        ) from None

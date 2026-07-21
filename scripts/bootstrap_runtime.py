#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from lib.common import (
    FONT_PATH,
    LOCKED_FFMPEG_SHA256,
    LOCKED_FFPROBE_SHA256,
    LOCKED_ALIGNER_PYTHON_VERSION,
    MODEL_ID,
    MODEL_REVISION,
    SKILL_ROOT,
    WorkflowError,
    atomic_write_json,
    load_env_file,
    redact,
    runtime_root,
    sha256_file,
)
from lib.runtime_contract import aligner_runtime_fingerprint


GZIP_HASHES = {
    "ffmpeg": "e3593e4765a4f400c7ef3d47b3404a4fbfeec0e1fd19377dcdbfc5cee41ac629",
    "ffprobe": "a20fc161097a6645cc7bc3094a9b8e8dbb36c9250327e08e14523c5a75b08389",
}
BIN_HASHES = {
    "ffmpeg": LOCKED_FFMPEG_SHA256,
    "ffprobe": LOCKED_FFPROBE_SHA256,
}
FONT_HASH = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
UV_VERSION = "0.11.16"
UV_GZIP_HASH = "03bbb85f5f97b51a5659e386a215f8ecb456df34618c4942f5aee056312c5a08"
UV_BIN_HASH = "f63ec276fa13f8f392542a334c0f58f36833b24304831e5f4c221e2edf7a16f3"


def install_media_runtime(root: Path) -> tuple[Path, Path]:
    bin_dir = root / "ffmpeg-8.1-darwin-arm64"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        source = SKILL_ROOT / f"assets/runtime/darwin-arm64/{name}.gz"
        if sha256_file(source) != GZIP_HASHES[name]:
            raise WorkflowError("RUNTIME_CHECKSUM", f"Bundled {name}.gz checksum mismatch")
        target = bin_dir / name
        if target.exists() and sha256_file(target) == BIN_HASHES[name]:
            target.chmod(0o755)
            continue
        temporary = bin_dir / f".{name}.tmp"
        temporary.unlink(missing_ok=True)
        with gzip.open(source, "rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        temporary.chmod(0o755)
        if sha256_file(temporary) != BIN_HASHES[name]:
            temporary.unlink(missing_ok=True)
            raise WorkflowError("RUNTIME_CHECKSUM", f"Decompressed {name} checksum mismatch")
        os.replace(temporary, target)

    ffmpeg, ffprobe = bin_dir / "ffmpeg", bin_dir / "ffprobe"
    filters = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-filters"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    for required in (" ass ", " loudnorm ", " ebur128 ", " blackdetect ", " silencedetect "):
        if required not in filters:
            raise WorkflowError("RUNTIME_FILTER", f"Bundled FFmpeg lacks {required.strip()}")
    encoders = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    ).stdout
    if "libx264" not in encoders or " aac " not in encoders:
        raise WorkflowError("RUNTIME_ENCODER", "Bundled FFmpeg lacks H.264 or AAC")
    return ffmpeg, ffprobe


def install_uv_runtime(root: Path) -> Path:
    source = SKILL_ROOT / "assets/runtime/darwin-arm64/uv.gz"
    if not source.is_file() or sha256_file(source) != UV_GZIP_HASH:
        raise WorkflowError("RUNTIME_CHECKSUM", "Bundled uv.gz checksum mismatch")
    bin_dir = root / f"uv-{UV_VERSION}-darwin-arm64"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "uv"
    if not target.exists() or sha256_file(target) != UV_BIN_HASH:
        temporary = bin_dir / ".uv.tmp"
        temporary.unlink(missing_ok=True)
        with gzip.open(source, "rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        temporary.chmod(0o755)
        if sha256_file(temporary) != UV_BIN_HASH:
            temporary.unlink(missing_ok=True)
            raise WorkflowError("RUNTIME_CHECKSUM", "Decompressed uv checksum mismatch")
        os.replace(temporary, target)
    target.chmod(0o755)
    checked = subprocess.run(
        [str(target), "--version"],
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if checked.returncode != 0 or not checked.stdout.startswith(f"uv {UV_VERSION} "):
        raise WorkflowError("RUNTIME_UV", "Bundled uv could not be verified")
    return target


def _uv_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(root / "managed-python")
    env["UV_NO_SYSTEM_CONFIG"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    return env


def _matches_aligner_python(path: str | Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(path),
                "-c",
                "import platform; print(platform.python_implementation(), platform.machine(), platform.python_version())",
            ],
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        result.returncode == 0
        and result.stdout.strip()
        == f"CPython arm64 {LOCKED_ALIGNER_PYTHON_VERSION}"
    )


def choose_python(root: Path, uv: Path) -> Path:
    candidates = []
    explicit = os.getenv("SOURCE_LED_BOOTSTRAP_PYTHON", "").strip()
    if explicit:
        candidates.append(explicit)
    candidates.extend(("python3.11", "python3.12", sys.executable, "python3"))
    codex_runtime_root = Path.home() / ".cache/codex-runtimes"
    if codex_runtime_root.is_dir():
        candidates.extend(
            str(path)
            for path in sorted(
                codex_runtime_root.glob("*/dependencies/python/bin/python3")
            )
        )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if not path:
            continue
        if _matches_aligner_python(path):
            return Path(path)
    uv_env = _uv_environment(root)
    located = subprocess.run(
        [str(uv), "python", "find", LOCKED_ALIGNER_PYTHON_VERSION],
        env=uv_env,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )
    located_path = Path(located.stdout.strip()) if located.returncode == 0 else None
    if located_path is None or not located_path.is_file() or not _matches_aligner_python(located_path):
        installed = subprocess.run(
            [str(uv), "python", "install", LOCKED_ALIGNER_PYTHON_VERSION],
            env=uv_env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
        if installed.returncode == 0:
            located = subprocess.run(
                [str(uv), "python", "find", LOCKED_ALIGNER_PYTHON_VERSION],
                env=uv_env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
    if located.returncode == 0:
        path = Path(located.stdout.strip())
        if path.is_file() and _matches_aligner_python(path):
            return path
    raise WorkflowError(
        "RUNTIME_PYTHON", f"CPython {LOCKED_ALIGNER_PYTHON_VERSION} is required"
    )


def install_aligner(root: Path, uv: Path, *, prefetch: bool) -> Path:
    override = os.getenv("SOURCE_LED_ALIGNER_PYTHON", "").strip()
    if override:
        path = Path(override).expanduser()
        aligner_runtime_fingerprint(path)
        return path

    requirements = SKILL_ROOT / "scripts/requirements-lock.txt"
    runtime_tag = (
        f"{LOCKED_ALIGNER_PYTHON_VERSION}-{sha256_file(requirements)[:12]}"
    )
    venv = root / f"aligner-py-{runtime_tag}"
    python = venv / "bin/python"
    if not python.exists():
        source_python = choose_python(root, uv)
        result = subprocess.run([str(source_python), "-m", "venv", str(venv)], check=False)
        if result.returncode != 0:
            raise WorkflowError("RUNTIME_VENV", "Could not create the aligner environment")

    command = [str(uv), "pip", "sync", "--python", str(python), str(requirements)]
    result = subprocess.run(command, env=_uv_environment(root), check=False)
    if result.returncode != 0:
        raise WorkflowError("RUNTIME_INSTALL", "Could not install the pinned aligner runtime")

    aligner_runtime_fingerprint(python)

    if prefetch:
        align_script = SKILL_ROOT / "scripts/align_captions.py"
        result = subprocess.run(
            [str(python), str(align_script), "--prefetch"],
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError("RUNTIME_MODEL", "Could not download the pinned aligner model")
    return python


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-prefetch", action="store_true")
    parser.add_argument("--skip-aligner", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise WorkflowError("RUNTIME_PLATFORM", "This release supports Apple Silicon macOS only")
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = install_media_runtime(root)
    uv = install_uv_runtime(root)
    aligner_python = (
        None
        if args.skip_aligner
        else install_aligner(root, uv, prefetch=not args.no_prefetch)
    )
    aligner_fingerprint = (
        aligner_runtime_fingerprint(aligner_python) if aligner_python else None
    )
    if sha256_file(FONT_PATH) != FONT_HASH:
        raise WorkflowError("RUNTIME_FONT", "Bundled font checksum mismatch")

    env = load_env_file(args.env_file)
    configured = [name for name in (
        "VOLC_TTS_ENDPOINT",
        "VOLC_TTS_APP_ID",
        "VOLC_TTS_ACCESS_TOKEN",
        "VOLC_TTS_CLUSTER",
        "VOLC_TTS_VOICE",
        "VOLC_TTS_ENCODING",
    ) if env.get(name)]
    manifest = {
        "schema": 1,
        "platform": "darwin-arm64",
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "ffmpeg_sha256": sha256_file(ffmpeg),
        "ffprobe_sha256": sha256_file(ffprobe),
        "uv": str(uv),
        "uv_version": UV_VERSION,
        "uv_sha256": sha256_file(uv),
        "aligner_python_version": LOCKED_ALIGNER_PYTHON_VERSION,
        "font": str(FONT_PATH),
        "font_sha256": sha256_file(FONT_PATH),
        "aligner_python": str(aligner_python) if aligner_python else None,
        "aligner_runtime": aligner_fingerprint,
        "aligner_model": MODEL_ID,
        "aligner_revision": MODEL_REVISION,
        "volc_configured_names": configured,
    }
    atomic_write_json(root / "runtime.json", manifest)
    print(json_safe(manifest))
    return 0


def json_safe(manifest: dict[str, object]) -> str:
    import json

    return json.dumps(manifest, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from lib.common import REQUIRED_VOLC_ENV, WorkflowError, atomic_write_text, redact
from lib.volc_tts import canonical_endpoint


def parse_env_file(path: Path) -> dict[str, str]:
    try:
        permissions = stat.S_IMODE(path.stat().st_mode)
        if permissions & 0o077:
            raise WorkflowError("TTS_ENV_PERMISSIONS", "Source env file must use mode 0600")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkflowError("TTS_ENV_READ", "Cannot read the source env file") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise WorkflowError("TTS_CONFIG", "Source env file contains a malformed line")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key in REQUIRED_VOLC_ENV:
            if key in values:
                raise WorkflowError("TTS_CONFIG", f"Duplicate Volcengine setting: {key}")
            values[key] = value
    missing = [name for name in REQUIRED_VOLC_ENV if not values.get(name)]
    if missing:
        raise WorkflowError("TTS_CONFIG", "Missing Volcengine settings: " + ", ".join(missing))
    values["VOLC_TTS_ENDPOINT"] = canonical_endpoint(values["VOLC_TTS_ENDPOINT"])
    if values["VOLC_TTS_ENCODING"].lower() != "mp3":
        raise WorkflowError("TTS_ENCODING", "This workflow requires MP3 encoding")
    return values


def quote(value: str) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 8192
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WorkflowError("TTS_CONFIG", "Volcengine values contain invalid characters")
    return value


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-env", type=Path, required=True)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path.home() / ".config/source-led-ai-video/volc.env",
    )
    args = parser.parse_args()
    values = parse_env_file(args.from_env.expanduser())
    if not values["VOLC_TTS_ACCESS_TOKEN"].isascii():
        raise WorkflowError("TTS_CONFIG", "Volcengine access token must be ASCII")
    content = "# source-led-ai-video Volcengine TTS configuration\n" + "".join(
        f"{name}={quote(values[name])}\n" for name in REQUIRED_VOLC_ENV
    )
    destination = args.destination.expanduser()
    atomic_write_text(destination, content, mode=0o600)
    print(f"Configured {len(REQUIRED_VOLC_ENV)} required fields at {destination}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

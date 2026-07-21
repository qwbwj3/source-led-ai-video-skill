#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

from lib.common import (
    MODEL_ID,
    MODEL_REVISION,
    WorkflowError,
    atomic_write_json,
    atomic_write_text,
    caption_objects,
    load_and_validate_config,
    narration_text,
    spoken_glyphs,
)


PCM_SUBTYPE_GUID = bytes.fromhex("0100000000001000800000aa00389b71")
EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 1
EXPECTED_BITS_PER_SAMPLE = 24
EXPECTED_BLOCK_ALIGN = 3
EXPECTED_BYTE_RATE = 144_000


def wav_duration(path: Path) -> float:
    """Read PCM WAV duration, including WAVE_FORMAT_EXTENSIBLE PCM24 files.

    Python 3.11's wave module rejects FFmpeg's extensible 24-bit header even
    though the PCM payload is valid, so inspect the RIFF chunks directly.
    """
    try:
        file_size = path.stat().st_size
        if file_size < 12:
            raise ValueError("short RIFF header")
        with path.open("rb") as handle:
            header = handle.read(12)
            riff_id, riff_size, wave_id = struct.unpack("<4sI4s", header)
            if riff_id != b"RIFF" or wave_id != b"WAVE":
                raise ValueError("not RIFF/WAVE")
            riff_end = riff_size + 8
            if riff_end != file_size:
                raise ValueError("RIFF size does not match file length")

            format_seen = False
            data_seen = False
            data_size = 0
            while handle.tell() < riff_end:
                if riff_end - handle.tell() < 8:
                    raise ValueError("truncated chunk header")
                header = handle.read(8)
                chunk_id, chunk_size = struct.unpack("<4sI", header)
                chunk_end = handle.tell() + chunk_size
                padded_end = chunk_end + (chunk_size & 1)
                if padded_end > riff_end:
                    raise ValueError("chunk exceeds RIFF boundary")
                if chunk_id == b"fmt ":
                    if format_seen or data_seen:
                        raise ValueError("invalid fmt chunk placement")
                    if chunk_size not in {16, 40}:
                        raise ValueError("unsupported fmt chunk size")
                    payload = handle.read(chunk_size)
                    if len(payload) != chunk_size:
                        raise ValueError("truncated fmt chunk")
                    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack(
                        "<HHIIHH", payload[:16]
                    )
                    if audio_format == 1:
                        if chunk_size != 16:
                            raise ValueError("invalid PCM fmt chunk")
                    elif audio_format == 0xFFFE:
                        extension_size, valid_bits, _, subtype = struct.unpack(
                            "<HHI16s", payload[16:40]
                        )
                        if (
                            extension_size != 22
                            or valid_bits != EXPECTED_BITS_PER_SAMPLE
                            or subtype != PCM_SUBTYPE_GUID
                        ):
                            raise ValueError("invalid extensible PCM subtype")
                    else:
                        raise ValueError("not integer PCM")
                    if (
                        channels != EXPECTED_CHANNELS
                        or sample_rate != EXPECTED_SAMPLE_RATE
                        or byte_rate != EXPECTED_BYTE_RATE
                        or block_align != EXPECTED_BLOCK_ALIGN
                        or bits_per_sample != EXPECTED_BITS_PER_SAMPLE
                    ):
                        raise ValueError("unexpected PCM format")
                    format_seen = True
                elif chunk_id == b"data":
                    if not format_seen or data_seen:
                        raise ValueError("invalid data chunk placement")
                    if chunk_size <= 0 or chunk_size % EXPECTED_BLOCK_ALIGN:
                        raise ValueError("invalid PCM data size")
                    data_size = chunk_size
                    handle.seek(chunk_size, 1)
                    data_seen = True
                else:
                    handle.seek(chunk_size, 1)
                if chunk_size % 2:
                    if len(handle.read(1)) != 1:
                        raise ValueError("missing chunk padding")
            if handle.tell() != riff_end or not format_seen or not data_seen:
                raise ValueError("missing WAV chunks")
            duration = data_size / EXPECTED_BYTE_RATE
            if not (0 < duration <= 600):
                raise ValueError("duration out of range")
            return duration
    except (OSError, ValueError, struct.error) as exc:
        raise WorkflowError("ALIGN_AUDIO", "Alignment input must be a readable WAV") from exc


def srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt(entries: list[dict], *, shown_only: bool) -> str:
    blocks: list[str] = []
    index = 0
    for entry in entries:
        if shown_only and not entry["show"]:
            continue
        index += 1
        blocks.append(
            "\n".join(
                (
                    str(index),
                    f"{srt_time(entry['startMs'])} --> {srt_time(entry['endMs'])}",
                    entry["text"],
                )
            )
        )
    return "\n\n".join(blocks) + "\n"


def _aligned_field(item: object, field: str) -> object:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _aligned_seconds(item: object, field: str) -> float:
    value = _aligned_field(item, field)
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise WorkflowError("ALIGN_RESULT", f"Aligned {field} must be finite")
    return float(value)


def map_captions(
    config: dict, result: list[object], duration: float
) -> list[dict]:
    """Map caption glyph ranges onto aligner spans, including split spans."""
    if not result:
        raise WorkflowError("ALIGN_EMPTY", "Forced aligner returned no items")

    expected = spoken_glyphs(narration_text(config))
    spans: list[dict[str, float | int]] = []
    actual_parts: list[str] = []
    glyph_cursor = 0
    previous_start = -1.0
    duration_ms = round(duration * 1000)
    for item in result:
        raw_text = _aligned_field(item, "text")
        if type(raw_text) is not str:
            raise WorkflowError("ALIGN_RESULT", "Aligned text must be a string")
        start_seconds = _aligned_seconds(item, "start_time")
        end_seconds = _aligned_seconds(item, "end_time")
        # Qwen occasionally assigns one glyph a zero-width boundary between two
        # neighboring glyphs. Preserve that boundary; reject reversed or
        # non-monotonic timing.
        if start_seconds < 0 or end_seconds < start_seconds or start_seconds < previous_start:
            raise WorkflowError("ALIGN_RANGE", "Aligned item timing is invalid")
        previous_start = start_seconds
        glyphs = spoken_glyphs(raw_text)
        actual_parts.append(glyphs)
        if not glyphs:
            continue
        start_ms = round(start_seconds * 1000)
        end_ms = round(end_seconds * 1000)
        if start_ms >= duration_ms or end_ms <= 0:
            raise WorkflowError("ALIGN_RANGE", "Aligned item falls outside narration audio")
        end_ms = min(end_ms, duration_ms)
        if end_ms < start_ms:
            raise WorkflowError("ALIGN_RANGE", "Aligned item timing is reversed")
        spans.append(
            {
                "glyph_start": glyph_cursor,
                "glyph_end": glyph_cursor + len(glyphs),
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
        glyph_cursor += len(glyphs)

    actual = "".join(actual_parts)
    if actual != expected:
        raise WorkflowError("ALIGN_MISMATCH", "Aligned glyphs do not reconstruct narration")
    if not spans or glyph_cursor != len(expected):
        raise WorkflowError("ALIGN_MAP", "Aligned output has no mappable spoken spans")

    def boundary_time(offset: int, *, ending: bool) -> int:
        for span in spans:
            glyph_start = int(span["glyph_start"])
            glyph_end = int(span["glyph_end"])
            contains = glyph_start < offset <= glyph_end if ending else glyph_start <= offset < glyph_end
            if not contains:
                continue
            fraction = (offset - glyph_start) / (glyph_end - glyph_start)
            start_ms = int(span["start_ms"])
            end_ms = int(span["end_ms"])
            return round(start_ms + (end_ms - start_ms) * fraction)
        raise WorkflowError("ALIGN_MAP", "Caption boundary does not map to aligned output")

    captions: list[dict] = []
    caption_glyph_cursor = 0
    previous_end = 0
    for scene_index, scene in enumerate(config["narration"]["scenes"]):
        for caption_index, display in enumerate(caption_objects(scene)):
            target = spoken_glyphs(display["text"])
            end_cursor = caption_glyph_cursor + len(target)
            start_ms = max(
                previous_end,
                boundary_time(caption_glyph_cursor, ending=False),
            )
            end_ms = max(
                start_ms + 60,
                boundary_time(end_cursor, ending=True),
            )
            end_ms = min(end_ms, duration_ms)
            if end_ms <= start_ms:
                raise WorkflowError("ALIGN_RANGE", "Caption timing collapsed after clamping")
            captions.append(
                {
                    "sceneId": scene["id"],
                    "sceneIndex": scene_index,
                    "captionIndex": caption_index,
                    "text": display["text"],
                    "show": display["show"],
                    "startMs": start_ms,
                    "endMs": end_ms,
                }
            )
            previous_end = end_ms
            caption_glyph_cursor = end_cursor

    if caption_glyph_cursor != glyph_cursor:
        raise WorkflowError("ALIGN_LEFTOVER", "Aligned output contains unmapped text")
    if not captions or captions[-1]["endMs"] > duration_ms:
        raise WorkflowError("ALIGN_RANGE", "Captions exceed narration duration")
    return captions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    if args.prefetch:
        from mlx_audio.stt import load

        load(MODEL_ID, revision=MODEL_REVISION)
        print(json.dumps({"model": MODEL_ID, "revision": MODEL_REVISION}))
        return 0
    if not args.config or not args.audio or not args.out_dir:
        raise WorkflowError("ALIGN_ARGS", "--config, --audio and --out-dir are required")

    config, _ = load_and_validate_config(args.config)
    duration = wav_duration(args.audio)
    if args.out_dir.exists() and not args.out_dir.is_dir():
        raise WorkflowError("ALIGN_ARGS", "--out-dir must be a directory")

    from mlx_audio.stt import load

    aligner = load(MODEL_ID, revision=MODEL_REVISION)
    full_text = narration_text(config)
    result = list(
        aligner.generate(
            str(args.audio.resolve()),
            text=full_text,
            language=config["narration"].get("language", "Chinese"),
        )
    )
    captions = map_captions(config, result, duration)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.out_dir / "captions-all.json", captions)
    atomic_write_text(args.out_dir / "captions-all.srt", render_srt(captions, shown_only=False))
    atomic_write_text(args.out_dir / "captions-platform.srt", render_srt(captions, shown_only=True))
    atomic_write_json(
        args.out_dir / "alignment.json",
        {
            "schema": 1,
            "model": MODEL_ID,
            "revision": MODEL_REVISION,
            "audio_duration_seconds": round(duration, 6),
            "aligned_items": len(result),
            "captions": len(captions),
            "shown_captions": sum(1 for item in captions if item["show"]),
            "coverage": 1.0,
            "last_caption_end_ms": captions[-1]["endMs"],
        },
    )
    print(json.dumps({"captions": len(captions), "duration": duration}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

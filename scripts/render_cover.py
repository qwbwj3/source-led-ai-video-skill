#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from lib.ass_text import escape_ass_fragment, join_ass_lines
from lib.common import (
    FONT_PATH,
    WorkflowError,
    load_and_validate_config,
    normalized_path_identity,
    normalize_text,
    project_path,
    resolve_media_tools,
    run_process,
    safe_cover_hook_name,
)


SINGLE_LINE_LIMIT_UNITS = 6.0
SEMANTIC_BREAK_PREFIXES = (
    "只用一句话",
    "输入一句话",
    "上传一张图",
    "一段提示词",
    "一个提示词",
    "不用代码",
    "一句话",
    "一张图",
    "零代码",
    "一键生成",
    "一键做出",
)
WHITE_ASS = "&H00FFFFFF"
ORANGE_ASS = "&H00008CFF"
ORANGE_DRAWBOX = "0xFF8C00"


@dataclass(frozen=True)
class CoverLayout:
    width: int
    height: int
    max_line_units: float
    text_left: int
    text_top: int
    text_width: int
    max_font_size: int
    min_font_size: int
    outline: int
    shadow: int
    decoration_top: int
    decoration_width: int
    decoration_height: int


LAYOUTS = {
    "3x4": CoverLayout(
        width=1080,
        height=1440,
        max_line_units=10.5,
        text_left=72,
        text_top=154,
        text_width=890,
        max_font_size=120,
        min_font_size=76,
        outline=7,
        shadow=5,
        decoration_top=112,
        decoration_width=126,
        decoration_height=12,
    ),
    "4x3": CoverLayout(
        width=1440,
        height=1080,
        max_line_units=11.5,
        text_left=88,
        text_top=242,
        text_width=1100,
        max_font_size=112,
        min_font_size=72,
        outline=7,
        shadow=5,
        decoration_top=198,
        decoration_width=138,
        decoration_height=12,
    ),
}


def unit_width(token: str) -> float:
    width = 0.0
    for ch in token:
        if ch.isspace():
            width += 0.35
        elif ch in "\\{}":
            # These become full-width safe glyphs before libass sees them.
            width += 1.0
        elif ord(ch) < 128:
            width += 0.58 if ch.isalnum() else 0.42
        elif unicodedata.east_asian_width(ch) in {"W", "F"}:
            width += 1.0
        else:
            width += 0.8
    return width


def tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9+._-]*|\s+|.", text)


def _semantic_prefix_split(text: str, max_width: float) -> tuple[str, str] | None:
    for prefix in SEMANTIC_BREAK_PREFIXES:
        if not text.startswith(prefix) or len(text) == len(prefix):
            continue
        split_at = len(prefix)
        if split_at < len(text) and text[split_at] in "：:，,":
            split_at += 1
        left = text[:split_at].rstrip()
        right = text[split_at:].lstrip()
        if left and right and max(unit_width(left), unit_width(right)) <= max_width:
            return left, right
    return None


def wrap_hook_lines(
    text: str,
    max_width: float,
    *,
    single_line_limit: float = SINGLE_LINE_LIMIT_UNITS,
) -> tuple[str, ...]:
    pieces = tokens(text)
    total = sum(unit_width(piece) for piece in pieces)
    if total <= min(max_width, single_line_limit):
        return (text,)

    semantic_split = _semantic_prefix_split(text, max_width)
    if semantic_split is not None:
        return semantic_split

    candidates = []
    for index in range(1, len(pieces)):
        left = "".join(pieces[:index]).rstrip()
        right = "".join(pieces[index:]).lstrip()
        if not left or not right or right[0] in "，。；：、！？,.!?;:":
            continue
        lw, rw = unit_width(left), unit_width(right)
        overflow = max(0.0, lw - max_width) + max(0.0, rw - max_width)
        candidates.append((overflow * 20 + abs(lw - rw), left, right))
    if not candidates:
        raise WorkflowError("COVER_WRAP", "Cover hook cannot fit in two lines")
    _, left, right = min(candidates)
    if max(unit_width(left), unit_width(right)) > max_width:
        raise WorkflowError("COVER_WRAP", "Cover hook is too long for the safe area")
    return left, right


def wrap_hook(text: str, max_width: float) -> str:
    """Return an ASS-safe hook with only pipeline-owned line breaks active."""
    lines = wrap_hook_lines(text, max_width)
    return join_ass_lines(lines)


def adaptive_font_size(
    lines: tuple[str, ...],
    *,
    available_width: int,
    max_font_size: int,
    min_font_size: int,
) -> int:
    widest = max(unit_width(line) for line in lines)
    if widest <= 0:
        raise WorkflowError("COVER_WRAP", "Cover hook has no visible width")
    fitted = int((available_width * 0.92) / widest)
    return min(max_font_size, max(min_font_size, fitted))


def build_ass_document(layout: CoverLayout, lines: tuple[str, ...], font_size: int) -> str:
    line_gap = round(font_size * 1.18)
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
    )
    common_style = (
        f"Noto Sans CJK SC,{font_size},{{colour}},&H00FFFFFF,&H00101010,&H78000000,"
        f"-1,0,0,0,100,100,1,0,1,{layout.outline},{layout.shadow},7,"
        f"{layout.text_left},48,{{top}},1"
    )
    styles = [
        "Style: HookPrimary,"
        + common_style.format(colour=WHITE_ASS, top=layout.text_top),
        "Style: HookAccent,"
        + common_style.format(colour=ORANGE_ASS, top=layout.text_top + line_gap),
    ]
    events = [
        "Dialogue: 0,0:00:00.00,0:00:10.00,HookPrimary,,0,0,0,,"
        + escape_ass_fragment(lines[0])
    ]
    if len(lines) == 2:
        events.append(
            "Dialogue: 0,0:00:00.00,0:00:10.00,HookAccent,,0,0,0,,"
            + escape_ass_fragment(lines[1])
        )
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {layout.width}\nPlayResY: {layout.height}\n"
        "ScaledBorderAndShadow: yes\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        + style_format
        + "\n".join(styles)
        + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events)
        + "\n"
    )


def safe_hook_name(hook: str) -> str:
    return safe_cover_hook_name(hook)


def build_video_filter(layout: CoverLayout, text_mode: str) -> str:
    filters = [
        f"scale={layout.width}:{layout.height}:force_original_aspect_ratio=increase",
        f"crop={layout.width}:{layout.height}",
    ]
    if text_mode == "deterministic":
        filters.extend(
            (
                f"drawbox=x={layout.text_left}:y={layout.decoration_top}:"
                f"w={layout.decoration_width}:h={layout.decoration_height}:"
                f"color={ORANGE_DRAWBOX}:t=fill",
                "ass=cover.ass:fontsdir=fonts",
            )
        )
    elif text_mode != "imagegen":
        raise WorkflowError("CONFIG_COVER", "Unsupported cover text mode")
    return ",".join(filters)


def resolve_headline_lines(cover: dict, layout: CoverLayout) -> tuple[str, ...]:
    configured = cover.get("headline_lines")
    if configured is not None:
        return tuple(normalize_text(line) for line in configured)
    return wrap_hook_lines(normalize_text(cover["hook"]), layout.max_line_units)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ratio", choices=("3x4", "4x3"), required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args()
    config, project_root = load_and_validate_config(args.config)
    entry = config["cover"][args.ratio]
    source = project_path(project_root, entry["generated_source"], must_exist=True)
    output = project_path(project_root, entry["final"], must_exist=False)
    if normalized_path_identity(source) == normalized_path_identity(output):
        raise WorkflowError("COVER_PATHS", "Cover output must not overwrite its generated source")
    try:
        same_existing_file = output.exists() and os.path.samefile(source, output)
    except OSError as exc:
        raise WorkflowError("COVER_PATHS", "Cover paths cannot be compared safely") from exc
    if same_existing_file:
        raise WorkflowError("COVER_PATHS", "Cover output must use a separate file")
    if output.exists() and not output.is_file():
        raise WorkflowError("COVER_PATHS", "Cover output path must be a file")
    hook = normalize_text(config["cover"]["hook"])
    if safe_hook_name(hook) not in output.stem:
        raise WorkflowError("COVER_NAME", "Cover filename must contain the hook")
    ffmpeg, _ = resolve_media_tools(args.ffmpeg, args.ffprobe)
    output.parent.mkdir(parents=True, exist_ok=True)

    layout = LAYOUTS[args.ratio]
    text_mode = entry.get(
        "text_mode", config["cover"].get("text_mode", "deterministic")
    )

    with tempfile.TemporaryDirectory(prefix="cover-render-", dir=output.parent) as temp_raw:
        temp = Path(temp_raw)
        if text_mode == "deterministic":
            lines = resolve_headline_lines(config["cover"], layout)
            font_size = adaptive_font_size(
                lines,
                available_width=layout.text_width,
                max_font_size=layout.max_font_size,
                min_font_size=layout.min_font_size,
            )
            fonts = temp / "fonts"
            fonts.mkdir()
            shutil.copy2(FONT_PATH, fonts / FONT_PATH.name)
            ass = temp / "cover.ass"
            ass.write_text(build_ass_document(layout, lines, font_size), encoding="utf-8")
        temp_output = temp / "cover.png"
        video_filter = build_video_filter(layout, text_mode)
        result = run_process(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source,
                "-vf",
                video_filter,
                "-frames:v",
                "1",
                temp_output,
            ],
            cwd=temp,
        )
        if result.returncode != 0 or not temp_output.exists():
            raise WorkflowError("COVER_RENDER", "FFmpeg could not render the cover")
        if normalized_path_identity(source) == normalized_path_identity(output):
            raise WorkflowError("COVER_PATHS", "Cover output identity matches its source")
        try:
            if output.exists() and os.path.samefile(source, output):
                raise WorkflowError("COVER_PATHS", "Cover output would overwrite its source")
        except OSError as exc:
            raise WorkflowError("COVER_PATHS", "Cover paths cannot be compared safely") from exc
        os.replace(temp_output, output)
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

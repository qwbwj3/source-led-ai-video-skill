#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any

from lib.common import (
    BGM_FADE_OUT_DEFAULT_SECONDS,
    FIXED_FINAL_LUFS,
    FIXED_MAX_DURATION_SECONDS,
    FIXED_RETIME_RATIO_LIMITS,
    FONT_PATH,
    MODEL_ID,
    MODEL_REVISION,
    REQUIRED_VOLC_ENV,
    SKILL_ROOT,
    MANAGED_PROJECT_ROOTS,
    WorkflowError,
    assert_managed_tree,
    atomic_write_json,
    canonical_json,
    caption_objects,
    ensure_managed_dir,
    load_and_validate_config,
    load_env_file,
    managed_atomic_write_json,
    managed_atomic_write_text,
    managed_copy_file,
    managed_path,
    narration_hash,
    narration_text,
    normalize_text,
    project_path,
    read_json,
    redact,
    resolve_aligner_python,
    resolve_media_tools,
    open_managed_lock,
    remove_managed_tree,
    replace_managed_dir,
    run_process,
    runtime_root,
    sha256_bytes,
    sha256_file,
)
from lib.volc_tts import cache_provenance, synthesize
from lib.ass_text import join_ass_lines
from lib.runtime_contract import aligner_runtime_fingerprint
from review_gate import (
    policy_fingerprint,
    require_confirmed_rights,
    verify_approval,
    verify_packaged_approval,
)


FPS = 30
SAMPLE_RATE = 48_000


IMPLEMENTATION_FILES = (
    "SKILL.md",
    "assets/project-template.json",
    "references/cover-standard.md",
    "references/environment-contract.md",
    "references/production-standard.md",
    "references/project-schema.md",
    "references/qa-standard.md",
    "scripts/workflow.py",
    "scripts/bootstrap_runtime.py",
    "scripts/align_captions.py",
    "scripts/review_gate.py",
    "scripts/precheck_scan.py",
    "scripts/render_cover.py",
    "scripts/lib/common.py",
    "scripts/lib/ass_text.py",
    "scripts/lib/volc_tts.py",
    "scripts/lib/runtime_contract.py",
    "scripts/requirements-lock.txt",
)

REQUIRED_BUILD_ARTIFACTS = (
    "video.mp4",
    "narration-raw.mp3",
    "narration-master.wav",
    "audio-mix.wav",
    "captions-all.json",
    "captions-all.srt",
    "captions-platform.srt",
    "captions-platform.ass",
    "alignment.json",
    "video-filter.txt",
    "provenance.json",
    "qa/auto-qa.json",
    "qa/auto-qa.md",
    "qa/contact-sheet.jpg",
    "covers/cover-3x4.png",
    "covers/cover-4x3.png",
    "project.json",
    "review/review-input.json",
    "review/narration.txt",
    "review/lexical-scan.json",
    "review/approval.json",
)


def _managed_owner(path: Path) -> Path | None:
    """Infer the project root from a lexical managed directory component."""
    current = Path(os.path.abspath(os.fspath(path)))
    while current.parent != current:
        if current.name in MANAGED_PROJECT_ROOTS:
            return current.parent
        current = current.parent
    return None


def _assert_plain_tree(path: Path) -> Path:
    """Reject a symlinked bundle/tree even when it was copied outside a project."""
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        root_stat = os.lstat(lexical)
    except OSError as exc:
        raise WorkflowError("MANAGED_PATH", "Artifact directory is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkflowError("MANAGED_PATH", "Artifact root must be a plain directory")
    for walk_root, dir_names, file_names in os.walk(lexical, topdown=True, followlinks=False):
        for name in [*dir_names, *file_names]:
            try:
                entry_stat = os.lstat(Path(walk_root) / name)
            except OSError as exc:
                raise WorkflowError("MANAGED_PATH", "Artifact tree cannot be inspected") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise WorkflowError("MANAGED_SYMLINK", "Artifact trees cannot contain symlinks")
    return lexical


def _checked_tree(path: Path, project_root: Path | None = None) -> tuple[Path, Path | None]:
    owner = project_root.resolve() if project_root is not None else _managed_owner(path)
    if owner is not None:
        return assert_managed_tree(owner, path), owner
    return _assert_plain_tree(path), None


def implementation_fingerprint() -> dict[str, str]:
    fingerprint = {
        relative: sha256_file(SKILL_ROOT / relative)
        for relative in IMPLEMENTATION_FILES
    }
    fingerprint.update(policy_fingerprint())
    return dict(sorted(fingerprint.items()))


def config_sha256(config: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(config))


def copy_review_evidence(
    stage: Path,
    config: dict[str, Any],
    project_root: Path,
    approval: dict[str, Any],
) -> None:
    assert_managed_tree(project_root, stage)
    managed_atomic_write_json(project_root, stage / "project.json", config)
    review_out = ensure_managed_dir(project_root, stage / "review")
    for name in (
        "review-input.json",
        "narration.txt",
        "review-content.md",
        "lexical-scan.json",
        "approval.json",
    ):
        try:
            source = managed_path(
                project_root,
                Path("review") / name,
                must_exist=True,
                kind="file",
            )
        except WorkflowError as exc:
            if exc.code == "MANAGED_MISSING":
                raise WorkflowError("REVIEW_EVIDENCE", f"Missing review evidence: {name}") from exc
            raise
        managed_copy_file(project_root, source, review_out / name)
    semantic_report = approval.get("semantic_report")
    if type(semantic_report) is not str:
        raise WorkflowError("REVIEW_EVIDENCE", "Semantic review path is invalid")
    report = managed_path(
        project_root, semantic_report, must_exist=True, kind="file"
    )
    relative = Path(approval["semantic_report"])
    destination = stage / relative
    managed_copy_file(project_root, report, destination)


def write_build_manifest(
    stage: Path,
    config: dict[str, Any],
    build_key: str,
    implementation: dict[str, str],
) -> dict[str, Any]:
    stage, project_root = _checked_tree(stage)
    for relative in REQUIRED_BUILD_ARTIFACTS:
        artifact = stage / relative
        if project_root is not None:
            try:
                managed_path(project_root, artifact, must_exist=True, kind="file")
            except WorkflowError as exc:
                if exc.code == "MANAGED_MISSING":
                    raise WorkflowError("BUILD_ARTIFACT", f"Missing required artifact: {relative}") from exc
                raise
        if not artifact.is_file():
            raise WorkflowError("BUILD_ARTIFACT", f"Missing required artifact: {relative}")
    artifacts = {
        path.relative_to(stage).as_posix(): sha256_file(path)
        for path in sorted(stage.rglob("*"))
        if path.is_file() and path.name != "build-manifest.json"
    }
    manifest = {
        "schema": 1,
        "kind": "build",
        "build_key": build_key,
        "config_sha256": config_sha256(config),
        "implementation": implementation,
        "artifacts": artifacts,
    }
    if project_root is not None:
        managed_atomic_write_json(project_root, stage / "build-manifest.json", manifest)
    else:
        atomic_write_json(stage / "build-manifest.json", manifest)
    return manifest


def verify_hash_manifest(
    root: Path,
    manifest_name: str,
    *,
    allowed_unlisted: tuple[str, ...] = (),
) -> dict[str, Any]:
    root, project_root = _checked_tree(root)
    manifest_path = root / manifest_name
    if project_root is not None:
        try:
            managed_path(project_root, manifest_path, must_exist=True, kind="file")
        except WorkflowError as exc:
            if exc.code == "MANAGED_MISSING":
                raise WorkflowError("MANIFEST_MISSING", f"Missing {manifest_name}") from exc
            raise
    if not manifest_path.is_file():
        raise WorkflowError("MANIFEST_MISSING", f"Missing {manifest_name}")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise WorkflowError("MANIFEST_INVALID", f"Invalid {manifest_name}")
    artifacts = manifest.get("artifacts")
    if manifest.get("schema") != 1 or not isinstance(artifacts, dict) or not artifacts:
        raise WorkflowError("MANIFEST_INVALID", f"Invalid {manifest_name}")
    for relative, expected in artifacts.items():
        if not isinstance(relative, str) or not re.fullmatch(r"[^\r\n]+", relative):
            raise WorkflowError("MANIFEST_INVALID", "Manifest contains an invalid path")
        path = project_path(root, relative, must_exist=True)
        if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise WorkflowError("MANIFEST_INVALID", f"Invalid artifact record: {relative}")
        if sha256_file(path) != expected:
            raise WorkflowError("MANIFEST_STALE", f"Artifact changed after QA: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != manifest_name
    }
    unexpected = sorted(actual - set(artifacts) - set(allowed_unlisted))
    if unexpected:
        raise WorkflowError("MANIFEST_UNLISTED", "Unlisted artifact: " + unexpected[0])
    return manifest


def verify_build_integrity(
    config_path: Path,
    run_dir: Path,
    *,
    require_current_implementation: bool = True,
) -> dict[str, Any]:
    config, project_root = load_and_validate_config(config_path)
    current_approval = verify_approval(config_path)
    run_lexical = managed_path(
        project_root,
        Path(os.path.abspath(os.fspath(run_dir))),
        must_exist=True,
        kind="dir",
    )
    try:
        run_lexical.relative_to(project_root.resolve() / "review-runs")
    except ValueError as exc:
        raise WorkflowError("BUILD_PATH", "Review run must be inside project/review-runs") from exc
    run_dir = run_lexical
    assert_managed_tree(project_root, run_dir)
    manifest = verify_hash_manifest(
        run_dir,
        "build-manifest.json",
        allowed_unlisted=("qa/visual-review.json", "bundle-manifest.json"),
    )
    if manifest.get("build_key") != run_dir.name:
        raise WorkflowError("BUILD_KEY", "Run directory does not match its build key")
    if manifest.get("config_sha256") != config_sha256(config):
        raise WorkflowError("BUILD_STALE", "Project config changed after this run")
    if require_current_implementation and manifest.get("implementation") != implementation_fingerprint():
        raise WorkflowError("BUILD_STALE", "Pipeline implementation changed after this run")
    for relative in REQUIRED_BUILD_ARTIFACTS:
        if relative not in manifest["artifacts"]:
            raise WorkflowError("BUILD_ARTIFACT", f"Manifest lacks required artifact: {relative}")
    if manifest["artifacts"].get("review/approval.json") != sha256_file(
        managed_path(
            project_root,
            "review/approval.json",
            must_exist=True,
            kind="file",
        )
    ):
        raise WorkflowError("BUILD_STALE", "Current review approval differs from the run")
    bundled_approval = read_json(run_dir / "review/approval.json")
    if bundled_approval != current_approval:
        raise WorkflowError("BUILD_STALE", "Bundled approval does not match current approval")
    auto = read_json(run_dir / "qa/auto-qa.json")
    if not auto.get("passed") or auto.get("video_sha256") != sha256_file(run_dir / "video.mp4"):
        raise WorkflowError("QA_STALE", "Automatic QA is failed or stale")
    provenance = read_json(run_dir / "provenance.json")
    if (
        provenance.get("build_key") != run_dir.name
        or provenance.get("config_sha256") != config_sha256(config)
    ):
        raise WorkflowError("PROVENANCE_STALE", "Run provenance does not match the project")
    return {
        "manifest": manifest,
        "auto": auto,
        "provenance": provenance,
        "approval": current_approval,
    }


def verify_visual_record(run_dir: Path, build_manifest: dict[str, Any]) -> dict[str, Any]:
    run_dir, project_root = _checked_tree(run_dir)
    visual_path = run_dir / "qa/visual-review.json"
    if project_root is not None:
        try:
            managed_path(project_root, visual_path, must_exist=True, kind="file")
        except WorkflowError as exc:
            if exc.code == "MANAGED_MISSING":
                raise WorkflowError("VISUAL_MISSING", "Visual and audio review is missing") from exc
            raise
    if not visual_path.is_file():
        raise WorkflowError("VISUAL_MISSING", "Visual and audio review is missing")
    visual = read_json(visual_path)
    if type(visual) is not dict:
        raise WorkflowError("VISUAL_INVALID", "Visual review must be a JSON object")
    required = {
        "schema",
        "result",
        "reviewer",
        "notes",
        "failed_checks",
        "checks",
        "build_manifest_sha256",
        "video_sha256",
        "contact_sheet_sha256",
        "cover_3x4_sha256",
        "cover_4x3_sha256",
    }
    if not required.issubset(visual):
        raise WorkflowError("VISUAL_INVALID", "Visual review is incomplete")
    if type(visual["schema"]) is not int or visual["schema"] != 2:
        raise WorkflowError("VISUAL_INVALID", "Visual review schema must be 2")
    result = visual["result"]
    reviewer = visual["reviewer"]
    notes = visual["notes"]
    failed_checks = visual["failed_checks"]
    checks = visual["checks"]
    if type(result) is not str or result not in {"pass", "fail"}:
        raise WorkflowError("VISUAL_INVALID", "Visual review result must be pass or fail")
    if (
        type(reviewer) is not str
        or normalize_text(reviewer) != reviewer
        or not reviewer
    ):
        raise WorkflowError("VISUAL_INVALID", "Visual reviewer is invalid")
    if (
        type(notes) is not str
        or normalize_text(notes) != notes
        or len(notes) < 12
    ):
        raise WorkflowError("VISUAL_INVALID", "Visual review notes are invalid")
    if (
        type(failed_checks) is not list
        or any(type(name) is not str for name in failed_checks)
        or failed_checks != sorted(set(failed_checks))
        or any(name not in VISUAL_CHECKS for name in failed_checks)
    ):
        raise WorkflowError("VISUAL_INVALID", "Visual failed_checks are invalid")
    if (
        type(checks) is not dict
        or set(checks) != set(VISUAL_CHECKS)
        or any(type(value) is not bool for value in checks.values())
    ):
        raise WorkflowError("VISUAL_INVALID", "Visual checks are incomplete or invalid")
    expected_checks = {name: name not in failed_checks for name in VISUAL_CHECKS}
    if checks != expected_checks:
        raise WorkflowError("VISUAL_INVALID", "Visual checks contradict failed_checks")
    if (result == "pass" and failed_checks) or (result == "fail" and not failed_checks):
        raise WorkflowError("VISUAL_INVALID", "Visual result contradicts failed_checks")
    hash_fields = (
        "build_manifest_sha256",
        "video_sha256",
        "contact_sheet_sha256",
        "cover_3x4_sha256",
        "cover_4x3_sha256",
    )
    if any(
        type(visual[field]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", visual[field]) is None
        for field in hash_fields
    ):
        raise WorkflowError("VISUAL_INVALID", "Visual artifact hashes are invalid")
    if (
        visual["build_manifest_sha256"] != sha256_file(run_dir / "build-manifest.json")
        or visual["video_sha256"] != build_manifest["artifacts"].get("video.mp4")
        or visual["contact_sheet_sha256"]
        != build_manifest["artifacts"].get("qa/contact-sheet.jpg")
        or visual["cover_3x4_sha256"]
        != build_manifest["artifacts"].get("covers/cover-3x4.png")
        or visual["cover_4x3_sha256"]
        != build_manifest["artifacts"].get("covers/cover-4x3.png")
    ):
        raise WorkflowError("VISUAL_STALE", "Visual review is not bound to current artifacts")
    return visual


def probe(ffprobe: Path, path: Path) -> dict[str, Any]:
    result = run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,bit_rate:stream=index,codec_type,codec_name,profile,width,height,pix_fmt,r_frame_rate,avg_frame_rate,sample_rate,channels,channel_layout,nb_frames:stream_tags=rotate:stream_side_data=rotation,displaymatrix:format_tags=major_brand",
            "-of",
            "json",
            path,
        ]
    )
    if result.returncode != 0:
        raise WorkflowError("MEDIA_PROBE", f"FFprobe failed for {path.name}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("MEDIA_PROBE", f"Invalid FFprobe output for {path.name}") from exc


def media_duration(data: dict[str, Any]) -> float:
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError("MEDIA_DURATION", "Media duration is unavailable") from exc
    if not (0 < duration <= 600):
        raise WorkflowError("MEDIA_DURATION", "Media duration is outside the supported range")
    return duration


def stream(data: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((item for item in data.get("streams", []) if item.get("codec_type") == kind), None)


def validate_base(ffprobe: Path, base: Path, config: dict[str, Any]) -> dict[str, Any]:
    info = probe(ffprobe, base)
    streams = info.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    if len(streams) != 1 or len(videos) != 1:
        raise WorkflowError("BASE_STREAMS", "Base must contain exactly one video stream")
    video = videos[0]
    if (
        video.get("codec_name"),
        video.get("width"),
        video.get("height"),
        video.get("pix_fmt"),
        video.get("r_frame_rate"),
        video.get("avg_frame_rate"),
    ) != ("h264", 1080, 1920, "yuv420p", "30/1", "30/1"):
        raise WorkflowError("BASE_FORMAT", "Base must be H.264 yuv420p 1080x1920 CFR 30 fps")
    rotation = (video.get("tags") or {}).get("rotate")
    try:
        tag_rotation = 0.0 if rotation in (None, "") else float(rotation)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("BASE_ROTATION", "Base has invalid rotation metadata") from exc
    side_data = video.get("side_data_list") or []
    rotated_side_data = False
    if type(side_data) is not list:
        rotated_side_data = True
    else:
        for item in side_data:
            if type(item) is not dict:
                rotated_side_data = True
                break
            side_rotation = item.get("rotation")
            try:
                nonzero_rotation = side_rotation not in (None, "") and abs(float(side_rotation)) > 1e-6
            except (TypeError, ValueError):
                nonzero_rotation = True
            if item.get("displaymatrix") or nonzero_rotation:
                rotated_side_data = True
                break
    if abs(tag_rotation) > 1e-6 or rotated_side_data:
        raise WorkflowError("BASE_ROTATION", "Base must not rely on rotation metadata")
    duration = media_duration(info)
    configured_end = float(config["source_timeline"]["scene_boundaries_seconds"][-1])
    if abs(duration - configured_end) > 0.12:
        raise WorkflowError("BASE_TIMELINE", "Configured scene timeline does not reach the base video end")
    return info


def validate_cover_files(
    ffprobe: Path, config: dict[str, Any], project_root: Path
) -> list[dict[str, Any]]:
    hook = normalize_text(config["cover"]["hook"])
    safe_hook = re.sub(r"[\\/:*?\"<>|\s，。；：、！？,.!?;:]+", "", hook)[:32]
    expected = {"3x4": (1080, 1440), "4x3": (1440, 1080)}
    records = []
    source_hashes = set()
    cover_files: list[Path] = []
    for ratio in ("3x4", "4x3"):
        entry = config["cover"][ratio]
        generated = project_path(project_root, entry["generated_source"], must_exist=True)
        final = project_path(project_root, entry["final"], must_exist=True)
        cover_files.extend((generated, final))
        if not generated.is_file() or not final.is_file():
            raise WorkflowError("COVER_FILE", f"{ratio} cover paths must be files")
        if safe_hook not in final.stem:
            raise WorkflowError("COVER_NAME", f"{ratio} cover filename must include the hook")
        if generated.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise WorkflowError("COVER_SOURCE", f"{ratio} imagegen source must be a PNG")
        generated_info = probe(ffprobe, generated)
        generated_streams = [
            item for item in generated_info.get("streams", []) if item.get("codec_type") == "video"
        ]
        expected_ratio = 3 / 4 if ratio == "3x4" else 4 / 3
        if (
            len(generated_streams) != 1
            or len(generated_info.get("streams", [])) != 1
            or generated_streams[0].get("codec_name") != "png"
            or not generated_streams[0].get("width")
            or not generated_streams[0].get("height")
            or abs(
                generated_streams[0]["width"] / generated_streams[0]["height"]
                - expected_ratio
            )
            > 0.015
        ):
            raise WorkflowError("COVER_SOURCE", f"{ratio} imagegen source has the wrong format or ratio")
        source_hashes.add(sha256_file(generated))
        info = probe(ffprobe, final)
        images = [item for item in info.get("streams", []) if item.get("codec_type") == "video"]
        if (
            len(images) != 1
            or len(info.get("streams", [])) != 1
            or images[0].get("codec_name") != "png"
            or (images[0].get("width"), images[0].get("height")) != expected[ratio]
            or final.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise WorkflowError("COVER_SIZE", f"{ratio} cover has the wrong dimensions")
        records.append(
            {
                "ratio": ratio,
                "generated_source": generated,
                "final": final,
                "source_sha256": sha256_file(generated),
                "final_sha256": sha256_file(final),
            }
        )
    if len(source_hashes) != 2:
        raise WorkflowError("COVER_VARIANTS", "3:4 and 4:3 must come from two independent imagegen calls")
    for index, first in enumerate(cover_files):
        for second in cover_files[index + 1 :]:
            try:
                same_file = os.path.samefile(first, second)
            except OSError as exc:
                raise WorkflowError("COVER_FILE", "Cover files cannot be compared safely") from exc
            if same_file:
                raise WorkflowError("COVER_FILE", "Cover source and final files must be separate inodes")
    return records


def caption_unit_width(text: str) -> float:
    width = 0.0
    for ch in text:
        if ch.isspace():
            width += 0.35
        elif ord(ch) < 128:
            width += 0.58 if ch.isalnum() else 0.42
        elif unicodedata.east_asian_width(ch) in {"W", "F"}:
            width += 1.0
        else:
            width += 0.8
    return width


def caption_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9+._-]*|\s+|.", text)


def wrap_caption(text: str, max_units: float) -> list[str]:
    if caption_unit_width(text) <= max_units:
        return [text]
    pieces = caption_tokens(text)
    candidates = []
    for index in range(1, len(pieces)):
        left = "".join(pieces[:index]).rstrip()
        right = "".join(pieces[index:]).lstrip()
        if not left or not right or right[0] in "，。；：、！？,.!?;:":
            continue
        left_width, right_width = caption_unit_width(left), caption_unit_width(right)
        overflow = max(0.0, left_width - max_units) + max(0.0, right_width - max_units)
        bonus = -1.0 if left[-1] in "，。；：、！？ " else 0.0
        candidates.append((overflow * 20 + abs(left_width - right_width) + bonus, left, right))
    if not candidates:
        raise WorkflowError("CAPTION_WRAP", f"Caption cannot fit safely: {text}")
    _, left, right = min(candidates)
    if max(caption_unit_width(left), caption_unit_width(right)) > max_units:
        raise WorkflowError("CAPTION_WRAP", f"Caption is too long for two lines: {text}")
    return [left, right]


def ass_time(milliseconds: int) -> str:
    centiseconds = round(milliseconds / 10)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def build_ass(
    stage: Path,
    captions: list[dict[str, Any]],
    config: dict[str, Any],
    project_root: Path,
) -> Path:
    assert_managed_tree(project_root, stage)
    style = config.get("caption_style", {})
    font_size = int(style.get("font_size", 68))
    margin_v = int(style.get("margin_v", 470))
    max_units = float(style.get("max_line_units", 14))
    if not (54 <= font_size <= 76 and 350 <= margin_v <= 620 and 10 <= max_units <= 16):
        raise WorkflowError("CAPTION_STYLE", "Caption style is outside the safe range")
    fonts = ensure_managed_dir(project_root, stage / "fonts")
    managed_copy_file(project_root, FONT_PATH, fonts / FONT_PATH.name)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\nWrapStyle: 2\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,3,12,0,2,58,58,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for item in captions:
        if not item["show"]:
            continue
        display = join_ass_lines(wrap_caption(item["text"], max_units))
        events.append(
            f"Dialogue: 0,{ass_time(item['startMs'])},{ass_time(item['endMs'])},Main,,0,0,0,,{display}\n"
        )
    path = stage / "captions-platform.ass"
    managed_atomic_write_text(project_root, path, header + "".join(events))
    return path


def target_timeline(
    captions: list[dict[str, Any]], config: dict[str, Any], audio_duration: float
) -> dict[str, Any]:
    scenes = config["narration"]["scenes"]
    lead_ms = int(config["narration"].get("caption_lead_ms", 80))
    total_frames = math.ceil(audio_duration * FPS - 1e-9)
    boundaries = [0]
    for scene_index in range(1, len(scenes)):
        first = next(item for item in captions if item["sceneIndex"] == scene_index)
        previous_last = max(item["endMs"] for item in captions if item["sceneIndex"] == scene_index - 1)
        candidate_ms = max(previous_last, first["startMs"] - lead_ms)
        frame = round(candidate_ms * FPS / 1000)
        if frame <= boundaries[-1] or frame >= total_frames:
            raise WorkflowError("TIMELINE_TARGET", "Derived scene boundary is invalid")
        boundaries.append(frame)
    boundaries.append(total_frames)

    source_seconds = [float(value) for value in config["source_timeline"]["scene_boundaries_seconds"]]
    source_frames = [round(value * FPS) for value in source_seconds]
    if source_frames[0] != 0 or any(b <= a for a, b in zip(source_frames, source_frames[1:])):
        raise WorkflowError("TIMELINE_SOURCE", "Source boundaries collapse at 30 fps")
    low, high = FIXED_RETIME_RATIO_LIMITS
    ratios = []
    for index in range(len(scenes)):
        ratio = (boundaries[index + 1] - boundaries[index]) / (
            source_frames[index + 1] - source_frames[index]
        )
        if not low <= ratio <= high:
            raise WorkflowError(
                "TIMELINE_RETIME",
                f"Scene {scenes[index]['id']} retime factor {ratio:.3f} is outside {low:.2f}-{high:.2f}",
            )
        ratios.append(ratio)
    return {
        "fps": FPS,
        "source_boundaries_frames": source_frames,
        "target_boundaries_frames": boundaries,
        "retime_ratios": ratios,
        "total_frames": total_frames,
        "duration_seconds": total_frames / FPS,
    }


def create_video_filter(
    stage: Path, timeline: dict[str, Any], project_root: Path
) -> Path:
    assert_managed_tree(project_root, stage)
    source = timeline["source_boundaries_frames"]
    target = timeline["target_boundaries_frames"]
    ratios = timeline["retime_ratios"]
    count = len(ratios)
    parts = [f"[0:v]split={count}" + "".join(f"[v{i}]" for i in range(count)) + ";"]
    for index, ratio in enumerate(ratios):
        parts.append(
            f"[v{index}]trim=start_frame={source[index]}:end_frame={source[index + 1]},"
            f"setpts=(PTS-STARTPTS)*{ratio:.12f}[s{index}];"
        )
    parts.append("".join(f"[s{i}]" for i in range(count)))
    parts.append(
        f"concat=n={count}:v=1:a=0,fps={FPS},format=yuv420p,"
        "ass=captions-platform.ass:fontsdir=fonts[vout]\n"
    )
    path = stage / "video-filter.txt"
    managed_atomic_write_text(project_root, path, "".join(parts))
    return path


def run_checked(args: list[str | Path], code: str, message: str, *, cwd: Path | None = None) -> None:
    result = run_process(args, cwd=cwd)
    if result.returncode != 0:
        raise WorkflowError(code, message)


def measure_loudness(ffmpeg: Path, media: Path) -> tuple[float, float]:
    result = run_process(
        [ffmpeg, "-hide_banner", "-nostats", "-i", media, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    )
    if result.returncode != 0:
        raise WorkflowError("QA_LOUDNESS", "Loudness analysis failed")
    text = result.stderr or ""
    integrated = re.findall(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?) LUFS", text)
    peaks = re.findall(r"True peak:\s*\n\s*Peak:\s*(-?\d+(?:\.\d+)?) dBFS", text)
    if not integrated or not peaks:
        raise WorkflowError("QA_LOUDNESS", "Could not measure loudness")
    return float(integrated[-1]), float(peaks[-1])


def render_contact_sheet(ffmpeg: Path, video: Path, output: Path, duration: float) -> None:
    interval = max(duration / 16, 0.1)
    run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            video,
            "-vf",
            f"fps=1/{interval:.6f},scale=270:480,tile=4x4:padding=6:margin=6",
            "-frames:v",
            "1",
            output,
        ],
        "QA_CONTACT_SHEET",
        "Could not generate contact sheet",
    )


def auto_qa(
    ffmpeg: Path,
    ffprobe: Path,
    stage: Path,
    config: dict[str, Any],
    timeline: dict[str, Any],
    cover_records: list[dict[str, Any]],
    project_root: Path,
) -> dict[str, Any]:
    assert_managed_tree(project_root, stage)
    final = managed_path(
        project_root, stage / "video.mp4", must_exist=True, kind="file"
    )
    info = probe(ffprobe, final)
    video = stream(info, "video")
    audio = stream(info, "audio")
    duration = media_duration(info)
    failures: list[str] = []
    if len(info.get("streams", [])) != 2:
        failures.append("expected exactly one video and one audio stream")
    if not video or (
        video.get("codec_name"),
        video.get("width"),
        video.get("height"),
        video.get("pix_fmt"),
        video.get("r_frame_rate"),
    ) != ("h264", 1080, 1920, "yuv420p", "30/1"):
        failures.append("video stream is not H.264 1080x1920 yuv420p CFR30")
    if not audio or (
        audio.get("codec_name"),
        audio.get("sample_rate"),
        audio.get("channels"),
    ) != ("aac", "48000", 2):
        failures.append("audio stream is not AAC 48kHz stereo")
    if abs(duration - timeline["duration_seconds"]) > 1 / FPS + 0.01:
        failures.append("container duration differs from frame timeline")
    if duration > FIXED_MAX_DURATION_SECONDS:
        failures.append("duration exceeds project maximum")

    decode = run_process([ffmpeg, "-v", "error", "-i", final, "-f", "null", "-"])
    if decode.returncode != 0:
        failures.append("full decode failed")
    loudness, true_peak = measure_loudness(ffmpeg, final)
    target = FIXED_FINAL_LUFS
    if abs(loudness - target) > 0.5:
        failures.append(f"loudness {loudness:.1f} LUFS is outside target ±0.5")
    if true_peak > -1.4:
        failures.append(f"true peak {true_peak:.1f} dBFS exceeds -1.4")

    black = run_process(
        [ffmpeg, "-hide_banner", "-nostats", "-i", final, "-vf", "blackdetect=d=0.30:pix_th=0.02:pic_th=0.98", "-an", "-f", "null", "-"]
    )
    if black.returncode != 0:
        failures.append("black-frame detector failed")
    black_events = re.findall(r"black_duration:([0-9.]+)", black.stderr or "")
    if black_events:
        failures.append("undeclared full-black interval detected")
    silence = run_process(
        [ffmpeg, "-hide_banner", "-nostats", "-i", final, "-vn", "-af", "silencedetect=n=-42dB:d=0.80", "-f", "null", "-"]
    )
    if silence.returncode != 0:
        failures.append("silence detector failed")
    silence_events = re.findall(r"silence_duration: ([0-9.]+)", silence.stderr or "")
    if silence_events:
        failures.append("undeclared silence longer than 0.8 seconds detected")

    captions = read_json(
        managed_path(
            project_root, stage / "captions-all.json", must_exist=True, kind="file"
        )
    )
    previous_end = 0
    for item in captions:
        if item["startMs"] < previous_end or item["endMs"] <= item["startMs"]:
            failures.append("caption timings overlap or reverse")
            break
        previous_end = item["endMs"]
    if captions[-1]["endMs"] > timeline["duration_seconds"] * 1000 + 1:
        failures.append("captions exceed final duration")

    qa_dir = ensure_managed_dir(project_root, stage / "qa")
    contact_sheet = managed_path(project_root, qa_dir / "contact-sheet.jpg", kind="file")
    render_contact_sheet(ffmpeg, final, contact_sheet, duration)
    managed_path(project_root, contact_sheet, must_exist=True, kind="file")
    report = {
        "schema": 1,
        "status": "AUTO_PASS_VISUAL_PENDING" if not failures else "FAIL",
        "passed": not failures,
        "failures": failures,
        "duration_seconds": round(duration, 6),
        "loudness_lufs": loudness,
        "true_peak_dBFS": true_peak,
        "black_intervals": len(black_events),
        "long_silences": len(silence_events),
        "timeline": timeline,
        "captions": {
            "total": len(captions),
            "shown": sum(1 for item in captions if item["show"]),
            "last_end_ms": captions[-1]["endMs"],
        },
        "covers": [
            {
                "ratio": item["ratio"],
                "source_sha256": item["source_sha256"],
                "final_sha256": item["final_sha256"],
            }
            for item in cover_records
        ],
        "video_sha256": sha256_file(final),
        "contact_sheet": "qa/contact-sheet.jpg",
    }
    managed_atomic_write_json(project_root, qa_dir / "auto-qa.json", report)
    lines = [
        "# 自动 QA",
        "",
        f"- 结论：{'通过，等待视觉审片' if report['passed'] else '失败'}",
        f"- 时长：{duration:.3f} 秒",
        f"- 响度：{loudness:.1f} LUFS",
        f"- True Peak：{true_peak:.1f} dBFS",
        f"- 字幕：{report['captions']['shown']}/{report['captions']['total']} 条烧录",
    ]
    if failures:
        lines += ["", "## 阻断项", ""] + [f"- {item}" for item in failures]
    managed_atomic_write_text(project_root, qa_dir / "auto-qa.md", "\n".join(lines) + "\n")
    if failures:
        raise WorkflowError("QA_FAILED", "; ".join(failures))
    return report


def validate_replay_audio(
    audio_arg: Path,
    proof_arg: Path | None,
    config: dict[str, Any],
    project_root: Path,
) -> tuple[Path, dict[str, Any]]:
    if proof_arg is None:
        raise WorkflowError("REPLAY_PROOF", "--reuse-tts requires --reuse-tts-provenance")
    audio = audio_arg.resolve()
    proof_path = proof_arg.resolve()
    for path in (audio, proof_path):
        try:
            path.relative_to(project_root.resolve())
        except ValueError as exc:
            raise WorkflowError("REPLAY_PATH", "Replay audio and proof must be inside the project") from exc
        if not path.is_file():
            raise WorkflowError("REPLAY_PATH", "Replay audio or proof is missing")
    proof = read_json(proof_path)
    if (
        not isinstance(proof, dict)
        or proof.get("schema") != 1
        or proof.get("purpose") != "regression-replay"
        or proof.get("approved") is not True
        or proof.get("narration_sha256") != narration_hash(config)
        or proof.get("audio_sha256") != sha256_file(audio)
        or proof.get("provider") != "volcengine"
    ):
        raise WorkflowError("REPLAY_PROOF", "Replay proof does not match narration and audio")
    return audio, proof


def run_build(args: argparse.Namespace) -> dict[str, Any]:
    config_path = args.config.resolve()
    config, project_root = load_and_validate_config(config_path)
    approval = verify_approval(config_path)
    ffmpeg, ffprobe = resolve_media_tools(args.ffmpeg, args.ffprobe)
    aligner = resolve_aligner_python(args.aligner_python)
    aligner_runtime = aligner_runtime_fingerprint(aligner)
    base = project_path(project_root, config["paths"]["base_video"], must_exist=True)
    validate_base(ffprobe, base, config)
    cover_records = validate_cover_files(ffprobe, config, project_root)
    bgm = None
    if config["paths"].get("bgm"):
        bgm = project_path(project_root, config["paths"]["bgm"], must_exist=True)

    replay_audio: Path | None = None
    replay_proof: dict[str, Any] | None = None
    if args.reuse_tts:
        replay_audio, replay_proof = validate_replay_audio(
            args.reuse_tts,
            getattr(args, "reuse_tts_provenance", None),
            config,
            project_root,
        )
        tts_profile = {
            "mode": "replay",
            "audio_sha256": sha256_file(replay_audio),
            "proof_sha256": sha256_file(args.reuse_tts_provenance.resolve()),
        }
    else:
        env = load_env_file(args.env_file)
        missing = [name for name in REQUIRED_VOLC_ENV if not env.get(name)]
        if missing:
            raise WorkflowError("TTS_CONFIG", "Missing Volcengine settings: " + ", ".join(missing))
        tts_profile = {"mode": "live"} | cache_provenance(
            narration_text(config), env
        )
    implementation = implementation_fingerprint()
    semantic = {
        "config": config,
        "narration_sha256": narration_hash(config),
        "approval_sha256": sha256_file(
            managed_path(
                project_root,
                "review/approval.json",
                must_exist=True,
                kind="file",
            )
        ),
        "base_sha256": sha256_file(base),
        "bgm_sha256": sha256_file(bgm) if bgm else None,
        "covers": [item["final_sha256"] for item in cover_records],
        "font_sha256": sha256_file(FONT_PATH),
        "ffmpeg_sha256": sha256_file(ffmpeg),
        "ffprobe_sha256": sha256_file(ffprobe),
        "aligner_model": MODEL_ID,
        "aligner_revision": MODEL_REVISION,
        "aligner_runtime": aligner_runtime,
        "implementation": implementation,
        "tts_profile": tts_profile,
    }
    build_key = sha256_bytes(canonical_json(semantic))[:20]
    control = ensure_managed_dir(project_root, ".source-led-ai-video")
    staging_root = ensure_managed_dir(project_root, control / "staging")
    review_root = ensure_managed_dir(project_root, "review-runs")
    lock_path = control / "build.lock"
    with open_managed_lock(project_root, lock_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        published_review = managed_path(
            project_root, review_root / build_key, kind="dir"
        )
        if published_review.exists():
            assert_managed_tree(project_root, published_review)
            integrity = verify_build_integrity(config_path, published_review)
            visual_path = managed_path(
                project_root, published_review / "qa/visual-review.json", kind="file"
            )
            status = "reused_needs_visual_review"
            if visual_path.is_file():
                visual = verify_visual_record(published_review, integrity["manifest"])
                status = "reused_visual_pass" if visual.get("result") == "pass" else "reused_visual_fail"
            return {"status": status, "build_key": build_key, "review_run": str(published_review)}

        stage = managed_path(
            project_root, staging_root / f"{build_key}.tmp", kind="dir"
        )
        if stage.exists():
            remove_managed_tree(project_root, stage)
        stage = ensure_managed_dir(project_root, stage)
        try:
            raw_audio = managed_path(
                project_root, stage / "narration-raw.mp3", kind="file"
            )
            if replay_audio:
                managed_copy_file(project_root, replay_audio, raw_audio)
                tts_result = {"cache": "replay", "cache_key": sha256_file(raw_audio)}
            else:
                tts_result = synthesize(config_path, raw_audio, env, runtime_root())
            raw_audio = managed_path(
                project_root, raw_audio, must_exist=True, kind="file"
            )
            raw_info = probe(ffprobe, raw_audio)
            if not stream(raw_info, "audio") or stream(raw_info, "video") or media_duration(raw_info) <= 1:
                raise WorkflowError("TTS_AUDIO", "TTS output is not a valid audio-only file")

            master = managed_path(
                project_root, stage / "narration-master.wav", kind="file"
            )
            run_checked(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    raw_audio,
                    "-af",
                    "acompressor=threshold=0.1:ratio=3:attack=5:release=100:makeup=2,loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s24le",
                    master,
                ],
                "VOICE_MASTER",
                "Could not create narration master",
            )
            master = managed_path(
                project_root, master, must_exist=True, kind="file"
            )
            audio_duration = media_duration(probe(ffprobe, master))

            align_result = run_process(
                [
                    aligner,
                    Path(__file__).with_name("align_captions.py"),
                    "--config",
                    config_path,
                    "--audio",
                    master,
                    "--out-dir",
                    stage,
                ]
            )
            if align_result.returncode != 0:
                raise WorkflowError("ALIGN_FAILED", "Forced alignment failed")
            assert_managed_tree(project_root, stage)
            captions = read_json(
                managed_path(
                    project_root,
                    stage / "captions-all.json",
                    must_exist=True,
                    kind="file",
                )
            )
            timeline = target_timeline(captions, config, audio_duration)
            build_ass(stage, captions, config, project_root)
            create_video_filter(stage, timeline, project_root)

            mixed = managed_path(
                project_root, stage / "audio-mix.wav", kind="file"
            )
            target_duration = timeline["duration_seconds"]
            if bgm:
                fade_out = max(
                    0.0,
                    target_duration
                    - float(
                        config.get("audio", {}).get(
                            "bgm_fade_out_seconds", BGM_FADE_OUT_DEFAULT_SECONDS
                        )
                    ),
                )
                mix_filter = (
                    f"[0:a]apad,atrim=0:{target_duration:.6f},pan=stereo|c0=c0|c1=c0[voice];"
                    f"[1:a]atrim=0:{target_duration:.6f},loudnorm=I=-32:TP=-9:LRA=11,"
                    f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.6f}:d={target_duration - fade_out:.6f}[bgm];"
                    f"[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                    f"loudnorm=I={FIXED_FINAL_LUFS}:TP=-1.5:LRA=11[mix]"
                )
                mix_args: list[str | Path] = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    master,
                    "-stream_loop",
                    "-1",
                    "-i",
                    bgm,
                    "-filter_complex",
                    mix_filter,
                    "-map",
                    "[mix]",
                ]
            else:
                mix_filter = (
                    f"apad,atrim=0:{target_duration:.6f},pan=stereo|c0=c0|c1=c0,"
                    f"loudnorm=I={FIXED_FINAL_LUFS}:TP=-1.5:LRA=11"
                )
                mix_args = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    master,
                    "-af",
                    mix_filter,
                ]
            mix_args += ["-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s24le", mixed]
            run_checked(mix_args, "AUDIO_MIX", "Could not mix narration and BGM")
            mixed = managed_path(
                project_root, mixed, must_exist=True, kind="file"
            )

            final = managed_path(project_root, stage / "video.mp4", kind="file")
            run_checked(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    base,
                    "-i",
                    mixed,
                    "-filter_complex_script",
                    "video-filter.txt",
                    "-map",
                    "[vout]",
                    "-map",
                    "1:a:0",
                    "-frames:v",
                    str(timeline["total_frames"]),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-profile:v",
                    "high",
                    "-level",
                    "4.1",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "2",
                    "-map_metadata",
                    "-1",
                    "-movflags",
                    "+faststart",
                    final,
                ],
                "VIDEO_RENDER",
                "Could not render final review video",
                cwd=stage,
            )
            final = managed_path(
                project_root, final, must_exist=True, kind="file"
            )

            covers_dir = ensure_managed_dir(project_root, stage / "covers")
            for item in cover_records:
                destination = covers_dir / f"cover-{item['ratio']}.png"
                managed_copy_file(project_root, item["final"], destination)
                if sha256_file(destination) != item["final_sha256"]:
                    raise WorkflowError("COVER_COPY", f"{item['ratio']} cover copy changed")
            assert_managed_tree(project_root, stage)
            provenance = {
                "schema": 1,
                "build_key": build_key,
                "project_name": config["project_name"],
                "config_sha256": config_sha256(config),
                "narration_sha256": narration_hash(config),
                "approval": approval,
                "inputs": {
                    "base_sha256": semantic["base_sha256"],
                    "bgm_sha256": semantic["bgm_sha256"],
                },
                "runtime": {
                    "ffmpeg_sha256": semantic["ffmpeg_sha256"],
                    "ffprobe_sha256": semantic["ffprobe_sha256"],
                    "font_sha256": semantic["font_sha256"],
                    "aligner_model": MODEL_ID,
                    "aligner_revision": MODEL_REVISION,
                    "aligner_runtime": aligner_runtime,
                    "implementation": implementation,
                },
                "tts": tts_profile | {"cache": tts_result.get("cache"), "cache_key": tts_result.get("cache_key")},
                "timeline": timeline,
                "artifacts": {
                    "raw_audio_sha256": sha256_file(raw_audio),
                    "narration_master_sha256": sha256_file(master),
                    "audio_mix_sha256": sha256_file(mixed),
                    "captions_all_sha256": sha256_file(stage / "captions-all.json"),
                    "captions_platform_sha256": sha256_file(stage / "captions-platform.srt"),
                    "video_sha256": sha256_file(final),
                },
            }
            managed_atomic_write_json(project_root, stage / "provenance.json", provenance)
            auto_qa(
                ffmpeg,
                ffprobe,
                stage,
                config,
                timeline,
                cover_records,
                project_root,
            )
            copy_review_evidence(stage, config, project_root, approval)
            if read_json(
                managed_path(
                    project_root,
                    stage / "review/approval.json",
                    must_exist=True,
                    kind="file",
                )
            ) != approval:
                raise WorkflowError("REVIEW_STALE", "Review approval changed during the build")
            write_build_manifest(stage, config, build_key, implementation)
            replace_managed_dir(project_root, stage, published_review)
            return {"status": "needs_visual_review", "build_key": build_key, "review_run": str(published_review)}
        except Exception as original:
            if os.path.lexists(stage):
                try:
                    remove_managed_tree(project_root, stage)
                except WorkflowError as cleanup_error:
                    raise cleanup_error from original
            raise


VISUAL_CHECKS = (
    "no_pixelated_upscale",
    "no_caption_ui_overlap",
    "no_caption_clipping",
    "info_card_readable",
    "no_visible_url_or_external_ui",
    "covers_readable_and_hooked",
    "voice_and_bgm_acceptable",
)


def confirm_visual(
    config_path: Path,
    run_dir: Path,
    reviewer: str,
    notes: str,
    result: str,
    failed_checks: list[str],
) -> dict[str, Any]:
    _, project_root = load_and_validate_config(config_path)
    run_dir = managed_path(
        project_root,
        Path(os.path.abspath(os.fspath(run_dir))),
        must_exist=True,
        kind="dir",
    )
    integrity = verify_build_integrity(config_path.resolve(), run_dir)
    if type(reviewer) is not str:
        raise WorkflowError("VISUAL_REVIEWER", "Visual reviewer must be text")
    if type(notes) is not str:
        raise WorkflowError("VISUAL_NOTES", "Visual review notes must be text")
    if type(result) is not str or result not in {"pass", "fail"}:
        raise WorkflowError("VISUAL_RESULT", "Visual review result must be pass or fail")
    if type(failed_checks) is not list or any(
        type(name) is not str for name in failed_checks
    ):
        raise WorkflowError("VISUAL_CHECK", "Failed checks must be a list of check names")
    if len(failed_checks) != len(set(failed_checks)):
        raise WorkflowError("VISUAL_CHECK", "Failed checks cannot contain duplicates")
    reviewer_clean = normalize_text(reviewer)
    notes_clean = normalize_text(notes)
    if not reviewer_clean:
        raise WorkflowError("VISUAL_REVIEWER", "Visual reviewer is required")
    if len(notes_clean) < 12:
        raise WorkflowError("VISUAL_NOTES", "Visual review notes must be specific")
    unknown = sorted(set(failed_checks) - set(VISUAL_CHECKS))
    if unknown:
        raise WorkflowError("VISUAL_CHECK", "Unknown visual check: " + ", ".join(unknown))
    if result == "pass" and failed_checks:
        raise WorkflowError("VISUAL_RESULT", "A passing review cannot contain failed checks")
    if result == "fail" and not failed_checks:
        raise WorkflowError("VISUAL_RESULT", "A failed review must name at least one failed check")
    report = {
        "schema": 2,
        "result": result,
        "reviewer": reviewer_clean,
        "notes": notes_clean,
        "failed_checks": sorted(set(failed_checks)),
        "checks": {name: name not in failed_checks for name in VISUAL_CHECKS},
        "build_manifest_sha256": sha256_file(run_dir / "build-manifest.json"),
        "video_sha256": integrity["manifest"]["artifacts"]["video.mp4"],
        "contact_sheet_sha256": integrity["manifest"]["artifacts"]["qa/contact-sheet.jpg"],
        "cover_3x4_sha256": integrity["manifest"]["artifacts"]["covers/cover-3x4.png"],
        "cover_4x3_sha256": integrity["manifest"]["artifacts"]["covers/cover-4x3.png"],
    }
    managed_atomic_write_json(
        project_root, run_dir / "qa/visual-review.json", report
    )
    return report


def finalize(config_path: Path, run_dir: Path) -> dict[str, Any]:
    config, project_root = load_and_validate_config(config_path)
    run_lexical = managed_path(
        project_root,
        Path(os.path.abspath(os.fspath(run_dir))),
        must_exist=True,
        kind="dir",
    )
    expected_root = project_root.resolve() / "review-runs"
    try:
        run_lexical.relative_to(expected_root)
    except ValueError as exc:
        raise WorkflowError("FINALIZE_PATH", "Review run must be inside project/review-runs") from exc
    run_dir = run_lexical
    assert_managed_tree(project_root, run_dir)
    integrity = verify_build_integrity(config_path.resolve(), run_dir)
    require_confirmed_rights(integrity["approval"])
    visual = verify_visual_record(run_dir, integrity["manifest"])
    if visual.get("result") != "pass" or not all(visual.get("checks", {}).values()):
        raise WorkflowError("FINALIZE_QA", "Automatic and visual QA must both pass")
    deliverables = ensure_managed_dir(project_root, "deliverables")
    final_dir = managed_path(
        project_root, deliverables / run_dir.name, kind="dir"
    )
    if final_dir.exists():
        raise WorkflowError("FINALIZE_EXISTS", "Deliverable already exists")
    artifacts = {
        path.relative_to(run_dir).as_posix(): sha256_file(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "bundle-manifest.json"
    }
    bundle_manifest = {
        "schema": 1,
        "kind": "bundle",
        "build_key": run_dir.name,
        "build_manifest_sha256": sha256_file(run_dir / "build-manifest.json"),
        "artifacts": artifacts,
    }
    managed_atomic_write_json(
        project_root, run_dir / "bundle-manifest.json", bundle_manifest
    )
    replace_managed_dir(project_root, run_dir, final_dir)
    latest = {
        "schema": 1,
        "build_key": final_dir.name,
        "bundle": str(final_dir.relative_to(project_root)),
        "video": str((final_dir / "video.mp4").relative_to(project_root)),
    }
    managed_atomic_write_json(project_root, deliverables / "latest.json", latest)
    return latest


def verify_bundle(ffmpeg: Path, ffprobe: Path, bundle: Path) -> dict[str, Any]:
    bundle = Path(os.path.abspath(os.fspath(bundle)))
    bundle, _ = _checked_tree(bundle)
    bundle_manifest = verify_hash_manifest(bundle, "bundle-manifest.json")
    if bundle_manifest.get("kind") != "bundle" or bundle_manifest.get("build_key") != bundle.name:
        raise WorkflowError("BUNDLE_KEY", "Bundle manifest does not match its directory")
    build_manifest = verify_hash_manifest(
        bundle,
        "build-manifest.json",
        allowed_unlisted=("qa/visual-review.json", "bundle-manifest.json"),
    )
    if (
        build_manifest.get("kind") != "build"
        or bundle_manifest.get("build_manifest_sha256")
        != sha256_file(bundle / "build-manifest.json")
    ):
        raise WorkflowError("BUNDLE_MANIFEST", "Build manifest binding is invalid")
    for relative in (*REQUIRED_BUILD_ARTIFACTS, "build-manifest.json", "qa/visual-review.json"):
        if relative not in bundle_manifest["artifacts"]:
            raise WorkflowError("BUNDLE_ARTIFACT", f"Bundle lacks required artifact: {relative}")
    qa = read_json(bundle / "qa/auto-qa.json")
    visual = verify_visual_record(bundle, build_manifest)
    video = bundle / "video.mp4"
    failures: list[str] = []
    if not qa.get("passed"):
        failures.append("automatic QA is not passed")
    if visual.get("result") != "pass" or not all(visual.get("checks", {}).values()):
        failures.append("visual/audio review is not passed")
    if qa.get("video_sha256") != sha256_file(video):
        failures.append("video hash changed after automatic QA")
    try:
        packaged_approval = verify_packaged_approval(bundle / "project.json")
        require_confirmed_rights(packaged_approval)
    except WorkflowError as exc:
        failures.append(f"packaged review approval is invalid: {exc.code}")
    decode = run_process([ffmpeg, "-v", "error", "-i", video, "-f", "null", "-"])
    if decode.returncode != 0:
        failures.append("video no longer decodes")
    info = probe(ffprobe, video)
    video_stream = stream(info, "video")
    audio_stream = stream(info, "audio")
    if len(info.get("streams", [])) != 2 or not video_stream or not audio_stream:
        failures.append("bundle must have exactly one video and one audio stream")
    elif (
        video_stream.get("codec_name"),
        video_stream.get("width"),
        video_stream.get("height"),
        video_stream.get("pix_fmt"),
        video_stream.get("r_frame_rate"),
    ) != ("h264", 1080, 1920, "yuv420p", "30/1"):
        failures.append("video stream contract changed")
    elif (
        audio_stream.get("codec_name"),
        audio_stream.get("sample_rate"),
        audio_stream.get("channels"),
    ) != ("aac", "48000", 2):
        failures.append("audio stream contract changed")
    loudness, true_peak = measure_loudness(ffmpeg, video)
    if abs(loudness - float(qa.get("loudness_lufs", 999))) > 0.2:
        failures.append("decoded loudness differs from automatic QA")
    if abs(true_peak - float(qa.get("true_peak_dBFS", 999))) > 0.2:
        failures.append("decoded true peak differs from automatic QA")
    return {"passed": not failures, "failures": failures, "probe": info}


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--env-file", type=Path)
    run.add_argument("--reuse-tts", type=Path)
    run.add_argument("--reuse-tts-provenance", type=Path)
    run.add_argument("--ffmpeg")
    run.add_argument("--ffprobe")
    run.add_argument("--aligner-python")
    visual = sub.add_parser("confirm-visual")
    visual.add_argument("--config", type=Path, required=True)
    visual.add_argument("--run-dir", type=Path, required=True)
    visual.add_argument("--reviewer", required=True)
    visual.add_argument("--notes", required=True)
    visual.add_argument("--result", choices=("pass", "fail"), required=True)
    visual.add_argument("--failed-check", action="append", default=[])
    finish = sub.add_parser("finalize")
    finish.add_argument("--config", type=Path, required=True)
    finish.add_argument("--run-dir", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--ffmpeg")
    verify.add_argument("--ffprobe")
    args = parser.parse_args()
    if args.command == "run":
        result = run_build(args)
    elif args.command == "confirm-visual":
        result = confirm_visual(
            args.config,
            args.run_dir,
            args.reviewer,
            args.notes,
            args.result,
            args.failed_check,
        )
    elif args.command == "finalize":
        result = finalize(args.config.resolve(), args.run_dir)
    else:
        ffmpeg, ffprobe = resolve_media_tools(args.ffmpeg, args.ffprobe)
        result = verify_bundle(ffmpeg, ffprobe, args.bundle)
        if not result["passed"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

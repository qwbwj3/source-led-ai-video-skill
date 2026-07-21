#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.common import (
    WorkflowError,
    atomic_write_json,
    canonical_json,
    normalize_text,
    project_output_path,
    project_path,
    read_json,
    sha256_bytes,
    sha256_file,
    spoken_glyphs,
)
from source_package import validate_source_package


EDIT_PLAN_VERSION = 1
TIMELINE_LOCK_VERSION = 1
COVER_PROMPT_VERSION = 1
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
TRACK_PATTERN = re.compile(r"V[1-9][0-9]*")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise WorkflowError("EDIT_PLAN_TYPE", f"{label} must be an object")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise WorkflowError("EDIT_PLAN_TYPE", f"{label} must be a string")
    if not allow_empty and not normalize_text(value):
        raise WorkflowError("EDIT_PLAN_TYPE", f"{label} cannot be empty")
    return value


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise WorkflowError("COVER_PROMPT_TIME", f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise WorkflowError("COVER_PROMPT_TIME", f"{label} must include a timezone")
    return text


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise WorkflowError("EDIT_PLAN_HASH", f"{label} must be lowercase SHA-256")
    return text


def _project_contract(config_path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = config_path.expanduser().resolve()
    config = read_json(resolved)
    if type(config) is not dict or config.get("version") != 2:
        raise WorkflowError("EDIT_PLAN_CONFIG", "Edit planning requires project version 2")
    canvas = _mapping(config.get("canvas"), "canvas")
    if canvas.get("fps") != 30:
        raise WorkflowError("EDIT_PLAN_CONFIG", "Project fps must be 30")
    narration = _mapping(config.get("narration"), "narration")
    scenes = narration.get("scenes")
    if type(scenes) is not list or not scenes:
        raise WorkflowError("EDIT_PLAN_CONFIG", "Project narration scenes cannot be empty")
    _mapping(config.get("edit"), "edit")
    _mapping(config.get("cover"), "cover")
    return resolved.parent, config


def _configured_output(
    project_root: Path, config: dict[str, Any], section: str, field: str
) -> Path:
    parent = _mapping(config.get(section), section)
    raw = _text(parent.get(field), f"{section}.{field}")
    expected = {
        ("edit", "plan"): "edit/edit-plan.json",
        ("edit", "timeline_lock"): "edit/timeline.lock.json",
    }.get((section, field))
    if expected is None or raw != expected:
        raise WorkflowError(
            "EDIT_PLAN_PATH", f"{section}.{field} must be {expected or 'fixed'}"
        )
    return project_output_path(project_root, project_root / raw)


def _asset_index(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["asset_id"]: item for item in package["assets"]["items"]}


def _validate_plan_payload(
    project_root: Path, config: dict[str, Any], plan: Any, package: dict[str, Any]
) -> dict[str, Any]:
    value = _mapping(plan, "edit plan")
    if type(value.get("version")) is not int or value["version"] != EDIT_PLAN_VERSION:
        raise WorkflowError(
            "EDIT_PLAN_VERSION", f"edit plan version must be {EDIT_PLAN_VERSION}"
        )
    if value.get("project_version") != 2 or value.get("fps") != 30:
        raise WorkflowError("EDIT_PLAN_VERSION", "edit plan must target project v2 at 30 fps")
    planned_scenes = value.get("scenes")
    project_scenes = config["narration"]["scenes"]
    if type(planned_scenes) is not list or len(planned_scenes) != len(project_scenes):
        raise WorkflowError("EDIT_PLAN_SCENE", "edit plan must contain every project scene")
    assets = _asset_index(package)
    for index, (raw_scene, project_scene) in enumerate(zip(planned_scenes, project_scenes)):
        scene = _mapping(raw_scene, f"scenes[{index}]")
        if scene.get("scene_id") != project_scene.get("id"):
            raise WorkflowError("EDIT_PLAN_SCENE", "edit plan scene order must match project.json")
        if normalize_text(_text(scene.get("purpose"), f"scenes[{index}].purpose")) != normalize_text(
            project_scene.get("purpose", "")
        ):
            raise WorkflowError("EDIT_PLAN_SCENE", f"Purpose drift in {scene['scene_id']}")
        if scene.get("caption_region") != project_scene.get("caption_region"):
            raise WorkflowError("EDIT_PLAN_SCENE", f"Caption region drift in {scene['scene_id']}")
        shots = scene.get("shots")
        if type(shots) is not list or not shots:
            raise WorkflowError("EDIT_PLAN_SHOT", f"{scene['scene_id']} must contain shots")
        allowed_assets = set(project_scene.get("asset_ids", []))
        used_assets: set[str] = set()
        for shot_index, raw_shot in enumerate(shots):
            shot = _mapping(raw_shot, f"{scene['scene_id']}.shots[{shot_index}]")
            asset_id = _text(shot.get("asset_id"), "shot.asset_id")
            if asset_id not in allowed_assets or asset_id not in assets:
                raise WorkflowError(
                    "EDIT_PLAN_ASSET", f"{scene['scene_id']} references undeclared asset {asset_id}"
                )
            asset = assets[asset_id]
            used_assets.add(asset_id)
            if asset.get("kind") != "video":
                raise WorkflowError("EDIT_PLAN_ASSET", f"Edit shot asset must be video: {asset_id}")
            source_in = shot.get("source_in_frame")
            source_out = shot.get("source_out_frame")
            if (
                type(source_in) is not int
                or type(source_out) is not int
                or source_in < 0
                or source_out <= source_in
                or source_out > asset["frame_count"]
            ):
                raise WorkflowError("EDIT_PLAN_FRAME", f"Invalid source frame range for {asset_id}")
            weight = shot.get("timeline_weight", 1)
            if type(weight) is not int or weight <= 0:
                raise WorkflowError("EDIT_PLAN_FRAME", "timeline_weight must be a positive integer")
            track_id = shot.get("track_id", "V1")
            if type(track_id) is not str or TRACK_PATTERN.fullmatch(track_id) is None:
                raise WorkflowError("EDIT_PLAN_TRACK", "track_id must look like V1")
            if shot.get("crop_mode", "fit") not in {"fit", "fill"}:
                raise WorkflowError("EDIT_PLAN_SHOT", "crop_mode must be fit or fill")
            if type(shot.get("allow_scale_up", False)) is not bool:
                raise WorkflowError("EDIT_PLAN_SHOT", "allow_scale_up must be true or false")
        if used_assets != allowed_assets:
            missing = sorted(allowed_assets - used_assets)
            raise WorkflowError(
                "EDIT_PLAN_ASSET",
                f"{scene['scene_id']} does not use declared assets: {', '.join(missing)}",
            )
        source_boundaries = config["source_timeline"]["scene_boundaries_seconds"]
        expected_source_frames = round(
            (float(source_boundaries[index + 1]) - float(source_boundaries[index]))
            * 30
        )
        planned_source_frames = sum(
            int(shot["source_out_frame"]) - int(shot["source_in_frame"])
            for shot in shots
        )
        if planned_source_frames != expected_source_frames:
            raise WorkflowError(
                "EDIT_PLAN_FRAME",
                f"{scene['scene_id']} source frames do not match clean-base boundaries",
            )
        if "information_card" in scene:
            card = _mapping(scene["information_card"], "information_card")
            raw_path = _text(card.get("path"), "information_card.path")
            if Path(raw_path).suffix.lower() not in {".html", ".htm"}:
                raise WorkflowError("EDIT_PLAN_PATH", "information_card.path must be HTML")
            card_path = project_output_path(project_root, project_root / raw_path)
            if not card_path.is_file():
                raise WorkflowError("EDIT_PLAN_PATH", "information card file is missing")
            expected_hash = _sha256(card.get("sha256"), "information_card.sha256")
            if sha256_file(card_path) != expected_hash:
                raise WorkflowError("EDIT_PLAN_HASH", "information card hash mismatch")
    return value


def validate_edit_plan(config_path: Path, plan: Any | None = None) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    package = validate_source_package(config_path)
    if plan is None:
        plan_path = _configured_output(project_root, config, "edit", "plan")
        if not plan_path.is_file():
            raise WorkflowError("EDIT_PLAN_MISSING", "edit plan does not exist")
        plan = read_json(plan_path)
    return _validate_plan_payload(project_root, config, plan, package)


def write_edit_plan(config_path: Path, draft: Any) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    checked = validate_edit_plan(config_path, draft)
    path = _configured_output(project_root, config, "edit", "plan")
    atomic_write_json(path, checked)
    return checked


def _validate_durations(
    durations: Any, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    value = _mapping(durations, "scene durations")
    if set(value) != {"version", "fps", "alignment_sha256", "scenes"}:
        raise WorkflowError(
            "TIMELINE_DURATION",
            "scene durations must contain version, fps, alignment_sha256, and scenes",
        )
    if value.get("version") != 1 or value.get("fps") != 30:
        raise WorkflowError("TIMELINE_DURATION", "scene durations must use version 1 at 30 fps")
    _sha256(value.get("alignment_sha256"), "durations.alignment_sha256")
    raw_scenes = value.get("scenes")
    project_scenes = config["narration"]["scenes"]
    if type(raw_scenes) is not list or len(raw_scenes) != len(project_scenes):
        raise WorkflowError("TIMELINE_DURATION", "scene durations must cover every scene")
    result: dict[str, int] = {}
    for index, (raw_scene, project_scene) in enumerate(zip(raw_scenes, project_scenes)):
        scene = _mapping(raw_scene, f"durations.scenes[{index}]")
        if set(scene) != {"scene_id", "duration_frames"}:
            raise WorkflowError(
                "TIMELINE_DURATION", "scene duration entries have unknown fields"
            )
        if scene.get("scene_id") != project_scene.get("id"):
            raise WorkflowError("TIMELINE_DURATION", "scene duration order must match project.json")
        duration = scene.get("duration_frames")
        if type(duration) is not int or duration <= 0:
            raise WorkflowError("TIMELINE_DURATION", "duration_frames must be a positive integer")
        result[scene["scene_id"]] = duration
    return value, result


def _allocate_frames(total: int, weights: list[int]) -> list[int]:
    if total < len(weights):
        raise WorkflowError("TIMELINE_FRAME", "Scene is shorter than its shot count")
    remaining = total - len(weights)
    weight_sum = sum(weights)
    base = [1 + remaining * weight // weight_sum for weight in weights]
    missing = total - sum(base)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(remaining * weights[index] % weight_sum), index),
    )
    for index in order[:missing]:
        base[index] += 1
    return base


def compile_timeline(config_path: Path, durations: Any) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    package = validate_source_package(config_path)
    plan = validate_edit_plan(config_path)
    duration_payload, duration_by_scene = _validate_durations(durations, config)
    timeline_scenes: list[dict[str, Any]] = []
    cursor = 0
    for planned_scene in plan["scenes"]:
        duration = duration_by_scene[planned_scene["scene_id"]]
        weights = [shot.get("timeline_weight", 1) for shot in planned_scene["shots"]]
        allocations = _allocate_frames(duration, weights)
        scene_start = cursor
        clips: list[dict[str, Any]] = []
        for shot, allocated in zip(planned_scene["shots"], allocations):
            clip_start = cursor
            cursor += allocated
            source_duration = shot["source_out_frame"] - shot["source_in_frame"]
            retime_ratio = allocated / source_duration
            low, high = config["source_timeline"].get("retime_ratio_limits", [0.8, 1.2])
            if not float(low) <= retime_ratio <= float(high):
                raise WorkflowError(
                    "TIMELINE_RETIME",
                    f"Shot {shot['asset_id']} needs retime ratio {retime_ratio:.4f}",
                )
            clips.append(
                {
                    "asset_id": shot["asset_id"],
                    "source_in_frame": shot["source_in_frame"],
                    "source_out_frame": shot["source_out_frame"],
                    "timeline_start_frame": clip_start,
                    "timeline_end_frame": cursor,
                    "track_id": shot.get("track_id", "V1"),
                    "crop_mode": shot.get("crop_mode", "fit"),
                    "allow_scale_up": shot.get("allow_scale_up", False),
                    "retime_ratio": round(retime_ratio, 8),
                }
            )
        scene_item = {
            "scene_id": planned_scene["scene_id"],
            "timeline_start_frame": scene_start,
            "timeline_end_frame": cursor,
            "caption_region": planned_scene["caption_region"],
            "clips": clips,
        }
        if "information_card" in planned_scene:
            scene_item["information_card"] = planned_scene["information_card"]
        timeline_scenes.append(scene_item)
    lock = {
        "version": TIMELINE_LOCK_VERSION,
        "project_version": 2,
        "fps": 30,
        "edit_plan_sha256": sha256_bytes(canonical_json(plan)),
        "durations_sha256": sha256_bytes(canonical_json(duration_payload)),
        "alignment_sha256": duration_payload["alignment_sha256"],
        "base_sha256": sha256_file(
            project_path(project_root, config["paths"]["base_video"], must_exist=True)
        ),
        "source_package_sha256": package["manifest"]["package_sha256"],
        "total_frames": cursor,
        "scenes": timeline_scenes,
    }
    checked = _validate_timeline_payload(project_root, config, plan, package, lock)
    lock_path = _configured_output(project_root, config, "edit", "timeline_lock")
    atomic_write_json(lock_path, checked)
    return checked


def _validate_timeline_payload(
    project_root: Path,
    config: dict[str, Any],
    plan: dict[str, Any],
    package: dict[str, Any],
    lock: Any,
) -> dict[str, Any]:
    value = _mapping(lock, "timeline lock")
    if value.get("version") != TIMELINE_LOCK_VERSION or value.get("project_version") != 2:
        raise WorkflowError("TIMELINE_VERSION", "timeline lock must target project version 2")
    if value.get("fps") != 30:
        raise WorkflowError("TIMELINE_VERSION", "timeline lock fps must be 30")
    expected_plan_hash = sha256_bytes(canonical_json(plan))
    if _sha256(value.get("edit_plan_sha256"), "edit_plan_sha256") != expected_plan_hash:
        raise WorkflowError("TIMELINE_HASH", "timeline lock does not match edit plan")
    durations_sha256 = _sha256(value.get("durations_sha256"), "durations_sha256")
    alignment_sha256 = _sha256(value.get("alignment_sha256"), "alignment_sha256")
    expected_base_hash = sha256_file(
        project_path(project_root, config["paths"]["base_video"], must_exist=True)
    )
    if _sha256(value.get("base_sha256"), "base_sha256") != expected_base_hash:
        raise WorkflowError("TIMELINE_HASH", "timeline lock does not match clean base")
    if (
        _sha256(value.get("source_package_sha256"), "source_package_sha256")
        != package["manifest"]["package_sha256"]
    ):
        raise WorkflowError("TIMELINE_HASH", "timeline lock does not match Source Package")
    total_frames = value.get("total_frames")
    if type(total_frames) is not int or total_frames <= 0:
        raise WorkflowError("TIMELINE_FRAME", "total_frames must be a positive integer")
    scenes = value.get("scenes")
    if type(scenes) is not list or len(scenes) != len(plan["scenes"]):
        raise WorkflowError("TIMELINE_SCENE", "timeline lock must contain every edit-plan scene")
    cursor = 0
    for index, (raw_scene, planned_scene) in enumerate(zip(scenes, plan["scenes"])):
        scene = _mapping(raw_scene, f"timeline.scenes[{index}]")
        if scene.get("scene_id") != planned_scene["scene_id"]:
            raise WorkflowError("TIMELINE_SCENE", "timeline scene order must match edit plan")
        if scene.get("caption_region") != planned_scene["caption_region"]:
            raise WorkflowError("TIMELINE_SCENE", "timeline caption region drifted")
        start = scene.get("timeline_start_frame")
        end = scene.get("timeline_end_frame")
        if type(start) is not int or type(end) is not int or start != cursor or end <= start:
            raise WorkflowError("TIMELINE_FRAME", "timeline scenes must be contiguous integer frames")
        clips = scene.get("clips")
        if type(clips) is not list or len(clips) != len(planned_scene["shots"]):
            raise WorkflowError("TIMELINE_FRAME", "timeline clips must match planned shots")
        clip_cursor = start
        for clip_index, (raw_clip, shot) in enumerate(zip(clips, planned_scene["shots"])):
            clip = _mapping(raw_clip, f"timeline clips[{clip_index}]")
            if clip.get("asset_id") != shot["asset_id"]:
                raise WorkflowError("TIMELINE_ASSET", "timeline asset drifted from edit plan")
            if clip.get("source_in_frame") != shot["source_in_frame"] or clip.get(
                "source_out_frame"
            ) != shot["source_out_frame"]:
                raise WorkflowError("TIMELINE_FRAME", "timeline source range drifted")
            clip_start = clip.get("timeline_start_frame")
            clip_end = clip.get("timeline_end_frame")
            if (
                type(clip_start) is not int
                or type(clip_end) is not int
                or clip_start != clip_cursor
                or clip_end <= clip_start
                or clip_end > end
            ):
                raise WorkflowError("TIMELINE_FRAME", "timeline clips must be contiguous")
            if clip.get("track_id") != shot.get("track_id", "V1"):
                raise WorkflowError("TIMELINE_TRACK", "timeline track drifted")
            if clip.get("crop_mode") != shot.get("crop_mode", "fit") or clip.get(
                "allow_scale_up"
            ) != shot.get("allow_scale_up", False):
                raise WorkflowError("TIMELINE_ASSET", "timeline visual policy drifted")
            source_duration = clip["source_out_frame"] - clip["source_in_frame"]
            expected_ratio = (clip_end - clip_start) / source_duration
            ratio = clip.get("retime_ratio")
            if type(ratio) not in {int, float} or not math.isclose(
                float(ratio), expected_ratio, rel_tol=0.0, abs_tol=1e-8
            ):
                raise WorkflowError("TIMELINE_RETIME", "timeline retime ratio is invalid")
            low, high = config["source_timeline"].get("retime_ratio_limits", [0.8, 1.2])
            if not float(low) <= expected_ratio <= float(high):
                raise WorkflowError("TIMELINE_RETIME", "timeline retime ratio is out of range")
            clip_cursor = clip_end
        if clip_cursor != end:
            raise WorkflowError("TIMELINE_FRAME", "timeline clips do not fill their scene")
        cursor = end
    if cursor != total_frames:
        raise WorkflowError("TIMELINE_FRAME", "total_frames does not match scene ranges")
    reconstructed_durations = {
        "version": 1,
        "fps": 30,
        "alignment_sha256": alignment_sha256,
        "scenes": [
            {
                "scene_id": scene["scene_id"],
                "duration_frames": scene["timeline_end_frame"]
                - scene["timeline_start_frame"],
            }
            for scene in scenes
        ],
    }
    if sha256_bytes(canonical_json(reconstructed_durations)) != durations_sha256:
        raise WorkflowError("TIMELINE_HASH", "timeline duration binding is invalid")
    return value


def validate_timeline_lock(config_path: Path, lock: Any | None = None) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    package = validate_source_package(config_path)
    plan = validate_edit_plan(config_path)
    if lock is None:
        lock_path = _configured_output(project_root, config, "edit", "timeline_lock")
        if not lock_path.is_file():
            raise WorkflowError("TIMELINE_MISSING", "timeline lock does not exist")
        lock = read_json(lock_path)
    return _validate_timeline_payload(project_root, config, plan, package, lock)


def _cover_prompt_path(project_root: Path, config: dict[str, Any]) -> Path:
    cover = _mapping(config.get("cover"), "cover")
    raw = _text(cover.get("prompt_record"), "cover.prompt_record")
    if raw != "covers/cover-prompt.json":
        raise WorkflowError(
            "COVER_PROMPT_PATH",
            "cover.prompt_record must be covers/cover-prompt.json",
        )
    return project_output_path(project_root, project_root / raw)


def cover_prompt_template(config: dict[str, Any]) -> dict[str, Any]:
    cover = _mapping(config.get("cover"), "cover")
    generations: dict[str, Any] = {}
    for ratio, display_ratio in (("3x4", "3:4"), ("4x3", "4:3")):
        entry = _mapping(cover.get(ratio), f"cover.{ratio}")
        generations[ratio] = {
            "ratio": display_ratio,
            "status": "pending",
            "generation_version": 0,
            "prompt": "",
            "model": "",
            "generated_at": None,
            "result_id": None,
            "source_path": entry.get("generated_source"),
            "source_sha256": None,
            "retry_reason": None,
        }
    return {
        "version": COVER_PROMPT_VERSION,
        "project_version": 2,
        "hook": cover.get("hook"),
        "headline_lines": cover.get("headline_lines"),
        "cover_semantic_sha256": sha256_bytes(
            canonical_json(_cover_semantic(cover))
        ),
        "prompt_version": "1",
        "generations": generations,
    }


def _cover_semantic(cover: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": cover.get("hook"),
        "headline_lines": cover.get("headline_lines"),
        "allow_people": cover.get("allow_people", False),
        "visual_brief": cover.get("visual_brief", ""),
        "text_mode": cover.get("text_mode", "deterministic"),
        "ratios": {
            ratio: {
                "text_mode": cover[ratio].get(
                    "text_mode", cover.get("text_mode", "deterministic")
                ),
                "generated_source": cover[ratio].get("generated_source"),
                "final": cover[ratio].get("final"),
            }
            for ratio in ("3x4", "4x3")
        },
    }


def _cover_ratio_semantic(cover: dict[str, Any], ratio: str) -> dict[str, Any]:
    """Return the cover settings that can invalidate one ratio only."""
    entry = _mapping(cover.get(ratio), f"cover.{ratio}")
    return {
        "hook": cover.get("hook"),
        "headline_lines": cover.get("headline_lines"),
        "allow_people": cover.get("allow_people", False),
        "visual_brief": cover.get("visual_brief", ""),
        "text_mode": entry.get(
            "text_mode", cover.get("text_mode", "deterministic")
        ),
        "generated_source": entry.get("generated_source"),
        "final": entry.get("final"),
    }


def _cover_ratio_semantic_sha256(cover: dict[str, Any], ratio: str) -> str:
    return sha256_bytes(canonical_json(_cover_ratio_semantic(cover, ratio)))


def _pending_generation_v2(
    cover: dict[str, Any],
    ratio: str,
    *,
    semantic_revision: int,
    archived_revisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    display_ratio = "3:4" if ratio == "3x4" else "4:3"
    return {
        "ratio": display_ratio,
        "status": "pending",
        "generation_version": 0,
        "semantic_revision": semantic_revision,
        "semantic_sha256": _cover_ratio_semantic_sha256(cover, ratio),
        "prompt": "",
        "model": "",
        "generated_at": None,
        "attempt_id": None,
        "result_id": None,
        "source_path": cover[ratio]["generated_source"],
        "source_sha256": None,
        "retry_reason": None,
        "attempts": [],
        "archived_revisions": archived_revisions or [],
    }


def _legacy_attempt(entry: dict[str, Any], ratio: str) -> dict[str, Any] | None:
    status = entry.get("status")
    if status == "pending":
        return None
    if status not in {"completed", "failed"}:
        raise WorkflowError("COVER_PROMPT_STATUS", f"Invalid status for {ratio}")
    result_id = entry.get("result_id")
    clean_result_id = normalize_text(result_id) if type(result_id) is str else ""
    attempt_id = (
        f"provider-result:{clean_result_id}"
        if clean_result_id
        else f"local-attempt:{uuid.uuid4()}"
    )
    return {
        "generation_version": 1,
        "legacy_generation_version": entry.get("generation_version"),
        "attempt_id": attempt_id,
        "result_id": clean_result_id or None,
        "status": status,
        "prompt": entry.get("prompt"),
        "model": entry.get("model"),
        "generated_at": entry.get("generated_at"),
        "source_path": entry.get("source_path"),
        "source_sha256": entry.get("source_sha256"),
        "retry_reason": entry.get("retry_reason"),
    }


def _upgrade_legacy_generation(
    entry: dict[str, Any], cover: dict[str, Any], ratio: str
) -> dict[str, Any]:
    upgraded = _pending_generation_v2(
        cover, ratio, semantic_revision=1, archived_revisions=[]
    )
    attempt = _legacy_attempt(entry, ratio)
    if attempt is None:
        return upgraded
    upgraded["attempts"] = [attempt]
    for field in (
        "status",
        "prompt",
        "model",
        "generated_at",
        "attempt_id",
        "result_id",
        "source_sha256",
        "retry_reason",
    ):
        upgraded[field] = attempt[field]
    upgraded["generation_version"] = 1
    return upgraded


def _validate_attempt(
    attempt: Any,
    *,
    ratio: str,
    expected_version: int,
) -> dict[str, Any]:
    value = _mapping(attempt, f"{ratio}.attempts[{expected_version - 1}]")
    if value.get("generation_version") != expected_version:
        raise WorkflowError(
            "COVER_PROMPT_VERSION", f"{ratio} attempt versions must be contiguous"
        )
    status = value.get("status")
    if status not in {"completed", "failed"}:
        raise WorkflowError("COVER_PROMPT_STATUS", f"Invalid attempt status for {ratio}")
    _text(value.get("attempt_id"), f"{ratio}.attempt_id")
    result_id = value.get("result_id")
    if result_id is not None:
        _text(result_id, f"{ratio}.result_id")
    _text(value.get("prompt"), f"{ratio}.prompt")
    _text(value.get("model"), f"{ratio}.model")
    _timestamp(value.get("generated_at"), f"{ratio}.generated_at")
    _text(value.get("source_path"), f"{ratio}.source_path")
    if status == "completed":
        _sha256(value.get("source_sha256"), f"{ratio}.source_sha256")
        if value.get("retry_reason") is not None:
            raise WorkflowError(
                "COVER_PROMPT_STATUS", "Completed cover attempt cannot have retry_reason"
            )
    else:
        _text(value.get("retry_reason"), f"{ratio}.retry_reason")
        if value.get("source_sha256") is not None:
            raise WorkflowError(
                "COVER_PROMPT_STATUS", "Failed cover attempt cannot bind a source hash"
            )
    return value


def _validate_v2_generation(
    entry: Any,
    *,
    cover: dict[str, Any],
    ratio: str,
    display_ratio: str,
    source_path: Path,
) -> tuple[dict[str, Any], str | None, str | None, list[str]]:
    value = _mapping(entry, f"generations.{ratio}")
    if value.get("ratio") != display_ratio:
        raise WorkflowError("COVER_PROMPT_RATIO", f"Wrong ratio label for {ratio}")
    expected_source = cover[ratio]["generated_source"]
    if value.get("source_path") != expected_source:
        raise WorkflowError("COVER_PROMPT_STALE", f"Source path drift for {ratio}")
    semantic_revision = value.get("semantic_revision")
    if type(semantic_revision) is not int or semantic_revision < 1:
        raise WorkflowError(
            "COVER_PROMPT_VERSION", "semantic_revision must be a positive integer"
        )
    if (
        _sha256(value.get("semantic_sha256"), f"{ratio}.semantic_sha256")
        != _cover_ratio_semantic_sha256(cover, ratio)
    ):
        raise WorkflowError(
            "COVER_PROMPT_STALE", f"Cover prompt semantic drift for {ratio}"
        )
    attempts = value.get("attempts")
    if type(attempts) is not list:
        raise WorkflowError("COVER_PROMPT_TYPE", f"{ratio}.attempts must be a list")
    checked_attempts = [
        _validate_attempt(attempt, ratio=ratio, expected_version=index)
        for index, attempt in enumerate(attempts, start=1)
    ]
    if value.get("generation_version") != len(checked_attempts):
        raise WorkflowError(
            "COVER_PROMPT_VERSION", "generation_version must equal current attempt count"
        )
    archives = value.get("archived_revisions")
    if type(archives) is not list:
        raise WorkflowError(
            "COVER_PROMPT_TYPE", f"{ratio}.archived_revisions must be a list"
        )
    attempt_ids: list[str] = []
    previous_revision = 0
    for archive_index, raw_archive in enumerate(archives):
        archive = _mapping(
            raw_archive, f"{ratio}.archived_revisions[{archive_index}]"
        )
        revision = archive.get("semantic_revision")
        if (
            type(revision) is not int
            or revision <= previous_revision
            or revision >= semantic_revision
        ):
            raise WorkflowError(
                "COVER_PROMPT_VERSION", "Archived semantic revisions must increase"
            )
        previous_revision = revision
        _sha256(archive.get("semantic_sha256"), f"{ratio}.archive.semantic_sha256")
        _text(archive.get("source_path"), f"{ratio}.archive.source_path")
        archived_attempts = archive.get("attempts")
        if type(archived_attempts) is not list or not archived_attempts:
            raise WorkflowError(
                "COVER_PROMPT_TYPE", "Archived revisions must contain attempt metadata"
            )
        for attempt_index, archived_attempt in enumerate(archived_attempts, start=1):
            checked = _validate_attempt(
                archived_attempt, ratio=ratio, expected_version=attempt_index
            )
            attempt_ids.append(checked["attempt_id"])
    attempt_ids.extend(attempt["attempt_id"] for attempt in checked_attempts)
    if len(attempt_ids) != len(set(attempt_ids)):
        raise WorkflowError("COVER_PROMPT_ID", f"Duplicate attempt ID in {ratio}")

    status = value.get("status")
    if not checked_attempts:
        if status != "pending":
            raise WorkflowError(
                "COVER_PROMPT_STATUS", "A cover with no attempts must be pending"
            )
        pending_fields = {
            "prompt": "",
            "model": "",
            "generated_at": None,
            "attempt_id": None,
            "result_id": None,
            "source_sha256": None,
            "retry_reason": None,
        }
        if any(value.get(field) != expected for field, expected in pending_fields.items()):
            raise WorkflowError(
                "COVER_PROMPT_STATUS", "Pending cover metadata is inconsistent"
            )
        return value, None, None, attempt_ids

    latest = checked_attempts[-1]
    if status != latest["status"]:
        raise WorkflowError("COVER_PROMPT_STATUS", "Cover status differs from latest attempt")
    for field in (
        "prompt",
        "model",
        "generated_at",
        "attempt_id",
        "result_id",
        "source_sha256",
        "retry_reason",
    ):
        if value.get(field) != latest.get(field):
            raise WorkflowError(
                "COVER_PROMPT_STATUS", f"Current {ratio} metadata differs from latest attempt"
            )
    if latest["source_path"] != expected_source:
        raise WorkflowError("COVER_PROMPT_STALE", f"Attempt source path drift for {ratio}")
    if status == "completed":
        expected_hash = latest["source_sha256"]
        if not source_path.is_file() or sha256_file(source_path) != expected_hash:
            raise WorkflowError("COVER_PROMPT_HASH", f"Generated source mismatch for {ratio}")
        return value, latest["attempt_id"], expected_hash, attempt_ids
    return value, None, None, attempt_ids


def validate_cover_prompt(
    config_path: Path,
    record: Any | None = None,
    *,
    require_completed: bool = False,
) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    path = _cover_prompt_path(project_root, config)
    if record is None:
        if not path.is_file():
            raise WorkflowError("COVER_PROMPT_MISSING", "cover prompt record does not exist")
        record = read_json(path)
    value = _mapping(record, "cover prompt record")
    if value.get("version") != COVER_PROMPT_VERSION or value.get("project_version") != 2:
        raise WorkflowError("COVER_PROMPT_VERSION", "cover prompt record version is invalid")
    cover = config["cover"]
    if value.get("hook") != cover.get("hook") or value.get("headline_lines") != cover.get(
        "headline_lines"
    ):
        raise WorkflowError("COVER_PROMPT_STALE", "cover prompt record does not match project hook")
    expected_semantic_hash = sha256_bytes(canonical_json(_cover_semantic(cover)))
    if (
        _sha256(value.get("cover_semantic_sha256"), "cover_semantic_sha256")
        != expected_semantic_hash
    ):
        raise WorkflowError(
            "COVER_PROMPT_STALE", "cover prompt record does not match cover settings"
        )
    prompt_version = _text(value.get("prompt_version"), "prompt_version")
    if prompt_version not in {"1", "2"}:
        raise WorkflowError("COVER_PROMPT_VERSION", "Unsupported cover prompt version")
    generations = _mapping(value.get("generations"), "generations")
    if set(generations) != {"3x4", "4x3"}:
        raise WorkflowError("COVER_PROMPT_RATIO", "cover prompt record needs 3x4 and 4x3")
    completed_call_ids: list[str] = []
    completed_hashes: list[str] = []
    all_attempt_ids: list[str] = []
    for ratio, display_ratio in (("3x4", "3:4"), ("4x3", "4:3")):
        expected_source = cover[ratio]["generated_source"]
        source_path = project_output_path(project_root, project_root / expected_source)
        if prompt_version == "2":
            entry, completed_id, completed_hash, attempt_ids = _validate_v2_generation(
                generations[ratio],
                cover=cover,
                ratio=ratio,
                display_ratio=display_ratio,
                source_path=source_path,
            )
            all_attempt_ids.extend(attempt_ids)
            if completed_id is not None:
                completed_call_ids.append(completed_id)
                completed_hashes.append(completed_hash or "")
            if require_completed and entry["status"] != "completed":
                raise WorkflowError(
                    "COVER_PROMPT_INCOMPLETE",
                    f"Cover generation is not completed for {ratio}",
                )
            continue

        entry = _mapping(generations[ratio], f"generations.{ratio}")
        if entry.get("ratio") != display_ratio:
            raise WorkflowError("COVER_PROMPT_RATIO", f"Wrong ratio label for {ratio}")
        if entry.get("source_path") != expected_source:
            raise WorkflowError("COVER_PROMPT_STALE", f"Source path drift for {ratio}")
        status = entry.get("status")
        if status not in {"pending", "completed", "failed"}:
            raise WorkflowError("COVER_PROMPT_STATUS", f"Invalid status for {ratio}")
        generation_version = entry.get("generation_version")
        if type(generation_version) is not int or generation_version < 0:
            raise WorkflowError("COVER_PROMPT_VERSION", "generation_version must be non-negative")
        if status == "pending":
            if generation_version != 0:
                raise WorkflowError("COVER_PROMPT_VERSION", "Pending generation_version must be 0")
        else:
            if generation_version < 1:
                raise WorkflowError("COVER_PROMPT_VERSION", "Attempted generation_version must be positive")
            _text(entry.get("prompt"), f"{ratio}.prompt")
            _text(entry.get("model"), f"{ratio}.model")
            _timestamp(entry.get("generated_at"), f"{ratio}.generated_at")
            _text(entry.get("result_id"), f"{ratio}.result_id")
        if status == "completed":
            expected_hash = _sha256(entry.get("source_sha256"), f"{ratio}.source_sha256")
            if not source_path.is_file() or sha256_file(source_path) != expected_hash:
                raise WorkflowError("COVER_PROMPT_HASH", f"Generated source mismatch for {ratio}")
            completed_call_ids.append(entry["result_id"])
            completed_hashes.append(expected_hash)
        if status == "failed":
            _text(entry.get("retry_reason"), f"{ratio}.retry_reason")
        if require_completed and status != "completed":
            raise WorkflowError(
                "COVER_PROMPT_INCOMPLETE",
                f"Cover generation is not completed for {ratio}",
            )
    if prompt_version == "2" and len(all_attempt_ids) != len(set(all_attempt_ids)):
        raise WorkflowError("COVER_PROMPT_ID", "Cover attempt IDs must be globally unique")
    if require_completed and (
        len(set(completed_call_ids)) != 2 or len(set(completed_hashes)) != 2
    ):
        raise WorkflowError(
            "COVER_PROMPT_INDEPENDENCE",
            "3:4 and 4:3 covers require separate calls and distinct outputs",
        )
    return value


def _archive_current_generation(entry: dict[str, Any]) -> list[dict[str, Any]]:
    archives = json.loads(
        json.dumps(entry.get("archived_revisions", []), ensure_ascii=False)
    )
    attempts = entry.get("attempts", [])
    if attempts:
        archives.append(
            {
                "semantic_revision": entry["semantic_revision"],
                "semantic_sha256": entry["semantic_sha256"],
                "source_path": entry["source_path"],
                "attempts": json.loads(json.dumps(attempts, ensure_ascii=False)),
            }
        )
    return archives


def sync_cover_prompt(config_path: Path) -> dict[str, Any]:
    """Synchronize mutable cover settings without losing prior attempts.

    A semantic change resets only the affected ratio. Shared hook or visual
    settings intentionally affect both ratios. Version-1 records remain valid
    and migrate on their first sync.
    """
    project_root, config = _project_contract(config_path)
    path = _cover_prompt_path(project_root, config)
    if not path.is_file():
        return initialize_cover_prompt(config_path)
    raw = _mapping(read_json(path), "cover prompt record")
    if raw.get("version") != COVER_PROMPT_VERSION or raw.get("project_version") != 2:
        raise WorkflowError("COVER_PROMPT_VERSION", "cover prompt record version is invalid")
    generations = _mapping(raw.get("generations"), "generations")
    if set(generations) != {"3x4", "4x3"}:
        raise WorkflowError("COVER_PROMPT_RATIO", "cover prompt record needs 3x4 and 4x3")

    cover = config["cover"]
    expected_whole = sha256_bytes(canonical_json(_cover_semantic(cover)))
    same_legacy_semantic = (
        raw.get("prompt_version") == "1"
        and raw.get("hook") == cover.get("hook")
        and raw.get("headline_lines") == cover.get("headline_lines")
        and raw.get("cover_semantic_sha256") == expected_whole
    )
    if same_legacy_semantic:
        # Validate the old record while its exact semantics are still current.
        validate_cover_prompt(config_path, raw)

    updated = json.loads(json.dumps(raw, ensure_ascii=False))
    updated.update(
        {
            "hook": cover.get("hook"),
            "headline_lines": cover.get("headline_lines"),
            "cover_semantic_sha256": expected_whole,
            "prompt_version": "2",
        }
    )
    new_generations: dict[str, Any] = {}
    for ratio in ("3x4", "4x3"):
        raw_entry = _mapping(generations[ratio], f"generations.{ratio}")
        current_semantic = _cover_ratio_semantic_sha256(cover, ratio)
        if raw.get("prompt_version") == "2":
            old_revision = raw_entry.get("semantic_revision")
            if type(old_revision) is not int or old_revision < 1:
                raise WorkflowError(
                    "COVER_PROMPT_VERSION", "semantic_revision must be a positive integer"
                )
            _sha256(raw_entry.get("semantic_sha256"), f"{ratio}.semantic_sha256")
            if (
                raw_entry["semantic_sha256"] == current_semantic
                and raw_entry.get("source_path")
                == cover[ratio]["generated_source"]
            ):
                new_generations[ratio] = json.loads(
                    json.dumps(raw_entry, ensure_ascii=False)
                )
                continue
            archives = _archive_current_generation(raw_entry)
            new_generations[ratio] = _pending_generation_v2(
                cover,
                ratio,
                semantic_revision=old_revision + 1,
                archived_revisions=archives,
            )
            continue

        # Legacy records cannot identify which ratio caused a whole-cover
        # semantic mismatch. Preserve known attempt metadata, then reset safely.
        legacy = _upgrade_legacy_generation(raw_entry, cover, ratio)
        if same_legacy_semantic:
            new_generations[ratio] = legacy
            continue
        legacy_attempts = legacy["attempts"]
        archives: list[dict[str, Any]] = []
        if legacy_attempts:
            archives.append(
                {
                    "semantic_revision": 1,
                    "semantic_sha256": _sha256(
                        raw.get("cover_semantic_sha256"),
                        "legacy cover_semantic_sha256",
                    ),
                    "semantic_scope": "legacy-whole-cover",
                    "source_path": _text(
                        raw_entry.get("source_path"), f"{ratio}.source_path"
                    ),
                    "attempts": legacy_attempts,
                }
            )
        new_generations[ratio] = _pending_generation_v2(
            cover,
            ratio,
            semantic_revision=2 if archives else 1,
            archived_revisions=archives,
        )

    updated["generations"] = new_generations
    checked = validate_cover_prompt(config_path, updated)
    atomic_write_json(path, checked)
    return checked


def initialize_cover_prompt(config_path: Path) -> dict[str, Any]:
    project_root, config = _project_contract(config_path)
    path = _cover_prompt_path(project_root, config)
    if path.exists():
        raise WorkflowError("COVER_PROMPT_EXISTS", "cover prompt record already exists")
    record = cover_prompt_template(config)
    checked = validate_cover_prompt(config_path, record)
    atomic_write_json(path, checked)
    return checked


def record_cover_generation(
    config_path: Path,
    *,
    ratio: str,
    status: str,
    prompt: str,
    model: str | None,
    result_id: str | None,
    retry_reason: str | None = None,
) -> dict[str, Any]:
    if ratio not in {"3x4", "4x3"} or status not in {"completed", "failed"}:
        raise WorkflowError("COVER_PROMPT_STATUS", "Invalid cover generation update")
    project_root, config = _project_contract(config_path)
    path = _cover_prompt_path(project_root, config)
    current = validate_cover_prompt(config_path)
    if current.get("prompt_version") != "2":
        current = sync_cover_prompt(config_path)
    updated = json.loads(json.dumps(current, ensure_ascii=False))
    entry = updated["generations"][ratio]
    clean_prompt = normalize_text(prompt)
    if not clean_prompt:
        raise WorkflowError("COVER_PROMPT_TYPE", "Cover generation prompt is required")
    clean_model = normalize_text(model) if type(model) is str else ""
    if not clean_model:
        clean_model = "builtin-imagegen"
    clean_result_id = normalize_text(result_id) if type(result_id) is str else ""
    attempt_id = (
        f"provider-result:{clean_result_id}"
        if clean_result_id
        else f"local-attempt:{uuid.uuid4()}"
    )
    generation_version = len(entry["attempts"]) + 1
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    source_path = config["cover"][ratio]["generated_source"]
    source_sha256: str | None = None
    clean_retry = normalize_text(retry_reason) if retry_reason else None
    if status == "completed":
        source = project_output_path(
            project_root,
            project_root / source_path,
        )
        if not source.is_file():
            raise WorkflowError("COVER_PROMPT_HASH", "Generated cover source is missing")
        source_sha256 = sha256_file(source)
        clean_retry = None
    elif not clean_retry:
        raise WorkflowError("COVER_PROMPT_STATUS", "Failed generation needs a retry reason")
    attempt = {
        "generation_version": generation_version,
        "attempt_id": attempt_id,
        "result_id": clean_result_id or None,
        "status": status,
        "prompt": clean_prompt,
        "model": clean_model,
        "generated_at": generated_at,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "retry_reason": clean_retry,
    }
    entry["attempts"].append(attempt)
    entry.update(
        {
            "status": status,
            "generation_version": generation_version,
            "prompt": clean_prompt,
            "model": clean_model,
            "generated_at": generated_at,
            "attempt_id": attempt_id,
            "result_id": clean_result_id or None,
            "source_sha256": source_sha256,
            "retry_reason": clean_retry,
        }
    )
    checked = validate_cover_prompt(config_path, updated)
    atomic_write_json(path, checked)
    return checked


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Validate and compile V2 edit contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "validate-plan",
        "validate-lock",
        "init-cover",
        "sync-cover",
        "validate-cover",
    ):
        child = subparsers.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
    write_parser = subparsers.add_parser("write-plan")
    write_parser.add_argument("--config", type=Path, required=True)
    write_parser.add_argument("--draft", type=Path, required=True)
    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--config", type=Path, required=True)
    compile_parser.add_argument("--durations", type=Path, required=True)
    record_cover = subparsers.add_parser("record-cover")
    record_cover.add_argument("--config", type=Path, required=True)
    record_cover.add_argument("--ratio", choices=("3x4", "4x3"), required=True)
    record_cover.add_argument("--status", choices=("completed", "failed"), required=True)
    record_cover.add_argument("--prompt", required=True)
    record_cover.add_argument("--model")
    record_cover.add_argument("--result-id")
    record_cover.add_argument("--retry-reason")
    args = parser.parse_args()
    if args.command == "write-plan":
        result = write_edit_plan(args.config, read_json(args.draft))
    elif args.command == "validate-plan":
        result = validate_edit_plan(args.config)
    elif args.command == "compile":
        result = compile_timeline(args.config, read_json(args.durations))
    elif args.command == "validate-lock":
        result = validate_timeline_lock(args.config)
    elif args.command == "init-cover":
        result = initialize_cover_prompt(args.config)
    elif args.command == "sync-cover":
        result = sync_cover_prompt(args.config)
    elif args.command == "record-cover":
        result = record_cover_generation(
            args.config,
            ratio=args.ratio,
            status=args.status,
            prompt=args.prompt,
            model=args.model,
            result_id=args.result_id,
            retry_reason=args.retry_reason,
        )
    else:
        result = validate_cover_prompt(args.config, require_completed=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

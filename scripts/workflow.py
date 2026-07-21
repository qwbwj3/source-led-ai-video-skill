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
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
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
from lib.telemetry import (
    TelemetryError,
    append_event,
    identity_log_lock,
    next_attempt,
    read_events,
    stage_summary,
)
from edit_plan import (
    compile_timeline,
    validate_cover_prompt,
    validate_edit_plan,
    validate_timeline_lock,
)
from review_gate import (
    policy_fingerprint,
    require_confirmed_rights,
    verify_approval,
    verify_packaged_approval,
)
from source_package import validate_source_package


FPS = 30
SAMPLE_RATE = 48_000


RENDERER_IMPLEMENTATION_FILES = (
    "scripts/workflow.py",
    "scripts/align_captions.py",
    "scripts/lib/common.py",
    "scripts/lib/ass_text.py",
    "scripts/lib/volc_tts.py",
    "scripts/lib/runtime_contract.py",
    "scripts/requirements-lock.txt",
)

RELEASE_IMPLEMENTATION_FILES = tuple(
    dict.fromkeys(
        (
            *RENDERER_IMPLEMENTATION_FILES,
            "scripts/review_gate.py",
            "scripts/precheck_scan.py",
            "scripts/source_package.py",
            "scripts/edit_plan.py",
        )
    )
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
    "qa/review-points.json",
    "covers/cover-3x4.png",
    "covers/cover-4x3.png",
    "project.json",
    "review/review-input.json",
    "review/narration.txt",
    "review/lexical-scan.json",
    "review/approval.json",
)

VIDEO_CACHE_ARTIFACTS = (
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
)

DYNAMIC_CONFIG_KEYS = {
    "analytics",
    "creator_feedback",
    "platform_metrics",
    "performance",
}


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


def implementation_fingerprint() -> dict[str, Any]:
    return {
        "schema": 2,
        "renderer": {
            relative: sha256_file(SKILL_ROOT / relative)
            for relative in RENDERER_IMPLEMENTATION_FILES
        },
        "release": {
            relative: sha256_file(SKILL_ROOT / relative)
            for relative in RELEASE_IMPLEMENTATION_FILES
        },
        "review_policy": policy_fingerprint(),
    }


def config_sha256(config: dict[str, Any]) -> str:
    stable = {
        key: value for key, value in config.items() if key not in DYNAMIC_CONFIG_KEYS
    }
    return sha256_bytes(canonical_json(stable))


def validate_v2_contracts(
    config_path: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate the evidence/edit/cover inputs that must exist before TTS."""
    if config.get("version") != 2:
        return None
    project_root = config_path.resolve().parent
    package = validate_source_package(config_path)
    plan = validate_edit_plan(config_path)
    prompt = validate_cover_prompt(config_path, require_completed=True)
    package_dir = project_path(
        project_root, config["source_package"]["manifest"], must_exist=True
    ).parent
    package_files = {
        "source-package/manifest.json": package_dir / "manifest.json",
        "source-package/claims.jsonl": package_dir / "claims.jsonl",
        "source-package/assets.json": package_dir / "assets.json",
        "source-package/toolchain.json": package_dir / "toolchain.json",
        "source-package/brief.md": package_dir / "brief.md",
    }
    for label, path in package_files.items():
        if not path.is_file():
            raise WorkflowError("SOURCE_PACKAGE_MISSING", f"Missing {label}")
    plan_path = project_path(project_root, config["edit"]["plan"], must_exist=True)
    prompt_path = project_path(
        project_root, config["cover"]["prompt_record"], must_exist=True
    )
    asset_index = {
        item["asset_id"]: item for item in package["assets"]["items"]
    }
    used_asset_ids = sorted(
        {
            asset_id
            for scene in config["narration"]["scenes"]
            for asset_id in scene["asset_ids"]
        }
    )
    used_assets = {
        asset_id: asset_index[asset_id]["sha256"] for asset_id in used_asset_ids
    }
    source_assets_for_video = {
        "version": package["assets"]["version"],
        "items": [
            {key: value for key, value in item.items() if key != "rights_status"}
            for item in package["assets"]["items"]
        ],
    }
    manifest_for_video = dict(package["manifest"])
    manifest_for_video.pop("package_sha256", None)
    source_content_hash = sha256_bytes(
        canonical_json(
            {
                "manifest": manifest_for_video,
                "claims": package["claims"],
                "assets": source_assets_for_video,
                "toolchain": package["toolchain"],
                "brief": package["brief"],
            }
        )
    )
    information_cards: dict[str, str] = {}
    for scene in plan["scenes"]:
        card = scene.get("information_card")
        if type(card) is dict:
            information_cards[card["path"]] = card["sha256"]
    return {
        "package": package,
        "plan": plan,
        "prompt": prompt,
        "source_package": package["manifest"]["package_sha256"],
        "source_content": source_content_hash,
        "edit_plan": sha256_bytes(canonical_json(plan)),
        "cover_prompt": sha256_bytes(canonical_json(prompt)),
        "used_assets": used_assets,
        "package_files": package_files,
        "plan_path": plan_path,
        "prompt_path": prompt_path,
        "information_cards": information_cards,
    }


def compile_v2_timeline_lock(
    config_path: Path,
    contracts: dict[str, Any] | None,
    timeline: dict[str, Any],
    alignment_path: Path,
) -> dict[str, Any] | None:
    if contracts is None:
        return None
    target = timeline["target_boundaries_frames"]
    durations = {
        "version": 1,
        "fps": FPS,
        "alignment_sha256": sha256_file(alignment_path),
        "scenes": [
            {
                "scene_id": scene["scene_id"],
                "duration_frames": target[index + 1] - target[index],
            }
            for index, scene in enumerate(contracts["plan"]["scenes"])
        ],
    }
    lock = compile_timeline(config_path, durations)
    checked = validate_timeline_lock(config_path, lock)
    locked_boundaries = [
        checked["scenes"][0]["timeline_start_frame"],
        *[scene["timeline_end_frame"] for scene in checked["scenes"]],
    ]
    if locked_boundaries != target:
        raise WorkflowError(
            "TIMELINE_BINDING", "Timeline lock differs from the aligned render timeline"
        )
    if checked["alignment_sha256"] != sha256_file(alignment_path):
        raise WorkflowError(
            "TIMELINE_BINDING", "Timeline lock differs from forced alignment"
        )
    contracts["timeline_lock"] = sha256_bytes(canonical_json(checked))
    contracts["lock"] = checked
    return checked


def copy_v2_contracts(
    project_root: Path,
    stage: Path,
    config: dict[str, Any],
    contracts: dict[str, Any] | None,
) -> None:
    if contracts is None:
        return
    for relative, source in contracts["package_files"].items():
        managed_copy_file(project_root, source, stage / relative)
    managed_copy_file(
        project_root, contracts["plan_path"], stage / config["edit"]["plan"]
    )
    lock_path = project_path(
        project_root, config["edit"]["timeline_lock"], must_exist=True
    )
    managed_copy_file(
        project_root, lock_path, stage / config["edit"]["timeline_lock"]
    )
    managed_copy_file(
        project_root,
        contracts["prompt_path"],
        stage / config["cover"]["prompt_record"],
    )
    for relative, expected_hash in contracts["information_cards"].items():
        source = project_path(project_root, relative, must_exist=True)
        if sha256_file(source) != expected_hash:
            raise WorkflowError("EDIT_PLAN_HASH", "Information card changed during build")
        managed_copy_file(project_root, source, stage / relative)


def video_config_payload(config: dict[str, Any]) -> dict[str, Any]:
    """Return only configuration that can affect the rendered video.

    Covers and project analytics are deliberately excluded. Unknown future
    fields remain included by default so a schema extension cannot silently
    create an unsafe cache hit.
    """
    return {
        key: value
        for key, value in config.items()
        if key != "cover" and key not in DYNAMIC_CONFIG_KEYS
    }


def compute_build_keys(
    *,
    config: dict[str, Any],
    approval_sha256: str,
    base_sha256: str,
    bgm_sha256: str | None,
    cover_records: list[dict[str, Any]],
    implementation: dict[str, Any],
    tts_profile: dict[str, Any],
    ffmpeg_sha256: str,
    ffprobe_sha256: str,
    aligner_runtime: dict[str, Any],
    contract_hashes: dict[str, Any] | None = None,
) -> dict[str, str]:
    renderer_implementation = implementation.get("renderer", implementation)
    release_implementation = implementation
    contracts = contract_hashes or {}
    video_semantic = {
        "schema": 1,
        "config": video_config_payload(config),
        "narration_sha256": narration_hash(config),
        "base_sha256": base_sha256,
        "bgm_sha256": bgm_sha256,
        "font_sha256": sha256_file(FONT_PATH),
        "ffmpeg_sha256": ffmpeg_sha256,
        "ffprobe_sha256": ffprobe_sha256,
        "aligner_model": MODEL_ID,
        "aligner_revision": MODEL_REVISION,
        "aligner_runtime": aligner_runtime,
        "implementation": renderer_implementation,
        "contracts": {
            key: contracts[key]
            for key in ("source_content", "edit_plan")
            if key in contracts
        },
        "tts_profile": tts_profile,
    }
    video_key = sha256_bytes(canonical_json(video_semantic))
    cover_semantic = {
        "schema": 1,
        "cover": config["cover"],
        "prompt_record_sha256": contracts.get("cover_prompt"),
        "artifacts": [
            {
                "ratio": item["ratio"],
                "source_sha256": item["source_sha256"],
                "final_sha256": item["final_sha256"],
            }
            for item in cover_records
        ],
    }
    cover_key = sha256_bytes(canonical_json(cover_semantic))
    build_key = sha256_bytes(
        canonical_json(
            {
                "schema": 1,
                "video_key": video_key,
                "cover_key": cover_key,
                "approval_sha256": approval_sha256,
                "config_sha256": config_sha256(config),
                "release_implementation": release_implementation,
                "contracts": contracts,
            }
        )
    )[:20]
    return {
        "video_key": video_key,
        "cover_key": cover_key,
        "build_key": build_key,
    }


class BuildTelemetry:
    def __init__(self, project_root: Path, build_key: str) -> None:
        self.path = project_root / ".source-led-ai-video" / "events.jsonl"
        self.build_key = build_key
        self.attempts: dict[str, int] = {}
        self.active_stage: str | None = None

    def start(self, stage: str, inputs: dict[str, str] | None = None) -> int:
        events = read_events(self.path, repair_tail=True)
        attempt = next_attempt(events, stage, self.build_key)
        self.attempts[stage] = attempt
        self.active_stage = stage
        append_event(
            self.path,
            event_type="stage_started",
            stage=stage,
            attempt=attempt,
            build_key=self.build_key,
            input_hashes=inputs,
        )
        return attempt

    def finish(self, stage: str, outputs: dict[str, str] | None = None) -> None:
        append_event(
            self.path,
            event_type="stage_finished",
            stage=stage,
            attempt=self.attempts[stage],
            build_key=self.build_key,
            output_hashes=outputs,
        )
        if self.active_stage == stage:
            self.active_stage = None

    def fail(self, stage: str, error_code: str) -> None:
        append_event(
            self.path,
            event_type="stage_failed",
            stage=stage,
            attempt=self.attempts[stage],
            build_key=self.build_key,
            error_code=error_code,
        )
        if self.active_stage == stage:
            self.active_stage = None

    def cache_hit(
        self,
        stage: str,
        cache_key: str,
        outputs: dict[str, str] | None = None,
    ) -> None:
        append_event(
            self.path,
            event_type="cache_hit",
            stage=stage,
            attempt=self.attempts[stage],
            build_key=self.build_key,
            cache_key=cache_key,
            output_hashes=outputs,
        )


def artifact_hashes(root: Path, relatives: tuple[str, ...]) -> dict[str, str]:
    return {relative: sha256_file(root / relative) for relative in relatives}


def _validate_artifact_manifest(
    root: Path, manifest: dict[str, Any], expected_key: str
) -> dict[str, str] | None:
    if (
        type(manifest) is not dict
        or manifest.get("schema") != 1
        or manifest.get("cache_key") != expected_key
        or type(manifest.get("artifacts")) is not dict
        or set(manifest["artifacts"]) != set(VIDEO_CACHE_ARTIFACTS)
    ):
        return None
    for relative, expected in manifest["artifacts"].items():
        path = root / relative
        if (
            type(expected) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or not path.is_file()
            or sha256_file(path) != expected
        ):
            return None
    return manifest["artifacts"]


def restore_video_cache(
    project_root: Path, control: Path, video_key: str, stage: Path
) -> dict[str, str] | None:
    cache_dir = managed_path(
        project_root, control / "video-cache" / video_key, kind="dir"
    )
    if not cache_dir.is_dir():
        return None
    assert_managed_tree(project_root, cache_dir)
    manifest_path = managed_path(project_root, cache_dir / "manifest.json", kind="file")
    if not manifest_path.is_file():
        return None
    try:
        manifest = read_json(manifest_path)
    except WorkflowError:
        return None
    artifacts = _validate_artifact_manifest(cache_dir, manifest, video_key)
    if artifacts is None:
        return None
    for relative in VIDEO_CACHE_ARTIFACTS:
        managed_copy_file(project_root, cache_dir / relative, stage / relative)
    return artifacts


def publish_video_cache(
    project_root: Path, control: Path, video_key: str, stage: Path
) -> dict[str, str]:
    cache_root = ensure_managed_dir(project_root, control / "video-cache")
    destination = managed_path(project_root, cache_root / video_key, kind="dir")
    if destination.is_dir():
        try:
            manifest = read_json(destination / "manifest.json")
            existing = _validate_artifact_manifest(destination, manifest, video_key)
        except WorkflowError:
            existing = None
        if existing is not None:
            return existing
    temporary = managed_path(
        project_root, cache_root / f"{video_key}.tmp", kind="dir"
    )
    if temporary.exists():
        remove_managed_tree(project_root, temporary)
    temporary = ensure_managed_dir(project_root, temporary)
    for relative in VIDEO_CACHE_ARTIFACTS:
        source = managed_path(
            project_root, stage / relative, must_exist=True, kind="file"
        )
        managed_copy_file(project_root, source, temporary / relative)
    artifacts = artifact_hashes(temporary, VIDEO_CACHE_ARTIFACTS)
    managed_atomic_write_json(
        project_root,
        temporary / "manifest.json",
        {"schema": 1, "cache_key": video_key, "artifacts": artifacts},
    )
    if destination.exists():
        remove_managed_tree(project_root, destination)
    replace_managed_dir(project_root, temporary, destination)
    return artifacts


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


def required_build_artifacts(config: dict[str, Any]) -> tuple[str, ...]:
    required = list(REQUIRED_BUILD_ARTIFACTS)
    if config.get("version") == 2:
        required.extend(
            (
                "source-package/manifest.json",
                "source-package/claims.jsonl",
                "source-package/assets.json",
                "source-package/toolchain.json",
                "source-package/brief.md",
                config["edit"]["plan"],
                config["edit"]["timeline_lock"],
                config["cover"]["prompt_record"],
            )
        )
    return tuple(dict.fromkeys(required))


def write_build_manifest(
    stage: Path,
    config: dict[str, Any],
    build_key: str,
    implementation: dict[str, Any],
) -> dict[str, Any]:
    stage, project_root = _checked_tree(stage)
    for relative in required_build_artifacts(config):
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
    allowed_unlisted_prefixes: tuple[str, ...] = (),
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
    unexpected = sorted(
        item
        for item in actual - set(artifacts) - set(allowed_unlisted)
        if not any(item.startswith(prefix) for prefix in allowed_unlisted_prefixes)
    )
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
    for relative in required_build_artifacts(config):
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
    if config.get("version") == 2:
        contracts = validate_v2_contracts(config_path, config)
        if contracts is None:
            raise WorkflowError("CONTRACT_STALE", "V2 contracts are unavailable")
        lock = validate_timeline_lock(config_path)
        expected_contracts = {
            "source_package_sha256": contracts["source_package"],
            "edit_plan_sha256": contracts["edit_plan"],
            "timeline_lock_sha256": sha256_bytes(canonical_json(lock)),
            "cover_prompt_sha256": contracts["cover_prompt"],
        }
        if provenance.get("contracts") != expected_contracts:
            raise WorkflowError("CONTRACT_STALE", "Current V2 contracts differ from the run")
        if lock.get("alignment_sha256") != sha256_file(run_dir / "alignment.json"):
            raise WorkflowError("CONTRACT_STALE", "Timeline lock differs from run alignment")
        locked_boundaries = [
            lock["scenes"][0]["timeline_start_frame"],
            *[scene["timeline_end_frame"] for scene in lock["scenes"]],
        ]
        if locked_boundaries != provenance.get("timeline", {}).get(
            "target_boundaries_frames"
        ):
            raise WorkflowError("CONTRACT_STALE", "Timeline lock differs from run timing")
    return {
        "manifest": manifest,
        "auto": auto,
        "provenance": provenance,
        "approval": current_approval,
    }


def load_review_points(
    run_dir: Path, build_manifest: dict[str, Any]
) -> dict[str, Any]:
    path = run_dir / "qa/review-points.json"
    if not path.is_file():
        raise WorkflowError("VISUAL_EVIDENCE_REQUIRED", "Review points are missing")
    record = read_json(path)
    points = record.get("points") if type(record) is dict else None
    if (
        record.get("schema") != 1
        or record.get("video_sha256")
        != build_manifest.get("artifacts", {}).get("video.mp4")
        or type(points) is not list
        or not points
    ):
        raise WorkflowError("VISUAL_EVIDENCE_INVALID", "Review points are invalid")
    seen: set[str] = set()
    for point in points:
        if type(point) is not dict or set(point) != {
            "id",
            "label",
            "frame",
            "time_seconds",
            "path",
            "sha256",
        }:
            raise WorkflowError("VISUAL_EVIDENCE_INVALID", "Review point is malformed")
        point_id = point["id"]
        if (
            type(point_id) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", point_id) is None
            or point_id in seen
            or type(point["frame"]) is not int
            or point["frame"] < 0
            or type(point["time_seconds"]) not in {int, float}
            or not math.isclose(
                float(point["time_seconds"]), point["frame"] / FPS, abs_tol=1e-6
            )
            or type(point["label"]) is not str
            or not normalize_text(point["label"])
            or type(point["path"]) is not str
            or not point["path"].startswith("qa/evidence-frames/")
            or ".." in Path(point["path"]).parts
            or type(point["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", point["sha256"]) is None
        ):
            raise WorkflowError("VISUAL_EVIDENCE_INVALID", "Review point fields are invalid")
        evidence = project_path(run_dir, point["path"], must_exist=True)
        if (
            build_manifest.get("artifacts", {}).get(point["path"])
            != point["sha256"]
            or not evidence.is_file()
            or sha256_file(evidence) != point["sha256"]
        ):
            raise WorkflowError("VISUAL_EVIDENCE_STALE", "Review evidence frame changed")
        seen.add(point_id)
    return record


def verify_visual_record(
    run_dir: Path,
    build_manifest: dict[str, Any],
    *,
    require_schema3: bool = False,
) -> dict[str, Any]:
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
    if type(visual["schema"]) is not int or visual["schema"] not in {2, 3}:
        raise WorkflowError("VISUAL_INVALID", "Visual review schema must be 2 or 3")
    if require_schema3 and visual["schema"] != 3:
        raise WorkflowError(
            "VISUAL_REVIEW_STALE", "Final delivery requires a schema-3 evidence review"
        )
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
    if visual["schema"] == 3:
        audio_review = visual.get("audio_review")
        evidence_frames = visual.get("evidence_frames")
        problem_timestamps = visual.get("problem_timestamps", [])
        review_points_sha256 = visual.get("review_points_sha256")
        review_points = load_review_points(run_dir, build_manifest)
        if (
            type(audio_review) is not dict
            or set(audio_review)
            != {"video_duration_seconds", "listened_seconds", "complete"}
            or type(audio_review["video_duration_seconds"]) not in {int, float}
            or type(audio_review["listened_seconds"]) not in {int, float}
            or type(audio_review["complete"]) is not bool
            or not math.isfinite(float(audio_review["video_duration_seconds"]))
            or not math.isfinite(float(audio_review["listened_seconds"]))
            or float(audio_review["video_duration_seconds"]) <= 0
            or float(audio_review["listened_seconds"]) < 0
            or float(audio_review["listened_seconds"])
            > float(audio_review["video_duration_seconds"]) + 1.0
        ):
            raise WorkflowError("VISUAL_INVALID", "Audio review duration evidence is invalid")
        expected_complete = (
            float(audio_review["listened_seconds"])
            + 0.05
            >= float(audio_review["video_duration_seconds"])
        )
        if audio_review["complete"] != expected_complete:
            raise WorkflowError("VISUAL_INVALID", "Audio review completion contradicts duration")
        if type(review_points_sha256) is not str or review_points_sha256 != sha256_file(
            run_dir / "qa/review-points.json"
        ):
            raise WorkflowError("VISUAL_EVIDENCE_STALE", "Review point record changed")
        if type(evidence_frames) is not list or (result == "pass" and not evidence_frames):
            raise WorkflowError("VISUAL_INVALID", "Timestamped evidence frames are required")
        expected_points = {point["id"]: point for point in review_points["points"]}
        observed: set[str] = set()
        for item in evidence_frames:
            if (
                type(item) is not dict
                or set(item)
                != {
                    "point_id",
                    "time_seconds",
                    "label",
                    "evidence_path",
                    "evidence_sha256",
                }
                or item.get("point_id") not in expected_points
                or type(item["time_seconds"]) not in {int, float}
                or not math.isfinite(float(item["time_seconds"]))
                or not 0 <= float(item["time_seconds"]) <= float(audio_review["video_duration_seconds"])
                or type(item["label"]) is not str
                or not normalize_text(item["label"])
            ):
                raise WorkflowError("VISUAL_INVALID", "Timestamped evidence frame is invalid")
            point = expected_points[item["point_id"]]
            if (
                not math.isclose(
                    float(item["time_seconds"]),
                    float(point["time_seconds"]),
                    abs_tol=1 / FPS + 0.05,
                )
                or item.get("evidence_path") != point["path"]
                or item.get("evidence_sha256") != point["sha256"]
                or item["point_id"] in observed
            ):
                raise WorkflowError("VISUAL_EVIDENCE_STALE", "Review evidence is not bound")
            observed.add(item["point_id"])
        if result == "pass" and observed != set(expected_points):
            raise WorkflowError(
                "VISUAL_EVIDENCE_REQUIRED", "A pass must inspect every required review point"
            )
        if type(problem_timestamps) is not list:
            raise WorkflowError("VISUAL_INVALID", "Problem timestamps are invalid")
        for item in problem_timestamps:
            if (
                type(item) is not dict
                or set(item) != {"time_seconds", "label"}
                or type(item["time_seconds"]) not in {int, float}
                or not math.isfinite(float(item["time_seconds"]))
                or not 0
                <= float(item["time_seconds"])
                <= float(audio_review["video_duration_seconds"])
                or type(item["label"]) is not str
                or not normalize_text(item["label"])
            ):
                raise WorkflowError("VISUAL_INVALID", "Problem timestamp is invalid")
        if result == "pass" and problem_timestamps:
            raise WorkflowError("VISUAL_INVALID", "A pass cannot contain problem timestamps")
        if result == "pass" and not audio_review["complete"]:
            raise WorkflowError("VISUAL_INCOMPLETE_LISTEN", "A pass requires a complete listen")
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
    top_margin_v = int(style.get("top_margin_v", 260))
    max_units = float(style.get("max_line_units", 14))
    if not (
        54 <= font_size <= 76
        and 350 <= margin_v <= 620
        and 180 <= top_margin_v <= 480
        and 10 <= max_units <= 16
    ):
        raise WorkflowError("CAPTION_STYLE", "Caption style is outside the safe range")
    fonts = ensure_managed_dir(project_root, stage / "fonts")
    managed_copy_file(project_root, FONT_PATH, fonts / FONT_PATH.name)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\nWrapStyle: 2\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Bottom,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,3,12,0,2,58,58,{margin_v},1\n"
        f"Style: Top,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,3,12,0,8,58,58,{top_margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for item in captions:
        scene_index = item.get("sceneIndex")
        if type(scene_index) is not int or not 0 <= scene_index < len(
            config["narration"]["scenes"]
        ):
            raise WorkflowError("CAPTION_SCENE", "Caption scene index is invalid")
        region = config["narration"]["scenes"][scene_index].get(
            "caption_region", "bottom"
        )
        if not item["show"] or region == "hidden":
            continue
        ass_style = "Top" if region == "top" else "Bottom"
        display = join_ass_lines(wrap_caption(item["text"], max_units))
        events.append(
            f"Dialogue: 0,{ass_time(item['startMs'])},{ass_time(item['endMs'])},{ass_style},,0,0,0,,{display}\n"
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


def review_point_plan(
    config: dict[str, Any],
    timeline: dict[str, Any],
    captions: list[dict[str, Any]],
    contracts: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    total_frames = int(timeline["total_frames"])
    target = timeline["target_boundaries_frames"]
    points: list[dict[str, Any]] = [
        {"id": "opening", "label": "开场完整画面", "frame": 0}
    ]
    for index, frame in enumerate(target[1:-1], start=2):
        points.append(
            {
                "id": f"scene-{index:02d}-boundary",
                "label": f"第 {index} 场开始边界",
                "frame": int(frame),
            }
        )
    points.append(
        {
            "id": "ending",
            "label": "结尾完整画面",
            "frame": max(0, total_frames - 1),
        }
    )
    max_units = float(config.get("caption_style", {}).get("max_line_units", 14))
    for item in captions:
        if item.get("show") and len(wrap_caption(item["text"], max_units)) == 2:
            midpoint_ms = (int(item["startMs"]) + int(item["endMs"])) // 2
            points.append(
                {
                    "id": "two-line-caption",
                    "label": "双行字幕代表帧",
                    "frame": min(
                        total_frames - 1,
                        max(0, round(midpoint_ms * FPS / 1000)),
                    ),
                }
            )
            break
    if contracts is not None:
        for index, scene in enumerate(contracts["plan"]["scenes"]):
            if "information_card" not in scene:
                continue
            start, end = int(target[index]), int(target[index + 1])
            for suffix, label, frame in (
                ("entry", "信息卡进入", start),
                ("mid", "信息卡中段", start + (end - start) // 2),
                ("exit", "信息卡退出", max(start, end - 1)),
            ):
                points.append(
                    {
                        "id": f"{scene['scene_id']}-card-{suffix}",
                        "label": label,
                        "frame": min(total_frames - 1, frame),
                    }
                )
    for item in points:
        item["time_seconds"] = round(item["frame"] / FPS, 6)
    return points


def render_review_evidence(
    ffmpeg: Path,
    video: Path,
    qa_dir: Path,
    points: list[dict[str, Any]],
    project_root: Path,
) -> dict[str, Any]:
    evidence_dir = managed_path(project_root, qa_dir / "evidence-frames", kind="dir")
    if evidence_dir.exists():
        remove_managed_tree(project_root, evidence_dir)
    evidence_dir = ensure_managed_dir(project_root, evidence_dir)
    unique_frames = sorted({int(item["frame"]) for item in points})
    expression = "+".join(f"eq(n\\,{frame})" for frame in unique_frames)
    pattern = evidence_dir / "capture-%03d.jpg"
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
            f"select='{expression}'",
            "-vsync",
            "0",
            "-q:v",
            "2",
            pattern,
        ],
        "QA_EVIDENCE_FRAMES",
        "Could not generate full-resolution review evidence",
    )
    captures = sorted(evidence_dir.glob("capture-*.jpg"))
    if len(captures) != len(unique_frames):
        raise WorkflowError(
            "QA_EVIDENCE_FRAMES", "Review evidence frame count is incomplete"
        )
    by_frame: dict[int, tuple[str, str]] = {}
    for frame, capture in zip(unique_frames, captures):
        destination = managed_path(
            project_root, evidence_dir / f"frame-{frame:06d}.jpg", kind="file"
        )
        os.replace(capture, destination)
        destination = managed_path(
            project_root, destination, must_exist=True, kind="file"
        )
        relative = destination.relative_to(qa_dir.parent).as_posix()
        by_frame[frame] = (relative, sha256_file(destination))
    bound_points = []
    for item in points:
        relative, digest = by_frame[int(item["frame"])]
        bound_points.append(item | {"path": relative, "sha256": digest})
    record = {
        "schema": 1,
        "video_sha256": sha256_file(video),
        "points": bound_points,
    }
    managed_atomic_write_json(project_root, qa_dir / "review-points.json", record)
    return record


def auto_qa(
    ffmpeg: Path,
    ffprobe: Path,
    stage: Path,
    config: dict[str, Any],
    timeline: dict[str, Any],
    cover_records: list[dict[str, Any]],
    project_root: Path,
    contracts: dict[str, Any] | None = None,
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
    review_points = render_review_evidence(
        ffmpeg,
        final,
        qa_dir,
        review_point_plan(config, timeline, captions, contracts),
        project_root,
    )
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
        "review_points": "qa/review-points.json",
        "review_point_count": len(review_points["points"]),
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
    contracts = validate_v2_contracts(config_path, config)
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
    approval_sha256 = sha256_file(
        managed_path(
            project_root,
            "review/approval.json",
            must_exist=True,
            kind="file",
        )
    )
    base_sha256 = sha256_file(base)
    bgm_sha256 = sha256_file(bgm) if bgm else None
    ffmpeg_sha256 = sha256_file(ffmpeg)
    ffprobe_sha256 = sha256_file(ffprobe)
    keys = compute_build_keys(
        config=config,
        approval_sha256=approval_sha256,
        base_sha256=base_sha256,
        bgm_sha256=bgm_sha256,
        cover_records=cover_records,
        implementation=implementation,
        tts_profile=tts_profile,
        ffmpeg_sha256=ffmpeg_sha256,
        ffprobe_sha256=ffprobe_sha256,
        aligner_runtime=aligner_runtime,
        contract_hashes=(
            {
                key: contracts[key]
                for key in (
                    "source_package",
                    "source_content",
                    "edit_plan",
                    "cover_prompt",
                )
            }
            if contracts is not None
            else None
        ),
    )
    video_key = keys["video_key"]
    cover_key = keys["cover_key"]
    build_key = keys["build_key"]
    semantic = {
        "base_sha256": base_sha256,
        "bgm_sha256": bgm_sha256,
        "ffmpeg_sha256": ffmpeg_sha256,
        "ffprobe_sha256": ffprobe_sha256,
        "font_sha256": sha256_file(FONT_PATH),
    }
    control = ensure_managed_dir(project_root, ".source-led-ai-video")
    staging_root = ensure_managed_dir(project_root, control / "staging")
    review_root = ensure_managed_dir(project_root, "review-runs")
    lock_path = control / "build.lock"
    with open_managed_lock(project_root, lock_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        telemetry = BuildTelemetry(project_root, build_key)
        telemetry.start(
            "build",
            {
                "video_key": video_key,
                "cover_key": cover_key,
                "approval": approval_sha256,
            },
        )
        published_review = managed_path(
            project_root, review_root / build_key, kind="dir"
        )
        if published_review.exists():
            try:
                assert_managed_tree(project_root, published_review)
                integrity = verify_build_integrity(config_path, published_review)
                visual_path = managed_path(
                    project_root, published_review / "qa/visual-review.json", kind="file"
                )
                status = "reused_needs_visual_review"
                if visual_path.is_file():
                    visual = verify_visual_record(
                        published_review,
                        integrity["manifest"],
                        require_schema3=True,
                    )
                    status = "reused_visual_pass" if visual.get("result") == "pass" else "reused_visual_fail"
                outputs = {"build_manifest": sha256_file(published_review / "build-manifest.json")}
                telemetry.cache_hit("build", build_key, outputs)
                telemetry.finish("build", outputs)
                return {
                    "status": status,
                    "build_key": build_key,
                    "video_key": video_key,
                    "cover_key": cover_key,
                    "review_run": str(published_review),
                }
            except Exception as exc:
                telemetry.fail("build", getattr(exc, "code", "UNEXPECTED"))
                raise

        stage = managed_path(
            project_root, staging_root / f"{build_key}.tmp", kind="dir"
        )
        resume_requested = bool(getattr(args, "resume", False))
        if stage.exists() and not resume_requested:
            remove_managed_tree(project_root, stage)
        stage = ensure_managed_dir(project_root, stage)
        try:
            telemetry.start("video_cache", {"video_key": video_key})
            cached_video = restore_video_cache(
                project_root, control, video_key, stage
            )
            if cached_video is not None:
                telemetry.cache_hit("video_cache", video_key, cached_video)
                telemetry.finish("video_cache", cached_video)
            else:
                telemetry.finish("video_cache", {})
            raw_audio = managed_path(
                project_root, stage / "narration-raw.mp3", kind="file"
            )
            if cached_video is None:
                telemetry.start("tts", {"narration": narration_hash(config)})
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
                raw_hashes = {"narration-raw.mp3": sha256_file(raw_audio)}
                if tts_result.get("cache") in {"hit", "replay"}:
                    cache_key = str(tts_result.get("cache_key"))
                    if re.fullmatch(r"[0-9a-f]{20,64}", cache_key):
                        telemetry.cache_hit("tts", cache_key, raw_hashes)
                telemetry.finish("tts", raw_hashes)
            else:
                tts_result = {"cache": "video-cache", "cache_key": video_key}
                raw_audio = managed_path(
                    project_root, raw_audio, must_exist=True, kind="file"
                )

            master = managed_path(
                project_root, stage / "narration-master.wav", kind="file"
            )
            if cached_video is None:
                telemetry.start(
                    "voice_master", {"narration_raw": sha256_file(raw_audio)}
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
            if cached_video is None:
                telemetry.finish(
                    "voice_master", {"narration-master.wav": sha256_file(master)}
                )
            audio_duration = media_duration(probe(ffprobe, master))

            if cached_video is None:
                telemetry.start(
                    "alignment", {"narration_master": sha256_file(master)}
                )
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
                telemetry.finish(
                    "alignment",
                    {
                        name: sha256_file(stage / name)
                        for name in (
                            "alignment.json",
                            "captions-all.json",
                            "captions-all.srt",
                            "captions-platform.srt",
                        )
                    },
                )
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
            timeline_lock = compile_v2_timeline_lock(
                config_path,
                contracts,
                timeline,
                managed_path(
                    project_root,
                    stage / "alignment.json",
                    must_exist=True,
                    kind="file",
                ),
            )
            build_ass(stage, captions, config, project_root)
            create_video_filter(stage, timeline, project_root)

            mixed = managed_path(
                project_root, stage / "audio-mix.wav", kind="file"
            )
            target_duration = timeline["duration_seconds"]
            if cached_video is None and bgm:
                telemetry.start(
                    "audio_mix",
                    {
                        "narration_master": sha256_file(master),
                        "bgm": sha256_file(bgm),
                    },
                )
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
            elif cached_video is None:
                telemetry.start(
                    "audio_mix", {"narration_master": sha256_file(master)}
                )
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
            if cached_video is None:
                mix_args += ["-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s24le", mixed]
                run_checked(mix_args, "AUDIO_MIX", "Could not mix narration and BGM")
            mixed = managed_path(
                project_root, mixed, must_exist=True, kind="file"
            )
            if cached_video is None:
                telemetry.finish(
                    "audio_mix", {"audio-mix.wav": sha256_file(mixed)}
                )

            final = managed_path(project_root, stage / "video.mp4", kind="file")
            if cached_video is None:
                telemetry.start(
                    "video_render",
                    {
                        "base": base_sha256,
                        "audio_mix": sha256_file(mixed),
                        "filter": sha256_file(stage / "video-filter.txt"),
                    },
                )
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
            if cached_video is None:
                telemetry.finish("video_render", {"video.mp4": sha256_file(final)})

            telemetry.start("covers", {"cover_key": cover_key})
            covers_dir = ensure_managed_dir(project_root, stage / "covers")
            for item in cover_records:
                destination = covers_dir / f"cover-{item['ratio']}.png"
                managed_copy_file(project_root, item["final"], destination)
                if sha256_file(destination) != item["final_sha256"]:
                    raise WorkflowError("COVER_COPY", f"{item['ratio']} cover copy changed")
            telemetry.finish(
                "covers",
                {
                    "cover-3x4": sha256_file(covers_dir / "cover-3x4.png"),
                    "cover-4x3": sha256_file(covers_dir / "cover-4x3.png"),
                },
            )
            assert_managed_tree(project_root, stage)
            provenance = {
                "schema": 1,
                "build_key": build_key,
                "video_key": video_key,
                "cover_key": cover_key,
                "project_name": config["project_name"],
                "config_sha256": config_sha256(config),
                "narration_sha256": narration_hash(config),
                "approval": approval,
                "inputs": {
                    "base_sha256": semantic["base_sha256"],
                    "bgm_sha256": semantic["bgm_sha256"],
                    "source_assets": (
                        contracts["used_assets"] if contracts is not None else {}
                    ),
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
                "contracts": (
                    {
                        "source_package_sha256": contracts["source_package"],
                        "edit_plan_sha256": contracts["edit_plan"],
                        "timeline_lock_sha256": contracts["timeline_lock"],
                        "cover_prompt_sha256": contracts["cover_prompt"],
                    }
                    if contracts is not None
                    else None
                ),
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
            telemetry.start("qa", {"video": sha256_file(final)})
            auto_qa(
                ffmpeg,
                ffprobe,
                stage,
                config,
                timeline,
                cover_records,
                project_root,
                contracts,
            )
            telemetry.finish(
                "qa",
                {
                    "auto_qa": sha256_file(stage / "qa/auto-qa.json"),
                    "contact_sheet": sha256_file(stage / "qa/contact-sheet.jpg"),
                },
            )
            if cached_video is None:
                telemetry.start(
                    "video_cache_publish", {"video_key": video_key}
                )
                published_cache = publish_video_cache(
                    project_root, control, video_key, stage
                )
                telemetry.finish("video_cache_publish", published_cache)
            copy_review_evidence(stage, config, project_root, approval)
            if contracts is not None:
                current_contracts = validate_v2_contracts(config_path, config)
                if current_contracts is None or any(
                    current_contracts[key] != contracts[key]
                    for key in ("source_package", "edit_plan", "cover_prompt")
                ):
                    raise WorkflowError(
                        "CONTRACT_STALE", "V2 source, edit, or cover contract changed"
                    )
                current_lock = validate_timeline_lock(config_path)
                if (
                    timeline_lock is None
                    or sha256_bytes(canonical_json(current_lock))
                    != contracts["timeline_lock"]
                ):
                    raise WorkflowError(
                        "CONTRACT_STALE", "V2 timeline lock changed during the build"
                    )
                copy_v2_contracts(project_root, stage, config, contracts)
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
            outputs = {
                "build_manifest": sha256_file(published_review / "build-manifest.json"),
                "video": sha256_file(published_review / "video.mp4"),
            }
            telemetry.finish("build", outputs)
            return {
                "status": "needs_visual_review",
                "build_key": build_key,
                "video_key": video_key,
                "cover_key": cover_key,
                "review_run": str(published_review),
            }
        except Exception as original:
            error_code = getattr(original, "code", "UNEXPECTED")
            if telemetry.active_stage is not None:
                telemetry.fail(telemetry.active_stage, error_code)
            if "build" in telemetry.attempts:
                telemetry.fail("build", error_code)
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
    *,
    listened_seconds: float | None = None,
    video_seconds: float | None = None,
    evidence_frames: list[dict[str, Any]] | None = None,
    strict_evidence: bool = False,
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
    detected_duration = integrity["auto"].get("duration_seconds")
    if type(detected_duration) not in {int, float}:
        detected_duration = integrity["provenance"].get("timeline", {}).get(
            "duration_seconds"
        )
    if type(detected_duration) not in {int, float}:
        config, _ = load_and_validate_config(config_path)
        detected_duration = config["source_timeline"]["scene_boundaries_seconds"][-1]
    detected_duration = float(detected_duration)
    if video_seconds is None:
        video_seconds = detected_duration
    if not math.isfinite(float(video_seconds)) or float(video_seconds) <= 0:
        raise WorkflowError("VISUAL_DURATION", "Video review duration is invalid")
    if abs(float(video_seconds) - detected_duration) > 0.2:
        raise WorkflowError("VISUAL_DURATION", "Declared video duration does not match automatic QA")
    if listened_seconds is None:
        if result == "pass":
            raise WorkflowError("VISUAL_LISTEN_REQUIRED", "Pass requires --listened-seconds")
        listened_seconds = 0.0
    if not math.isfinite(float(listened_seconds)) or float(listened_seconds) < 0:
        raise WorkflowError("VISUAL_LISTEN_DURATION", "Listened duration is invalid")
    if result == "pass" and float(listened_seconds) + 0.05 < float(video_seconds):
        raise WorkflowError("VISUAL_INCOMPLETE_LISTEN", "A pass requires listening to the full video")
    if float(listened_seconds) > float(video_seconds) + 1.0:
        raise WorkflowError(
            "VISUAL_LISTEN_DURATION", "Listened duration cannot exceed the video"
        )
    review_points = load_review_points(run_dir, integrity["manifest"])
    if evidence_frames is None:
        if result == "pass":
            raise WorkflowError("VISUAL_EVIDENCE_REQUIRED", "Pass requires timestamped evidence frames")
        evidence_frames = []
    if type(evidence_frames) is not list:
        raise WorkflowError("VISUAL_EVIDENCE_REQUIRED", "Timestamped evidence frames are invalid")
    submitted: list[dict[str, Any]] = []
    for item in evidence_frames:
        if (
            type(item) is not dict
            or set(item) != {"time_seconds", "label"}
            or type(item["time_seconds"]) not in {int, float}
            or not math.isfinite(float(item["time_seconds"]))
            or not 0 <= float(item["time_seconds"]) <= float(video_seconds)
            or type(item["label"]) is not str
            or not normalize_text(item["label"])
        ):
            raise WorkflowError("VISUAL_EVIDENCE", "Timestamped evidence frame is invalid")
        submitted.append(item)
    bound_evidence: list[dict[str, Any]] = []
    missing_points: list[str] = []
    for point in review_points["points"]:
        match = next(
            (
                item
                for item in submitted
                if math.isclose(
                    float(item["time_seconds"]),
                    float(point["time_seconds"]),
                    abs_tol=1 / FPS + 0.05,
                )
            ),
            None,
        )
        if match is None:
            missing_points.append(point["id"])
            continue
        bound_evidence.append(
            {
                "point_id": point["id"],
                "time_seconds": point["time_seconds"],
                "label": normalize_text(match["label"]),
                "evidence_path": point["path"],
                "evidence_sha256": point["sha256"],
            }
        )
    if result == "pass" and missing_points:
        raise WorkflowError(
            "VISUAL_EVIDENCE_REQUIRED",
            "Pass is missing review points: " + ", ".join(missing_points),
        )
    problem_timestamps = [
        {
            "time_seconds": round(float(item["time_seconds"]), 6),
            "label": normalize_text(item["label"]),
        }
        for item in submitted
        if not any(
            math.isclose(
                float(item["time_seconds"]),
                float(point["time_seconds"]),
                abs_tol=1 / FPS + 0.05,
            )
            for point in review_points["points"]
        )
    ]
    report = {
        "schema": 3,
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
        "audio_review": {
            "video_duration_seconds": round(float(video_seconds), 6),
            "listened_seconds": round(float(listened_seconds), 6),
            "complete": float(listened_seconds) + 0.05 >= float(video_seconds),
        },
        "review_points_sha256": sha256_file(run_dir / "qa/review-points.json"),
        "evidence_frames": bound_evidence,
        "problem_timestamps": problem_timestamps,
    }
    managed_atomic_write_json(
        project_root, run_dir / "qa/visual-review.json", report
    )
    return verify_visual_record(
        run_dir, integrity["manifest"], require_schema3=True
    )


def _parse_expiry(value: Any) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights expiry must be an ISO timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


def required_rights_assets(
    project_root: Path, config: dict[str, Any]
) -> list[dict[str, str]]:
    required = [
        {
            "asset_id": "base_video",
            "asset_sha256": sha256_file(
                project_path(
                    project_root, config["paths"]["base_video"], must_exist=True
                )
            ),
            "source_id": "source-base-video",
        }
    ]
    bgm_path = config["paths"].get("bgm")
    if bgm_path:
        required.append(
            {
                "asset_id": "bgm",
                "asset_sha256": sha256_file(
                    project_path(project_root, bgm_path, must_exist=True)
                ),
                "source_id": "source-bgm",
            }
        )
    if config.get("version") == 2:
        package = validate_source_package(project_root / "project.json")
        assets = {
            item["asset_id"]: item for item in package["assets"]["items"]
        }
        used = {
            asset_id
            for scene in config["narration"]["scenes"]
            for asset_id in scene["asset_ids"]
        }
        for asset_id in sorted(used):
            asset = assets[asset_id]
            if asset["rights_status"] != "confirmed":
                raise WorkflowError(
                    "RIGHTS_SOURCE_PENDING",
                    f"Source Package rights are not confirmed for {asset_id}",
                )
            required.append(
                {
                    "asset_id": asset_id,
                    "asset_sha256": asset["sha256"],
                    "source_id": asset["source_id"],
                }
            )
    return required


def _validate_rights_confirmation(
    project_root: Path,
    record: dict[str, Any],
    *,
    require_evidence_files: bool,
    require_schema2: bool,
) -> tuple[int, str, str | None, bool, str]:
    schema = record.get("schema")
    if schema not in {1, 2} or (require_schema2 and schema != 2):
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights ledger schema is invalid")
    required = {
        "schema",
        "event",
        "asset_sha256",
        "platforms",
        "commercial",
        "expires_at",
        "rights_basis",
        "evidence_path",
        "evidence_sha256",
        "confirmed_by",
    }
    if not required.issubset(record):
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights confirmation is incomplete")
    digest = record.get("asset_sha256")
    evidence_digest = record.get("evidence_sha256")
    platforms = record.get("platforms")
    if (
        type(digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(evidence_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None
        or type(platforms) is not list
        or not platforms
        or any(type(item) is not str for item in platforms)
        or len(platforms) != len(set(platforms))
        or type(record.get("commercial")) is not bool
        or type(record.get("rights_basis")) is not str
        or not normalize_text(record["rights_basis"])
        or type(record.get("confirmed_by")) is not str
        or not normalize_text(record["confirmed_by"])
    ):
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights confirmation fields are invalid")
    expiry = _parse_expiry(record.get("expires_at"))
    if schema == 2:
        regions = record.get("regions")
        attribution = record.get("attribution")
        for field in ("asset_id", "source_id", "author", "source", "confirmed_at"):
            if type(record.get(field)) is not str or not normalize_text(record[field]):
                raise WorkflowError(
                    "RIGHTS_LEDGER_INVALID", f"Rights confirmation lacks {field}"
                )
        _parse_expiry(record["confirmed_at"])
        if (
            type(regions) is not list
            or "CN" not in regions
            or any(type(item) is not str for item in regions)
            or len(regions) != len(set(regions))
            or type(attribution) is not dict
            or set(attribution) != {"required", "placement", "text"}
            or type(attribution.get("required")) is not bool
            or type(attribution.get("placement")) is not str
            or type(attribution.get("text")) is not str
        ):
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights scope metadata is invalid")
        if attribution["required"]:
            if attribution["placement"] not in {
                "body",
                "pinned_comment",
                "body_or_pinned_comment",
            } or not normalize_text(attribution["text"]):
                raise WorkflowError(
                    "RIGHTS_LEDGER_INVALID", "Attribution plan is incomplete"
                )
        elif attribution["placement"] != "none" or attribution["text"]:
            raise WorkflowError(
                "RIGHTS_LEDGER_INVALID", "No-attribution record is contradictory"
            )
    evidence_raw = record.get("evidence_path")
    if type(evidence_raw) is not str or not evidence_raw.startswith(
        "private/rights-evidence/"
    ):
        raise WorkflowError(
            "RIGHTS_LEDGER_INVALID",
            "Rights evidence must be inside private/rights-evidence",
        )
    if require_evidence_files:
        evidence = project_path(project_root, evidence_raw, must_exist=True)
        if sha256_file(evidence) != evidence_digest:
            raise WorkflowError(
                "RIGHTS_EVIDENCE_STALE", "Rights evidence hash does not match"
            )
    active_scope = (
        (expiry is None or expiry > datetime.now(timezone.utc))
        and "douyin" in platforms
        and record["commercial"] is True
        and (schema == 1 or "CN" in record["regions"])
    )
    return schema, digest, record.get("asset_id"), active_scope, evidence_raw


def _normalize_required_rights_assets(
    required: dict[str, str] | list[dict[str, str]],
) -> list[dict[str, str | None]]:
    if type(required) is dict:
        return [
            {
                "asset_id": asset_id,
                "asset_sha256": digest,
                "source_id": None,
            }
            for digest, asset_id in required.items()
        ]
    if type(required) is not list:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Required rights assets are invalid")
    normalized: list[dict[str, str | None]] = []
    for item in required:
        if (
            type(item) is not dict
            or type(item.get("asset_id")) is not str
            or not normalize_text(item["asset_id"])
            or type(item.get("asset_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["asset_sha256"]) is None
            or (
                item.get("source_id") is not None
                and (
                    type(item.get("source_id")) is not str
                    or not normalize_text(item["source_id"])
                )
            )
        ):
            raise WorkflowError(
                "RIGHTS_LEDGER_INVALID", "Required rights asset identity is invalid"
            )
        normalized.append(
            {
                "asset_id": item["asset_id"],
                "asset_sha256": item["asset_sha256"],
                "source_id": item.get("source_id"),
            }
        )
    return normalized


def verify_rights_ledger(
    project_root: Path,
    config: dict[str, Any] | None,
    *,
    ledger_path: Path | None = None,
    require_evidence_files: bool = True,
    required_asset_hashes: dict[str, str] | list[dict[str, str]] | None = None,
    require_schema2: bool | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    ledger = ledger_path or (project_root / "private" / "rights-events.jsonl")
    try:
        relative_ledger = ledger.relative_to(project_root)
        ledger = project_path(
            project_root, relative_ledger.as_posix(), must_exist=True
        )
    except WorkflowError as exc:
        if exc.code == "PATH_MISSING":
            raise WorkflowError("RIGHTS_LEDGER_MISSING", "Rights ledger is missing") from exc
        raise
    except ValueError as exc:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights ledger leaves the project") from exc
    try:
        raw_lines = ledger.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights ledger cannot be read") from exc
    if not raw_lines:
        raise WorkflowError("RIGHTS_LEDGER_MISSING", "Rights ledger is empty")
    if require_schema2 is None:
        require_schema2 = bool(config is not None and config.get("version") == 2)
    legacy_state: dict[str, dict[str, Any] | None] = {}
    schema2_state: dict[tuple[str, str], dict[str, Any] | None] = {}
    evidence_paths: set[str] = set()
    for line_number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                "RIGHTS_LEDGER_INVALID", f"Rights ledger line {line_number} is invalid"
            ) from exc
        if type(record) is not dict:
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights ledger entries must be objects")
        event = record.get("event")
        if event == "rights_confirmed":
            schema, digest, asset_id, active_scope, evidence_path = _validate_rights_confirmation(
                project_root,
                record,
                require_evidence_files=require_evidence_files,
                require_schema2=require_schema2,
            )
            evidence_paths.add(evidence_path)
            legacy_state[digest] = record if active_scope else None
            if schema == 2:
                assert type(asset_id) is str
                schema2_state[(asset_id, digest)] = record if active_scope else None
        elif event in {"rights_revoked", "rights_superseded"}:
            schema = record.get("schema")
            digest = record.get("asset_sha256")
            asset_id = record.get("asset_id")
            timestamp_field = "revoked_at" if event == "rights_revoked" else "superseded_at"
            if (
                schema not in {1, 2}
                or (require_schema2 and schema != 2)
                or type(digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or (
                    schema == 2
                    and (type(asset_id) is not str or not normalize_text(asset_id))
                )
                or type(record.get("reason")) is not str
                or not normalize_text(record["reason"])
                or type(record.get("confirmed_by")) is not str
                or not normalize_text(record["confirmed_by"])
            ):
                raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights revocation is invalid")
            _parse_expiry(record.get(timestamp_field))
            legacy_state[digest] = None
            if schema == 2:
                assert type(asset_id) is str
                schema2_state[(asset_id, digest)] = None
        else:
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Unknown rights ledger event")

    if required_asset_hashes is not None:
        required_assets = _normalize_required_rights_assets(required_asset_hashes)
    elif config is not None:
        required_assets = required_rights_assets(project_root, config)
    else:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Required rights assets are missing")
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    mismatched_sources: list[str] = []
    for required in required_assets:
        asset_id = str(required["asset_id"])
        digest = str(required["asset_sha256"])
        if require_schema2:
            record = schema2_state.get((asset_id, digest))
        else:
            record = legacy_state.get(digest)
        if record is None:
            missing.append(asset_id)
            continue
        expected_source_id = required.get("source_id")
        if (
            require_schema2
            and expected_source_id is not None
            and record.get("source_id") != expected_source_id
        ):
            mismatched_sources.append(asset_id)
            continue
        records.append(record)
    if missing:
        raise WorkflowError(
            "RIGHTS_SCOPE",
            "Active Douyin commercial rights are missing for: " + ", ".join(missing),
        )
    if mismatched_sources:
        raise WorkflowError(
            "RIGHTS_LEDGER_INVALID",
            "Rights source IDs do not match: " + ", ".join(mismatched_sources),
        )
    return {
        "schema": 1,
        "ledger": str(ledger.relative_to(project_root)),
        "ledger_sha256": sha256_file(ledger),
        "records": records,
        "evidence_paths": sorted(evidence_paths),
    }


def copy_rights_evidence(
    project_root: Path, run_dir: Path, rights: dict[str, Any]
) -> None:
    ledger = project_path(project_root, rights["ledger"], must_exist=True)
    managed_copy_file(project_root, ledger, run_dir / "private/rights-events.jsonl")
    for relative in rights["evidence_paths"]:
        source = project_path(project_root, relative, must_exist=True)
        managed_copy_file(project_root, source, run_dir / relative)


def release_key_for(build_key: str, rights_ledger_sha256: str) -> str:
    if (
        type(build_key) is not str
        or re.fullmatch(r"[0-9a-f]{20}", build_key) is None
        or type(rights_ledger_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", rights_ledger_sha256) is None
    ):
        raise WorkflowError("BUNDLE_KEY", "Release identity is invalid")
    return sha256_bytes(
        canonical_json(
            {
                "schema": 1,
                "build_key": build_key,
                "rights_ledger_sha256": rights_ledger_sha256,
            }
        )
    )[:20]


def _clear_latest_for_bundle(
    project_root: Path, deliverables: Path, final_dir: Path
) -> None:
    latest_path = deliverables / "latest.json"
    if not latest_path.exists():
        return
    latest_path = managed_path(
        project_root, latest_path, must_exist=True, kind="file"
    )
    try:
        latest = read_json(latest_path)
    except Exception:
        latest = None
    expected = str(final_dir.relative_to(project_root))
    if type(latest) is dict:
        recorded_bundle = latest.get("bundle")
        if type(recorded_bundle) is str and recorded_bundle != expected:
            return
    try:
        latest_path.unlink()
    except OSError as exc:
        raise WorkflowError("FINALIZE_LATEST", "Could not clear stale latest.json") from exc


def _quarantine_release(
    project_root: Path,
    deliverables: Path,
    final_dir: Path,
    release_key: str,
) -> Path:
    quarantine = managed_path(
        project_root,
        deliverables
        / f".{release_key}.quarantine-{os.getpid()}-{time.monotonic_ns()}",
        kind="dir",
    )
    moved = replace_managed_dir(project_root, final_dir, quarantine)
    _clear_latest_for_bundle(project_root, deliverables, final_dir)
    return moved


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
    control = ensure_managed_dir(project_root, ".source-led-ai-video")
    with open_managed_lock(project_root, control / "build.lock") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _finalize_locked(config_path, config, project_root, run_dir)


def _finalize_locked(
    config_path: Path,
    config: dict[str, Any],
    project_root: Path,
    run_dir: Path,
) -> dict[str, Any]:
    integrity = verify_build_integrity(config_path.resolve(), run_dir)
    require_confirmed_rights(integrity["approval"])
    visual = verify_visual_record(
        run_dir, integrity["manifest"], require_schema3=True
    )
    if visual.get("result") != "pass" or not all(visual.get("checks", {}).values()):
        raise WorkflowError("FINALIZE_QA", "Automatic and visual QA must both pass")
    deliverables = ensure_managed_dir(project_root, "deliverables")
    staging = Path(
        tempfile.mkdtemp(
            prefix=".release-pending.",
            suffix=".finalize.tmp",
            dir=deliverables,
        )
    )
    staging = managed_path(project_root, staging, must_exist=True, kind="dir")
    try:
        for source in sorted(run_dir.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(run_dir)
            managed_copy_file(project_root, source, staging / relative)

        # Use the same lock as rights_ledger.py. The release identity is derived
        # from the copied ledger bytes, never from an earlier live read.
        ledger_path = project_root / "private" / "rights-events.jsonl"
        try:
            private_stat = os.lstat(ledger_path.parent)
        except OSError as exc:
            raise WorkflowError(
                "RIGHTS_LEDGER_MISSING", "Rights directory is missing"
            ) from exc
        if stat.S_ISLNK(private_stat.st_mode) or not stat.S_ISDIR(private_stat.st_mode):
            raise WorkflowError(
                "RIGHTS_LEDGER_INVALID", "Rights directory must be a plain directory"
            )
        try:
            with identity_log_lock(ledger_path):
                rights = verify_rights_ledger(project_root, config)
                copy_rights_evidence(project_root, staging, rights)
                copied_ledger = staging / "private" / "rights-events.jsonl"
                copied_ledger_sha256 = sha256_file(copied_ledger)
                if copied_ledger_sha256 != rights["ledger_sha256"]:
                    raise WorkflowError(
                        "FINALIZE_RIGHTS_RACE",
                        "Rights ledger changed while taking snapshot",
                    )
        except TelemetryError as exc:
            raise WorkflowError(
                "RIGHTS_LEDGER_INVALID", "Rights ledger lock is unsafe"
            ) from exc

        release_key = release_key_for(run_dir.name, copied_ledger_sha256)
        final_dir = managed_path(
            project_root, deliverables / release_key, kind="dir"
        )
        if final_dir.exists():
            assert_managed_tree(project_root, final_dir)
            existing_valid = False
            try:
                existing = verify_bundle_contracts(
                    final_dir,
                    expected_build_key=run_dir.name,
                    expected_release_key=release_key,
                )
                existing_valid = not existing["failures"]
            except WorkflowError:
                existing_valid = False
            if existing_valid:
                remove_managed_tree(project_root, staging)
                staging = None
                latest = {
                    "schema": 1,
                    "build_key": run_dir.name,
                    "release_key": release_key,
                    "rights_ledger_sha256": copied_ledger_sha256,
                    "bundle": str(final_dir.relative_to(project_root)),
                    "video": str((final_dir / "video.mp4").relative_to(project_root)),
                }
                managed_atomic_write_json(
                    project_root, deliverables / "latest.json", latest
                )
                return latest
            _quarantine_release(
                project_root, deliverables, final_dir, release_key
            )

        artifacts = {
            path.relative_to(staging).as_posix(): sha256_file(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file() and path.name != "bundle-manifest.json"
        }
        bundle_manifest = {
            "schema": 1,
            "kind": "bundle",
            "build_key": run_dir.name,
            "release_key": release_key,
            "rights_ledger_sha256": copied_ledger_sha256,
            "build_manifest_sha256": sha256_file(staging / "build-manifest.json"),
            "artifacts": artifacts,
        }
        managed_atomic_write_json(
            project_root, staging / "bundle-manifest.json", bundle_manifest
        )
        staged = verify_bundle_contracts(
            staging,
            expected_build_key=run_dir.name,
            expected_release_key=release_key,
        )
        if staged["failures"]:
            raise WorkflowError(
                "FINALIZE_STAGING",
                "Staged deliverable failed release verification: "
                + "; ".join(staged["failures"]),
            )
        # Close the ordinary check/copy gap: all staged bytes were verified
        # above, then the manifest is checked immediately before atomic publish.
        verify_hash_manifest(staging, "bundle-manifest.json")
        try:
            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_file():
                    os.chmod(path, 0o400)
                elif path.is_dir():
                    os.chmod(path, 0o700)
            os.chmod(staging, 0o700)
        except OSError as exc:
            raise WorkflowError(
                "FINALIZE_PERMISSIONS",
                "Could not lock staged release permissions",
            ) from exc
        replace_managed_dir(project_root, staging, final_dir)
        staging = None
    except Exception:
        if staging is not None and staging.exists():
            remove_managed_tree(project_root, staging)
        raise

    try:
        published = verify_bundle_contracts(
            final_dir,
            expected_build_key=run_dir.name,
            expected_release_key=release_key,
        )
        if published["failures"]:
            raise WorkflowError(
                "FINALIZE_PUBLISHED",
                "Published deliverable failed release verification: "
                + "; ".join(published["failures"]),
            )
    except Exception:
        _quarantine_release(project_root, deliverables, final_dir, release_key)
        raise
    latest = {
        "schema": 1,
        "build_key": run_dir.name,
        "release_key": release_key,
        "rights_ledger_sha256": copied_ledger_sha256,
        "bundle": str(final_dir.relative_to(project_root)),
        "video": str((final_dir / "video.mp4").relative_to(project_root)),
    }
    managed_atomic_write_json(project_root, deliverables / "latest.json", latest)
    return latest


def _packaged_path(
    bundle: Path,
    raw: Any,
    label: str,
    *,
    must_exist: bool = False,
) -> Path:
    if type(raw) is not str or not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise WorkflowError("BUNDLE_CONFIG", f"Invalid packaged path: {label}")
    try:
        return project_path(bundle, raw, must_exist=must_exist)
    except WorkflowError as exc:
        raise WorkflowError("BUNDLE_CONFIG", f"Unsafe packaged path: {label}") from exc


def validate_packaged_config(bundle: Path, config: Any) -> dict[str, Any]:
    """Validate path-bearing config fields without requiring original inputs.

    A delivery bundle intentionally excludes the clean ChatCut base and raw
    ImageGen masters, so the normal project validator cannot be reused here.
    This verifier still enforces the platform, scene shape, relative paths, and
    every fixed V2 contract location before any configured path is opened.
    """
    if type(config) is not dict or config.get("version") not in {1, 2}:
        raise WorkflowError("BUNDLE_CONFIG", "Packaged project version is invalid")
    if config.get("platform") != "douyin":
        raise WorkflowError("BUNDLE_CONFIG", "Packaged project platform is invalid")
    paths = config.get("paths")
    cover = config.get("cover")
    narration = config.get("narration")
    if type(paths) is not dict or type(cover) is not dict or type(narration) is not dict:
        raise WorkflowError("BUNDLE_CONFIG", "Packaged project structure is invalid")
    _packaged_path(bundle, paths.get("base_video"), "paths.base_video")
    if paths.get("bgm"):
        _packaged_path(bundle, paths["bgm"], "paths.bgm")
    scenes = narration.get("scenes")
    if type(scenes) is not list or not scenes or any(type(scene) is not dict for scene in scenes):
        raise WorkflowError("BUNDLE_CONFIG", "Packaged narration scenes are invalid")
    for ratio in ("3x4", "4x3"):
        entry = cover.get(ratio)
        if type(entry) is not dict:
            raise WorkflowError("BUNDLE_CONFIG", f"Packaged cover {ratio} is invalid")
        _packaged_path(bundle, entry.get("generated_source"), f"cover.{ratio}.generated_source")
        _packaged_path(bundle, entry.get("final"), f"cover.{ratio}.final")
    if config["version"] == 2:
        fixed = {
            "source_package.manifest": (
                config.get("source_package", {}).get("manifest")
                if type(config.get("source_package")) is dict
                else None,
                "source-package/manifest.json",
            ),
            "edit.plan": (
                config.get("edit", {}).get("plan")
                if type(config.get("edit")) is dict
                else None,
                "edit/edit-plan.json",
            ),
            "edit.timeline_lock": (
                config.get("edit", {}).get("timeline_lock")
                if type(config.get("edit")) is dict
                else None,
                "edit/timeline.lock.json",
            ),
            "cover.prompt_record": (
                cover.get("prompt_record"),
                "covers/cover-prompt.json",
            ),
        }
        for label, (raw, expected) in fixed.items():
            if raw != expected:
                raise WorkflowError("BUNDLE_CONFIG", f"{label} must be {expected}")
            _packaged_path(bundle, raw, label, must_exist=True)
        for scene in scenes:
            asset_ids = scene.get("asset_ids")
            if type(asset_ids) is not list or not asset_ids or any(
                type(asset_id) is not str for asset_id in asset_ids
            ):
                raise WorkflowError("BUNDLE_CONFIG", "Packaged V2 scene assets are invalid")
    return config


def verify_bundle_contracts(
    bundle: Path,
    *,
    expected_build_key: str | None = None,
    expected_release_key: str | None = None,
) -> dict[str, Any]:
    """Verify manifests, review evidence, V2 contracts, and rights without media tools."""
    bundle = Path(os.path.abspath(os.fspath(bundle)))
    bundle, _ = _checked_tree(bundle)
    bundle_manifest = verify_hash_manifest(bundle, "bundle-manifest.json")
    declared_build_key = bundle_manifest.get("build_key")
    expected_key = expected_build_key or declared_build_key
    declared_release_key = bundle_manifest.get("release_key")
    if (
        type(expected_key) is not str
        or re.fullmatch(r"[0-9a-f]{20}", expected_key) is None
        or bundle_manifest.get("kind") != "bundle"
        or declared_build_key != expected_key
    ):
        raise WorkflowError("BUNDLE_KEY", "Bundle manifest does not match its directory")
    if declared_release_key is None:
        if expected_release_key is not None or bundle.name != expected_key:
            raise WorkflowError("BUNDLE_KEY", "Legacy bundle directory does not match build key")
    else:
        declared_rights_sha256 = bundle_manifest.get("rights_ledger_sha256")
        if (
            type(declared_rights_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", declared_rights_sha256) is None
        ):
            raise WorkflowError("BUNDLE_KEY", "Bundle rights identity is invalid")
        packaged_ledger = _packaged_path(
            bundle,
            "private/rights-events.jsonl",
            "private.rights_ledger",
            must_exist=True,
        )
        actual_rights_sha256 = sha256_file(packaged_ledger)
        recomputed_release_key = release_key_for(
            expected_key, actual_rights_sha256
        )
        release_key = expected_release_key or bundle.name
        if (
            type(release_key) is not str
            or re.fullmatch(r"[0-9a-f]{20}", release_key) is None
            or declared_release_key != release_key
            or declared_rights_sha256 != actual_rights_sha256
            or declared_release_key != recomputed_release_key
            or (expected_release_key is None and bundle.name != release_key)
        ):
            raise WorkflowError("BUNDLE_KEY", "Bundle release key is invalid")
    build_manifest = verify_hash_manifest(
        bundle,
        "build-manifest.json",
        allowed_unlisted=(
            "qa/visual-review.json",
            "bundle-manifest.json",
            "private/rights-events.jsonl",
        ),
        allowed_unlisted_prefixes=("private/rights-evidence/",),
    )
    if (
        build_manifest.get("kind") != "build"
        or build_manifest.get("build_key") != expected_key
        or bundle_manifest.get("build_manifest_sha256")
        != sha256_file(bundle / "build-manifest.json")
    ):
        raise WorkflowError("BUNDLE_MANIFEST", "Build manifest binding is invalid")
    packaged_config = validate_packaged_config(
        bundle,
        read_json(_packaged_path(bundle, "project.json", "project.json", must_exist=True)),
    )
    if build_manifest.get("config_sha256") != config_sha256(packaged_config):
        raise WorkflowError("BUNDLE_CONFIG", "Packaged config hash is stale")
    required = required_build_artifacts(packaged_config)
    for relative in (*required, "build-manifest.json", "qa/visual-review.json"):
        if relative not in bundle_manifest["artifacts"]:
            raise WorkflowError("BUNDLE_ARTIFACT", f"Bundle lacks required artifact: {relative}")
    for relative in required:
        if relative not in build_manifest["artifacts"]:
            raise WorkflowError("BUNDLE_ARTIFACT", f"Build manifest lacks artifact: {relative}")
    qa = read_json(_packaged_path(bundle, "qa/auto-qa.json", "qa.auto", must_exist=True))
    visual = verify_visual_record(
        bundle, build_manifest, require_schema3=True
    )
    video = _packaged_path(bundle, "video.mp4", "video", must_exist=True)
    failures: list[str] = []
    if not qa.get("passed"):
        failures.append("automatic QA is not passed")
    if visual.get("result") != "pass" or not all(visual.get("checks", {}).values()):
        failures.append("visual/audio review is not passed")
    if qa.get("video_sha256") != sha256_file(video):
        failures.append("video hash changed after automatic QA")
    try:
        packaged_approval = verify_packaged_approval(
            _packaged_path(bundle, "project.json", "project.json", must_exist=True)
        )
        require_confirmed_rights(packaged_approval)
        packaged_provenance = read_json(
            _packaged_path(bundle, "provenance.json", "provenance", must_exist=True)
        )
        inputs = packaged_provenance.get("inputs", {})
        if packaged_config.get("version") == 2:
            packaged_contracts = packaged_provenance.get("contracts")
            manifest_record = read_json(
                _packaged_path(
                    bundle,
                    packaged_config["source_package"]["manifest"],
                    "source_package.manifest",
                    must_exist=True,
                )
            )
            plan_record = read_json(
                _packaged_path(
                    bundle, packaged_config["edit"]["plan"], "edit.plan", must_exist=True
                )
            )
            lock_record = read_json(
                _packaged_path(
                    bundle,
                    packaged_config["edit"]["timeline_lock"],
                    "edit.timeline_lock",
                    must_exist=True,
                )
            )
            prompt_record = read_json(
                _packaged_path(
                    bundle,
                    packaged_config["cover"]["prompt_record"],
                    "cover.prompt_record",
                    must_exist=True,
                )
            )
            if type(plan_record) is not dict or type(plan_record.get("scenes")) is not list:
                raise WorkflowError("CONTRACT_STALE", "Packaged edit plan is malformed")
            for scene in plan_record["scenes"]:
                if type(scene) is not dict:
                    raise WorkflowError("CONTRACT_STALE", "Packaged edit scene is malformed")
                card = scene.get("information_card")
                if card is not None:
                    if (
                        type(card) is not dict
                        or type(card.get("path")) is not str
                        or type(card.get("sha256")) is not str
                    ):
                        raise WorkflowError("CONTRACT_STALE", "Packaged information card is malformed")
                    card_path = _packaged_path(
                        bundle, card["path"], "information_card.path", must_exist=True
                    )
                    if sha256_file(card_path) != card["sha256"]:
                        raise WorkflowError("CONTRACT_STALE", "Packaged information card is stale")
            expected_contracts = {
                "source_package_sha256": manifest_record.get("package_sha256"),
                "edit_plan_sha256": sha256_bytes(canonical_json(plan_record)),
                "timeline_lock_sha256": sha256_bytes(canonical_json(lock_record)),
                "cover_prompt_sha256": sha256_bytes(canonical_json(prompt_record)),
            }
            if packaged_contracts != expected_contracts:
                raise WorkflowError("CONTRACT_STALE", "Packaged V2 contracts are inconsistent")
            if type(lock_record) is not dict or lock_record.get("alignment_sha256") != sha256_file(
                _packaged_path(bundle, "alignment.json", "alignment", must_exist=True)
            ):
                raise WorkflowError("CONTRACT_STALE", "Packaged timeline lock is stale")
            lock_scenes = lock_record.get("scenes")
            if type(lock_scenes) is not list or not lock_scenes or any(
                type(scene) is not dict or type(scene.get("timeline_end_frame")) is not int
                for scene in lock_scenes
            ):
                raise WorkflowError("CONTRACT_STALE", "Packaged timeline lock is malformed")
            locked_boundaries = [
                lock_scenes[0].get("timeline_start_frame"),
                *[
                    scene["timeline_end_frame"]
                    for scene in lock_scenes
                ],
            ]
            if locked_boundaries != packaged_provenance.get("timeline", {}).get(
                "target_boundaries_frames"
            ):
                raise WorkflowError("CONTRACT_STALE", "Packaged timeline boundaries are stale")
        required_assets = [
            {
                "asset_id": "base_video",
                "asset_sha256": inputs.get("base_sha256"),
                "source_id": "source-base-video",
            }
        ]
        if inputs.get("bgm_sha256"):
            required_assets.append(
                {
                    "asset_id": "bgm",
                    "asset_sha256": inputs["bgm_sha256"],
                    "source_id": "source-bgm",
                }
            )
        source_assets = inputs.get("source_assets", {})
        if type(source_assets) is not dict or any(
            type(asset_id) is not str or type(digest) is not str
            for asset_id, digest in source_assets.items()
        ):
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Packaged source assets are invalid")
        if packaged_config.get("version") == 2:
            packaged_assets = read_json(
                _packaged_path(
                    bundle, "source-package/assets.json", "source-package.assets", must_exist=True
                )
            )
            if type(packaged_assets) is not dict or type(packaged_assets.get("items")) is not list:
                raise WorkflowError("CONTRACT_STALE", "Packaged source assets are malformed")
            asset_index = {
                item["asset_id"]: {
                    "asset_sha256": item["sha256"],
                    "source_id": item.get("source_id"),
                }
                for item in packaged_assets.get("items", [])
                if type(item) is dict
                and type(item.get("asset_id")) is str
                and type(item.get("sha256")) is str
            }
            expected_source_assets = {
                asset_id: (
                    asset_index.get(asset_id, {}).get("asset_sha256")
                    if type(asset_index.get(asset_id)) is dict
                    else None
                )
                for scene in packaged_config["narration"]["scenes"]
                for asset_id in scene["asset_ids"]
            }
            if source_assets != expected_source_assets:
                raise WorkflowError("CONTRACT_STALE", "Packaged source asset hashes drifted")
            required_assets.extend(
                {
                    "asset_id": asset_id,
                    "asset_sha256": digest,
                    "source_id": asset_index[asset_id]["source_id"],
                }
                for asset_id, digest in source_assets.items()
            )
        else:
            required_assets.extend(
                {
                    "asset_id": asset_id,
                    "asset_sha256": digest,
                    "source_id": None,
                }
                for asset_id, digest in source_assets.items()
            )
        if any(
            type(item.get("asset_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["asset_sha256"]) is None
            for item in required_assets
        ):
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Packaged rights inputs are invalid")
        verify_rights_ledger(
            bundle,
            None,
            ledger_path=bundle / "private/rights-events.jsonl",
            required_asset_hashes=required_assets,
            require_schema2=packaged_config.get("version") == 2,
        )
    except (KeyError, IndexError, TypeError) as exc:
        failures.append("packaged review approval is invalid: BUNDLE_CONTRACT_INVALID")
    except WorkflowError as exc:
        failures.append(f"packaged review approval is invalid: {exc.code}")
    return {
        "bundle": bundle,
        "bundle_manifest": bundle_manifest,
        "build_manifest": build_manifest,
        "config": packaged_config,
        "qa": qa,
        "visual": visual,
        "video": video,
        "failures": failures,
    }


def verify_bundle(ffmpeg: Path, ffprobe: Path, bundle: Path) -> dict[str, Any]:
    checked = verify_bundle_contracts(bundle)
    bundle = checked["bundle"]
    qa = checked["qa"]
    video = checked["video"]
    failures = list(checked["failures"])
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


def workflow_status(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    project_root = config_path.parent
    missing: list[str] = []
    config: dict[str, Any] | None = None
    if not config_path.is_file():
        missing.append("project.json")
    else:
        try:
            raw = read_json(config_path)
            if type(raw) is not dict:
                missing.append("valid project.json")
            else:
                config = raw
        except Exception:
            missing.append("valid project.json")
    if config is not None:
        paths = config.get("paths") if type(config.get("paths")) is dict else {}
        base_raw = paths.get("base_video")
        try:
            base_exists = type(base_raw) is str and project_path(
                project_root, base_raw, must_exist=True
            ).is_file()
        except WorkflowError:
            base_exists = False
        if not base_exists:
            missing.append("clean base video")
        cover = config.get("cover") if type(config.get("cover")) is dict else {}
        for ratio in ("3x4", "4x3"):
            item = cover.get(ratio) if type(cover.get(ratio)) is dict else {}
            for field in ("generated_source", "final"):
                raw_path = item.get(field)
                try:
                    cover_exists = type(raw_path) is str and project_path(
                        project_root, raw_path, must_exist=True
                    ).is_file()
                except WorkflowError:
                    cover_exists = False
                if not cover_exists:
                    missing.append(f"cover {ratio} {field}")
        if config.get("version") == 2:
            try:
                validate_v2_contracts(config_path, config)
            except WorkflowError as exc:
                missing.append(f"valid V2 contracts ({exc.code})")
            except (KeyError, IndexError, TypeError):
                missing.append("valid V2 contracts (CONFIG_INVALID)")
    approval_path = project_root / "review" / "approval.json"
    if not approval_path.is_file():
        missing.append("publish approval")
    else:
        try:
            verify_approval(config_path)
        except WorkflowError as exc:
            missing.append(f"valid publish approval ({exc.code})")

    event_path = project_root / ".source-led-ai-video" / "events.jsonl"
    try:
        events = read_events(event_path, repair_tail=False)
    except Exception:
        events = []
        missing.append("readable production event log")
    stages = stage_summary(events)
    latest_build_stage = next(
        (item for item in reversed(stages) if item["stage"] == "build"), None
    )
    failed = (
        latest_build_stage
        if latest_build_stage is not None
        and latest_build_stage["status"] == "stage_failed"
        else None
    )
    review_root = project_root / "review-runs"
    runs = (
        sorted(
            (path for path in review_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if review_root.is_dir()
        else []
    )
    active_run: Path | None = None
    active_integrity: dict[str, Any] | None = None
    for candidate in runs:
        try:
            active_integrity = verify_build_integrity(config_path, candidate)
            active_run = candidate
            break
        except (WorkflowError, KeyError, IndexError, TypeError):
            continue
    visual_result = None
    if active_run is not None and active_integrity is not None:
        visual_path = active_run / "qa" / "visual-review.json"
        if visual_path.is_file():
            try:
                visual = verify_visual_record(
                    active_run,
                    active_integrity["manifest"],
                    require_schema3=True,
                )
                visual_result = visual.get("result")
            except (WorkflowError, KeyError, IndexError, TypeError):
                visual_result = "invalid"

    rights_status = "unchecked"
    rights_ledger_sha256: str | None = None
    if config is not None:
        try:
            validated, validated_root = load_and_validate_config(config_path)
            rights_result = verify_rights_ledger(validated_root, validated)
            rights_status = "ready"
            rights_ledger_sha256 = rights_result["ledger_sha256"]
        except WorkflowError as exc:
            rights_status = exc.code

    deliverable_latest = project_root / "deliverables" / "latest.json"
    latest_status = "missing"
    if deliverable_latest.exists():
        latest_status = "invalid"
        try:
            safe_latest = managed_path(
                project_root,
                deliverable_latest,
                must_exist=True,
                kind="file",
            )
            latest_record = read_json(safe_latest)
            if type(latest_record) is not dict:
                raise WorkflowError("FINALIZE_LATEST", "latest.json is invalid")
            latest_build_key = latest_record.get("build_key")
            latest_release_key = latest_record.get("release_key")
            if rights_ledger_sha256 is None:
                latest_status = "rights_not_ready"
            else:
                expected_release_key = release_key_for(
                    latest_build_key, rights_ledger_sha256
                )
                expected_bundle = f"deliverables/{latest_release_key}"
                if (
                    latest_release_key != expected_release_key
                    or latest_record.get("rights_ledger_sha256")
                    != rights_ledger_sha256
                    or latest_record.get("bundle") != expected_bundle
                ):
                    latest_status = "stale_rights_or_identity"
                elif active_run is None or active_run.name != latest_build_key:
                    latest_status = "superseded_build"
                else:
                    latest_bundle = managed_path(
                        project_root,
                        project_root / expected_bundle,
                        must_exist=True,
                        kind="dir",
                    )
                    checked_latest = verify_bundle_contracts(
                        latest_bundle,
                        expected_build_key=latest_build_key,
                        expected_release_key=latest_release_key,
                    )
                    latest_status = (
                        "ready" if not checked_latest["failures"] else "invalid"
                    )
        except Exception:
            latest_status = "invalid"

    if missing:
        next_action = "provide missing inputs: " + ", ".join(sorted(set(missing)))
    elif failed is not None:
        next_action = f"workflow.py resume --config {config_path}"
    elif rights_status != "ready":
        next_action = "record active Douyin commercial rights and evidence"
    elif latest_status == "ready":
        next_action = "verify the latest deliverable bundle"
    elif active_run is None:
        next_action = f"workflow.py run --config {config_path}"
    elif visual_result != "pass":
        next_action = "complete full visual/audio review with timestamped evidence"
    else:
        next_action = f"workflow.py finalize --config {config_path} --run-dir {active_run}"
    return {
        "schema": 1,
        "project": str(project_root),
        "missing_inputs": sorted(set(missing)),
        "stages": stages,
        "active_review_run": str(active_run) if active_run else None,
        "visual_result": visual_result,
        "rights_status": rights_status,
        "latest_status": latest_status,
        "latest_build_elapsed_seconds": (
            latest_build_stage.get("elapsed_seconds")
            if latest_build_stage is not None
            else None
        ),
        "next_action": next_action,
    }


def parse_evidence_frame(value: str) -> dict[str, Any]:
    raw_time, separator, label = value.partition(":")
    if not separator or not label.strip():
        raise argparse.ArgumentTypeError("evidence frame must be SECONDS:LABEL")
    try:
        seconds = float(raw_time)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("evidence timestamp must be numeric") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("evidence timestamp must be non-negative")
    return {"time_seconds": seconds, "label": normalize_text(label)}


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
    run.set_defaults(resume=False)
    resume = sub.add_parser("resume")
    resume.add_argument("--config", type=Path, required=True)
    resume.add_argument("--env-file", type=Path)
    resume.add_argument("--reuse-tts", type=Path)
    resume.add_argument("--reuse-tts-provenance", type=Path)
    resume.add_argument("--ffmpeg")
    resume.add_argument("--ffprobe")
    resume.add_argument("--aligner-python")
    resume.set_defaults(resume=True)
    status = sub.add_parser("status")
    status.add_argument("--config", type=Path, required=True)
    visual = sub.add_parser("confirm-visual")
    visual.add_argument("--config", type=Path, required=True)
    visual.add_argument("--run-dir", type=Path, required=True)
    visual.add_argument("--reviewer", required=True)
    visual.add_argument("--notes", required=True)
    visual.add_argument("--result", choices=("pass", "fail"), required=True)
    visual.add_argument("--failed-check", action="append", default=[])
    visual.add_argument("--listened-seconds", type=float)
    visual.add_argument("--video-seconds", type=float)
    visual.add_argument(
        "--evidence-frame", action="append", type=parse_evidence_frame, default=[]
    )
    finish = sub.add_parser("finalize")
    finish.add_argument("--config", type=Path, required=True)
    finish.add_argument("--run-dir", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--ffmpeg")
    verify.add_argument("--ffprobe")
    args = parser.parse_args()
    if args.command in {"run", "resume"}:
        result = run_build(args)
    elif args.command == "status":
        result = workflow_status(args.config)
    elif args.command == "confirm-visual":
        result = confirm_visual(
            args.config,
            args.run_dir,
            args.reviewer,
            args.notes,
            args.result,
            args.failed_check,
            listened_seconds=args.listened_seconds,
            video_seconds=args.video_seconds,
            evidence_frames=args.evidence_frame or None,
            strict_evidence=True,
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

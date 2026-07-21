#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lib.common import (
    WorkflowError,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    normalize_text,
    project_output_path,
    project_path,
    read_json,
    sha256_bytes,
    sha256_file,
)


PACKAGE_VERSION = 1
PACKAGE_FILES = ("claims.jsonl", "assets.json", "toolchain.json", "brief.md")
ID_PATTERNS = {
    "source": re.compile(r"source-[a-z0-9][a-z0-9-]{0,47}"),
    "claim": re.compile(r"claim-[a-z0-9][a-z0-9-]{0,47}"),
    "asset": re.compile(r"asset-[a-z0-9][a-z0-9-]{0,47}"),
    "step": re.compile(r"step-[a-z0-9][a-z0-9-]{0,47}"),
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise WorkflowError("SOURCE_PACKAGE_TYPE", f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not normalize_text(value):
        raise WorkflowError("SOURCE_PACKAGE_TYPE", f"{label} must be non-empty text")
    return value


def _version(value: Any, label: str) -> None:
    if type(value) is not int or value != PACKAGE_VERSION:
        raise WorkflowError(
            "SOURCE_PACKAGE_VERSION", f"{label} must use version {PACKAGE_VERSION}"
        )


def _identifier(value: Any, kind: str, label: str) -> str:
    text = _text(value, label)
    if ID_PATTERNS[kind].fullmatch(text) is None:
        raise WorkflowError(
            "SOURCE_PACKAGE_ID", f"{label} must use a lowercase {kind}- identifier"
        )
    return text


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise WorkflowError("SOURCE_PACKAGE_TIME", f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise WorkflowError("SOURCE_PACKAGE_TIME", f"{label} must include a timezone")
    return text


def _url(value: Any, label: str) -> str:
    text = _text(value, label)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkflowError("SOURCE_PACKAGE_URL", f"{label} must be an http(s) URL")
    return text


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise WorkflowError("SOURCE_PACKAGE_HASH", f"{label} must be lowercase SHA-256")
    return text


def _package_location(config_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    resolved_config = config_path.expanduser().resolve()
    config = read_json(resolved_config)
    if type(config) is not dict or config.get("version") != 2:
        raise WorkflowError("SOURCE_PACKAGE_CONFIG", "Source Package requires project version 2")
    source_package = _mapping(config.get("source_package"), "source_package")
    raw_manifest = _text(source_package.get("manifest"), "source_package.manifest")
    if raw_manifest != "source-package/manifest.json":
        raise WorkflowError(
            "SOURCE_PACKAGE_CONFIG",
            "source_package.manifest must be source-package/manifest.json",
        )
    project_root = resolved_config.parent
    manifest_path = project_output_path(project_root, project_root / raw_manifest)
    return project_root, manifest_path, config


def _regular_input(project_root: Path, raw_path: Any, label: str) -> Path:
    text = _text(raw_path, label)
    project_path(project_root, text, must_exist=True)
    path = project_output_path(project_root, project_root / text)
    try:
        stat_result = os.lstat(path)
    except OSError as exc:
        raise WorkflowError("SOURCE_PACKAGE_PATH", f"Cannot inspect {label}") from exc
    if not path.is_file() or path.is_symlink():
        raise WorkflowError("SOURCE_PACKAGE_PATH", f"{label} must be a regular project file")
    return path


def _validate_manifest(
    project_root: Path, manifest: Any, *, require_package_hash: bool
) -> tuple[dict[str, Any], set[str]]:
    value = _mapping(manifest, "manifest")
    _version(value.get("version"), "manifest")
    if value.get("source_type") not in {"x", "github", "builder", "prepared"}:
        raise WorkflowError(
            "SOURCE_PACKAGE_TYPE", "manifest.source_type must be x, github, builder, or prepared"
        )
    _url(value.get("source_url"), "manifest.source_url")
    _timestamp(value.get("captured_at"), "manifest.captured_at")
    _text(value.get("analyzer_version"), "manifest.analyzer_version")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise WorkflowError("SOURCE_PACKAGE_VERSION", "manifest.schema_version must be 1")
    revision = value.get("revision")
    if revision is not None and type(revision) is not dict:
        raise WorkflowError("SOURCE_PACKAGE_TYPE", "manifest.revision must be an object")

    sources = value.get("sources")
    if type(sources) is not list or not sources:
        raise WorkflowError("SOURCE_PACKAGE_SOURCE", "manifest.sources cannot be empty")
    source_ids: set[str] = set()
    for index, raw_source in enumerate(sources):
        source = _mapping(raw_source, f"manifest.sources[{index}]")
        source_id = _identifier(source.get("source_id"), "source", "source_id")
        if source_id in source_ids:
            raise WorkflowError("SOURCE_PACKAGE_ID", f"Duplicate source_id: {source_id}")
        source_ids.add(source_id)
        if source.get("kind") not in {
            "x_post",
            "x_reply",
            "github_readme",
            "github_release",
            "documentation",
            "project_page",
            "prepared_note",
        }:
            raise WorkflowError("SOURCE_PACKAGE_SOURCE", f"Invalid source kind for {source_id}")
        _url(source.get("url"), f"{source_id}.url")
        _timestamp(source.get("captured_at"), f"{source_id}.captured_at")
        content_path = _regular_input(
            project_root, source.get("content_path"), f"{source_id}.content_path"
        )
        expected_hash = _sha256(source.get("content_sha256"), f"{source_id}.content_sha256")
        if sha256_file(content_path) != expected_hash:
            raise WorkflowError(
                "SOURCE_PACKAGE_HASH", f"Source snapshot hash mismatch: {source_id}"
            )
    if require_package_hash:
        _sha256(value.get("package_sha256"), "manifest.package_sha256")
    elif "package_sha256" in value:
        raise WorkflowError(
            "SOURCE_PACKAGE_HASH", "Draft manifest must not provide package_sha256"
        )
    return value, source_ids


def _validate_claims(claims: Any, source_ids: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
    if type(claims) is not list or not claims:
        raise WorkflowError("SOURCE_PACKAGE_CLAIM", "claims cannot be empty")
    result: list[dict[str, Any]] = []
    claim_ids: set[str] = set()
    for index, raw_claim in enumerate(claims):
        claim = _mapping(raw_claim, f"claims[{index}]")
        claim_id = _identifier(claim.get("claim_id"), "claim", "claim_id")
        if claim_id in claim_ids:
            raise WorkflowError("SOURCE_PACKAGE_ID", f"Duplicate claim_id: {claim_id}")
        claim_ids.add(claim_id)
        _text(claim.get("statement"), f"{claim_id}.statement")
        if claim.get("classification") not in {
            "author_statement",
            "corroborated_fact",
            "editorial_inference",
            "unverified",
        }:
            raise WorkflowError("SOURCE_PACKAGE_CLAIM", f"Invalid classification: {claim_id}")
        if claim.get("confidence") not in {"high", "medium", "low"}:
            raise WorkflowError("SOURCE_PACKAGE_CLAIM", f"Invalid confidence: {claim_id}")
        _timestamp(claim.get("verified_at"), f"{claim_id}.verified_at")
        evidence = claim.get("evidence")
        if type(evidence) is not list or not evidence:
            raise WorkflowError(
                "SOURCE_PACKAGE_EVIDENCE", f"{claim_id} must cite at least one source"
            )
        cited_sources: set[str] = set()
        for evidence_index, raw_evidence in enumerate(evidence):
            item = _mapping(raw_evidence, f"{claim_id}.evidence[{evidence_index}]")
            source_id = _identifier(item.get("source_id"), "source", "evidence.source_id")
            if source_id not in source_ids:
                raise WorkflowError(
                    "SOURCE_PACKAGE_EVIDENCE",
                    f"{claim_id} cites unknown source_id: {source_id}",
                )
            cited_sources.add(source_id)
            _text(item.get("locator"), f"{claim_id}.evidence.locator")
            if "excerpt_sha256" in item:
                _sha256(item["excerpt_sha256"], f"{claim_id}.evidence.excerpt_sha256")
        if claim.get("classification") == "corroborated_fact" and len(cited_sources) < 2:
            raise WorkflowError(
                "SOURCE_PACKAGE_EVIDENCE",
                f"Corroborated claim {claim_id} needs two independent sources",
            )
        result.append(claim)
    return result, claim_ids


def _validate_assets(
    project_root: Path, assets: Any, source_ids: set[str]
) -> tuple[dict[str, Any], set[str]]:
    value = _mapping(assets, "assets")
    _version(value.get("version"), "assets")
    items = value.get("items")
    if type(items) is not list or not items:
        raise WorkflowError("SOURCE_PACKAGE_ASSET", "assets.items cannot be empty")
    asset_ids: set[str] = set()
    for index, raw_asset in enumerate(items):
        asset = _mapping(raw_asset, f"assets.items[{index}]")
        asset_id = _identifier(asset.get("asset_id"), "asset", "asset_id")
        if asset_id in asset_ids:
            raise WorkflowError("SOURCE_PACKAGE_ID", f"Duplicate asset_id: {asset_id}")
        asset_ids.add(asset_id)
        if asset.get("kind") not in {"video", "image", "audio", "document"}:
            raise WorkflowError("SOURCE_PACKAGE_ASSET", f"Invalid asset kind: {asset_id}")
        source_id = _identifier(asset.get("source_id"), "source", f"{asset_id}.source_id")
        if source_id not in source_ids:
            raise WorkflowError(
                "SOURCE_PACKAGE_EVIDENCE", f"{asset_id} cites unknown source_id: {source_id}"
            )
        local_path = _regular_input(
            project_root, asset.get("local_path"), f"{asset_id}.local_path"
        )
        expected_hash = _sha256(asset.get("sha256"), f"{asset_id}.sha256")
        if sha256_file(local_path) != expected_hash:
            raise WorkflowError("SOURCE_PACKAGE_HASH", f"Asset hash mismatch: {asset_id}")
        if asset.get("rights_status") not in {"pending", "confirmed", "rejected"}:
            raise WorkflowError(
                "SOURCE_PACKAGE_ASSET", f"Invalid rights_status: {asset_id}"
            )
        if asset.get("kind") == "video":
            frame_count = asset.get("frame_count")
            if type(frame_count) is not int or frame_count <= 0:
                raise WorkflowError(
                    "SOURCE_PACKAGE_ASSET", f"{asset_id}.frame_count must be a positive integer"
                )
        if "width" in asset and (type(asset["width"]) is not int or asset["width"] <= 0):
            raise WorkflowError("SOURCE_PACKAGE_ASSET", f"Invalid width: {asset_id}")
        if "height" in asset and (type(asset["height"]) is not int or asset["height"] <= 0):
            raise WorkflowError("SOURCE_PACKAGE_ASSET", f"Invalid height: {asset_id}")
    return value, asset_ids


def _validate_toolchain(toolchain: Any, claim_ids: set[str]) -> dict[str, Any]:
    value = _mapping(toolchain, "toolchain")
    _version(value.get("version"), "toolchain")
    steps = value.get("steps")
    if type(steps) is not list or not steps:
        raise WorkflowError("SOURCE_PACKAGE_TOOLCHAIN", "toolchain.steps cannot be empty")
    step_ids: set[str] = set()
    for index, raw_step in enumerate(steps):
        step = _mapping(raw_step, f"toolchain.steps[{index}]")
        step_id = _identifier(step.get("step_id"), "step", "step_id")
        if step_id in step_ids:
            raise WorkflowError("SOURCE_PACKAGE_ID", f"Duplicate step_id: {step_id}")
        step_ids.add(step_id)
        _text(step.get("tool"), f"{step_id}.tool")
        _text(step.get("action"), f"{step_id}.action")
        references = step.get("claim_ids")
        if type(references) is not list or not references:
            raise WorkflowError(
                "SOURCE_PACKAGE_TOOLCHAIN", f"{step_id}.claim_ids cannot be empty"
            )
        for claim_id in references:
            _identifier(claim_id, "claim", f"{step_id}.claim_ids")
            if claim_id not in claim_ids:
                raise WorkflowError(
                    "SOURCE_PACKAGE_EVIDENCE", f"{step_id} cites unknown claim_id: {claim_id}"
                )
    return value


def _package_hash(
    manifest: dict[str, Any],
    claims: list[dict[str, Any]],
    assets: dict[str, Any],
    toolchain: dict[str, Any],
    brief: str,
) -> str:
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("package_sha256", None)
    payload = {
        "manifest": manifest_without_hash,
        "claims": claims,
        "assets": assets,
        "toolchain": toolchain,
        "brief_sha256": sha256_bytes(brief.encode("utf-8")),
    }
    return sha256_bytes(canonical_json(payload))


def _validate_payload(
    project_root: Path, payload: Any, *, require_package_hash: bool
) -> dict[str, Any]:
    value = _mapping(payload, "source package")
    manifest, source_ids = _validate_manifest(
        project_root, value.get("manifest"), require_package_hash=require_package_hash
    )
    claims, claim_ids = _validate_claims(value.get("claims"), source_ids)
    assets, asset_ids = _validate_assets(project_root, value.get("assets"), source_ids)
    toolchain = _validate_toolchain(value.get("toolchain"), claim_ids)
    brief = _text(value.get("brief"), "brief").rstrip()
    if require_package_hash:
        computed = _package_hash(manifest, claims, assets, toolchain, brief)
        if manifest["package_sha256"] != computed:
            raise WorkflowError("SOURCE_PACKAGE_HASH", "Source Package hash mismatch")
    return {
        "manifest": manifest,
        "claims": claims,
        "assets": assets,
        "toolchain": toolchain,
        "brief": brief,
        "source_ids": sorted(source_ids),
        "claim_ids": sorted(claim_ids),
        "asset_ids": sorted(asset_ids),
    }


def _validate_project_bindings(
    config: dict[str, Any], package: dict[str, Any]
) -> None:
    """Bind every public scene assertion and source asset to the package.

    A package may retain unused research, but a narration scene cannot cite an
    unknown, unverified, or rejected item. This is the bridge between the
    evidence cache and the production configuration.
    """
    claims = {item["claim_id"]: item for item in package["claims"]}
    assets = {
        item["asset_id"]: item for item in package["assets"]["items"]
    }
    for scene in config["narration"]["scenes"]:
        scene_id = scene["id"]
        for claim_id in scene["claim_ids"]:
            claim = claims.get(claim_id)
            if claim is None:
                raise WorkflowError(
                    "SOURCE_PACKAGE_BINDING",
                    f"Scene {scene_id} cites unknown claim: {claim_id}",
                )
            if claim["classification"] == "unverified":
                raise WorkflowError(
                    "SOURCE_PACKAGE_UNVERIFIED",
                    f"Scene {scene_id} cannot publish unverified claim: {claim_id}",
                )
        for asset_id in scene["asset_ids"]:
            asset = assets.get(asset_id)
            if asset is None:
                raise WorkflowError(
                    "SOURCE_PACKAGE_BINDING",
                    f"Scene {scene_id} cites unknown asset: {asset_id}",
                )
            if asset["rights_status"] == "rejected":
                raise WorkflowError(
                    "SOURCE_PACKAGE_RIGHTS",
                    f"Scene {scene_id} cannot use rejected asset: {asset_id}",
                )


def _claims_text(claims: list[dict[str, Any]]) -> str:
    return "".join(canonical_json(claim).decode("utf-8") + "\n" for claim in claims)


def write_source_package(config_path: Path, draft: Any) -> dict[str, Any]:
    project_root, manifest_path, config = _package_location(config_path)
    checked = _validate_payload(project_root, draft, require_package_hash=False)
    _validate_project_bindings(config, checked)
    manifest = dict(checked["manifest"])
    manifest["package_sha256"] = _package_hash(
        manifest,
        checked["claims"],
        checked["assets"],
        checked["toolchain"],
        checked["brief"],
    )
    package_dir = manifest_path.parent
    targets = {
        "claims": project_output_path(project_root, package_dir / "claims.jsonl"),
        "assets": project_output_path(project_root, package_dir / "assets.json"),
        "toolchain": project_output_path(project_root, package_dir / "toolchain.json"),
        "brief": project_output_path(project_root, package_dir / "brief.md"),
        "manifest": project_output_path(project_root, manifest_path),
    }
    # Manifest is the commit marker. A crash before the final replace yields a
    # hash-invalid package instead of a false cache hit.
    atomic_write_text(targets["claims"], _claims_text(checked["claims"]))
    atomic_write_json(targets["assets"], checked["assets"])
    atomic_write_json(targets["toolchain"], checked["toolchain"])
    atomic_write_text(targets["brief"], checked["brief"].rstrip() + "\n")
    atomic_write_json(targets["manifest"], manifest)
    return validate_source_package(config_path)


def _read_claims(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkflowError("SOURCE_PACKAGE_READ", "Cannot read claims.jsonl") from exc
    claims: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise WorkflowError(
                "SOURCE_PACKAGE_READ", f"claims.jsonl has a blank line at {line_number}"
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                "SOURCE_PACKAGE_READ", f"Invalid claims.jsonl line {line_number}"
            ) from exc
        claims.append(value)
    return claims


def validate_source_package(config_path: Path) -> dict[str, Any]:
    project_root, manifest_path, config = _package_location(config_path)
    package_dir = manifest_path.parent
    required = {
        "manifest": manifest_path,
        "claims": package_dir / "claims.jsonl",
        "assets": package_dir / "assets.json",
        "toolchain": package_dir / "toolchain.json",
        "brief": package_dir / "brief.md",
    }
    safe_paths: dict[str, Path] = {}
    for label, candidate in required.items():
        safe = project_output_path(project_root, candidate)
        if not safe.is_file():
            raise WorkflowError("SOURCE_PACKAGE_MISSING", f"Missing Source Package file: {label}")
        safe_paths[label] = safe
    try:
        brief = safe_paths["brief"].read_text(encoding="utf-8").rstrip("\n")
    except OSError as exc:
        raise WorkflowError("SOURCE_PACKAGE_READ", "Cannot read brief.md") from exc
    payload = {
        "manifest": read_json(safe_paths["manifest"]),
        "claims": _read_claims(safe_paths["claims"]),
        "assets": read_json(safe_paths["assets"]),
        "toolchain": read_json(safe_paths["toolchain"]),
        "brief": brief,
    }
    checked = _validate_payload(project_root, payload, require_package_hash=True)
    _validate_project_bindings(config, checked)
    return checked


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Write or validate a V2 Source Package")
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--config", type=Path, required=True)
    write_parser.add_argument("--draft", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "write":
        result = write_source_package(args.config, read_json(args.draft))
    else:
        result = validate_source_package(args.config)
    print(json.dumps({key: result[key] for key in ("source_ids", "claim_ids", "asset_ids")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.common import (
    WorkflowError,
    load_and_validate_config,
    normalize_text,
    project_output_path,
    project_path,
    sha256_file,
)
from lib.telemetry import TelemetryError, append_identity_record
from source_package import validate_source_package


def _asset(
    config_path: Path, asset_id: str
) -> tuple[dict[str, Any], Path, str, str]:
    config, project_root = load_and_validate_config(config_path)
    if asset_id == "base_video":
        path = project_path(project_root, config["paths"]["base_video"], must_exist=True)
        expected_source_id = "source-base-video"
    elif asset_id == "bgm":
        raw = config["paths"].get("bgm")
        if not raw:
            raise WorkflowError("RIGHTS_ASSET", "This project has no BGM")
        path = project_path(project_root, raw, must_exist=True)
        expected_source_id = "source-bgm"
    elif config.get("version") == 2:
        package = validate_source_package(config_path)
        assets = {
            item["asset_id"]: item for item in package["assets"]["items"]
        }
        if asset_id not in assets:
            raise WorkflowError("RIGHTS_ASSET", "Unknown Source Package asset")
        path = project_path(
            project_root, assets[asset_id]["local_path"], must_exist=True
        )
        expected_source_id = assets[asset_id]["source_id"]
    else:
        raise WorkflowError("RIGHTS_ASSET", "Unknown project asset")
    return config, path, sha256_file(path), expected_source_id


def _validate_optional_timestamp(value: str | None) -> None:
    if value is None:
        return
    if not value:
        raise WorkflowError(
            "RIGHTS_LEDGER_INVALID", "Rights expiry must be an ISO timestamp or null"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkflowError(
            "RIGHTS_LEDGER_INVALID", "Rights expiry must include a timezone"
        )


def _append(project_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    ledger = project_output_path(
        project_root, project_root / "private" / "rights-events.jsonl"
    )
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = project_output_path(project_root, ledger)
    try:
        return append_identity_record(ledger, record)
    except TelemetryError as exc:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights ledger is unsafe") from exc


def confirm(
    config_path: Path,
    *,
    asset_id: str,
    source_id: str,
    author: str,
    source: str,
    rights_basis: str,
    evidence_path: str,
    confirmed_by: str,
    expires_at: str | None,
    attribution_required: bool,
    attribution_placement: str,
    attribution_text: str,
) -> dict[str, Any]:
    config, path, digest, expected_source_id = _asset(config_path, asset_id)
    project_root = config_path.expanduser().resolve().parent
    evidence = project_path(project_root, evidence_path, must_exist=True)
    if not evidence_path.startswith("private/rights-evidence/"):
        raise WorkflowError(
            "RIGHTS_EVIDENCE", "Evidence must be inside private/rights-evidence"
        )
    clean = {
        name: normalize_text(value)
        for name, value in {
            "asset_id": asset_id,
            "source_id": source_id,
            "author": author,
            "source": source,
            "rights_basis": rights_basis,
            "confirmed_by": confirmed_by,
            "attribution_placement": attribution_placement,
            "attribution_text": attribution_text,
        }.items()
    }
    if any(not clean[name] for name in ("asset_id", "source_id", "author", "source", "rights_basis", "confirmed_by")):
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Rights metadata is incomplete")
    if clean["source_id"] != expected_source_id:
        raise WorkflowError(
            "RIGHTS_SOURCE_ID",
            f"Rights source ID for {clean['asset_id']} must be {expected_source_id}",
        )
    _validate_optional_timestamp(expires_at)
    if attribution_required:
        if clean["attribution_placement"] not in {
            "body",
            "pinned_comment",
            "body_or_pinned_comment",
        } or not clean["attribution_text"]:
            raise WorkflowError("RIGHTS_LEDGER_INVALID", "Attribution plan is incomplete")
    elif clean["attribution_placement"] != "none" or clean["attribution_text"]:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "No-attribution plan is contradictory")
    record = {
        "schema": 2,
        "event": "rights_confirmed",
        "asset_id": clean["asset_id"],
        "asset_sha256": digest,
        "source_id": clean["source_id"],
        "author": clean["author"],
        "source": clean["source"],
        "platforms": ["douyin"],
        "regions": ["CN"],
        "commercial": True,
        "expires_at": expires_at,
        "rights_basis": clean["rights_basis"],
        "attribution": {
            "required": attribution_required,
            "placement": clean["attribution_placement"],
            "text": clean["attribution_text"],
        },
        "evidence_path": evidence_path,
        "evidence_sha256": sha256_file(evidence),
        "confirmed_by": clean["confirmed_by"],
        # Microseconds distinguish a legitimate re-confirmation immediately after
        # revocation; replaying the exact record remains content-idempotent in the
        # append-only ledger.
        "confirmed_at": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    return _append(project_root, record)


def revoke(
    config_path: Path, *, asset_id: str, reason: str, confirmed_by: str
) -> dict[str, Any]:
    _, _, digest, _ = _asset(config_path, asset_id)
    project_root = config_path.expanduser().resolve().parent
    clean_reason = normalize_text(reason)
    clean_reviewer = normalize_text(confirmed_by)
    if not clean_reason or not clean_reviewer:
        raise WorkflowError("RIGHTS_LEDGER_INVALID", "Revocation metadata is incomplete")
    return _append(
        project_root,
        {
            "schema": 2,
            "event": "rights_revoked",
            "asset_id": asset_id,
            "asset_sha256": digest,
            "revoked_at": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "reason": clean_reason,
            "confirmed_by": clean_reviewer,
        },
    )


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("confirm")
    add.add_argument("--config", type=Path, required=True)
    add.add_argument("--asset-id", required=True)
    add.add_argument("--source-id", required=True)
    add.add_argument("--author", required=True)
    add.add_argument("--source", required=True)
    add.add_argument("--rights-basis", required=True)
    add.add_argument("--evidence-path", required=True)
    add.add_argument("--confirmed-by", required=True)
    add.add_argument("--expires-at")
    add.add_argument(
        "--attribution-placement",
        choices=("none", "body", "pinned_comment", "body_or_pinned_comment"),
        required=True,
    )
    add.add_argument("--attribution-text", default="")
    revoke_parser = sub.add_parser("revoke")
    revoke_parser.add_argument("--config", type=Path, required=True)
    revoke_parser.add_argument("--asset-id", required=True)
    revoke_parser.add_argument("--reason", required=True)
    revoke_parser.add_argument("--confirmed-by", required=True)
    args = parser.parse_args()
    if args.command == "confirm":
        result = confirm(
            args.config,
            asset_id=args.asset_id,
            source_id=args.source_id,
            author=args.author,
            source=args.source,
            rights_basis=args.rights_basis,
            evidence_path=args.evidence_path,
            confirmed_by=args.confirmed_by,
            expires_at=args.expires_at,
            attribution_required=args.attribution_placement != "none",
            attribution_placement=args.attribution_placement,
            attribution_text=args.attribution_text,
        )
    else:
        result = revoke(
            args.config,
            asset_id=args.asset_id,
            reason=args.reason,
            confirmed_by=args.confirmed_by,
        )
    print(
        json.dumps(
            {
                "event": result["event"],
                "asset_sha256": result["asset_sha256"],
                "recorded_at": result["recorded_at"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

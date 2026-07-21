#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from edit_plan import initialize_cover_prompt
from lib.common import SKILL_ROOT, WorkflowError, atomic_write_json, read_json


def _legacy_v1(template: dict) -> dict:
    template["version"] = 1
    template.pop("source_package", None)
    template.pop("edit", None)
    template.get("cover", {}).pop("prompt_record", None)
    for scene in template.get("narration", {}).get("scenes", []):
        for field in ("purpose", "claim_ids", "asset_ids", "caption_region"):
            scene.pop(field, None)
    return template


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--project-version", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    config_path = root / "project.json"
    if config_path.exists():
        raise WorkflowError("INIT_EXISTS", "project.json already exists")
    for relative in (
        "source",
        "source-package/raw",
        "edit",
        "covers/source",
        "covers/final",
        "review",
        "private",
        ".source-led-ai-video/analytics/imports",
        "review-runs",
        "deliverables",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    template = read_json(SKILL_ROOT / "assets/project-template.json")
    if args.project_version == 1:
        template = _legacy_v1(template)
    template["project_name"] = args.name.strip()
    atomic_write_json(config_path, template)
    if args.project_version == 2:
        atomic_write_json(
            root / "source-package-draft.json",
            read_json(SKILL_ROOT / "assets/source-package-draft.example.json"),
        )
        initialize_cover_prompt(config_path)
    print(config_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

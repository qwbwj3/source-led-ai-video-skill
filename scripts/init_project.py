#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from lib.common import SKILL_ROOT, WorkflowError, atomic_write_json, read_json


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    config_path = root / "project.json"
    if config_path.exists():
        raise WorkflowError("INIT_EXISTS", "project.json already exists")
    for relative in (
        "source",
        "covers/source",
        "review",
        "review-runs",
        "deliverables",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    template = read_json(SKILL_ROOT / "assets/project-template.json")
    template["project_name"] = args.name.strip()
    atomic_write_json(config_path, template)
    print(config_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2)

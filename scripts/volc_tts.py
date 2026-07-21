#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import review_gate
from lib.common import (
    WorkflowError,
    load_and_validate_config,
    load_env_file,
    narration_hash,
    project_output_path,
    redact,
    runtime_root,
)
from lib.volc_tts import synthesize


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    config_path = args.config.resolve()
    approval = review_gate.verify_approval(config_path)
    config, project_root = load_and_validate_config(config_path)
    if approval.get("narration_sha256") != narration_hash(config):
        raise WorkflowError("REVIEW_STALE", "TTS blocked: narration changed after approval")
    output = project_output_path(project_root, args.out)
    env = load_env_file(args.env_file)
    result = synthesize(config_path, output, env, runtime_root())
    result["output"] = str(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

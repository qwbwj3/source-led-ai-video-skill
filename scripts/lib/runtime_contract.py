from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .common import (
    LOCKED_ALIGNER_PYTHON_VERSION,
    SKILL_ROOT,
    WorkflowError,
    canonical_json,
    sha256_file,
)


REQUIREMENTS_PATH = SKILL_ROOT / "scripts/requirements-lock.txt"


def locked_requirements() -> dict[str, str]:
    requirements: dict[str, str] = {}
    try:
        lines = REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkflowError("RUNTIME_LOCK", "Cannot read the aligner requirement lock") from exc
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", value)
        if not match:
            raise WorkflowError("RUNTIME_LOCK", "Aligner requirements must use exact versions")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if name in requirements:
            raise WorkflowError("RUNTIME_LOCK", "Aligner requirements contain a duplicate package")
        requirements[name] = match.group(2)
    if not requirements:
        raise WorkflowError("RUNTIME_LOCK", "Aligner requirement lock is empty")
    return dict(sorted(requirements.items()))


def aligner_runtime_fingerprint(python: Path) -> dict[str, Any]:
    python = Path(python)
    if not (python.is_file() and os.access(python, os.X_OK)):
        raise WorkflowError("RUNTIME_ALIGNER", "Aligner Python is missing or not executable")
    expected = locked_requirements()
    probe_code = r'''
import importlib.metadata
import json
import platform
import re
import sys

expected = json.loads(sys.argv[1])
installed = {}
for distribution in importlib.metadata.distributions():
    raw = distribution.metadata.get("Name") or ""
    if not raw:
        continue
    name = re.sub(r"[-_.]+", "-", raw).lower()
    installed[name] = distribution.version
required = {name: installed.get(name) for name in expected}
print(json.dumps({
    "python": platform.python_version(),
    "python_implementation": platform.python_implementation(),
    "machine": platform.machine(),
    "required": required,
    "installed": dict(sorted(installed.items())),
}, sort_keys=True, separators=(",", ":")))
'''
    try:
        result = subprocess.run(
            [str(python), "-c", probe_code, json.dumps(expected, sort_keys=True)],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("RUNTIME_ALIGNER", "Could not inspect the aligner runtime") from exc
    if result.returncode != 0:
        raise WorkflowError("RUNTIME_ALIGNER", "Could not inspect the aligner runtime")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError("RUNTIME_ALIGNER", "Aligner runtime returned invalid metadata") from exc
    if (
        not isinstance(data, dict)
        or data.get("python_implementation") != "CPython"
        or data.get("machine") != "arm64"
        or data.get("python") != LOCKED_ALIGNER_PYTHON_VERSION
        or data.get("required") != expected
        or not isinstance(data.get("installed"), dict)
    ):
        raise WorkflowError("RUNTIME_ALIGNER", "Aligner runtime does not match the locked contract")
    fingerprint = {
        "schema": 1,
        "python": data["python"],
        "python_implementation": data["python_implementation"],
        "machine": data["machine"],
        "requirements_sha256": sha256_file(REQUIREMENTS_PATH),
        "required": expected,
        "installed_sha256": hashlib.sha256(
            canonical_json(data["installed"])
        ).hexdigest(),
    }
    return fingerprint | {
        "fingerprint_sha256": hashlib.sha256(canonical_json(fingerprint)).hexdigest()
    }

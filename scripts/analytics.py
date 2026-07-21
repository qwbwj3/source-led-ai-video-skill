#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from lib.common import WorkflowError, ensure_managed_dir, normalize_text, redact
from lib.telemetry import TelemetryError, append_identity_record


PLATFORM_METRICS = (
    "views",
    "retention_2s",
    "retention_5s",
    "average_watch_seconds",
    "completion_rate",
    "likes",
    "comments",
    "shares",
    "favorites",
    "follows",
    "cover_impressions",
    "cover_clicks",
    "cover_ctr",
)
RATE_METRICS = frozenset(
    {"retention_2s", "retention_5s", "completion_rate", "cover_ctr"}
)
COUNT_METRICS = frozenset(
    {
        "views",
        "likes",
        "comments",
        "shares",
        "favorites",
        "follows",
        "cover_impressions",
        "cover_clicks",
    }
)
TEXT_LIMITS = {
    "model": 100,
    "stage": 64,
    "publication_id": 80,
    "feedback": 500,
    "decision": 240,
}
BUILD_KEY_PATTERN = re.compile(r"[0-9a-f]{20}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COVER_VARIANTS = frozenset({"3x4", "4x3", "video-frame"})
SENSITIVE_TEXT_PATTERNS = (
    (
        "URL",
        re.compile(
            r"(?i)(?:https?://|www\.|(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|ai|dev|app)(?:/|\b))"
        ),
    ),
    (
        "email",
        re.compile(
            r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b"
        ),
    ),
    (
        "phone number",
        re.compile(r"(?<!\d)(?:\+?86[-.\s]?)?1[3-9](?:[-.\s]?\d){9}(?!\d)"),
    ),
    ("identity number", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("account number", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    (
        "secret-like value",
        re.compile(
            r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
        ),
    ),
    (
        "secret-like value",
        re.compile(r"(?i)\b(?:sk|ak|token|secret)[-_][a-z0-9_-]{12,}\b"),
    ),
    (
        "secret-like value",
        re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}\b"),
    ),
    (
        "secret-like value",
        re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}(?:\.[a-zA-Z0-9_-]{8,})?\b"),
    ),
)


def _safe_text(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str:
        raise WorkflowError("ANALYTICS_TEXT", f"{label} must be text")
    clean = normalize_text(value)
    if not clean:
        raise WorkflowError("ANALYTICS_TEXT", f"{label} is required")
    if len(clean) > TEXT_LIMITS[label]:
        raise WorkflowError("ANALYTICS_TEXT", f"{label} is too long")
    for description, pattern in SENSITIVE_TEXT_PATTERNS:
        if pattern.search(clean):
            raise WorkflowError(
                "ANALYTICS_SENSITIVE", f"{label} contains a {description}"
            )
    return clean


def _nullable_integer(raw: str) -> int | None:
    if raw.casefold() == "null":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer or null") from exc


def _analytics_path(project_root: Path) -> Path:
    root = project_root.resolve()
    if not root.is_dir():
        raise WorkflowError("ANALYTICS_PROJECT", "Project directory is missing")
    control = ensure_managed_dir(root, ".source-led-ai-video")
    analytics = ensure_managed_dir(root, control / "analytics")
    return analytics / "events.jsonl"


def _append(project_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = _analytics_path(project_root)
    identity = {"schema": 1, **payload}
    try:
        # append_identity_record owns the lock and opens both lock/log leaves
        # with O_NOFOLLOW plus regular-file checks.
        return append_identity_record(path, identity)
    except TelemetryError as exc:
        raise WorkflowError("ANALYTICS_LOG", "Analytics log cannot be written safely") from exc


def record_model_usage(
    project_root: Path,
    *,
    model: str,
    stage: str,
    input_tokens: int | None,
    output_tokens: int | None,
    processed_tokens: int | None,
) -> dict[str, Any]:
    clean_model = _safe_text(model, "model")
    clean_stage = _safe_text(stage, "stage")
    values = (input_tokens, output_tokens, processed_tokens)
    if any(
        value is not None
        and (type(value) is not int or isinstance(value, bool) or value < 0)
        for value in values
    ):
        raise WorkflowError(
            "ANALYTICS_TOKENS", "Token counts must be non-negative integers or null"
        )
    if (
        processed_tokens is not None
        and input_tokens is not None
        and output_tokens is not None
        and processed_tokens < input_tokens + output_tokens
    ):
        raise WorkflowError(
            "ANALYTICS_TOKENS", "Processed tokens cannot be below input plus output"
        )
    return _append(
        project_root,
        {
            "event": "model_usage",
            "model": clean_model,
            "stage": clean_stage,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "processed_tokens": processed_tokens,
        },
    )


def record_platform_snapshot(
    project_root: Path,
    *,
    publication_id: str,
    build_key: str,
    video_sha256: str,
    cover_variant: str,
    window: str,
    metrics: dict[str, int | float | None],
) -> dict[str, Any]:
    clean_publication = _safe_text(publication_id, "publication_id")
    if type(build_key) is not str or BUILD_KEY_PATTERN.fullmatch(build_key) is None:
        raise WorkflowError("ANALYTICS_IDENTITY", "build_key must be 20 lowercase hex characters")
    if type(video_sha256) is not str or SHA256_PATTERN.fullmatch(video_sha256) is None:
        raise WorkflowError("ANALYTICS_IDENTITY", "video_sha256 must be lowercase SHA-256")
    if cover_variant not in COVER_VARIANTS:
        raise WorkflowError("ANALYTICS_IDENTITY", "cover_variant is invalid")
    if window not in {"2h", "24h", "72h", "7d"}:
        raise WorkflowError("ANALYTICS_WINDOW", "Unsupported platform snapshot window")
    unknown = set(metrics) - set(PLATFORM_METRICS)
    if unknown:
        raise WorkflowError("ANALYTICS_METRIC", "Unknown platform metric")
    normalized: dict[str, int | float | None] = {}
    for name in PLATFORM_METRICS:
        value = metrics.get(name)
        if value is not None:
            if type(value) not in {int, float} or isinstance(value, bool):
                raise WorkflowError("ANALYTICS_METRIC", f"Invalid platform metric: {name}")
            if name in COUNT_METRICS and type(value) is not int:
                raise WorkflowError(
                    "ANALYTICS_METRIC", f"Count metric must be an integer: {name}"
                )
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise WorkflowError("ANALYTICS_METRIC", f"Invalid platform metric: {name}")
            if name in RATE_METRICS and number > 1:
                raise WorkflowError(
                    "ANALYTICS_RATE", f"Rate metric must be between 0 and 1: {name}"
                )
        normalized[name] = value

    impressions = normalized["cover_impressions"]
    clicks = normalized["cover_clicks"]
    ctr = normalized["cover_ctr"]
    if impressions == 0 and clicks not in {None, 0, 0.0}:
        raise WorkflowError(
            "ANALYTICS_CTR", "Cover clicks must be zero or null at zero impressions"
        )
    if ctr is not None:
        if impressions is None or clicks is None or float(impressions) <= 0:
            raise WorkflowError(
                "ANALYTICS_CTR",
                "Cover CTR requires positive impressions and an explicit click count",
            )
        expected = float(clicks) / float(impressions)
        if expected > 1 or not math.isclose(
            float(ctr), expected, rel_tol=1e-6, abs_tol=1e-6
        ):
            raise WorkflowError(
                "ANALYTICS_CTR", "Cover CTR must equal clicks divided by impressions"
            )
    # Every unavailable platform field is explicit null; omission never means 0.
    return _append(
        project_root,
        {
            "event": "platform_snapshot",
            "publication_id": clean_publication,
            "build_key": build_key,
            "video_sha256": video_sha256,
            "cover_variant": cover_variant,
            "window": window,
            "metrics": normalized,
        },
    )


def record_creator_feedback(
    project_root: Path, *, build_key: str, feedback: str, decision: str | None
) -> dict[str, Any]:
    if type(build_key) is not str or BUILD_KEY_PATTERN.fullmatch(build_key) is None:
        raise WorkflowError("ANALYTICS_IDENTITY", "build_key must be 20 lowercase hex characters")
    clean_feedback = _safe_text(feedback, "feedback")
    clean_decision = _safe_text(decision, "decision", allow_none=True)
    return _append(
        project_root,
        {
            "event": "creator_feedback",
            "build_key": build_key,
            "feedback": clean_feedback,
            "decision": clean_decision,
        },
    )


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model-usage")
    model.add_argument("--project", type=Path, required=True)
    model.add_argument("--model", required=True)
    model.add_argument("--stage", required=True)
    model.add_argument("--input-tokens", type=_nullable_integer, required=True)
    model.add_argument("--output-tokens", type=_nullable_integer, required=True)
    model.add_argument("--processed-tokens", type=_nullable_integer, required=True)
    snapshot = sub.add_parser("platform-snapshot")
    snapshot.add_argument("--project", type=Path, required=True)
    snapshot.add_argument("--publication-id", required=True)
    snapshot.add_argument("--build-key", required=True)
    snapshot.add_argument("--video-sha256", required=True)
    snapshot.add_argument("--cover-variant", choices=tuple(sorted(COVER_VARIANTS)), required=True)
    snapshot.add_argument("--window", choices=("2h", "24h", "72h", "7d"), required=True)
    for metric in PLATFORM_METRICS:
        snapshot.add_argument(
            "--" + metric.replace("_", "-"),
            type=int if metric in COUNT_METRICS else float,
        )
    feedback = sub.add_parser("creator-feedback")
    feedback.add_argument("--project", type=Path, required=True)
    feedback.add_argument("--build-key", required=True)
    feedback.add_argument("--feedback", required=True)
    feedback.add_argument("--decision")
    args = parser.parse_args()
    if args.command == "model-usage":
        result = record_model_usage(
            args.project,
            model=args.model,
            stage=args.stage,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            processed_tokens=args.processed_tokens,
        )
    elif args.command == "platform-snapshot":
        result = record_platform_snapshot(
            args.project,
            publication_id=args.publication_id,
            build_key=args.build_key,
            video_sha256=args.video_sha256,
            cover_variant=args.cover_variant,
            window=args.window,
            metrics={name: getattr(args, name) for name in PLATFORM_METRICS},
        )
    else:
        result = record_creator_feedback(
            args.project,
            build_key=args.build_key,
            feedback=args.feedback,
            decision=args.decision,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

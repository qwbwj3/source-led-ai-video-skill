from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import analytics  # noqa: E402
import rights_ledger  # noqa: E402
import workflow  # noqa: E402
from lib import common  # noqa: E402
from lib import telemetry  # noqa: E402


class TelemetryContractTests(unittest.TestCase):
    def test_append_is_idempotent_monotonic_and_recovers_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workflow-events-") as raw:
            path = Path(raw) / "events.jsonl"
            digest = "a" * 64
            first = telemetry.append_event(
                path,
                event_type="stage_started",
                stage="tts",
                attempt=1,
                build_key="b" * 20,
                input_hashes={"narration": digest},
            )
            duplicate = telemetry.append_event(
                path,
                event_type="stage_started",
                stage="tts",
                attempt=1,
                build_key="b" * 20,
                input_hashes={"narration": digest},
            )
            self.assertEqual(first["event_id"], duplicate["event_id"])
            self.assertEqual(1, len(telemetry.read_events(path)))
            with path.open("ab") as handle:
                handle.write(b'{"secret":"must-not-survive"')
            finished = telemetry.append_event(
                path,
                event_type="stage_finished",
                stage="tts",
                attempt=1,
                build_key="b" * 20,
                output_hashes={"audio": "c" * 64},
            )
            events = telemetry.read_events(path)
            self.assertEqual([1, 2], [item["sequence"] for item in events])
            self.assertGreater(finished["monotonic_ns"], first["monotonic_ns"])
            self.assertNotIn("must-not-survive", path.read_text(encoding="utf-8"))

    def test_event_schema_cannot_accept_secret_bearing_free_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workflow-events-") as raw:
            with self.assertRaises(TypeError):
                telemetry.append_event(  # type: ignore[call-arg]
                    Path(raw) / "events.jsonl",
                    event_type="stage_failed",
                    stage="tts",
                    attempt=1,
                    error_code="TTS_HTTP",
                    message="access-token=secret",
                )

    def test_log_and_lock_symlink_leaves_are_rejected_without_touching_target(self) -> None:
        for leaf in ("log", "lock"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory(
                prefix="workflow-symlink-"
            ) as raw:
                root = Path(raw)
                log = root / "managed/events.jsonl"
                log.parent.mkdir()
                outside = root / "outside.txt"
                outside.write_text("do-not-change", encoding="utf-8")
                attack_leaf = (
                    log if leaf == "log" else log.with_suffix(log.suffix + ".lock")
                )
                attack_leaf.symlink_to(outside)
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.append_event(
                        log,
                        event_type="stage_started",
                        stage="tts",
                        attempt=1,
                    )
                self.assertEqual("do-not-change", outside.read_text(encoding="utf-8"))

    def test_non_regular_log_leaf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workflow-nonregular-") as raw:
            path = Path(raw) / "events.jsonl"
            path.mkdir()
            with self.assertRaises(telemetry.TelemetryError):
                telemetry.read_events(path)

    def test_stage_summary_reports_terminal_elapsed_time_per_attempt(self) -> None:
        first_attempt = [
            {
                "sequence": 1,
                "event": "stage_started",
                "stage": "tts",
                "attempt": 1,
                "build_key": "a" * 20,
                "monotonic_ns": 1_000_000_000,
                "recorded_at": "2026-01-01T00:00:00Z",
            },
            {
                "sequence": 2,
                "event": "stage_failed",
                "stage": "tts",
                "attempt": 1,
                "build_key": "a" * 20,
                "monotonic_ns": 3_500_000_000,
                "recorded_at": "2026-01-01T00:00:02Z",
                "error_code": "TTS_HTTP",
            },
        ]
        failed = telemetry.stage_summary(first_attempt)[0]
        self.assertEqual("stage_failed", failed["status"])
        self.assertEqual(2.5, failed["elapsed_seconds"])

        second_attempt = first_attempt + [
            {
                "sequence": 3,
                "event": "stage_started",
                "stage": "tts",
                "attempt": 2,
                "build_key": "a" * 20,
                "monotonic_ns": 4_000_000_000,
                "recorded_at": "2026-01-01T00:00:03Z",
            },
            {
                "sequence": 4,
                "event": "stage_finished",
                "stage": "tts",
                "attempt": 2,
                "build_key": "a" * 20,
                "monotonic_ns": 4_250_000_000,
                "recorded_at": "2026-01-01T00:00:04Z",
            },
        ]
        finished = telemetry.stage_summary(second_attempt)[0]
        self.assertEqual("stage_finished", finished["status"])
        self.assertEqual(2, finished["attempt"])
        self.assertEqual(0.25, finished["elapsed_seconds"])

    def test_stage_summary_falls_back_to_utc_across_reboot(self) -> None:
        events = [
            {
                "sequence": 1,
                "event": "stage_started",
                "stage": "build",
                "build_key": "a" * 20,
                "attempt": 1,
                "recorded_at": "2026-07-21T10:00:00.000Z",
                "monotonic_ns": 9_000_000_000,
            },
            {
                "sequence": 2,
                "event": "stage_finished",
                "stage": "build",
                "build_key": "a" * 20,
                "attempt": 1,
                "recorded_at": "2026-07-21T10:02:03.500Z",
                "monotonic_ns": 2_000_000_000,
            },
        ]
        self.assertEqual(123.5, telemetry.stage_summary(events)[0]["elapsed_seconds"])


class CacheKeyContractTests(unittest.TestCase):
    def config(self) -> dict:
        return {
            "version": 1,
            "project_name": "cache contract",
            "platform": "douyin",
            "commercial": True,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "paths": {"base_video": "source/base.mp4", "bgm": ""},
            "narration": {
                "language": "Chinese",
                "scenes": [
                    {"id": "scene-01", "text": "测试文案。", "captions": ["测试文案。"]}
                ],
            },
            "source_timeline": {"scene_boundaries_seconds": [0, 3]},
            "cover": {
                "hook": "原始钩子",
                "3x4": {"generated_source": "a.png", "final": "b.png"},
                "4x3": {"generated_source": "c.png", "final": "d.png"},
            },
        }

    def keys(self, config: dict, cover_hash: str) -> dict[str, str]:
        records = [
            {
                "ratio": "3x4",
                "source_sha256": cover_hash,
                "final_sha256": cover_hash,
            },
            {
                "ratio": "4x3",
                "source_sha256": "e" * 64,
                "final_sha256": "f" * 64,
            },
        ]
        return workflow.compute_build_keys(
            config=config,
            approval_sha256="1" * 64,
            base_sha256="2" * 64,
            bgm_sha256=None,
            cover_records=records,
            implementation={"workflow": "3" * 64},
            tts_profile={"schema": 1, "narration_sha256": "4" * 64},
            ffmpeg_sha256="5" * 64,
            ffprobe_sha256="6" * 64,
            aligner_runtime={"sha256": "7" * 64},
        )

    def test_cover_and_analytics_changes_do_not_invalidate_video_key(self) -> None:
        first_config = self.config()
        first = self.keys(first_config, "a" * 64)
        analytics_config = self.config()
        analytics_config["analytics"] = {"views": 999}
        analytics_only = self.keys(analytics_config, "a" * 64)
        self.assertEqual(first, analytics_only)
        second_config = self.config()
        second_config["cover"]["hook"] = "新钩子"
        second_config["analytics"] = {"views": 999}
        second = self.keys(second_config, "b" * 64)
        self.assertEqual(first["video_key"], second["video_key"])
        self.assertNotEqual(first["cover_key"], second["cover_key"])
        self.assertNotEqual(first["build_key"], second["build_key"])

    def test_operational_modules_are_not_part_of_renderer_fingerprint(self) -> None:
        fingerprint = workflow.implementation_fingerprint()
        renderer = fingerprint["renderer"]
        self.assertNotIn("scripts/analytics.py", renderer)
        self.assertNotIn("scripts/lib/telemetry.py", renderer)
        self.assertNotIn("scripts/render_cover.py", renderer)


class CaptionRegionContractTests(unittest.TestCase):
    def test_ass_uses_bottom_top_and_hides_scene_regions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="caption-regions-") as raw:
            root = Path(raw)
            stage = common.ensure_managed_dir(
                root, ".source-led-ai-video/staging/test.tmp"
            )
            config = {
                "narration": {
                    "scenes": [
                        {"caption_region": "bottom"},
                        {"caption_region": "top"},
                        {"caption_region": "hidden"},
                    ]
                },
                "caption_style": {"top_margin_v": 240},
            }
            captions = [
                {
                    "sceneIndex": 0,
                    "text": "底部字幕",
                    "show": True,
                    "startMs": 0,
                    "endMs": 500,
                },
                {
                    "sceneIndex": 1,
                    "text": "顶部字幕",
                    "show": True,
                    "startMs": 500,
                    "endMs": 1000,
                },
                {
                    "sceneIndex": 2,
                    "text": "隐藏字幕",
                    "show": True,
                    "startMs": 1000,
                    "endMs": 1500,
                },
            ]
            path = workflow.build_ass(stage, captions, config, root)
            document = path.read_text(encoding="utf-8")
            self.assertIn(",Bottom,,0,0,0,,底部字幕", document)
            self.assertIn(",Top,,0,0,0,,顶部字幕", document)
            self.assertNotIn("隐藏字幕", document)
            self.assertIn(",8,58,58,240,1", document)

    def test_review_points_cover_boundaries_info_card_two_line_and_ending(self) -> None:
        config = {
            "narration": {"scenes": [{"id": "scene-01"}, {"id": "scene-02"}]},
            "caption_style": {"max_line_units": 14},
        }
        timeline = {
            "total_frames": 180,
            "target_boundaries_frames": [0, 90, 180],
        }
        captions = [
            {
                "show": True,
                "text": "这是一个需要换成两行显示的字幕内容",
                "startMs": 300,
                "endMs": 1500,
            }
        ]
        contracts = {
            "plan": {
                "scenes": [
                    {
                        "scene_id": "scene-01",
                        "information_card": {"path": "cards/tool.html"},
                    },
                    {"scene_id": "scene-02"},
                ]
            }
        }
        points = workflow.review_point_plan(
            config, timeline, captions, contracts
        )
        point_ids = {item["id"] for item in points}
        self.assertTrue(
            {
                "opening",
                "scene-02-boundary",
                "two-line-caption",
                "scene-01-card-entry",
                "scene-01-card-mid",
                "scene-01-card-exit",
                "ending",
            }.issubset(point_ids)
        )


class RightsLedgerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rights-ledger-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "source/base.mp4"
        self.base.parent.mkdir(parents=True)
        self.base.write_bytes(b"owned base")
        self.evidence = self.root / "private/rights-evidence/proof.txt"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_text("contract evidence", encoding="utf-8")
        self.config = {"paths": {"base_video": "source/base.mp4", "bgm": ""}}

    def write_record(self, *, expires_at: str | None, commercial: bool = True) -> None:
        record = {
            "schema": 1,
            "event": "rights_confirmed",
            "asset_sha256": common.sha256_file(self.base),
            "platforms": ["douyin"],
            "commercial": commercial,
            "expires_at": expires_at,
            "rights_basis": "owner permission",
            "evidence_path": "private/rights-evidence/proof.txt",
            "evidence_sha256": common.sha256_file(self.evidence),
            "confirmed_by": "reviewer",
        }
        ledger = self.root / "private/rights-events.jsonl"
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_active_douyin_commercial_record_passes(self) -> None:
        self.write_record(
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        )
        result = workflow.verify_rights_ledger(self.root, self.config)
        self.assertEqual(1, len(result["records"]))

    def test_expired_or_noncommercial_record_is_blocked(self) -> None:
        for expires_at, commercial in (
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), True),
            (None, False),
        ):
            with self.subTest(expires_at=expires_at, commercial=commercial):
                self.write_record(expires_at=expires_at, commercial=commercial)
                with self.assertRaises(common.WorkflowError) as raised:
                    workflow.verify_rights_ledger(self.root, self.config)
                self.assertEqual("RIGHTS_SCOPE", raised.exception.code)

    def test_later_revocation_overrides_old_confirmation(self) -> None:
        self.write_record(expires_at=None)
        ledger = self.root / "private/rights-events.jsonl"
        revoked = {
            "schema": 1,
            "event": "rights_revoked",
            "asset_sha256": common.sha256_file(self.base),
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "reason": "author withdrew permission",
            "confirmed_by": "reviewer",
        }
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(revoked) + "\n")
        with self.assertRaises(common.WorkflowError) as raised:
            workflow.verify_rights_ledger(self.root, self.config)
        self.assertEqual("RIGHTS_SCOPE", raised.exception.code)

    def test_schema2_binds_author_source_region_attribution_and_asset_id(self) -> None:
        record = {
            "schema": 2,
            "event": "rights_confirmed",
            "asset_id": "asset-01",
            "asset_sha256": common.sha256_file(self.base),
            "source_id": "source-01",
            "author": "Builder author",
            "source": "original post archive",
            "platforms": ["douyin"],
            "regions": ["CN"],
            "commercial": True,
            "expires_at": None,
            "rights_basis": "written permission",
            "attribution": {
                "required": True,
                "placement": "body_or_pinned_comment",
                "text": "来源：Builder author",
            },
            "evidence_path": "private/rights-evidence/proof.txt",
            "evidence_sha256": common.sha256_file(self.evidence),
            "confirmed_by": "reviewer",
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        ledger = self.root / "private/rights-events.jsonl"
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = workflow.verify_rights_ledger(
            self.root,
            None,
            required_asset_hashes={common.sha256_file(self.base): "asset-01"},
            require_schema2=True,
        )
        self.assertEqual("asset-01", result["records"][0]["asset_id"])
        record["asset_id"] = "asset-wrong"
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(common.WorkflowError) as raised:
            workflow.verify_rights_ledger(
                self.root,
                None,
                required_asset_hashes={common.sha256_file(self.base): "asset-01"},
                require_schema2=True,
            )
        self.assertEqual("RIGHTS_SCOPE", raised.exception.code)


class RightsLedgerWriterTests(unittest.TestCase):
    def test_writer_resolves_asset_hash_and_revocation_is_effective(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rights-writer-") as raw:
            root = Path(raw)
            base = root / "source/base.mp4"
            base.parent.mkdir(parents=True)
            base.write_bytes(b"owned base")
            evidence = root / "private/rights-evidence/proof.txt"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("permission proof", encoding="utf-8")
            config = {
                "version": 1,
                "project_name": "rights writer",
                "platform": "douyin",
                "commercial": True,
                "canvas": {"width": 1080, "height": 1920, "fps": 30},
                "paths": {"base_video": "source/base.mp4", "bgm": ""},
                "narration": {
                    "scenes": [
                        {"id": "scene-01", "text": "测试文案。", "captions": ["测试文案。"]}
                    ]
                },
                "source_timeline": {"scene_boundaries_seconds": [0, 3]},
                "cover": {
                    "hook": "测试封面钩子",
                    "3x4": {"generated_source": "a.png", "final": "b.png"},
                    "4x3": {"generated_source": "c.png", "final": "d.png"},
                },
            }
            config_path = root / "project.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(common.WorkflowError) as raised:
                rights_ledger.confirm(
                    config_path,
                    asset_id="base_video",
                    source_id="source-base-video",
                    author="项目制作方",
                    source="本地制作底片",
                    rights_basis="自有素材",
                    evidence_path="private/rights-evidence/proof.txt",
                    confirmed_by="审核员",
                    expires_at="tomorrow",
                    attribution_required=False,
                    attribution_placement="none",
                    attribution_text="",
                )
            self.assertEqual("RIGHTS_LEDGER_INVALID", raised.exception.code)
            self.assertFalse((root / "private/rights-events.jsonl").exists())
            record = rights_ledger.confirm(
                config_path,
                asset_id="base_video",
                source_id="source-base-video",
                author="项目制作方",
                source="本地制作底片",
                rights_basis="自有素材",
                evidence_path="private/rights-evidence/proof.txt",
                confirmed_by="审核员",
                expires_at=None,
                attribution_required=False,
                attribution_placement="none",
                attribution_text="",
            )
            self.assertEqual(common.sha256_file(base), record["asset_sha256"])
            workflow.verify_rights_ledger(root, config)
            rights_ledger.revoke(
                config_path,
                asset_id="base_video",
                reason="停止授权",
                confirmed_by="审核员",
            )
            with self.assertRaises(common.WorkflowError) as raised:
                workflow.verify_rights_ledger(root, config)
            self.assertEqual("RIGHTS_SCOPE", raised.exception.code)
            rights_ledger.confirm(
                config_path,
                asset_id="base_video",
                source_id="source-base-video",
                author="项目制作方",
                source="本地制作底片",
                rights_basis="恢复自有素材授权",
                evidence_path="private/rights-evidence/proof.txt",
                confirmed_by="审核员",
                expires_at=None,
                attribution_required=False,
                attribution_placement="none",
                attribution_text="",
            )
            verified = workflow.verify_rights_ledger(root, config)
            self.assertEqual("base_video", verified["records"][0]["asset_id"])


class AnalyticsContractTests(unittest.TestCase):
    BUILD_KEY = "a" * 20
    SNAPSHOT_IDENTITY = {
        "publication_id": "douyin-publish-001",
        "build_key": BUILD_KEY,
        "video_sha256": "b" * 64,
        "cover_variant": "3x4",
    }

    def test_supported_events_and_null_platform_semantics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-") as raw:
            root = Path(raw)
            analytics.record_model_usage(
                root,
                model="terra-high",
                stage="source-analysis",
                input_tokens=10,
                output_tokens=5,
                processed_tokens=20,
            )
            snapshot = analytics.record_platform_snapshot(
                root,
                **self.SNAPSHOT_IDENTITY,
                window="24h",
                metrics={"views": 100},
            )
            analytics.record_creator_feedback(
                root,
                build_key=self.BUILD_KEY,
                feedback="封面信息密度偏高",
                decision="下条减少一行",
            )
            self.assertEqual(100, snapshot["metrics"]["views"])
            self.assertIsNone(snapshot["metrics"]["cover_ctr"])
            events = telemetry.read_events(
                root / ".source-led-ai-video/analytics/events.jsonl"
            )
            self.assertEqual(
                ["model_usage", "platform_snapshot", "creator_feedback"],
                [event["event"] for event in events],
            )

    def test_model_usage_preserves_explicit_null_and_checks_available_totals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-null-tokens-") as raw:
            record = analytics.record_model_usage(
                Path(raw),
                model="terra-high",
                stage="source-analysis",
                input_tokens=None,
                output_tokens=5,
                processed_tokens=None,
            )
            self.assertIsNone(record["input_tokens"])
            self.assertEqual(5, record["output_tokens"])
            self.assertIsNone(record["processed_tokens"])
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_model_usage(
                    Path(raw),
                    model="terra-high",
                    stage="script",
                    input_tokens=10,
                    output_tokens=5,
                    processed_tokens=14,
                )
            self.assertEqual("ANALYTICS_TOKENS", raised.exception.code)

    def test_nonfinite_and_out_of_range_rates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-invalid-rate-") as raw:
            root = Path(raw)
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    analytics.record_platform_snapshot(
                        root,
                        **self.SNAPSHOT_IDENTITY,
                        window="24h",
                        metrics={"views": value},
                    )
                self.assertEqual("ANALYTICS_METRIC", raised.exception.code)
            for metric in analytics.RATE_METRICS:
                with self.subTest(metric=metric), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    analytics.record_platform_snapshot(
                        root,
                        **self.SNAPSHOT_IDENTITY,
                        window="24h",
                        metrics={metric: 1.01},
                    )
                self.assertEqual("ANALYTICS_RATE", raised.exception.code)
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_platform_snapshot(
                    root,
                    **self.SNAPSHOT_IDENTITY,
                    window="24h",
                    metrics={"views": 1.5},
                )
            self.assertEqual("ANALYTICS_METRIC", raised.exception.code)

    def test_cover_ctr_requires_valid_denominator_and_exact_ratio(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-ctr-") as raw:
            root = Path(raw)
            valid = analytics.record_platform_snapshot(
                root,
                **self.SNAPSHOT_IDENTITY,
                window="24h",
                metrics={
                    "cover_impressions": 200,
                    "cover_clicks": 50,
                    "cover_ctr": 0.25,
                },
            )
            self.assertEqual(0.25, valid["metrics"]["cover_ctr"])
            zero = analytics.record_platform_snapshot(
                root,
                **self.SNAPSHOT_IDENTITY,
                window="72h",
                metrics={
                    "cover_impressions": 0,
                    "cover_clicks": 0,
                    "cover_ctr": None,
                },
            )
            self.assertIsNone(zero["metrics"]["cover_ctr"])
            invalid = (
                {
                    "cover_impressions": 0,
                    "cover_clicks": 0,
                    "cover_ctr": 0.0,
                },
                {"cover_impressions": 100, "cover_ctr": 0.1},
                {
                    "cover_impressions": 100,
                    "cover_clicks": 25,
                    "cover_ctr": 0.3,
                },
                {"cover_impressions": 0, "cover_clicks": 1, "cover_ctr": None},
            )
            for metrics in invalid:
                with self.subTest(metrics=metrics), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    analytics.record_platform_snapshot(
                        root,
                        **self.SNAPSHOT_IDENTITY,
                        window="7d",
                        metrics=metrics,
                    )
                self.assertEqual("ANALYTICS_CTR", raised.exception.code)

    def test_sensitive_or_oversized_free_text_is_rejected(self) -> None:
        samples = (
            "详情见 https://example.com/demo",
            "联系 creator@example.com",
            "手机号 13800138000",
            "身份证 11010519491231002X",
            "access_token=super-secret-value",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "Authorization: Bearer abcdefghijklmnop",
        )
        with tempfile.TemporaryDirectory(prefix="analytics-sensitive-") as raw:
            root = Path(raw)
            for sample in samples:
                with self.subTest(sample=sample), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    analytics.record_creator_feedback(
                        root,
                        build_key=self.BUILD_KEY,
                        feedback=sample,
                        decision=None,
                    )
                self.assertEqual("ANALYTICS_SENSITIVE", raised.exception.code)
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_creator_feedback(
                    root,
                    build_key=self.BUILD_KEY,
                    feedback="字" * 501,
                    decision=None,
                )
            self.assertEqual("ANALYTICS_TEXT", raised.exception.code)
            for model, stage in (("m" * 101, "source"), ("terra", "s" * 65)):
                with self.subTest(model=model, stage=stage), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    analytics.record_model_usage(
                        root,
                        model=model,
                        stage=stage,
                        input_tokens=None,
                        output_tokens=None,
                        processed_tokens=None,
                    )
                self.assertEqual("ANALYTICS_TEXT", raised.exception.code)
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_creator_feedback(
                    root,
                    build_key=self.BUILD_KEY,
                    feedback="节奏可以",
                    decision="字" * 241,
                )
            self.assertEqual("ANALYTICS_TEXT", raised.exception.code)

    def test_analytics_log_and_lock_symlinks_cannot_escape_project(self) -> None:
        for leaf in ("log", "lock"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory(
                prefix="analytics-symlink-"
            ) as raw:
                container = Path(raw)
                root = container / "project"
                root.mkdir()
                analytics_dir = root / ".source-led-ai-video/analytics"
                analytics_dir.mkdir(parents=True)
                path = analytics_dir / "events.jsonl"
                outside = container / "outside.txt"
                outside.write_text("do-not-change", encoding="utf-8")
                attack_leaf = (
                    path if leaf == "log" else path.with_suffix(path.suffix + ".lock")
                )
                attack_leaf.symlink_to(outside)
                with self.assertRaises(common.WorkflowError) as raised:
                    analytics.record_creator_feedback(
                        root,
                        build_key=self.BUILD_KEY,
                        feedback="封面节奏需要更快",
                        decision=None,
                    )
                self.assertEqual("ANALYTICS_LOG", raised.exception.code)
                self.assertEqual("do-not-change", outside.read_text(encoding="utf-8"))

    def test_platform_and_feedback_require_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-identity-") as raw:
            root = Path(raw)
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_platform_snapshot(
                    root,
                    publication_id="douyin-publish-001",
                    build_key="wrong",
                    video_sha256="b" * 64,
                    cover_variant="3x4",
                    window="24h",
                    metrics={"views": 1},
                )
            self.assertEqual("ANALYTICS_IDENTITY", raised.exception.code)
            with self.assertRaises(common.WorkflowError) as raised:
                analytics.record_creator_feedback(
                    root,
                    build_key="wrong",
                    feedback="节奏需要调整",
                    decision=None,
                )
            self.assertEqual("ANALYTICS_IDENTITY", raised.exception.code)


class StatusContractTests(unittest.TestCase):
    def test_malformed_v2_config_is_reported_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workflow-status-invalid-") as raw:
            root = Path(raw)
            config_path = root / "project.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "paths": {},
                        "cover": {},
                        "narration": {},
                    }
                ),
                encoding="utf-8",
            )
            status = workflow.workflow_status(config_path)
            self.assertTrue(
                any(
                    item.startswith("valid V2 contracts (")
                    for item in status["missing_inputs"]
                )
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import workflow  # noqa: E402
from lib import common  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


class FixedProjectPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="fixed-video-policy-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        base = self.root / "source/base.mp4"
        base.parent.mkdir(parents=True)
        base.write_bytes(b"base fixture")
        self.config = {
            "version": 1,
            "project_name": "固定发布标准",
            "platform": "douyin",
            "commercial": True,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "paths": {"base_video": "source/base.mp4", "bgm": ""},
            "narration": {
                "language": "Chinese",
                "scenes": [
                    {
                        "id": "scene-01",
                        "text": "固定标准不能由项目放宽。",
                        "captions": ["固定标准不能由项目放宽。"],
                    }
                ],
            },
            "source_timeline": {"scene_boundaries_seconds": [0, 3]},
            "cover": {
                "hook": "固定标准不能放宽",
                "3x4": {
                    "generated_source": "covers/source/3x4.png",
                    "final": "covers/封面-3x4-固定标准不能放宽.png",
                },
                "4x3": {
                    "generated_source": "covers/source/4x3.png",
                    "final": "covers/封面-4x3-固定标准不能放宽.png",
                },
            },
        }
        self.config_path = self.root / "project.json"

    def validate(self, config: dict) -> None:
        write_json(self.config_path, config)
        common.load_and_validate_config(self.config_path)

    def assert_rejected(self, config: dict, code: str) -> None:
        write_json(self.config_path, config)
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.config_path)
        self.assertEqual(code, raised.exception.code)

    def test_omitted_policy_fields_use_locked_pipeline_defaults(self) -> None:
        self.validate(self.config)
        self.assertEqual((0.8, 1.2), common.FIXED_RETIME_RATIO_LIMITS)
        self.assertEqual(-14.0, common.FIXED_FINAL_LUFS)
        self.assertEqual(90.0, common.FIXED_MAX_DURATION_SECONDS)

    def test_retime_limits_cannot_be_tightened_or_relaxed_per_project(self) -> None:
        for value in ([0.7, 1.2], [0.8, 1.3], [0.9, 1.1]):
            with self.subTest(value=value):
                candidate = copy.deepcopy(self.config)
                candidate["source_timeline"]["retime_ratio_limits"] = value
                self.assert_rejected(candidate, "CONFIG_TIMELINE")

        candidate = copy.deepcopy(self.config)
        candidate["source_timeline"]["retime_ratio_limits"] = [0.8, 1.2]
        self.validate(candidate)

    def test_audio_and_qa_thresholds_are_exact_and_cannot_be_relaxed(self) -> None:
        mutations = (
            ("final loudness", ("audio", "final_lufs"), -16, "CONFIG_AUDIO"),
            ("longer duration", ("qa", "max_duration_seconds"), 120, "CONFIG_QA"),
            ("shorter duration", ("qa", "max_duration_seconds"), 89, "CONFIG_QA"),
            ("allow black", ("qa", "allow_black"), True, "CONFIG_QA"),
            ("allow silence", ("qa", "allow_silence"), True, "CONFIG_QA"),
        )
        for label, keys, value, code in mutations:
            with self.subTest(policy=label):
                candidate = copy.deepcopy(self.config)
                candidate.setdefault(keys[0], {})[keys[1]] = value
                self.assert_rejected(candidate, code)

        exact = copy.deepcopy(self.config)
        exact["audio"] = {"final_lufs": -14}
        exact["qa"] = {
            "max_duration_seconds": 90,
            "allow_black": False,
            "allow_silence": False,
        }
        self.validate(exact)

    def test_bgm_fade_is_limited_to_a_reasonable_range(self) -> None:
        for value in (0, 0.49, 5.01, 100):
            with self.subTest(value=value):
                candidate = copy.deepcopy(self.config)
                candidate["audio"] = {"bgm_fade_out_seconds": value}
                self.assert_rejected(candidate, "CONFIG_AUDIO")
        for value in (0.5, 1.8, 5.0):
            with self.subTest(valid=value):
                candidate = copy.deepcopy(self.config)
                candidate["audio"] = {"bgm_fade_out_seconds": value}
                self.validate(candidate)


class VisualRecordPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="visual-policy-")
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name) / "review-runs" / ("a" * 20)
        manifest_path = self.run_dir / "build-manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(b'{"schema":1,"kind":"build"}\n')
        self.artifacts = {
            "video.mp4": "1" * 64,
            "qa/contact-sheet.jpg": "2" * 64,
            "covers/cover-3x4.png": "3" * 64,
            "covers/cover-4x3.png": "4" * 64,
        }
        self.build_manifest = {"artifacts": self.artifacts}
        self.valid = {
            "schema": 2,
            "result": "pass",
            "reviewer": "离线合约审核",
            "notes": "已完整检查全部镜头字幕封面与声音表现。",
            "failed_checks": [],
            "checks": {name: True for name in workflow.VISUAL_CHECKS},
            "build_manifest_sha256": common.sha256_file(manifest_path),
            "video_sha256": self.artifacts["video.mp4"],
            "contact_sheet_sha256": self.artifacts["qa/contact-sheet.jpg"],
            "cover_3x4_sha256": self.artifacts["covers/cover-3x4.png"],
            "cover_4x3_sha256": self.artifacts["covers/cover-4x3.png"],
        }

    @property
    def visual_path(self) -> Path:
        return self.run_dir / "qa/visual-review.json"

    def verify(self, value: object) -> dict:
        write_json(self.visual_path, value)
        return workflow.verify_visual_record(self.run_dir, self.build_manifest)

    def assert_invalid(self, value: object) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            self.verify(value)
        self.assertEqual("VISUAL_INVALID", raised.exception.code)

    def test_complete_pass_and_logically_complete_failure_are_accepted(self) -> None:
        self.assertEqual("pass", self.verify(self.valid)["result"])

        failure = copy.deepcopy(self.valid)
        failed = "no_visible_url_or_external_ui"
        failure["result"] = "fail"
        failure["failed_checks"] = [failed]
        failure["checks"][failed] = False
        self.assertEqual("fail", self.verify(failure)["result"])

    def test_missing_or_empty_checks_cannot_fake_a_pass(self) -> None:
        missing = copy.deepcopy(self.valid)
        missing.pop("checks")
        self.assert_invalid(missing)

        empty = copy.deepcopy(self.valid)
        empty["checks"] = {}
        self.assert_invalid(empty)

        partial = copy.deepcopy(self.valid)
        partial["checks"].pop(workflow.VISUAL_CHECKS[0])
        self.assert_invalid(partial)

    def test_schema_and_field_types_are_strict(self) -> None:
        mutations = {
            "non-object": [],
            "boolean schema": {**self.valid, "schema": True},
            "unknown result": {**self.valid, "result": "approved"},
            "numeric reviewer": {**self.valid, "reviewer": 1},
            "empty reviewer": {**self.valid, "reviewer": ""},
            "short notes": {**self.valid, "notes": "看过了"},
            "failed checks object": {**self.valid, "failed_checks": {}},
            "numeric check value": {
                **self.valid,
                "checks": {**self.valid["checks"], workflow.VISUAL_CHECKS[0]: 1},
            },
            "malformed hash": {**self.valid, "video_sha256": "not-a-hash"},
        }
        for label, value in mutations.items():
            with self.subTest(field=label):
                self.assert_invalid(value)

    def test_result_failed_checks_and_checks_must_agree_exactly(self) -> None:
        failed_name = "no_caption_clipping"
        mutations = []

        duplicate = copy.deepcopy(self.valid)
        duplicate["result"] = "fail"
        duplicate["failed_checks"] = [failed_name, failed_name]
        duplicate["checks"][failed_name] = False
        mutations.append(duplicate)

        unknown = copy.deepcopy(self.valid)
        unknown["result"] = "fail"
        unknown["failed_checks"] = ["invented_check"]
        mutations.append(unknown)

        pass_with_failure = copy.deepcopy(self.valid)
        pass_with_failure["failed_checks"] = [failed_name]
        pass_with_failure["checks"][failed_name] = False
        mutations.append(pass_with_failure)

        fail_without_failure = copy.deepcopy(self.valid)
        fail_without_failure["result"] = "fail"
        mutations.append(fail_without_failure)

        contradictory = copy.deepcopy(self.valid)
        contradictory["result"] = "fail"
        contradictory["failed_checks"] = [failed_name]
        mutations.append(contradictory)

        extra_check = copy.deepcopy(self.valid)
        extra_check["checks"]["invented_check"] = True
        mutations.append(extra_check)

        for index, value in enumerate(mutations):
            with self.subTest(case=index):
                self.assert_invalid(value)

    def test_well_formed_but_stale_binding_is_rejected_separately(self) -> None:
        stale = copy.deepcopy(self.valid)
        stale["video_sha256"] = "9" * 64
        with self.assertRaises(common.WorkflowError) as raised:
            self.verify(stale)
        self.assertEqual("VISUAL_STALE", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

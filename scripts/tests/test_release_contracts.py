from __future__ import annotations

import argparse
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import precheck_scan  # noqa: E402
import review_gate  # noqa: E402
import workflow  # noqa: E402
from lib import common  # noqa: E402
from lib import volc_tts as volc_tts_adapter  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _semantic_report(
    payload: dict,
    scan: dict,
    *,
    result: str = "pass",
    overall_line: str | None = None,
    material_rights: str = "需要（发布前确认）",
) -> str:
    revised = result == "revised-pass"
    platform_result = "改后可发" if revised else "可以发"
    overall = overall_line or ("结论：改后可发" if revised else "结论：可以发")
    required = "- 已修复：已按规则 C01 收缩原文主张。" if revised else "- 无"
    recheck = "\n复检：通过\n" if revised else ""
    industries = "、".join(payload["industries"]) or "无"
    commercial = "有" if payload["commercial"] else "无"
    lexical_counts = "; ".join(
        f"{name}={len(scan[name])}"
        for name in ("candidates", "personal_hits", "myth_advisories", "warnings")
    )
    marketing = "需要" if payload["commercial"] else "不适用"
    return (
        "# 抖音发布前语义复核\n\n"
        "## 审核范围\n"
        "- 平台：抖音\n"
        f"- 商业属性：{commercial}\n"
        f"- 强监管行业：{industries}\n"
        "- 审核内容：口播文案、封面钩子\n"
        f"- Review-Payload-SHA256: {payload['review_payload_sha256']}\n\n"
        "## 逐平台结论\n"
        f"- 抖音：{platform_result}\n\n"
        f"{overall}\n"
        f"{recheck}\n"
        "## 词面候选复核\n"
        f"- Lexical-Review: complete; {lexical_counts}\n"
        "- 复核声明：已逐条复核全部词面候选；词面候选不等于违规结论，零候选不等于语义安全。\n\n"
        "## 必改\n"
        f"{required}\n\n"
        "## 建议改\n"
        "- 无\n\n"
        "## 仅提示\n"
        "- 无\n\n"
        "## 无法判定\n"
        "- 无\n\n"
        "## 发布前检查单\n"
        "- AI生成内容标注：需要\n"
        "- 虚构演绎标注：不适用\n"
        f"- 营销信息标注：{marketing}\n"
        "- 转载与来源标注：需要\n"
        "- 事实证据：已确认\n"
        f"- 素材授权：{material_rights}\n\n"
        "## 边界声明\n"
        f"- {review_gate.REPORT_TRUST_STATEMENT}\n"
        f"- {review_gate.REPORT_BOUNDARY_STATEMENT}\n"
    )


class ProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="source-led-contract-")
        self.addCleanup(self._temporary.cleanup)
        self.temp_root = Path(self._temporary.name)
        self.project_root = self.temp_root / "中文 项目" / "视频 一号"
        self.project_root.mkdir(parents=True)
        self.other_cwd = self.temp_root / "任意 工作目录"
        self.other_cwd.mkdir()

        self.base_relative = Path("素材 文件") / "干净 基础.mp4"
        self.source_3x4_relative = Path("封面 素材") / "来源 3x4.png"
        self.source_4x3_relative = Path("封面 素材") / "来源 4x3.png"
        self.final_3x4_relative = Path("成品 封面") / "封面-3x4-一句话做出成品.png"
        self.final_4x3_relative = Path("成品 封面") / "封面-4x3-一句话做出成品.png"

        png_signature = b"\x89PNG\r\n\x1a\n"
        fixture_bytes = {
            self.base_relative: b"placeholder clean base",
            self.source_3x4_relative: png_signature + b"independent portrait source",
            self.source_4x3_relative: png_signature + b"independent landscape source",
            self.final_3x4_relative: png_signature + b"placeholder portrait cover",
            self.final_4x3_relative: png_signature + b"placeholder landscape cover",
        }
        for relative, content in fixture_bytes.items():
            path = self.project_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        self.config = {
            "version": 1,
            "project_name": "中文路径合约测试",
            "platform": "douyin",
            "commercial": True,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "paths": {
                "base_video": self.base_relative.as_posix(),
                "bgm": "",
            },
            "narration": {
                "language": "Chinese",
                "caption_lead_ms": 80,
                "scenes": [
                    {
                        "id": "scene-01",
                        "text": "先确认文案，再生成语音。",
                        "captions": ["先确认文案，再生成语音。"],
                    }
                ],
            },
            "source_timeline": {
                "scene_boundaries_seconds": [0.0, 3.0],
                "retime_ratio_limits": [0.8, 1.2],
            },
            "cover": {
                "hook": "一句话做出成品",
                "3x4": {
                    "generated_source": self.source_3x4_relative.as_posix(),
                    "final": self.final_3x4_relative.as_posix(),
                },
                "4x3": {
                    "generated_source": self.source_4x3_relative.as_posix(),
                    "final": self.final_4x3_relative.as_posix(),
                },
            },
        }
        self.config_path = self.project_root / "project.json"
        self.write_config()

    def write_config(self, config: dict | None = None) -> None:
        _write_json(self.config_path, self.config if config is None else config)

    def build_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            config=self.config_path,
            env_file=None,
            reuse_tts=None,
            ffmpeg=None,
            ffprobe=None,
            aligner_python=None,
        )

    def prepare_current_narration(self) -> dict:
        payload = review_gate.prepare(self.config_path)
        review_gate.scan(self.config_path)
        return payload

    def approve_current_narration(
        self, *, material_rights: str = "需要（发布前确认）"
    ) -> dict:
        payload = self.prepare_current_narration()
        lexical_scan = common.read_json(
            self.project_root / "review" / "lexical-scan.json"
        )
        report_path = self.project_root / "review" / "semantic-review.md"
        report_path.write_text(
            _semantic_report(
                payload,
                lexical_scan,
                material_rights=material_rights,
            ),
            encoding="utf-8",
        )
        return review_gate.approve(
            self.config_path,
            report_path,
            "pass",
            review_gate.REVIEWER,
        )

    def create_integrity_run(self, build_key: str) -> Path:
        run_dir = self.project_root / "review-runs" / build_key
        for relative in workflow.REQUIRED_BUILD_ARTIFACTS:
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"offline fixture: {relative}\n".encode("utf-8"))

        approval = common.read_json(self.project_root / "review/approval.json")
        workflow.copy_review_evidence(
            run_dir,
            self.config,
            self.project_root,
            approval,
        )
        video_hash = common.sha256_file(run_dir / "video.mp4")
        _write_json(
            run_dir / "qa/auto-qa.json",
            {"schema": 1, "passed": True, "video_sha256": video_hash},
        )
        _write_json(
            run_dir / "provenance.json",
            {
                "schema": 1,
                "build_key": build_key,
                "config_sha256": workflow.config_sha256(self.config),
            },
        )
        workflow.write_build_manifest(
            run_dir,
            self.config,
            build_key,
            workflow.implementation_fingerprint(),
        )
        return run_dir

    def assert_workflow_error(self, expected_code: str, callable_: object) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            callable_()
        self.assertEqual(expected_code, raised.exception.code)


class ReviewGateContractTests(ProjectFixture):
    def test_missing_approval_blocks_build_before_tts(self) -> None:
        with mock.patch.object(workflow, "synthesize") as synthesize:
            self.assert_workflow_error(
                "REVIEW_BLOCKED",
                lambda: workflow.run_build(self.build_args()),
            )

        synthesize.assert_not_called()
        self.assertFalse((self.project_root / ".source-led-ai-video").exists())
        self.assertEqual([], list(self.project_root.rglob("narration-raw.mp3")))

    def test_hook_commercial_or_narration_mutation_invalidates_approval_before_tts(
        self,
    ) -> None:
        self.approve_current_narration()
        approved = copy.deepcopy(self.config)
        cases = {
            "hook": lambda candidate: candidate["cover"].update(
                {"hook": "两句话做出成品"}
            ),
            "commercial": lambda candidate: candidate.update({"commercial": False}),
            "narration": lambda candidate: candidate["narration"]["scenes"][0].update(
                {
                    "text": "文案已经修改，旧审核不能继续使用。",
                    "captions": ["文案已经修改，旧审核不能继续使用。"],
                }
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(changed=label):
                candidate = copy.deepcopy(approved)
                mutate(candidate)
                self.write_config(candidate)
                with mock.patch.object(workflow, "synthesize") as synthesize:
                    self.assert_workflow_error(
                        "REVIEW_STALE",
                        lambda: workflow.run_build(self.build_args()),
                    )
                synthesize.assert_not_called()

        self.write_config(approved)
        self.assertFalse((self.project_root / ".source-led-ai-video").exists())
        self.assertEqual([], list(self.project_root.rglob("narration-raw.mp3")))

    def test_embedded_can_publish_phrase_cannot_unlock_tts(self) -> None:
        payload = self.prepare_current_narration()
        lexical_scan = common.read_json(
            self.project_root / "review" / "lexical-scan.json"
        )
        report_path = self.project_root / "review" / "semantic-review.md"
        report_path.write_text(
            _semantic_report(
                payload,
                lexical_scan,
                overall_line="说明：正文嵌入“结论：可以发”只是在描述目标，不能代表审核结论。",
            ),
            encoding="utf-8",
        )
        self.assert_workflow_error(
            "REVIEW_RESULT",
            lambda: review_gate.approve(
                self.config_path,
                report_path,
                "pass",
                review_gate.REVIEWER,
            ),
        )
        self.assertFalse((self.project_root / "review/approval.json").exists())

    def test_three_line_semantic_report_cannot_unlock_tts(self) -> None:
        payload = self.prepare_current_narration()
        report_path = self.project_root / "review" / "semantic-review.md"
        report_path.write_text(
            "- 平台：抖音\n"
            f"Review-Payload-SHA256: {payload['review_payload_sha256']}\n"
            "结论：可以发\n",
            encoding="utf-8",
        )
        self.assert_workflow_error(
            "REVIEW_REPORT",
            lambda: review_gate.approve(
                self.config_path,
                report_path,
                "pass",
                review_gate.REVIEWER,
            ),
        )

    def test_semantic_report_scope_lexical_checklist_and_boundary_are_bound(self) -> None:
        payload = self.prepare_current_narration()
        lexical_scan = common.read_json(
            self.project_root / "review" / "lexical-scan.json"
        )
        valid = _semantic_report(payload, lexical_scan)
        cases = (
            ("scope", valid.replace("- 商业属性：有", "- 商业属性：无"), "REVIEW_SCOPE"),
            (
                "lexical counts",
                valid.replace("candidates=0", "candidates=99"),
                "REVIEW_LEXICAL",
            ),
            (
                "checklist",
                valid.replace("- AI生成内容标注：需要\n", ""),
                "REVIEW_CHECKLIST",
            ),
            (
                "unconfirmed factual evidence",
                valid.replace("- 事实证据：已确认", "- 事实证据：需要（发布前确认）"),
                "REVIEW_CHECKLIST",
            ),
            (
                "vague material-rights action",
                valid.replace("- 素材授权：需要（发布前确认）", "- 素材授权：需要"),
                "REVIEW_CHECKLIST",
            ),
            (
                "boundary",
                valid.replace(f"- {review_gate.REPORT_TRUST_STATEMENT}\n", ""),
                "REVIEW_BOUNDARY",
            ),
        )
        report_path = self.project_root / "review" / "semantic-review.md"
        for label, report, code in cases:
            with self.subTest(case=label):
                report_path.write_text(report, encoding="utf-8")
                self.assert_workflow_error(
                    code,
                    lambda: review_gate.approve(
                        self.config_path,
                        report_path,
                        "pass",
                        review_gate.REVIEWER,
                    ),
                )

    def test_pending_rights_unlock_the_tts_gate_but_approval_tampering_is_rejected(
        self,
    ) -> None:
        approval = self.approve_current_narration()
        self.assertEqual(review_gate.RIGHTS_PENDING, approval["rights_clearance"])
        self.assertEqual(
            review_gate.RIGHTS_PENDING,
            review_gate.verify_approval(self.config_path)["rights_clearance"],
        )

        approval["rights_clearance"] = review_gate.RIGHTS_CONFIRMED
        _write_json(self.project_root / "review/approval.json", approval)
        self.assert_workflow_error(
            "REVIEW_STALE",
            lambda: review_gate.verify_approval(self.config_path),
        )

    def test_revised_pass_still_requires_recheck_and_resolved_change(self) -> None:
        payload = self.prepare_current_narration()
        lexical_scan = common.read_json(
            self.project_root / "review" / "lexical-scan.json"
        )
        report_path = self.project_root / "review" / "semantic-review.md"
        report_path.write_text(
            _semantic_report(payload, lexical_scan, result="revised-pass"),
            encoding="utf-8",
        )
        approval = review_gate.approve(
            self.config_path,
            report_path,
            "revised-pass",
            review_gate.REVIEWER,
        )
        self.assertEqual("revised-pass", approval["result"])

        (self.project_root / "review/approval.json").unlink()
        report_path.write_text(
            _semantic_report(payload, lexical_scan, result="revised-pass").replace(
                "复检：通过\n", ""
            ),
            encoding="utf-8",
        )
        self.assert_workflow_error(
            "REVIEW_RESULT",
            lambda: review_gate.approve(
                self.config_path,
                report_path,
                "revised-pass",
                review_gate.REVIEWER,
            ),
        )

    def test_scope_sentinel_requires_obvious_industry_declarations(self) -> None:
        cases = (
            ("medical", "医生讲解糖尿病治疗方法。", "medical"),
            ("finance", "这只股票的年化收益值得重点分析。", "finance"),
        )
        for label, text, industry in cases:
            with self.subTest(scope=label):
                candidate = copy.deepcopy(self.config)
                candidate["narration"]["scenes"][0].update(
                    {"text": text, "captions": [text]}
                )
                candidate["review_scope"] = {"industries": []}
                self.write_config(candidate)
                self.assert_workflow_error(
                    "REVIEW_SCOPE_SENTINEL",
                    lambda: review_gate.prepare(self.config_path),
                )

                candidate["review_scope"] = {"industries": [industry]}
                self.write_config(candidate)
                payload = review_gate.prepare(self.config_path)
                self.assertEqual([industry], payload["industries"])

    def test_scope_sentinel_checks_cover_hook_and_commercial_actions(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["commercial"] = False
        candidate["cover"]["hook"] = "立即购买课程"
        self.write_config(candidate)
        self.assert_workflow_error(
            "REVIEW_SCOPE_SENTINEL",
            lambda: review_gate.prepare(self.config_path),
        )

    def test_source_attribution_in_comments_is_not_a_commercial_anchor(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["commercial"] = False
        text = "完整项目来源会在评论区告知，方便你核对。"
        candidate["narration"]["scenes"][0].update(
            {"text": text, "captions": [text]}
        )
        self.write_config(candidate)
        payload = review_gate.prepare(self.config_path)
        self.assertFalse(payload["commercial"])

    def test_scope_sentinel_runs_before_scan_and_verify(self) -> None:
        self.approve_current_narration()
        candidate = copy.deepcopy(self.config)
        text = "医生正在讲解糖尿病治疗方法。"
        candidate["narration"]["scenes"][0].update(
            {"text": text, "captions": [text]}
        )
        candidate["review_scope"] = {"industries": []}
        self.write_config(candidate)
        for action in (
            lambda: review_gate.scan(self.config_path),
            lambda: review_gate.verify_approval(self.config_path),
        ):
            with self.subTest(action=action):
                self.assert_workflow_error("REVIEW_SCOPE_SENTINEL", action)

    def test_cli_works_from_arbitrary_cwd_with_chinese_and_space_paths(self) -> None:
        clean_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        prepare = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "review_gate.py"),
                "prepare",
                "--config",
                str(self.config_path),
            ],
            cwd=self.other_cwd,
            env=clean_env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(0, prepare.returncode, prepare.stderr)
        self.assertEqual(2, json.loads(prepare.stdout)["schema"])
        self.assertTrue((self.project_root / "review" / "review-input.json").is_file())

        blocked = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "workflow.py"),
                "run",
                "--config",
                str(self.config_path),
            ],
            cwd=self.other_cwd,
            env=clean_env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(2, blocked.returncode, blocked.stdout + blocked.stderr)
        self.assertIn("ERROR[REVIEW_BLOCKED]", blocked.stderr)
        self.assertFalse((self.project_root / ".source-led-ai-video").exists())

    def test_config_bound_scan_uses_commercial_and_industry_scope(self) -> None:
        self.config["commercial"] = False
        self.config["review_scope"] = {"industries": ["medical"]}
        self.write_config()
        payload = review_gate.prepare(self.config_path)
        scan = review_gate.scan(self.config_path)

        self.assertFalse(payload["commercial"])
        self.assertEqual(["medical"], payload["industries"])
        self.assertFalse(scan["commercial"])
        self.assertEqual(["medical"], scan["industries"])

        scan["industries"] = []
        _write_json(self.project_root / "review/lexical-scan.json", scan)
        self.assert_workflow_error(
            "REVIEW_SCAN_STALE",
            lambda: review_gate._require_current_evidence(
                self.config, self.project_root
            ),
        )

    def test_policy_fingerprint_change_invalidates_existing_approval(self) -> None:
        self.approve_current_narration()
        with mock.patch.object(review_gate, "policy_sha256", return_value="0" * 64):
            self.assert_workflow_error(
                "REVIEW_STALE",
                lambda: review_gate.verify_approval(self.config_path),
            )


class ProjectPathContractTests(ProjectFixture):
    def test_empty_config_paths_are_rejected(self) -> None:
        cases = (
            ("base video", ("paths", "base_video")),
            ("3x4 generated source", ("cover", "3x4", "generated_source")),
            ("3x4 final cover", ("cover", "3x4", "final")),
            ("4x3 generated source", ("cover", "4x3", "generated_source")),
            ("4x3 final cover", ("cover", "4x3", "final")),
        )
        for label, keys in cases:
            with self.subTest(path=label):
                candidate = copy.deepcopy(self.config)
                target = candidate
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = ""
                self.write_config(candidate)
                self.assert_workflow_error(
                    "PATH_EMPTY",
                    lambda: common.load_and_validate_config(self.config_path),
                )

    def test_any_parent_component_is_rejected(self) -> None:
        cases = (
            ("escaping base", ("paths", "base_video"), "../outside.mp4"),
            ("in-root traversal", ("paths", "base_video"), "素材 文件/../干净 基础.mp4"),
            (
                "cover source traversal",
                ("cover", "3x4", "generated_source"),
                "封面 素材/../来源.png",
            ),
            ("cover final escape", ("cover", "3x4", "final"), "../封面.png"),
        )
        for label, keys, value in cases:
            with self.subTest(path=label):
                candidate = copy.deepcopy(self.config)
                target = candidate
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.write_config(candidate)
                self.assert_workflow_error(
                    "PATH_ESCAPE",
                    lambda: common.load_and_validate_config(self.config_path),
                )


class CoverContractTests(ProjectFixture):
    @staticmethod
    def fake_probe(_ffprobe: Path, path: Path) -> dict:
        width, height = (1080, 1440) if "3x4" in path.name else (1440, 1080)
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "png",
                    "width": width,
                    "height": height,
                }
            ]
        }

    def test_same_generated_source_content_is_rejected_for_both_ratios(self) -> None:
        duplicate = b"\x89PNG\r\n\x1a\nthe same imagegen result must not back both covers"
        (self.project_root / self.source_3x4_relative).write_bytes(duplicate)
        (self.project_root / self.source_4x3_relative).write_bytes(duplicate)
        config, project_root = common.load_and_validate_config(self.config_path)

        with mock.patch.object(workflow, "probe", side_effect=self.fake_probe) as probe:
            self.assert_workflow_error(
                "COVER_VARIANTS",
                lambda: workflow.validate_cover_files(
                    Path("unused-ffprobe"), config, project_root
                ),
            )

        self.assertEqual(4, probe.call_count)

    def test_missing_generated_or_final_cover_is_rejected_before_probe(self) -> None:
        cases = (
            self.source_3x4_relative,
            self.final_3x4_relative,
        )
        for missing_relative in cases:
            with self.subTest(missing=missing_relative.as_posix()):
                missing = self.project_root / missing_relative
                original = missing.read_bytes()
                missing.unlink()
                try:
                    config, project_root = common.load_and_validate_config(
                        self.config_path
                    )
                    with mock.patch.object(workflow, "probe") as probe:
                        self.assert_workflow_error(
                            "PATH_MISSING",
                            lambda: workflow.validate_cover_files(
                                Path("unused-ffprobe"), config, project_root
                            ),
                        )
                    probe.assert_not_called()
                finally:
                    missing.write_bytes(original)


class ReleaseIntegrityContractTests(ProjectFixture):
    def test_non_object_manifest_fails_with_structured_error(self) -> None:
        run_dir = self.project_root / "review-runs" / ("d" * 20)
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "build-manifest.json", [])

        self.assert_workflow_error(
            "MANIFEST_INVALID",
            lambda: workflow.verify_hash_manifest(run_dir, "build-manifest.json"),
        )

    def test_visual_failure_is_recorded_and_blocks_finalize(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        run_dir = self.create_integrity_run("a" * 20)

        visual = workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "逐项检查封面、字幕、场景边界和完整声音，发现外部网址仍然可见。",
            "fail",
            ["no_visible_url_or_external_ui"],
        )

        self.assertEqual("fail", visual["result"])
        self.assertEqual(
            ["no_visible_url_or_external_ui"], visual["failed_checks"]
        )
        self.assertFalse(visual["checks"]["no_visible_url_or_external_ui"])
        self.assertTrue((run_dir / "qa/visual-review.json").is_file())
        self.assert_workflow_error(
            "FINALIZE_QA",
            lambda: workflow.finalize(self.config_path, run_dir),
        )
        self.assertTrue(run_dir.is_dir())
        self.assertFalse((self.project_root / "deliverables" / run_dir.name).exists())

    def test_pending_rights_allow_review_run_but_block_finalize_and_bundle_verify(
        self,
    ) -> None:
        approval = self.approve_current_narration()
        self.assertEqual(review_gate.RIGHTS_PENDING, approval["rights_clearance"])
        run_dir = self.create_integrity_run("e" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
        )

        self.assert_workflow_error(
            "RIGHTS_PENDING",
            lambda: workflow.finalize(self.config_path, run_dir),
        )
        self.assertTrue(run_dir.is_dir())
        self.assertFalse((self.project_root / "deliverables" / run_dir.name).exists())

        artifacts = {
            path.relative_to(run_dir).as_posix(): common.sha256_file(path)
            for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name != "bundle-manifest.json"
        }
        _write_json(
            run_dir / "bundle-manifest.json",
            {
                "schema": 1,
                "kind": "bundle",
                "build_key": run_dir.name,
                "build_manifest_sha256": common.sha256_file(
                    run_dir / "build-manifest.json"
                ),
                "artifacts": artifacts,
            },
        )
        probe_info = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "pix_fmt": "yuv420p",
                    "r_frame_rate": "30/1",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ]
        }
        with mock.patch.object(
            workflow, "run_process", return_value=mock.Mock(returncode=0)
        ), mock.patch.object(
            workflow, "probe", return_value=probe_info
        ), mock.patch.object(
            workflow, "measure_loudness", return_value=(999.0, 999.0)
        ):
            verification = workflow.verify_bundle(
                Path("/locked/ffmpeg"), Path("/locked/ffprobe"), run_dir
            )
        self.assertFalse(verification["passed"])
        self.assertIn(
            "packaged review approval is invalid: RIGHTS_PENDING",
            verification["failures"],
        )

    def test_confirmed_rights_allow_finalize(self) -> None:
        approval = self.approve_current_narration(material_rights="已确认")
        self.assertEqual(review_gate.RIGHTS_CONFIRMED, approval["rights_clearance"])
        run_dir = self.create_integrity_run("f" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
        )

        latest = workflow.finalize(self.config_path, run_dir)
        final_dir = self.project_root / latest["bundle"]
        self.assertTrue(final_dir.is_dir())
        self.assertFalse(run_dir.exists())
        self.assertEqual("f" * 20, latest["build_key"])

    def test_auto_qa_or_manifest_change_invalidates_visual_approval(self) -> None:
        self.approve_current_narration(material_rights="已确认")

        auto_changed_run = self.create_integrity_run("b" * 20)
        workflow.confirm_visual(
            self.config_path,
            auto_changed_run,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
        )
        auto_path = auto_changed_run / "qa/auto-qa.json"
        auto = common.read_json(auto_path)
        auto["loudness_lufs"] = -99.0
        _write_json(auto_path, auto)
        self.assert_workflow_error(
            "MANIFEST_STALE",
            lambda: workflow.finalize(self.config_path, auto_changed_run),
        )

        manifest_changed_run = self.create_integrity_run("c" * 20)
        workflow.confirm_visual(
            self.config_path,
            manifest_changed_run,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
        )
        manifest_path = manifest_changed_run / "build-manifest.json"
        manifest = common.read_json(manifest_path)
        manifest["changed_after_visual_review"] = True
        _write_json(manifest_path, manifest)
        self.assert_workflow_error(
            "VISUAL_STALE",
            lambda: workflow.finalize(self.config_path, manifest_changed_run),
        )

    def test_reuse_tts_without_matching_proof_is_blocked(self) -> None:
        config, project_root = common.load_and_validate_config(self.config_path)
        audio_path = project_root / "素材 文件" / "回归语音.mp3"
        audio_path.write_bytes(b"frozen volcengine replay audio")

        self.assert_workflow_error(
            "REPLAY_PROOF",
            lambda: workflow.validate_replay_audio(
                audio_path,
                None,
                config,
                project_root,
            ),
        )

        proof_path = project_root / "素材 文件" / "回归语音-proof.json"
        _write_json(
            proof_path,
            {
                "schema": 1,
                "purpose": "regression-replay",
                "approved": True,
                "provider": "volcengine",
                "narration_sha256": "0" * 64,
                "audio_sha256": common.sha256_file(audio_path),
            },
        )
        self.assert_workflow_error(
            "REPLAY_PROOF",
            lambda: workflow.validate_replay_audio(
                audio_path,
                proof_path,
                config,
                project_root,
            ),
        )


class CredentialRedactionContractTests(unittest.TestCase):
    def test_tts_http_failure_never_exposes_canary_credentials(self) -> None:
        canaries = {
            "VOLC_TTS_ENDPOINT": volc_tts_adapter.OFFICIAL_ENDPOINT,
            "VOLC_TTS_APP_ID": "CANARY_APP_81f3",
            "VOLC_TTS_ACCESS_TOKEN": "CANARY_TOKEN_9c72",
            "VOLC_TTS_CLUSTER": "CANARY_CLUSTER_44aa",
            "VOLC_TTS_VOICE": "CANARY_VOICE_0fd1",
            "VOLC_TTS_ENCODING": "mp3",
            "VOLC_TTS_SECRET_KEY": "CANARY_SECRET_a7e5",
        }
        sensitive_values = [
            value
            for key, value in canaries.items()
            if key not in {"VOLC_TTS_ENDPOINT", "VOLC_TTS_ENCODING"}
        ]
        response_message = " | ".join(
            sensitive_values
            + [canaries["VOLC_TTS_ACCESS_TOKEN"].encode("utf-8").hex()]
        )
        response_body = json.dumps(
            {"code": "AUTH_FAILURE", "message": response_message}
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            canaries["VOLC_TTS_ENDPOINT"],
            401,
            "unauthorized",
            {},
            io.BytesIO(response_body),
        )

        with tempfile.TemporaryDirectory(prefix="source-led-redaction-") as raw_temp:
            temp_root = Path(raw_temp)
            output = temp_root / "输出 音频.mp3"
            cache = temp_root / "缓存 目录"
            config = {
                "narration": {
                    "language": "Chinese",
                    "scenes": [
                        {
                            "id": "scene-01",
                            "text": "这是一段不会真的发送到网络的测试口播。",
                            "captions": ["这是一段不会真的发送到网络的测试口播。"],
                        }
                    ],
                }
            }
            approval = {
                "narration_sha256": common.narration_hash(config),
                "review_payload_sha256": "f" * 64,
            }
            with mock.patch.object(
                review_gate, "verify_approval", return_value=approval
            ), mock.patch.object(
                volc_tts_adapter,
                "load_and_validate_config",
                return_value=(config, temp_root),
            ), mock.patch.object(
                volc_tts_adapter,
                "resolve_media_tools",
                return_value=(Path("/locked/ffmpeg"), Path("/locked/ffprobe")),
            ), mock.patch.object(
                volc_tts_adapter,
                "_open_request",
                side_effect=http_error,
            ) as open_request:
                with self.assertRaises(common.WorkflowError) as raised:
                    volc_tts_adapter.synthesize(
                        temp_root / "project.json",
                        output,
                        canaries,
                        cache,
                    )

            self.assertEqual("TTS_HTTP", raised.exception.code)
            open_request.assert_called_once()
            exposed_text = str(raised.exception)
            artifact_bytes = b"".join(
                path.read_bytes() for path in cache.rglob("*") if path.is_file()
            )
            artifact_names = "\n".join(
                path.name for path in cache.rglob("*")
            )
            for canary in sensitive_values:
                with self.subTest(canary=canary):
                    self.assertNotIn(canary, exposed_text)
                    self.assertNotIn(canary.encode("utf-8"), artifact_bytes)
                    self.assertNotIn(canary, artifact_names)
            token_hex = canaries["VOLC_TTS_ACCESS_TOKEN"].encode("utf-8").hex()
            self.assertNotIn(token_hex, exposed_text)
            self.assertNotIn(token_hex.encode("ascii"), artifact_bytes)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

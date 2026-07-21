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
        evidence = run_dir / "qa/evidence-frames/frame-000000.jpg"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(b"offline full-resolution evidence frame")
        _write_json(
            run_dir / "qa/review-points.json",
            {
                "schema": 1,
                "video_sha256": video_hash,
                "points": [
                    {
                        "id": "opening",
                        "label": "开场完整画面",
                        "frame": 0,
                        "time_seconds": 0.0,
                        "path": "qa/evidence-frames/frame-000000.jpg",
                        "sha256": common.sha256_file(evidence),
                    }
                ],
            },
        )
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
                "inputs": {
                    "base_sha256": common.sha256_file(
                        self.project_root / self.base_relative
                    ),
                    "bgm_sha256": None,
                    "source_assets": {},
                },
            },
        )
        workflow.write_build_manifest(
            run_dir,
            self.config,
            build_key,
            workflow.implementation_fingerprint(),
        )
        return run_dir

    def create_rights_ledger(self) -> None:
        evidence = self.project_root / "private/rights-evidence/base.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("offline rights evidence\n", encoding="utf-8")
        record = {
            "schema": 1,
            "event": "rights_confirmed",
            "asset_sha256": common.sha256_file(
                self.project_root / self.base_relative
            ),
            "platforms": ["douyin"],
            "commercial": True,
            "expires_at": None,
            "rights_basis": "owned fixture media",
            "evidence_path": "private/rights-evidence/base.txt",
            "evidence_sha256": common.sha256_file(evidence),
            "confirmed_by": "offline contract reviewer",
        }
        ledger = self.project_root / "private/rights-events.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def assert_workflow_error(self, expected_code: str, callable_: object) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            callable_()
        self.assertEqual(expected_code, raised.exception.code)


class ReviewGateContractTests(ProjectFixture):
    def test_packaged_approval_accepts_project_v2_contract(self) -> None:
        packaged = copy.deepcopy(self.config)
        packaged["version"] = 2
        self.write_config(packaged)
        expected = {"schema": 2, "result": "pass"}
        with (
            mock.patch.object(review_gate, "validate_scope_sentinel") as scope,
            mock.patch.object(
                review_gate,
                "_verify_approval_for_config",
                return_value=expected,
            ) as verify,
        ):
            self.assertEqual(
                expected,
                review_gate.verify_packaged_approval(self.config_path),
            )

        scope.assert_called_once_with(packaged)
        verify.assert_called_once_with(packaged, self.project_root.resolve())

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

    def test_lexical_scan_covers_narration_and_cover_hook_with_source_labels(self) -> None:
        candidate = copy.deepcopy(self.config)
        narration = "这个演示号称全网第一，仍然需要结合证据判断。"
        candidate["narration"]["scenes"][0].update(
            {"text": narration, "captions": [narration]}
        )
        candidate["cover"]["hook"] = "全网第一成片"
        self.write_config(candidate)

        review_gate.prepare(self.config_path)
        scan = review_gate.scan(self.config_path)

        matched_sources = {
            hit["source"]
            for hit in scan["candidates"]
            if "全网第一" in hit["match"]
        }
        self.assertEqual({"narration", "cover_hook"}, matched_sources)
        self.assertEqual(["narration", "cover_hook"], scan["scanned_sources"])
        self.assertEqual(len("全网第一成片"), scan["cover_hook_characters"])
        for collection in review_gate.LEXICAL_HIT_COLLECTIONS:
            for hit in scan[collection]:
                self.assertIn(hit["source"], {"narration", "cover_hook"})

        legacy_narration_scan = precheck_scan.scan(
            narration + "\n",
            commercial=True,
            industries=set(),
            radius=24,
        )
        self.assertEqual(legacy_narration_scan["text_sha256"], scan["text_sha256"])
        self.assertEqual(legacy_narration_scan["characters"], scan["characters"])

    def test_cover_hook_hit_is_hash_bound_to_approval_evidence(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["hook"] = "全网第一成片"
        self.write_config(candidate)
        self.approve_current_narration()

        scan_path = self.project_root / "review/lexical-scan.json"
        lexical_scan = common.read_json(scan_path)
        lexical_scan["candidates"] = [
            hit
            for hit in lexical_scan["candidates"]
            if hit.get("source") != "cover_hook"
        ]
        _write_json(scan_path, lexical_scan)

        self.assert_workflow_error(
            "REVIEW_SCAN_STALE",
            lambda: review_gate.verify_approval(self.config_path),
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
    def test_packaged_v2_config_rejects_external_contract_paths(self) -> None:
        packaged = copy.deepcopy(self.config)
        packaged["version"] = 2
        packaged["source_package"] = {"manifest": "source-package/manifest.json"}
        packaged["edit"] = {
            "plan": "edit/edit-plan.json",
            "timeline_lock": "edit/timeline.lock.json",
        }
        packaged["cover"]["prompt_record"] = "covers/cover-prompt.json"
        packaged["narration"]["scenes"][0].update(
            {
                "purpose": "结果钩子",
                "claim_ids": ["claim-01"],
                "asset_ids": ["asset-01"],
                "caption_region": "bottom",
            }
        )
        for relative in (
            "source-package/manifest.json",
            "edit/edit-plan.json",
            "edit/timeline.lock.json",
            "covers/cover-prompt.json",
        ):
            _write_json(self.project_root / relative, {})
        workflow.validate_packaged_config(self.project_root, packaged)

        for escaped in ("../outside.json", "/private/tmp/outside.json"):
            with self.subTest(path=escaped):
                invalid = copy.deepcopy(packaged)
                invalid["source_package"]["manifest"] = escaped
                self.assert_workflow_error(
                    "BUNDLE_CONFIG",
                    lambda invalid=invalid: workflow.validate_packaged_config(
                        self.project_root, invalid
                    ),
                )

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
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
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
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("f" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )

        latest = workflow.finalize(self.config_path, run_dir)
        final_dir = self.project_root / latest["bundle"]
        self.assertTrue(final_dir.is_dir())
        self.assertTrue(run_dir.exists())
        self.assertEqual("f" * 20, latest["build_key"])
        self.assertEqual(latest, workflow.finalize(self.config_path, run_dir))

    def test_new_rights_ledger_creates_new_immutable_release(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("6" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        first = workflow.finalize(self.config_path, run_dir)
        ledger = self.project_root / "private/rights-events.jsonl"
        renewed_evidence = self.project_root / "private/rights-evidence/base-renewed.txt"
        renewed_evidence.write_text("renewed rights evidence\n", encoding="utf-8")
        renewed = json.loads(ledger.read_text(encoding="utf-8"))
        renewed["rights_basis"] = "renewed owned fixture media"
        renewed["evidence_path"] = "private/rights-evidence/base-renewed.txt"
        renewed["evidence_sha256"] = common.sha256_file(renewed_evidence)
        ledger.write_text(
            ledger.read_text(encoding="utf-8") + json.dumps(renewed) + "\n",
            encoding="utf-8",
        )
        second = workflow.finalize(self.config_path, run_dir)
        self.assertEqual(first["build_key"], second["build_key"])
        self.assertNotEqual(first["release_key"], second["release_key"])
        self.assertNotEqual(first["bundle"], second["bundle"])
        self.assertTrue((self.project_root / first["bundle"]).is_dir())
        self.assertTrue((self.project_root / second["bundle"]).is_dir())

    def test_finalize_revalidates_staging_after_copy(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("7" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        original_copy = workflow.copy_rights_evidence

        def corrupt_after_copy(project_root: Path, staging: Path, rights: dict) -> None:
            original_copy(project_root, staging, rights)
            (staging / "private/rights-events.jsonl").write_text(
                '{"schema":1,"event":"unknown"}\n', encoding="utf-8"
            )

        with mock.patch.object(
            workflow, "copy_rights_evidence", side_effect=corrupt_after_copy
        ):
            self.assert_workflow_error(
                "FINALIZE_RIGHTS_RACE",
                lambda: workflow.finalize(self.config_path, run_dir),
            )
        deliverables = self.project_root / "deliverables"
        self.assertFalse((deliverables / "latest.json").exists())
        self.assertEqual([], list(deliverables.iterdir()))

    def test_release_key_is_derived_from_the_copied_rights_snapshot(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("9" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        original_copy = workflow.copy_rights_evidence

        def append_live_after_snapshot(
            project_root: Path, staging: Path, rights: dict
        ) -> None:
            original_copy(project_root, staging, rights)
            ledger = project_root / "private/rights-events.jsonl"
            extra = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
            extra["rights_basis"] = "new live confirmation after copied snapshot"
            ledger.write_text(
                ledger.read_text(encoding="utf-8") + json.dumps(extra) + "\n",
                encoding="utf-8",
            )

        with mock.patch.object(
            workflow, "copy_rights_evidence", side_effect=append_live_after_snapshot
        ):
            latest = workflow.finalize(self.config_path, run_dir)

        bundle = self.project_root / latest["bundle"]
        bundled_ledger_hash = common.sha256_file(
            bundle / "private/rights-events.jsonl"
        )
        self.assertEqual(
            workflow.release_key_for(run_dir.name, bundled_ledger_hash),
            latest["release_key"],
        )
        self.assertNotEqual(
            bundled_ledger_hash,
            common.sha256_file(self.project_root / "private/rights-events.jsonl"),
        )
        self.assertEqual([], workflow.verify_bundle_contracts(bundle)["failures"])

    def test_corrupt_existing_release_is_quarantined_and_rebuilt(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("a" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        first = workflow.finalize(self.config_path, run_dir)
        final_dir = self.project_root / first["bundle"]
        video = final_dir / "video.mp4"
        os.chmod(video, 0o600)
        video.write_bytes(b"corrupt published bytes")

        rebuilt = workflow.finalize(self.config_path, run_dir)
        self.assertEqual(first["release_key"], rebuilt["release_key"])
        rebuilt_dir = self.project_root / rebuilt["bundle"]
        self.assertEqual(
            common.sha256_file(run_dir / "video.mp4"),
            common.sha256_file(rebuilt_dir / "video.mp4"),
        )
        self.assertTrue(
            any(
                path.is_dir() and ".quarantine-" in path.name
                for path in (self.project_root / "deliverables").iterdir()
            )
        )
        self.assertEqual([], workflow.verify_bundle_contracts(rebuilt_dir)["failures"])

    def test_revoked_live_rights_make_latest_stale_in_status(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("b" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        workflow.finalize(self.config_path, run_dir)
        ledger = self.project_root / "private/rights-events.jsonl"
        current = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        revoked = {
            "schema": 1,
            "event": "rights_revoked",
            "asset_sha256": current["asset_sha256"],
            "revoked_at": "2026-07-21T14:00:00Z",
            "reason": "permission withdrawn",
            "confirmed_by": "offline contract reviewer",
        }
        ledger.write_text(
            ledger.read_text(encoding="utf-8") + json.dumps(revoked) + "\n",
            encoding="utf-8",
        )

        status = workflow.workflow_status(self.config_path)
        self.assertEqual("RIGHTS_SCOPE", status["rights_status"])
        self.assertEqual("rights_not_ready", status["latest_status"])
        self.assertEqual(
            "record active Douyin commercial rights and evidence",
            status["next_action"],
        )

    def test_status_does_not_treat_a_stale_run_or_release_as_current(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("c" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        workflow.finalize(self.config_path, run_dir)

        changed = copy.deepcopy(self.config)
        changed["narration"]["scenes"][0]["text"] = "当前文案已修改，旧成片不能继续使用。"
        changed["narration"]["scenes"][0]["captions"] = [
            "当前文案已修改，旧成片不能继续使用。"
        ]
        self.write_config(changed)

        status = workflow.workflow_status(self.config_path)
        self.assertIsNone(status["active_review_run"])
        self.assertEqual("superseded_build", status["latest_status"])
        self.assertIn("valid publish approval", status["next_action"])
        self.assertNotIn("verify the latest", status["next_action"])
        self.assertNotIn("finalize", status["next_action"])

    def test_finalize_quarantines_failed_post_publish_verification(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("8" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        original_verify = workflow.verify_bundle_contracts

        def fail_only_after_publish(bundle: Path, **kwargs) -> dict:
            result = original_verify(bundle, **kwargs)
            if not bundle.name.startswith("."):
                result = dict(result)
                result["failures"] = list(result["failures"]) + [
                    "simulated post-publish mutation"
                ]
            return result

        with mock.patch.object(
            workflow,
            "verify_bundle_contracts",
            side_effect=fail_only_after_publish,
        ):
            self.assert_workflow_error(
                "FINALIZE_PUBLISHED",
                lambda: workflow.finalize(self.config_path, run_dir),
            )

        deliverables = self.project_root / "deliverables"
        self.assertFalse((deliverables / "latest.json").exists())
        self.assertFalse(
            any(path.is_dir() and not path.name.startswith(".") for path in deliverables.iterdir())
        )
        self.assertTrue(
            any(
                path.is_dir() and ".quarantine-" in path.name
                for path in deliverables.iterdir()
            )
        )

    def test_finalize_permission_failure_is_structured_and_cleans_staging(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        run_dir = self.create_integrity_run("d" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        with mock.patch.object(
            workflow.os, "chmod", side_effect=OSError("simulated chmod failure")
        ):
            self.assert_workflow_error(
                "FINALIZE_PERMISSIONS",
                lambda: workflow.finalize(self.config_path, run_dir),
            )
        self.assertEqual(
            [], list((self.project_root / "deliverables").iterdir())
        )

    def test_finalize_copies_evidence_for_revoked_confirmation_history(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        self.create_rights_ledger()
        ledger = self.project_root / "private/rights-events.jsonl"
        current = json.loads(ledger.read_text(encoding="utf-8"))
        old_evidence = self.project_root / "private/rights-evidence/old-base.txt"
        old_evidence.write_text("older permission evidence\n", encoding="utf-8")
        old = dict(current)
        old["evidence_path"] = "private/rights-evidence/old-base.txt"
        old["evidence_sha256"] = common.sha256_file(old_evidence)
        revoked = {
            "schema": 1,
            "event": "rights_revoked",
            "asset_sha256": current["asset_sha256"],
            "revoked_at": "2026-07-21T09:00:00Z",
            "reason": "旧授权撤回后重新确认",
            "confirmed_by": "offline contract reviewer",
        }
        ledger.write_text(
            "\n".join(json.dumps(item) for item in (old, revoked, current)) + "\n",
            encoding="utf-8",
        )

        run_dir = self.create_integrity_run("1" * 20)
        workflow.confirm_visual(
            self.config_path,
            run_dir,
            "离线合约审核",
            "完整检查封面、字幕、场景边界、信息卡和声音后记录视觉通过。",
            "pass",
            [],
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
        )
        latest = workflow.finalize(self.config_path, run_dir)
        bundle = self.project_root / latest["bundle"]
        self.assertTrue((bundle / "private/rights-evidence/old-base.txt").is_file())
        self.assertTrue((bundle / "private/rights-evidence/base.txt").is_file())
        verified = workflow.verify_rights_ledger(
            bundle,
            None,
            required_asset_hashes={current["asset_sha256"]: "base_video"},
            require_schema2=False,
        )
        self.assertEqual(1, len(verified["records"]))

    def test_schema2_visual_record_cannot_finalize_after_v12(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        run_dir = self.create_integrity_run("9" * 20)
        manifest = common.read_json(run_dir / "build-manifest.json")
        record = {
            "schema": 2,
            "result": "pass",
            "reviewer": "旧版审核记录",
            "notes": "旧版记录没有完整听审时长和真实证据帧绑定。",
            "failed_checks": [],
            "checks": {name: True for name in workflow.VISUAL_CHECKS},
            "build_manifest_sha256": common.sha256_file(
                run_dir / "build-manifest.json"
            ),
            "video_sha256": manifest["artifacts"]["video.mp4"],
            "contact_sheet_sha256": manifest["artifacts"]["qa/contact-sheet.jpg"],
            "cover_3x4_sha256": manifest["artifacts"]["covers/cover-3x4.png"],
            "cover_4x3_sha256": manifest["artifacts"]["covers/cover-4x3.png"],
        }
        _write_json(run_dir / "qa/visual-review.json", record)
        self.assert_workflow_error(
            "VISUAL_REVIEW_STALE",
            lambda: workflow.finalize(self.config_path, run_dir),
        )

    def test_pass_requires_full_listen_and_every_generated_review_point(self) -> None:
        self.approve_current_narration(material_rights="已确认")
        run_dir = self.create_integrity_run("8" * 20)
        self.assert_workflow_error(
            "VISUAL_INCOMPLETE_LISTEN",
            lambda: workflow.confirm_visual(
                self.config_path,
                run_dir,
                "离线合约审核",
                "已检查证据帧但尚未完成整条视频的听审。",
                "pass",
                [],
                listened_seconds=2.0,
                evidence_frames=[
                    {"time_seconds": 0.0, "label": "已检查开场证据帧"}
                ],
            ),
        )
        self.assert_workflow_error(
            "VISUAL_EVIDENCE_REQUIRED",
            lambda: workflow.confirm_visual(
                self.config_path,
                run_dir,
                "离线合约审核",
                "已听完整条视频，但没有提交必须检查的证据帧。",
                "pass",
                [],
                listened_seconds=3.0,
                evidence_frames=[],
            ),
        )

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
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
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
            listened_seconds=3.0,
            evidence_frames=[{"time_seconds": 0.0, "label": "已检查开场证据帧"}],
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

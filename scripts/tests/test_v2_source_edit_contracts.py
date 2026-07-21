from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edit_plan  # noqa: E402
import init_project  # noqa: E402
import rights_ledger  # noqa: E402
import source_package  # noqa: E402
import workflow  # noqa: E402
from lib import common  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


class V2ContractFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="source-led-v2-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文 项目"
        self.root.mkdir(parents=True)
        files = {
            "source/base.mp4": b"clean base",
            "source/bgm.wav": b"music",
            "source-package/raw/source-01.txt": b"author evidence\n",
            "source/demo.mp4": b"source video bytes",
        }
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        template_path = SCRIPTS_DIR.parent / "assets/project-template.json"
        self.config = json.loads(template_path.read_text(encoding="utf-8"))
        self.config["project_name"] = "V2 合约"
        self.config["paths"] = {"base_video": "source/base.mp4", "bgm": "source/bgm.wav"}
        self.config["narration"]["scenes"] = [
            {
                "id": "scene-01",
                "purpose": "结果钩子",
                "text": "展示真实结果",
                "claim_ids": ["claim-01"],
                "asset_ids": ["asset-01"],
                "caption_region": "bottom",
                "captions": ["展示真实结果"],
            }
        ]
        self.config["source_timeline"]["scene_boundaries_seconds"] = [0.0, 5.0]
        self.config_path = self.root / "project.json"
        _write_json(self.config_path, self.config)

        source_snapshot = self.root / "source-package/raw/source-01.txt"
        video_asset = self.root / "source/demo.mp4"
        self.package_draft = {
            "manifest": {
                "version": 1,
                "source_type": "github",
                "source_url": "https://github.com/example/project",
                "captured_at": "2026-07-21T10:00:00+08:00",
                "analyzer_version": "source-analyzer-1",
                "schema_version": 1,
                "revision": {"commit": "abc123"},
                "sources": [
                    {
                        "source_id": "source-01",
                        "kind": "github_readme",
                        "url": "https://github.com/example/project/blob/main/README.md",
                        "captured_at": "2026-07-21T10:00:00+08:00",
                        "content_path": "source-package/raw/source-01.txt",
                        "content_sha256": common.sha256_file(source_snapshot),
                    }
                ],
            },
            "claims": [
                {
                    "claim_id": "claim-01",
                    "statement": "作者展示了一个可见结果。",
                    "classification": "author_statement",
                    "confidence": "high",
                    "verified_at": "2026-07-21T10:05:00+08:00",
                    "evidence": [{"source_id": "source-01", "locator": "README lines 1-2"}],
                }
            ],
            "assets": {
                "version": 1,
                "items": [
                    {
                        "asset_id": "asset-01",
                        "kind": "video",
                        "source_id": "source-01",
                        "local_path": "source/demo.mp4",
                        "sha256": common.sha256_file(video_asset),
                        "rights_status": "pending",
                        "frame_count": 150,
                        "width": 1080,
                        "height": 1920,
                    }
                ],
            },
            "toolchain": {
                "version": 1,
                "steps": [
                    {
                        "step_id": "step-01",
                        "tool": "Example Tool",
                        "action": "生成演示结果",
                        "claim_ids": ["claim-01"],
                    }
                ],
            },
            "brief": "项目展示了一个有来源证据的具体结果。",
        }
        self.plan = {
            "version": 1,
            "project_version": 2,
            "fps": 30,
            "scenes": [
                {
                    "scene_id": "scene-01",
                    "purpose": "结果钩子",
                    "caption_region": "bottom",
                    "shots": [
                        {
                            "asset_id": "asset-01",
                            "source_in_frame": 0,
                            "source_out_frame": 150,
                            "timeline_weight": 1,
                            "track_id": "V1",
                            "crop_mode": "fit",
                            "allow_scale_up": False,
                        }
                    ],
                }
            ],
        }

    def build_package(self) -> dict:
        return source_package.write_source_package(self.config_path, self.package_draft)

    def completed_cover_record(self) -> dict:
        record = edit_plan.cover_prompt_template(self.config)
        for ratio, result_id in (("3x4", "call-portrait"), ("4x3", "call-landscape")):
            source = self.root / self.config["cover"][ratio]["generated_source"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(("independent-" + ratio).encode("utf-8"))
            record["generations"][ratio].update(
                {
                    "status": "completed",
                    "generation_version": 1,
                    "prompt": f"独立生成 {ratio} 中文封面",
                    "model": "imagegen",
                    "generated_at": "2026-07-21T12:00:00+08:00",
                    "result_id": result_id,
                    "source_sha256": common.sha256_file(source),
                }
            )
        return record


class ProjectV2Tests(V2ContractFixture):
    def test_required_cta_is_enforced_when_configured(self) -> None:
        self.config["narration"]["required_cta"] = common.FIXED_NARRATION_CTA
        _write_json(self.config_path, self.config)
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.config_path)
        self.assertEqual("CONFIG_REQUIRED_CTA", raised.exception.code)

        self.config["narration"]["scenes"][0]["text"] += common.FIXED_NARRATION_CTA
        self.config["narration"]["scenes"][0]["captions"] = [
            self.config["narration"]["scenes"][0]["text"]
        ]
        _write_json(self.config_path, self.config)
        common.load_and_validate_config(self.config_path)

    def test_v2_and_legacy_v1_are_both_accepted(self) -> None:
        config, _ = common.load_and_validate_config(self.config_path)
        self.assertEqual(2, config["version"])
        legacy = copy.deepcopy(self.config)
        legacy["version"] = 1
        legacy.pop("source_package")
        legacy.pop("edit")
        legacy["cover"].pop("prompt_record")
        for scene in legacy["narration"]["scenes"]:
            for field in ("purpose", "claim_ids", "asset_ids", "caption_region"):
                scene.pop(field)
        _write_json(self.config_path, legacy)
        loaded, _ = common.load_and_validate_config(self.config_path)
        self.assertEqual(1, loaded["version"])

    def test_v2_rejects_invalid_version_and_missing_scene_contract(self) -> None:
        invalid = copy.deepcopy(self.config)
        invalid["version"] = 3
        _write_json(self.config_path, invalid)
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.config_path)
        self.assertEqual("CONFIG_VERSION", raised.exception.code)
        invalid = copy.deepcopy(self.config)
        invalid["narration"]["scenes"][0].pop("claim_ids")
        _write_json(self.config_path, invalid)
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.config_path)
        self.assertEqual("CONFIG_SCENE", raised.exception.code)

    def test_v2_contract_paths_are_fixed_and_cannot_overwrite_project_files(self) -> None:
        mutations = (
            ("source_package", "manifest", "project.json"),
            ("edit", "plan", "review/approval.json"),
            ("edit", "timeline_lock", "private/rights-events.jsonl"),
            ("cover", "prompt_record", "project.json"),
        )
        for section, field, value in mutations:
            with self.subTest(path=f"{section}.{field}"):
                invalid = copy.deepcopy(self.config)
                invalid[section][field] = value
                _write_json(self.config_path, invalid)
                with self.assertRaises(common.WorkflowError) as raised:
                    common.load_and_validate_config(self.config_path)
                self.assertIn(raised.exception.code, {"CONFIG_PATH", "CONFIG_COVER"})

    def test_init_creates_v2_skeleton_and_explicit_v1(self) -> None:
        v2_root = Path(self.temporary.name) / "初始化 V2"
        with mock.patch.object(
            sys,
            "argv",
            ["init_project.py", "--project", str(v2_root), "--name", "初始化项目"],
        ):
            self.assertEqual(0, init_project.main())
        initialized = json.loads((v2_root / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(2, initialized["version"])
        self.assertTrue((v2_root / "source-package/raw").is_dir())
        self.assertTrue((v2_root / "edit").is_dir())
        draft = json.loads(
            (v2_root / "source-package-draft.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"manifest", "claims", "assets", "toolchain", "brief"},
            set(draft),
        )
        self.assertEqual("x_post", draft["manifest"]["sources"][0]["kind"])
        self.assertNotIn("package_sha256", draft["manifest"])
        self.assertTrue((v2_root / "covers/cover-prompt.json").is_file())
        edit_plan.validate_cover_prompt(v2_root / "project.json")

        v1_root = Path(self.temporary.name) / "初始化 V1"
        with mock.patch.object(
            sys,
            "argv",
            [
                "init_project.py",
                "--project",
                str(v1_root),
                "--name",
                "旧项目",
                "--project-version",
                "1",
            ],
        ):
            self.assertEqual(0, init_project.main())
        legacy = json.loads((v1_root / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(1, legacy["version"])
        self.assertNotIn("source_package", legacy)
        self.assertFalse((v1_root / "source-package-draft.json").exists())


class SourcePackageTests(V2ContractFixture):
    def test_write_validate_and_detect_tampering(self) -> None:
        checked = self.build_package()
        self.assertEqual(["claim-01"], checked["claim_ids"])
        manifest = json.loads(
            (self.root / "source-package/manifest.json").read_text(encoding="utf-8")
        )
        self.assertRegex(manifest["package_sha256"], r"^[0-9a-f]{64}$")
        (self.root / "source-package/brief.md").write_text("篡改", encoding="utf-8")
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.validate_source_package(self.config_path)
        self.assertEqual("SOURCE_PACKAGE_HASH", raised.exception.code)

    def test_rejects_unknown_evidence_and_path_escape(self) -> None:
        invalid = copy.deepcopy(self.package_draft)
        invalid["claims"][0]["evidence"][0]["source_id"] = "source-unknown"
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, invalid)
        self.assertEqual("SOURCE_PACKAGE_EVIDENCE", raised.exception.code)

        invalid = copy.deepcopy(self.package_draft)
        invalid["assets"]["items"][0]["local_path"] = "../outside.mp4"
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, invalid)
        self.assertEqual("PATH_ESCAPE", raised.exception.code)

    def test_rejects_source_package_version_drift(self) -> None:
        invalid = copy.deepcopy(self.package_draft)
        invalid["manifest"]["version"] = 2
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, invalid)
        self.assertEqual("SOURCE_PACKAGE_VERSION", raised.exception.code)

    def test_scene_bindings_reject_unknown_unverified_and_rejected_items(self) -> None:
        invalid_config = copy.deepcopy(self.config)
        invalid_config["narration"]["scenes"][0]["claim_ids"] = ["claim-unknown"]
        _write_json(self.config_path, invalid_config)
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, self.package_draft)
        self.assertEqual("SOURCE_PACKAGE_BINDING", raised.exception.code)

        _write_json(self.config_path, self.config)
        unverified = copy.deepcopy(self.package_draft)
        unverified["claims"][0]["classification"] = "unverified"
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, unverified)
        self.assertEqual("SOURCE_PACKAGE_UNVERIFIED", raised.exception.code)

        rejected = copy.deepcopy(self.package_draft)
        rejected["assets"]["items"][0]["rights_status"] = "rejected"
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, rejected)
        self.assertEqual("SOURCE_PACKAGE_RIGHTS", raised.exception.code)

    def test_corroborated_fact_requires_two_source_ids(self) -> None:
        invalid = copy.deepcopy(self.package_draft)
        invalid["claims"][0]["classification"] = "corroborated_fact"
        with self.assertRaises(common.WorkflowError) as raised:
            source_package.write_source_package(self.config_path, invalid)
        self.assertEqual("SOURCE_PACKAGE_EVIDENCE", raised.exception.code)


class V2RightsIdentityTests(V2ContractFixture):
    def _evidence(self, name: str) -> str:
        relative = f"private/rights-evidence/{name}.txt"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"permission evidence for {name}\n", encoding="utf-8")
        return relative

    def _confirm(self, asset_id: str, source_id: str, evidence_path: str) -> dict:
        return rights_ledger.confirm(
            self.config_path,
            asset_id=asset_id,
            source_id=source_id,
            author="素材作者",
            source="原始项目来源",
            rights_basis="书面商业发布授权",
            evidence_path=evidence_path,
            confirmed_by="审核员",
            expires_at=None,
            attribution_required=False,
            attribution_placement="none",
            attribution_text="",
        )

    def test_source_asset_confirmation_rejects_wrong_source_id(self) -> None:
        self.package_draft["assets"]["items"][0]["rights_status"] = "confirmed"
        self.build_package()
        with self.assertRaises(common.WorkflowError) as raised:
            self._confirm(
                "asset-01", "source-wrong", self._evidence("asset-wrong-source")
            )
        self.assertEqual("RIGHTS_SOURCE_ID", raised.exception.code)
        self.assertFalse((self.root / "private/rights-events.jsonl").exists())

    def test_identical_hash_assets_keep_independent_rights_state(self) -> None:
        self.config["paths"]["bgm"] = ""
        _write_json(self.config_path, self.config)
        shared_bytes = b"same bytes used by two distinct asset identities"
        (self.root / "source/base.mp4").write_bytes(shared_bytes)
        source_asset = self.root / "source/demo.mp4"
        source_asset.write_bytes(shared_bytes)
        asset = self.package_draft["assets"]["items"][0]
        asset["sha256"] = common.sha256_file(source_asset)
        asset["rights_status"] = "confirmed"
        self.build_package()

        base_evidence = self._evidence("base")
        asset_evidence = self._evidence("asset-01")
        self._confirm("base_video", "source-base-video", base_evidence)
        self._confirm("asset-01", "source-01", asset_evidence)

        loaded, _ = common.load_and_validate_config(self.config_path)
        required = workflow.required_rights_assets(self.root, loaded)
        self.assertEqual(
            ["base_video", "asset-01"],
            [item["asset_id"] for item in required],
        )
        self.assertEqual(1, len({item["asset_sha256"] for item in required}))
        verified = workflow.verify_rights_ledger(self.root, loaded)
        self.assertEqual(
            ["base_video", "asset-01"],
            [record["asset_id"] for record in verified["records"]],
        )

        revoked = rights_ledger.revoke(
            self.config_path,
            asset_id="asset-01",
            reason="该来源授权暂时撤回",
            confirmed_by="审核员",
        )
        self.assertEqual("asset-01", revoked["asset_id"])
        with self.assertRaises(common.WorkflowError) as raised:
            workflow.verify_rights_ledger(self.root, loaded)
        self.assertEqual("RIGHTS_SCOPE", raised.exception.code)
        base_only = workflow.verify_rights_ledger(
            self.root,
            None,
            required_asset_hashes=[required[0]],
            require_schema2=True,
        )
        self.assertEqual("base_video", base_only["records"][0]["asset_id"])

        self._confirm("asset-01", "source-01", asset_evidence)
        restored = workflow.verify_rights_ledger(self.root, loaded)
        self.assertEqual(2, len(restored["records"]))


class EditPlanTests(V2ContractFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_package()

    def test_compile_and_validate_integer_frame_lock(self) -> None:
        edit_plan.write_edit_plan(self.config_path, self.plan)
        durations = {
            "version": 1,
            "fps": 30,
            "alignment_sha256": "a" * 64,
            "scenes": [{"scene_id": "scene-01", "duration_frames": 150}],
        }
        lock = edit_plan.compile_timeline(self.config_path, durations)
        self.assertEqual(150, lock["total_frames"])
        self.assertEqual(0, lock["scenes"][0]["clips"][0]["timeline_start_frame"])
        self.assertEqual(150, lock["scenes"][0]["clips"][0]["timeline_end_frame"])
        edit_plan.validate_timeline_lock(self.config_path)

    def test_rejects_frame_overrun_and_information_card_escape(self) -> None:
        invalid = copy.deepcopy(self.plan)
        invalid["scenes"][0]["shots"][0]["source_out_frame"] = 151
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.write_edit_plan(self.config_path, invalid)
        self.assertEqual("EDIT_PLAN_FRAME", raised.exception.code)

        invalid = copy.deepcopy(self.plan)
        invalid["scenes"][0]["information_card"] = {"path": "../card.html"}
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.write_edit_plan(self.config_path, invalid)
        self.assertEqual("PROJECT_OUTPUT", raised.exception.code)

        invalid = copy.deepcopy(self.plan)
        invalid["scenes"][0]["shots"][0]["source_out_frame"] = 149
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.write_edit_plan(self.config_path, invalid)
        self.assertEqual("EDIT_PLAN_FRAME", raised.exception.code)

    def test_rejects_lock_version_and_frame_drift(self) -> None:
        edit_plan.write_edit_plan(self.config_path, self.plan)
        durations = {
            "version": 1,
            "fps": 30,
            "alignment_sha256": "a" * 64,
            "scenes": [{"scene_id": "scene-01", "duration_frames": 150}],
        }
        lock = edit_plan.compile_timeline(self.config_path, durations)
        invalid = copy.deepcopy(lock)
        invalid["version"] = 2
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_timeline_lock(self.config_path, invalid)
        self.assertEqual("TIMELINE_VERSION", raised.exception.code)
        invalid = copy.deepcopy(lock)
        invalid["scenes"][0]["clips"][0]["source_out_frame"] = 151
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_timeline_lock(self.config_path, invalid)
        self.assertEqual("TIMELINE_FRAME", raised.exception.code)
        invalid = copy.deepcopy(lock)
        invalid["durations_sha256"] = "b" * 64
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_timeline_lock(self.config_path, invalid)
        self.assertEqual("TIMELINE_HASH", raised.exception.code)


class CoverPromptTests(V2ContractFixture):
    def test_sync_after_initial_placeholder_hook_change(self) -> None:
        initial = edit_plan.initialize_cover_prompt(self.config_path)
        self.assertEqual("1", initial["prompt_version"])
        changed = copy.deepcopy(self.config)
        changed["cover"]["hook"] = "玩具照片变游戏"
        changed["cover"]["headline_lines"] = ["玩具照片", "变游戏"]
        changed["cover"]["3x4"]["final"] = "covers/封面-3x4-玩具照片变游戏.png"
        changed["cover"]["4x3"]["final"] = "covers/封面-4x3-玩具照片变游戏.png"
        _write_json(self.config_path, changed)

        synced = edit_plan.sync_cover_prompt(self.config_path)
        self.assertEqual("2", synced["prompt_version"])
        self.assertEqual("玩具照片变游戏", synced["hook"])
        self.assertEqual("pending", synced["generations"]["3x4"]["status"])
        self.assertEqual("pending", synced["generations"]["4x3"]["status"])
        edit_plan.validate_cover_prompt(self.config_path)

    def test_sync_resets_only_changed_ratio_and_preserves_completed_other(self) -> None:
        edit_plan.initialize_cover_prompt(self.config_path)
        edit_plan.sync_cover_prompt(self.config_path)
        portrait = self.root / self.config["cover"]["3x4"]["generated_source"]
        portrait.parent.mkdir(parents=True, exist_ok=True)
        portrait.write_bytes(b"portrait-current")
        completed = edit_plan.record_cover_generation(
            self.config_path,
            ratio="3x4",
            status="completed",
            prompt="3:4 直接文字封面",
            model="provider-model",
            result_id="provider-portrait",
        )
        portrait_revision = completed["generations"]["3x4"]["semantic_revision"]
        edit_plan.record_cover_generation(
            self.config_path,
            ratio="4x3",
            status="failed",
            prompt="4:3 文字版尝试",
            model="provider-model",
            result_id="provider-landscape-failed",
            retry_reason="文字需要改用确定性排版",
        )

        changed = copy.deepcopy(self.config)
        changed["cover"]["4x3"]["text_mode"] = "deterministic"
        changed["cover"]["4x3"]["generated_source"] = (
            "covers/source/cover-4x3-text-free.png"
        )
        _write_json(self.config_path, changed)
        synced = edit_plan.sync_cover_prompt(self.config_path)

        self.assertEqual("completed", synced["generations"]["3x4"]["status"])
        self.assertEqual(
            portrait_revision,
            synced["generations"]["3x4"]["semantic_revision"],
        )
        landscape = synced["generations"]["4x3"]
        self.assertEqual("pending", landscape["status"])
        self.assertEqual("covers/source/cover-4x3-text-free.png", landscape["source_path"])
        self.assertEqual(1, len(landscape["archived_revisions"]))
        self.assertEqual(
            "provider-result:provider-landscape-failed",
            landscape["archived_revisions"][0]["attempts"][0]["attempt_id"],
        )

    def test_failed_local_attempt_and_retry_keep_append_only_history(self) -> None:
        edit_plan.initialize_cover_prompt(self.config_path)
        edit_plan.sync_cover_prompt(self.config_path)
        failed = edit_plan.record_cover_generation(
            self.config_path,
            ratio="3x4",
            status="failed",
            prompt="第一次生成",
            model=None,
            result_id=None,
            retry_reason="服务未返回结果",
        )
        first = failed["generations"]["3x4"]["attempts"][0]
        self.assertTrue(first["attempt_id"].startswith("local-attempt:"))
        self.assertIsNone(first["result_id"])
        self.assertEqual("builtin-imagegen", first["model"])

        source = self.root / self.config["cover"]["3x4"]["generated_source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"retry-success")
        retried = edit_plan.record_cover_generation(
            self.config_path,
            ratio="3x4",
            status="completed",
            prompt="第二次生成",
            model=None,
            result_id=None,
        )
        attempts = retried["generations"]["3x4"]["attempts"]
        self.assertEqual(2, len(attempts))
        self.assertEqual(first, attempts[0])
        self.assertNotEqual(attempts[0]["attempt_id"], attempts[1]["attempt_id"])

    def test_v2_production_requires_distinct_current_outputs(self) -> None:
        edit_plan.initialize_cover_prompt(self.config_path)
        edit_plan.sync_cover_prompt(self.config_path)
        for ratio in ("3x4", "4x3"):
            source = self.root / self.config["cover"][ratio]["generated_source"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"same-output")
            edit_plan.record_cover_generation(
                self.config_path,
                ratio=ratio,
                status="completed",
                prompt=f"生成 {ratio}",
                model="provider-model",
                result_id=f"provider-{ratio}",
            )
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_cover_prompt(self.config_path, require_completed=True)
        self.assertEqual("COVER_PROMPT_INDEPENDENCE", raised.exception.code)

    def test_pending_and_completed_provenance(self) -> None:
        pending = edit_plan.initialize_cover_prompt(self.config_path)
        self.assertEqual("pending", pending["generations"]["3x4"]["status"])
        record = copy.deepcopy(pending)
        source_path = self.root / self.config["cover"]["3x4"]["generated_source"]
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"portrait generation")
        entry = record["generations"]["3x4"]
        entry.update(
            {
                "status": "completed",
                "generation_version": 1,
                "prompt": "独立生成 3:4 中文封面",
                "model": "imagegen",
                "generated_at": "2026-07-21T12:00:00+08:00",
                "result_id": "result-portrait-01",
                "source_sha256": common.sha256_file(source_path),
            }
        )
        edit_plan.validate_cover_prompt(self.config_path, record)
        entry["source_sha256"] = "0" * 64
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_cover_prompt(self.config_path, record)
        self.assertEqual("COVER_PROMPT_HASH", raised.exception.code)

    def test_rejects_cover_prompt_version(self) -> None:
        record = edit_plan.cover_prompt_template(self.config)
        record["version"] = 2
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_cover_prompt(self.config_path, record)
        self.assertEqual("COVER_PROMPT_VERSION", raised.exception.code)

    def test_production_requires_two_completed_distinct_calls(self) -> None:
        pending = edit_plan.cover_prompt_template(self.config)
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_cover_prompt(
                self.config_path, pending, require_completed=True
            )
        self.assertEqual("COVER_PROMPT_INCOMPLETE", raised.exception.code)

        completed = self.completed_cover_record()
        completed["generations"]["4x3"]["result_id"] = completed["generations"][
            "3x4"
        ]["result_id"]
        with self.assertRaises(common.WorkflowError) as raised:
            edit_plan.validate_cover_prompt(
                self.config_path, completed, require_completed=True
            )
        self.assertEqual("COVER_PROMPT_INDEPENDENCE", raised.exception.code)

    def test_record_cover_generation_hashes_the_configured_source(self) -> None:
        edit_plan.initialize_cover_prompt(self.config_path)
        source = self.root / self.config["cover"]["3x4"]["generated_source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"portrait output")
        record = edit_plan.record_cover_generation(
            self.config_path,
            ratio="3x4",
            status="completed",
            prompt="独立生成 3:4 封面",
            model="imagegen",
            result_id="call-portrait",
        )
        self.assertEqual(
            common.sha256_file(source),
            record["generations"]["3x4"]["source_sha256"],
        )


class WorkflowV2PreflightTests(V2ContractFixture):
    def test_complete_v2_preflight_returns_content_bound_hashes(self) -> None:
        self.build_package()
        edit_plan.write_edit_plan(self.config_path, self.plan)
        prompt = self.completed_cover_record()
        _write_json(self.root / self.config["cover"]["prompt_record"], prompt)
        contracts = workflow.validate_v2_contracts(self.config_path, self.config)
        self.assertIsNotNone(contracts)
        assert contracts is not None
        for field in ("source_package", "edit_plan", "cover_prompt"):
            self.assertRegex(contracts[field], r"^[0-9a-f]{64}$")
        self.assertEqual(
            {"asset-01": common.sha256_file(self.root / "source/demo.mp4")},
            contracts["used_assets"],
        )

    def test_rights_status_update_preserves_video_source_content_hash(self) -> None:
        self.build_package()
        edit_plan.write_edit_plan(self.config_path, self.plan)
        _write_json(
            self.root / self.config["cover"]["prompt_record"],
            self.completed_cover_record(),
        )
        pending = workflow.validate_v2_contracts(self.config_path, self.config)
        assert pending is not None
        confirmed_draft = copy.deepcopy(self.package_draft)
        confirmed_draft["assets"]["items"][0]["rights_status"] = "confirmed"
        source_package.write_source_package(self.config_path, confirmed_draft)
        confirmed = workflow.validate_v2_contracts(self.config_path, self.config)
        assert confirmed is not None
        self.assertEqual(pending["source_content"], confirmed["source_content"])
        self.assertNotEqual(pending["source_package"], confirmed["source_package"])

    def test_missing_source_contract_blocks_before_runtime_or_tts_config(self) -> None:
        args = mock.Mock(config=self.config_path)
        with (
            mock.patch.object(workflow, "verify_approval", return_value={"result": "pass"}),
            mock.patch.object(workflow, "resolve_media_tools") as media,
            mock.patch.object(workflow, "load_env_file") as env,
            mock.patch.object(workflow, "synthesize") as synthesize,
        ):
            with self.assertRaises(common.WorkflowError) as raised:
                workflow.run_build(args)
        self.assertEqual("SOURCE_PACKAGE_MISSING", raised.exception.code)
        media.assert_not_called()
        env.assert_not_called()
        synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()

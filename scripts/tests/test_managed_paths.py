from __future__ import annotations

import copy
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import common  # noqa: E402


class ManagedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="managed-paths-")
        self.addCleanup(self.temporary.cleanup)
        self.temp_root = Path(self.temporary.name)
        self.project_root = self.temp_root / "项目 空间"
        self.project_root.mkdir()
        self.external = self.temp_root / "外部 目录"
        self.external.mkdir()

    def assert_code(self, code: str, callable_: object) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            callable_()
        self.assertEqual(code, raised.exception.code)

    def test_all_managed_roots_reject_symlinks(self) -> None:
        for name in common.MANAGED_PROJECT_ROOTS:
            with self.subTest(root=name):
                link = self.project_root / name
                link.symlink_to(self.external, target_is_directory=True)
                self.assert_code(
                    "MANAGED_SYMLINK",
                    lambda link=link: common.ensure_managed_dir(self.project_root, link),
                )
                link.unlink()

    def test_nested_symlink_blocks_tree_read_and_safe_rmtree(self) -> None:
        stage = common.ensure_managed_dir(
            self.project_root, ".source-led-ai-video/staging/build.tmp"
        )
        protected = self.external / "keep.txt"
        protected.write_text("do not delete", encoding="utf-8")
        (stage / "redirect").symlink_to(self.external, target_is_directory=True)

        self.assert_code(
            "MANAGED_SYMLINK",
            lambda: common.assert_managed_tree(self.project_root, stage),
        )
        self.assert_code(
            "MANAGED_SYMLINK",
            lambda: common.remove_managed_tree(self.project_root, stage),
        )
        self.assertEqual("do not delete", protected.read_text(encoding="utf-8"))

    def test_lock_leaf_symlink_is_never_followed(self) -> None:
        control = common.ensure_managed_dir(self.project_root, ".source-led-ai-video")
        protected = self.external / "lock-target"
        protected.write_bytes(b"unchanged")
        lock_path = control / "build.lock"
        lock_path.symlink_to(protected)

        self.assert_code(
            "MANAGED_SYMLINK",
            lambda: common.open_managed_lock(self.project_root, lock_path),
        )
        self.assertEqual(b"unchanged", protected.read_bytes())

    def test_replace_rejects_symlink_destination(self) -> None:
        source = common.ensure_managed_dir(
            self.project_root, ".source-led-ai-video/staging/build.tmp"
        )
        (source / "artifact.txt").write_text("artifact", encoding="utf-8")
        review_root = common.ensure_managed_dir(self.project_root, "review-runs")
        destination = review_root / ("a" * 20)
        destination.symlink_to(self.external, target_is_directory=True)

        self.assert_code(
            "MANAGED_SYMLINK",
            lambda: common.replace_managed_dir(self.project_root, source, destination),
        )
        self.assertTrue(source.is_dir())


class CoverPathIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cover-identity-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "base.mp4").write_bytes(b"base")
        for name in ("portrait.png", "landscape.png", "final-landscape.png"):
            (self.root / name).write_bytes(b"png")
        self.config = {
            "version": 1,
            "project_name": "封面路径身份测试",
            "platform": "douyin",
            "commercial": True,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "paths": {"base_video": "base.mp4", "bgm": ""},
            "narration": {
                "scenes": [
                    {
                        "id": "scene-01",
                        "text": "测试封面路径。",
                        "captions": ["测试封面路径。"],
                    }
                ]
            },
            "source_timeline": {"scene_boundaries_seconds": [0, 1]},
            "cover": {
                "hook": "一句话生成模型",
                "3x4": {
                    "generated_source": "portrait.png",
                    "final": "final-portrait.png",
                },
                "4x3": {
                    "generated_source": "landscape.png",
                    "final": "final-landscape.png",
                },
            },
        }

    def write_config(self, config: dict) -> Path:
        path = self.root / "project.json"
        common.atomic_write_json(path, config)
        return path

    def assert_config_cover_error(self, config: dict) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.write_config(config))
        self.assertEqual("CONFIG_COVER", raised.exception.code)

    def test_unicode_nfc_nfd_equivalent_paths_are_duplicates(self) -> None:
        composed = "封面-é.png"
        decomposed = unicodedata.normalize("NFD", composed)
        self.assertNotEqual(composed, decomposed)
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["3x4"]["generated_source"] = composed
        candidate["cover"]["3x4"]["final"] = decomposed
        self.assert_config_cover_error(candidate)

    def test_existing_hardlinks_are_the_same_cover_file(self) -> None:
        final = self.root / "final-portrait.png"
        os.link(self.root / "portrait.png", final)
        self.assert_config_cover_error(self.config)


if __name__ == "__main__":
    unittest.main()

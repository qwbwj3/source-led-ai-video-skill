from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree


import sys


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import share_doc  # noqa: E402
import workflow  # noqa: E402
from lib import common, telemetry  # noqa: E402


class ShareDocumentContractTests(unittest.TestCase):
    IDENTITY = {
        "publication_id": "douyin:video-02",
        "build_key": "a" * 20,
        "video_sha256": "b" * 64,
    }
    CONTENT = {
        "title": "一句话做出 <3D> 游戏",
        "one_line_takeaway": "它把传统 3D 原型的启动成本压缩到了普通人可尝试的程度。",
        "overview": "这个项目把图片变成可玩的网页。\n适合先看演示，再研究实现。",
        "toolchain": [
            "准备一张主体清晰的参考图",
            "用视觉模型提取场景和交互要求",
            "生成网页原型并在浏览器里调整",
        ],
        "repository_url": "https://github.com/example/demo?a=1&b=2",
        "source_url": "https://x.com/example/status/123",
        "prompt": {
            "text": "Build <a game>\nUse A & B",
            "provenance": "author_public",
            "source_url": "https://x.com/example/status/123",
            "title": "作者公开写法",
        },
        "resources": [
            {
                "label": "作者说明",
                "url": "https://example.com/guide?q=a&lang=zh",
                "note": "包含参数与注意事项",
            },
            {"label": "补充判断", "note": "手机端也可以直接体验。"},
        ],
    }

    def include(self, root: Path, **overrides):
        values = {
            **self.IDENTITY,
            "decision": "include",
            "project_type": "open_source_project",
            "decided_by": "Hanye",
            "decision_note": "发布表现达到分享门槛",
            "content": self.CONTENT,
        }
        values.update(overrides)
        return share_doc.decide(root, **values)

    def test_include_is_deterministic_idempotent_and_emits_valid_feishu_xml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-a-") as first_raw, tempfile.TemporaryDirectory(
            prefix="share-doc-b-"
        ) as second_raw:
            first_root = Path(first_raw)
            second_root = Path(second_raw)
            first = self.include(first_root)
            duplicate = self.include(first_root)
            second = self.include(second_root)

            self.assertEqual(first["event_id"], duplicate["event_id"])
            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(first["entry_path"], second["entry_path"])
            first_xml = (first_root / first["entry_path"]).read_text(encoding="utf-8")
            second_xml = (second_root / second["entry_path"]).read_text(encoding="utf-8")
            self.assertEqual(first_xml, second_xml)
            ElementTree.fromstring(f"<fragment>{first_xml}</fragment>")
            self.assertIn("<h3>一句话做出", first_xml)
            self.assertNotIn("<h2>", first_xml)
            self.assertIn("资料编号：</b>SLAV-BBBBBBBBBBBB", first_xml)
            self.assertRegex(first_xml, r"内容版本：</b>C-[0-9a-f]{12}")
            self.assertIn("一句话结论", first_xml)
            self.assertEqual(3, first_xml.count('<li seq="auto">'))
            self.assertIn("原作者提示词：作者公开写法", first_xml)
            self.assertIn("一句话做出 &lt;3D&gt; 游戏", first_xml)
            self.assertIn('href="https://github.com/example/demo?a=1&amp;b=2"', first_xml)
            self.assertIn("Build &lt;a game&gt;<br/>Use A &amp; B", first_xml)
            self.assertNotIn("<3D>", first_xml)

            events = telemetry.read_events(
                first_root / ".source-led-ai-video/share-doc/events.jsonl"
            )
            self.assertEqual(1, len(events))
            self.assertEqual("share_doc_decision", events[0]["event"])

    def test_skip_is_explicit_and_cannot_be_synced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-skip-") as raw:
            root = Path(raw)
            record = share_doc.decide(
                root,
                **self.IDENTITY,
                decision="skip",
                project_type="community_demo",
                decided_by="Hanye",
                decision_note="流量与审核结果不适合进入分享文档",
            )
            self.assertEqual("skip", record["decision"])
            self.assertIsNone(record["entry_path"])
            with self.assertRaises(common.WorkflowError) as raised:
                share_doc.mark_synced(
                    root,
                    **self.IDENTITY,
                    document_id="doccnExample",
                    block_id="blockExample",
                )
            self.assertEqual("SHARE_DOC_SKIPPED", raised.exception.code)

    def test_conflicting_decisions_and_duplicate_publications_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-conflict-") as raw:
            root = Path(raw)
            self.include(root)
            with self.assertRaises(common.WorkflowError) as raised:
                self.include(root, decision_note="不同的决策说明")
            self.assertEqual("SHARE_DOC_ALREADY_DECIDED", raised.exception.code)
            with self.assertRaises(common.WorkflowError) as duplicate:
                self.include(root, publication_id="xiaohongshu:video-02")
            self.assertEqual("SHARE_DOC_DUPLICATE", duplicate.exception.code)
            events = telemetry.read_events(
                root / ".source-led-ai-video/share-doc/events.jsonl"
            )
            self.assertEqual(1, len(events))

    def test_mark_synced_is_idempotent_append_only_and_rejects_retargeting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-sync-") as raw:
            root = Path(raw)
            decision = self.include(root)
            with self.assertRaises(common.WorkflowError) as identity_error:
                share_doc.mark_synced(
                    root,
                    publication_id=self.IDENTITY["publication_id"],
                    build_key=None,
                    video_sha256=self.IDENTITY["video_sha256"],
                    document_id="doccnExample",
                    block_id="blockExample",
                )
            self.assertEqual("SHARE_DOC_IDENTITY", identity_error.exception.code)
            synced = share_doc.mark_synced(
                root,
                **self.IDENTITY,
                document_id="doccnExample",
                block_id="blockExample",
            )
            duplicate = share_doc.mark_synced(
                root,
                **self.IDENTITY,
                document_id="doccnExample",
                block_id="blockExample",
            )
            self.assertEqual(synced["event_id"], duplicate["event_id"])
            with self.assertRaises(common.WorkflowError) as raised:
                share_doc.mark_synced(
                    root,
                    **self.IDENTITY,
                    document_id="doccnDifferent",
                    block_id="blockDifferent",
                )
            self.assertEqual("SHARE_DOC_ALREADY_SYNCED", raised.exception.code)

            events = telemetry.read_events(
                root / ".source-led-ai-video/share-doc/events.jsonl"
            )
            self.assertEqual([1, 2], [event["sequence"] for event in events])
            self.assertEqual(decision["event_id"], events[1]["decision_event_id"])
            current = share_doc.status(root)["decisions"][0]
            self.assertTrue(current["synced"])
            self.assertEqual("开源项目", current["category_heading"])
            self.assertEqual(64, len(current["document_fingerprint"]))
            self.assertEqual(64, len(current["block_fingerprint"]))
            raw_log = (
                root / ".source-led-ai-video/share-doc/events.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn("doccnExample", raw_log)
            self.assertNotIn("blockExample", raw_log)

    def test_modified_entry_cannot_be_marked_synced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-stale-") as raw:
            root = Path(raw)
            decision = self.include(root)
            (root / decision["entry_path"]).write_text("<p>changed</p>\n", encoding="utf-8")
            with self.assertRaises(common.WorkflowError) as raised:
                share_doc.mark_synced(
                    root,
                    **self.IDENTITY,
                    document_id="doccnExample",
                    block_id="blockExample",
                )
            self.assertEqual("SHARE_DOC_ENTRY", raised.exception.code)

    def test_schema_identity_urls_and_content_are_strict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-invalid-") as raw:
            root = Path(raw)
            cases = (
                ({"project_type": "made_up"}, "SHARE_DOC_PROJECT_TYPE"),
                ({"build_key": "wrong"}, "SHARE_DOC_IDENTITY"),
                (
                    {
                        "content": {
                            **self.CONTENT,
                            "repository_url": "file:///private/etc/passwd",
                        }
                    },
                    "SHARE_DOC_URL",
                ),
                (
                    {"content": {**self.CONTENT, "unexpected": "field"}},
                    "SHARE_DOC_CONTENT",
                ),
                (
                    {"content": {**self.CONTENT, "toolchain": ["一步", "两步"]}},
                    "SHARE_DOC_CONTENT",
                ),
                (
                    {
                        "content": {**self.CONTENT, "repository_url": None},
                        "project_type": "open_source_project",
                    },
                    "SHARE_DOC_CONTENT",
                ),
                (
                    {"content": {**self.CONTENT, "prompt": "裸提示词"}},
                    "SHARE_DOC_CONTENT",
                ),
                (
                    {
                        "content": {
                            **self.CONTENT,
                            "prompt": {
                                "text": "示例",
                                "provenance": "unknown",
                            },
                        }
                    },
                    "SHARE_DOC_CONTENT",
                ),
            )
            for overrides, error_code in cases:
                with self.subTest(overrides=overrides), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    self.include(root, **overrides)
                self.assertEqual(error_code, raised.exception.code)

    def test_legacy_publication_can_omit_build_key_and_prompt_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-legacy-") as raw:
            root = Path(raw)
            content = {**self.CONTENT, "prompt": None}
            record = self.include(root, build_key=None, content=content)
            self.assertIsNone(record["build_key"])
            xml = (root / record["entry_path"]).read_text(encoding="utf-8")
            self.assertNotIn("提示词", xml)
            self.assertNotIn("暂无", xml)
            synced = share_doc.mark_synced(
                root,
                publication_id=self.IDENTITY["publication_id"],
                build_key=None,
                video_sha256=self.IDENTITY["video_sha256"],
                document_id="doccnLegacy",
                block_id="blockLegacy",
            )
            self.assertIsNone(synced["build_key"])

    def test_secret_like_public_content_and_url_query_are_rejected(self) -> None:
        samples = (
            {**self.CONTENT, "overview": "access_token=super-secret-value"},
            {
                **self.CONTENT,
                "repository_url": "https://example.com/demo?password=super-secret-value",
            },
            {
                **self.CONTENT,
                "repository_url": "https://example.com/demo?X-Amz-Signature=abcdef",
            },
            {
                **self.CONTENT,
                "repository_url": "https://example.com/demo?token=short",
            },
            {
                **self.CONTENT,
                "repository_url": "https://example.com/demo?sig=short",
            },
            {
                **self.CONTENT,
                "prompt": {
                    "text": "Authorization: Bearer abcdefghijklmnop",
                    "provenance": "editorial_reconstruction",
                },
            },
            {
                **self.CONTENT,
                "resources": [
                    {"label": "私密资料", "note": "sk-abcdefghijklmnopqrstuvwxyz"}
                ],
            },
        )
        with tempfile.TemporaryDirectory(prefix="share-doc-secret-") as raw:
            root = Path(raw)
            for content in samples:
                with self.subTest(content=content), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    self.include(root, content=content)
                self.assertEqual("SHARE_DOC_SENSITIVE", raised.exception.code)

    def test_private_or_local_urls_are_rejected(self) -> None:
        private_urls = (
            "http://127.0.0.1/demo",
            "http://localhost/demo",
            "http://10.1.2.3/demo",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/demo",
            "https://project.local/demo",
            "http://2130706433/demo",
            "http://0177.0.0.1/demo",
            "http://0x7f000001/demo",
            "http://127.1/demo",
        )
        with tempfile.TemporaryDirectory(prefix="share-doc-private-url-") as raw:
            root = Path(raw)
            for address in private_urls:
                with self.subTest(address=address), self.assertRaises(
                    common.WorkflowError
                ) as raised:
                    self.include(
                        root,
                        content={**self.CONTENT, "repository_url": address},
                    )
                self.assertEqual("SHARE_DOC_URL", raised.exception.code)

    def test_public_url_query_does_not_overmatch_normal_parameter_names(self) -> None:
        content = {
            **self.CONTENT,
            "repository_url": "https://example.com/demo?tokenizer=sentencepiece",
        }
        clean = share_doc.validate_content(content)
        self.assertEqual(content["repository_url"], clean["repository_url"])

    def test_publication_id_cannot_bind_two_video_hashes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="share-doc-publication-collision-") as raw:
            root = Path(raw)
            self.include(root)
            with self.assertRaises(common.WorkflowError) as raised:
                self.include(root, video_sha256="c" * 64)
            self.assertEqual("SHARE_DOC_DUPLICATE", raised.exception.code)

    def test_symlinked_log_or_transaction_lock_cannot_escape_project(self) -> None:
        for leaf in ("events.jsonl", "events.jsonl.lock", "transaction.lock"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory(
                prefix="share-doc-symlink-"
            ) as raw:
                container = Path(raw)
                root = container / "project"
                root.mkdir()
                state = root / ".source-led-ai-video/share-doc"
                state.mkdir(parents=True)
                (state / "entries").mkdir()
                outside = container / "outside.txt"
                outside.write_text("do-not-change", encoding="utf-8")
                (state / leaf).symlink_to(outside)
                with self.assertRaises(common.WorkflowError) as raised:
                    self.include(root)
                self.assertEqual("SHARE_DOC_LOG", raised.exception.code)
                self.assertEqual("do-not-change", outside.read_text(encoding="utf-8"))

    def test_operational_module_and_state_are_outside_build_release_and_cache(self) -> None:
        fingerprint = workflow.implementation_fingerprint()
        self.assertNotIn("scripts/share_doc.py", fingerprint["renderer"])
        self.assertNotIn("scripts/share_doc.py", fingerprint["release"])
        with tempfile.TemporaryDirectory(prefix="share-doc-operational-") as raw:
            root = Path(raw)
            self.include(root)
            self.assertFalse((root / "review-runs").exists())
            self.assertFalse((root / "deliverables").exists())
            self.assertFalse((root / ".source-led-ai-video/cache").exists())
            self.assertTrue((root / ".source-led-ai-video/share-doc").is_dir())
            self.assertEqual(
                "*\n!.gitignore\n",
                (root / ".source-led-ai-video/.gitignore").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import builtins
import copy
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import align_captions  # noqa: E402
import render_cover  # noqa: E402
from lib import common  # noqa: E402
from lib.ass_text import ASS_LINE_BREAK, join_ass_lines  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _chunk(chunk_id: bytes, payload: bytes, *, declared_size: int | None = None) -> bytes:
    size = len(payload) if declared_size is None else declared_size
    padding = b"\x00" if size % 2 and len(payload) == size else b""
    return struct.pack("<4sI", chunk_id, size) + payload + padding


def _wave_bytes(fmt_payload: bytes, data: bytes, *, data_size: int | None = None) -> bytes:
    body = b"WAVE" + _chunk(b"fmt ", fmt_payload) + _chunk(
        b"data", data, declared_size=data_size
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _pcm_fmt(format_tag: int = 1) -> bytes:
    return struct.pack("<HHIIHH", format_tag, 1, 48_000, 144_000, 3, 24)


def _extensible_fmt(subtype: bytes = align_captions.PCM_SUBTYPE_GUID) -> bytes:
    return _pcm_fmt(0xFFFE) + struct.pack("<HHI16s", 22, 24, 4, subtype)


class StrictProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="strict-video-config-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        for relative in (
            "source/base.mp4",
            "covers/source/portrait.png",
            "covers/source/landscape.png",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"file")
        self.config = {
            "version": 1,
            "project_name": "严格合约",
            "platform": "douyin",
            "commercial": True,
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "paths": {"base_video": "source/base.mp4", "bgm": ""},
            "narration": {
                "language": "Chinese",
                "caption_lead_ms": 80,
                "scenes": [
                    {
                        "id": "scene-01",
                        "text": "你好世界",
                        "captions": [
                            {"text": "你好", "show": True},
                            {"text": "世界", "show": False},
                        ],
                    }
                ],
            },
            "source_timeline": {
                "scene_boundaries_seconds": [0.0, 3.0],
                "retime_ratio_limits": [0.8, 1.2],
            },
            "caption_style": {"font_size": 68, "margin_v": 470, "max_line_units": 14},
            "audio": {"final_lufs": -14, "bgm_fade_out_seconds": 1.8},
            "cover": {
                "hook": "你好世界成片",
                "headline_lines": ["你好世界", "成片"],
                "text_mode": "imagegen",
                "allow_people": False,
                "3x4": {
                    "generated_source": "covers/source/portrait.png",
                    "final": "covers/final-3x4-你好世界成片.png",
                },
                "4x3": {
                    "generated_source": "covers/source/landscape.png",
                    "final": "covers/final-4x3-你好世界成片.png",
                },
            },
            "qa": {"max_duration_seconds": 90, "allow_black": False, "allow_silence": False},
        }
        self.config_path = self.root / "project.json"
        self.write_config()

    def write_config(self, value: object | None = None) -> None:
        _write_json(self.config_path, self.config if value is None else value)

    def assert_rejected(self, value: object, code: str | None = None) -> None:
        self.write_config(value)
        with self.assertRaises(common.WorkflowError) as raised:
            common.load_and_validate_config(self.config_path)
        if code:
            self.assertEqual(code, raised.exception.code)


class StrictConfigTests(StrictProjectFixture):
    def test_obviously_overlong_narration_is_blocked_before_paid_tts(self) -> None:
        candidate = copy.deepcopy(self.config)
        text = "中" * (common.MAX_NARRATION_CHARACTERS + 1)
        candidate["narration"]["scenes"][0]["text"] = text
        candidate["narration"]["scenes"][0]["captions"] = [text]
        self.assert_rejected(candidate, "CONFIG_NARRATION_LENGTH")

    def test_nested_container_and_scalar_types_are_not_coerced(self) -> None:
        mutations = (
            (("commercial",), "true"),
            (("canvas", "fps"), 30.0),
            (("paths", "base_video"), {"path": "source/base.mp4"}),
            (("narration", "language"), 1),
            (("narration", "caption_lead_ms"), "80"),
            (("narration", "scenes"), {"scene": 1}),
            (("narration", "scenes", 0, "id"), 1),
            (("narration", "scenes", 0, "text"), 1),
            (("narration", "scenes", 0, "captions"), {"text": "你好世界"}),
            (("narration", "scenes", 0, "captions", 0, "show"), "false"),
            (("source_timeline", "scene_boundaries_seconds", 0), "0"),
            (("source_timeline", "retime_ratio_limits", 1), "1.2"),
            (("caption_style", "font_size"), "68"),
            (("audio", "final_lufs"), "-14"),
            (("cover", "hook"), 1234),
            (("cover", "3x4"), ["covers/source/portrait.png"]),
            (("qa", "allow_black"), "false"),
        )
        for keys, replacement in mutations:
            with self.subTest(path=".".join(map(str, keys))):
                candidate = copy.deepcopy(self.config)
                target = candidate
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = replacement
                self.assert_rejected(candidate)

    def test_nonfinite_numbers_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                candidate = copy.deepcopy(self.config)
                candidate["source_timeline"]["scene_boundaries_seconds"][1] = value
                self.assert_rejected(candidate, "CONFIG_NUMBER")

    def test_caption_show_must_be_an_actual_boolean(self) -> None:
        for value in (0, 1, None, "false", [], {}):
            with self.subTest(value=value):
                candidate = copy.deepcopy(self.config)
                candidate["narration"]["scenes"][0]["captions"][0]["show"] = value
                self.assert_rejected(candidate, "CONFIG_CAPTION")

    def test_punctuation_only_scene_or_caption_is_rejected(self) -> None:
        scene = copy.deepcopy(self.config)
        scene["narration"]["scenes"][0]["text"] = "！？。，"
        self.assert_rejected(scene)

        caption = copy.deepcopy(self.config)
        caption["narration"]["scenes"][0]["captions"][0]["text"] = "！？。，"
        self.assert_rejected(caption, "CONFIG_CAPTION")

    def test_existing_project_inputs_must_be_regular_files(self) -> None:
        base = self.root / "source/base.mp4"
        base.unlink()
        base.mkdir()
        self.assert_rejected(self.config, "PATH_MISSING")

    def test_cover_paths_are_png_and_pairwise_distinct(self) -> None:
        duplicate = copy.deepcopy(self.config)
        duplicate["cover"]["3x4"]["final"] = duplicate["cover"]["3x4"]["generated_source"]
        self.assert_rejected(duplicate, "CONFIG_COVER")

        wrong_extension = copy.deepcopy(self.config)
        wrong_extension["cover"]["4x3"]["final"] = "covers/final-4x3-你好世界成片.jpg"
        self.assert_rejected(wrong_extension, "CONFIG_COVER")

    def test_cover_text_mode_and_faceless_flag_are_strict(self) -> None:
        for value in ("auto", "IMAGEGEN", "", 1, None, False):
            with self.subTest(text_mode=value):
                candidate = copy.deepcopy(self.config)
                candidate["cover"]["text_mode"] = value
                self.assert_rejected(candidate, "CONFIG_COVER")
        for value in (0, 1, "false", None, [], {}):
            with self.subTest(allow_people=value):
                candidate = copy.deepcopy(self.config)
                candidate["cover"]["allow_people"] = value
                self.assert_rejected(candidate, "CONFIG_COVER")
        for ratio in ("3x4", "4x3"):
            for value in ("auto", "IMAGEGEN", "", 1, None, False):
                with self.subTest(ratio=ratio, ratio_text_mode=value):
                    candidate = copy.deepcopy(self.config)
                    candidate["cover"][ratio]["text_mode"] = value
                    self.assert_rejected(candidate, "CONFIG_COVER")

    def test_cover_headline_lines_are_exact_and_reconstruct_hook(self) -> None:
        invalid = (
            "你好世界成片",
            [],
            [""],
            ["你好", "世界", "成片"],
            ["你好世界", 1],
            ["你好世界", "错字"],
        )
        for value in invalid:
            with self.subTest(headline_lines=value):
                candidate = copy.deepcopy(self.config)
                candidate["cover"]["headline_lines"] = value
                self.assert_rejected(candidate, "CONFIG_COVER")
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["headline_lines"] = ["你好世界成片"]
        self.write_config(candidate)
        common.load_and_validate_config(self.config_path)

    def test_deterministic_fallback_uses_configured_headline_lines(self) -> None:
        cover = copy.deepcopy(self.config["cover"])
        cover["headline_lines"] = ["你好", "世界成片"]
        self.assertEqual(
            ("你好", "世界成片"),
            render_cover.resolve_headline_lines(cover, render_cover.LAYOUTS["3x4"]),
        )

    def test_legacy_cover_config_without_mode_uses_deterministic_compatibility(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["cover"].pop("text_mode")
        candidate["cover"].pop("allow_people")
        self.write_config(candidate)
        config, _ = common.load_and_validate_config(self.config_path)
        self.assertNotIn("text_mode", config["cover"])
        self.assertEqual(
            "scale=1080:1440:force_original_aspect_ratio=increase,"
            "crop=1080:1440,"
            "drawbox=x=72:y=112:w=126:h=12:color=0xFF8C00:t=fill,"
            "ass=cover.ass:fontsdir=fonts",
            render_cover.build_video_filter(render_cover.LAYOUTS["3x4"], "deterministic"),
        )

    def test_imagegen_cover_mode_never_adds_duplicate_text_overlay(self) -> None:
        video_filter = render_cover.build_video_filter(
            render_cover.LAYOUTS["4x3"], "imagegen"
        )
        self.assertEqual(
            "scale=1440:1080:force_original_aspect_ratio=increase,crop=1440:1080",
            video_filter,
        )
        self.assertNotIn("ass=", video_filter)
        self.assertNotIn("drawbox", video_filter)

    def test_one_ratio_can_use_clean_deterministic_fallback(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["4x3"]["text_mode"] = "deterministic"
        self.write_config(candidate)
        config, _ = common.load_and_validate_config(self.config_path)
        self.assertEqual("imagegen", config["cover"]["text_mode"])
        self.assertEqual("deterministic", config["cover"]["4x3"]["text_mode"])
        video_filter = render_cover.build_video_filter(
            render_cover.LAYOUTS["4x3"], config["cover"]["4x3"]["text_mode"]
        )
        self.assertIn("ass=cover.ass", video_filter)

    def test_punctuation_only_sanitized_cover_hook_is_rejected(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["hook"] = "！？。，"
        self.assert_rejected(candidate, "CONFIG_COVER")

    def test_render_rejects_source_output_collision_before_tool_resolution(self) -> None:
        candidate = copy.deepcopy(self.config)
        candidate["cover"]["3x4"]["final"] = candidate["cover"]["3x4"]["generated_source"]
        self.write_config(candidate)
        source = self.root / "covers/source/portrait.png"
        original = source.read_bytes()
        with mock.patch.object(
            sys,
            "argv",
            ["render_cover.py", "--config", str(self.config_path), "--ratio", "3x4"],
        ), mock.patch.object(render_cover, "resolve_media_tools") as resolve:
            with self.assertRaises(common.WorkflowError):
                render_cover.main()
        resolve.assert_not_called()
        self.assertEqual(original, source.read_bytes())


class AssTextTests(unittest.TestCase):
    def test_only_pipeline_owned_line_breaks_remain_active(self) -> None:
        rendered = join_ass_lines(("甲\\N{\\pos(1,2)}\n乙", "丙\r\n丁"))
        self.assertEqual(1, rendered.count(ASS_LINE_BREAK))
        first, second = rendered.split(ASS_LINE_BREAK)
        self.assertEqual("甲＼N｛＼pos(1,2)｝ 乙", first)
        self.assertEqual("丙  丁", second)
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)

    def test_cover_wrapper_escapes_one_line_user_text(self) -> None:
        rendered = render_cover.wrap_hook("甲{\\N}乙", 100)
        self.assertEqual("甲｛＼N｝乙", rendered)

    def test_cover_wrapper_forces_two_lines_above_six_units(self) -> None:
        self.assertEqual(
            ("六字以内标题",),
            render_cover.wrap_hook_lines("六字以内标题", 100),
        )
        forced = render_cover.wrap_hook_lines("超过六个单位标题", 100)
        self.assertEqual(2, len(forced))
        self.assertEqual("超过六个单位标题", "".join(forced))
        self.assertEqual(
            1,
            render_cover.wrap_hook("超过六个单位标题", 100).count(ASS_LINE_BREAK),
        )

    def test_cover_wrapper_prefers_semantic_prefix_break(self) -> None:
        self.assertEqual(
            ("一句话", "做出机械臂"),
            render_cover.wrap_hook_lines("一句话做出机械臂", 10.5),
        )

    def test_forced_cover_lines_keep_user_ass_syntax_inert(self) -> None:
        rendered = render_cover.wrap_hook("一句话{\\pos(1,2)}做出机械臂", 100)
        self.assertEqual(1, rendered.count(ASS_LINE_BREAK))
        first, second = rendered.split(ASS_LINE_BREAK)
        self.assertEqual("一句话", first)
        self.assertEqual("｛＼pos(1,2)｝做出机械臂", second)
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)

    def test_cover_font_size_adapts_to_widest_line(self) -> None:
        short = render_cover.adaptive_font_size(
            ("一句话", "做出机械臂"),
            available_width=890,
            max_font_size=120,
            min_font_size=76,
        )
        long = render_cover.adaptive_font_size(
            ("这是明显更长", "也明显更长的行"),
            available_width=890,
            max_font_size=120,
            min_font_size=76,
        )
        self.assertGreater(short, long)

    def test_cover_ass_uses_white_primary_and_orange_accent_styles(self) -> None:
        document = render_cover.build_ass_document(
            render_cover.LAYOUTS["3x4"],
            ("一句话", "｛危险｝"),
            120,
        )
        self.assertIn(render_cover.WHITE_ASS, document)
        self.assertIn(render_cover.ORANGE_ASS, document)
        self.assertIn("HookPrimary", document)
        self.assertIn("HookAccent", document)
        self.assertNotIn("{危险}", document)


class WavContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="strict-wav-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "voice.wav"
        self.data = b"\x00\x00\x00" * 480

    def write(self, payload: bytes) -> None:
        self.path.write_bytes(payload)

    def assert_audio_rejected(self, payload: bytes) -> None:
        self.write(payload)
        with self.assertRaises(common.WorkflowError) as raised:
            align_captions.wav_duration(self.path)
        self.assertEqual("ALIGN_AUDIO", raised.exception.code)

    def test_accepts_exact_pcm_and_extensible_pcm_guid(self) -> None:
        for fmt in (_pcm_fmt(), _extensible_fmt()):
            with self.subTest(fmt_size=len(fmt)):
                self.write(_wave_bytes(fmt, self.data))
                self.assertAlmostEqual(0.01, align_captions.wav_duration(self.path))

    def test_rejects_float_subtypes_and_wrong_pcm_shape(self) -> None:
        float_guid = bytes.fromhex("0300000000001000800000aa00389b71")
        invalid = (
            _wave_bytes(_pcm_fmt(3), self.data),
            _wave_bytes(_extensible_fmt(float_guid), self.data),
            _wave_bytes(struct.pack("<HHIIHH", 1, 2, 48_000, 288_000, 6, 24), self.data),
            _wave_bytes(struct.pack("<HHIIHH", 1, 1, 48_000, 96_000, 2, 16), self.data),
        )
        for payload in invalid:
            with self.subTest(size=len(payload)):
                self.assert_audio_rejected(payload)

    def test_rejects_riff_size_mismatch_truncation_and_partial_frames(self) -> None:
        valid = _wave_bytes(_pcm_fmt(), self.data)
        bad_riff_size = valid[:4] + struct.pack("<I", len(valid) - 9) + valid[8:]
        truncated_chunk = _wave_bytes(_pcm_fmt(), self.data[:-3], data_size=len(self.data))
        partial_frame = _wave_bytes(_pcm_fmt(), b"\x00\x00")
        for payload in (valid + b"trailing", bad_riff_size, truncated_chunk, partial_frame):
            with self.subTest(size=len(payload)):
                self.assert_audio_rejected(payload)


class AlignmentMappingTests(StrictProjectFixture):
    def test_caption_boundary_can_split_one_aligner_item(self) -> None:
        result = [
            SimpleNamespace(text="你好世界", start_time=0.1, end_time=1.1),
        ]
        captions = align_captions.map_captions(self.config, result, 2.0)
        self.assertEqual([(100, 600), (600, 1100)], [
            (item["startMs"], item["endMs"]) for item in captions
        ])
        self.assertIs(captions[0]["show"], True)
        self.assertIs(captions[1]["show"], False)

    def test_invalid_aligner_times_fail_closed(self) -> None:
        result = [SimpleNamespace(text="你好世界", start_time=0.1, end_time=float("nan"))]
        with self.assertRaises(common.WorkflowError) as raised:
            align_captions.map_captions(self.config, result, 2.0)
        self.assertEqual("ALIGN_RESULT", raised.exception.code)

    def test_missing_cli_args_are_rejected_without_importing_model(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("mlx_audio"):
                self.fail("model dependency imported before argument validation")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(sys, "argv", ["align_captions.py"]), mock.patch(
            "builtins.__import__", side_effect=guarded_import
        ):
            with self.assertRaises(common.WorkflowError) as raised:
                align_captions.main()
        self.assertEqual("ALIGN_ARGS", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

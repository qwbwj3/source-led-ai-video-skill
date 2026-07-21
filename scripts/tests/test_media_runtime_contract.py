from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import workflow  # noqa: E402
import bootstrap_runtime  # noqa: E402
from lib import common  # noqa: E402


def base_probe(*, side_data: list[dict] | None = None, extra_stream: dict | None = None) -> dict:
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "tags": {},
    }
    if side_data is not None:
        video["side_data_list"] = side_data
    streams = [video]
    if extra_stream is not None:
        streams.append(extra_stream)
    return {"streams": streams, "format": {"duration": "3.000000"}}


class CleanBaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "source_timeline": {"scene_boundaries_seconds": [0.0, 3.0]}
        }

    def assert_code(self, code: str, callback) -> None:
        with self.assertRaises(common.WorkflowError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code)

    def test_rotation_display_matrix_is_rejected(self) -> None:
        rotated = base_probe(
            side_data=[{"rotation": 90, "displaymatrix": "non-identity matrix"}]
        )
        with mock.patch.object(workflow, "probe", return_value=rotated):
            self.assert_code(
                "BASE_ROTATION",
                lambda: workflow.validate_base(
                    Path("ffprobe"), Path("base.mp4"), self.config
                ),
            )

    def test_subtitle_data_or_attachment_stream_is_rejected(self) -> None:
        for kind in ("subtitle", "data", "attachment"):
            with self.subTest(kind=kind), mock.patch.object(
                workflow,
                "probe",
                return_value=base_probe(extra_stream={"codec_type": kind}),
            ):
                self.assert_code(
                    "BASE_STREAMS",
                    lambda: workflow.validate_base(
                        Path("ffprobe"), Path("base.mp4"), self.config
                    ),
                )

    def test_plain_single_video_stream_is_accepted(self) -> None:
        with mock.patch.object(workflow, "probe", return_value=base_probe()):
            result = workflow.validate_base(
                Path("ffprobe"), Path("base.mp4"), self.config
            )
        self.assertEqual("video", result["streams"][0]["codec_type"])


class VolcEnvironmentPrecedenceTests(unittest.TestCase):
    def test_explicit_secure_file_overrides_stale_ambient_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="volc-env-precedence-") as raw_temp:
            env_file = Path(raw_temp) / "volc.env"
            env_file.write_text(
                "\n".join(
                    (
                        "VOLC_TTS_ENDPOINT=https://openspeech.bytedance.com/api/v1/tts",
                        "VOLC_TTS_APP_ID=file-app",
                        "VOLC_TTS_ACCESS_TOKEN=file-token",
                        "VOLC_TTS_CLUSTER=file-cluster",
                        "VOLC_TTS_VOICE=file-voice",
                        "VOLC_TTS_ENCODING=mp3",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            with mock.patch.dict(
                os.environ,
                {
                    "VOLC_TTS_APP_ID": "ambient-app",
                    "VOLC_TTS_ACCESS_TOKEN": "ambient-token",
                    "VOLC_TTS_VOICE": "ambient-voice",
                },
                clear=False,
            ):
                values = common.load_env_file(env_file)
        self.assertEqual("file-app", values["VOLC_TTS_APP_ID"])
        self.assertEqual("file-token", values["VOLC_TTS_ACCESS_TOKEN"])
        self.assertEqual("file-voice", values["VOLC_TTS_VOICE"])


@unittest.skipUnless(
    platform.system() == "Darwin" and platform.machine() == "arm64",
    "bundled runtime is Apple Silicon only",
)
class BundledUvContractTests(unittest.TestCase):
    def test_bundled_uv_bootstraps_offline_with_locked_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-led-uv-contract-") as raw_temp:
            uv = bootstrap_runtime.install_uv_runtime(Path(raw_temp))
            self.assertEqual(bootstrap_runtime.UV_BIN_HASH, common.sha256_file(uv))
            result = subprocess.run(
                [str(uv), "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.stdout.startswith(f"uv {bootstrap_runtime.UV_VERSION} "))


if __name__ == "__main__":
    unittest.main()

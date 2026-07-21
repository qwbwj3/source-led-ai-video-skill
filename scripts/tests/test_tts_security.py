from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.response import addinfourl


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import install_volc_config  # noqa: E402
import review_gate  # noqa: E402
from lib import common  # noqa: E402
from lib import volc_tts as adapter  # noqa: E402


def _load_direct_cli():
    spec = importlib.util.spec_from_file_location(
        "source_led_direct_volc_tts", SCRIPTS_DIR / "volc_tts.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load direct TTS CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


direct_cli = _load_direct_cli()


def fake_env(endpoint: str = adapter.OFFICIAL_ENDPOINT) -> dict[str, str]:
    return {
        "VOLC_TTS_ENDPOINT": endpoint,
        "VOLC_TTS_APP_ID": "test-app-id",
        "VOLC_TTS_ACCESS_TOKEN": "test-access-token",
        "VOLC_TTS_CLUSTER": "test-cluster",
        "VOLC_TTS_VOICE": "test-voice",
        "VOLC_TTS_ENCODING": "mp3",
    }


def fake_mp3(marker: bytes = b"A") -> bytes:
    return b"ID3" + marker * (adapter.MIN_AUDIO_BYTES + 32)


def fake_probe_result(valid: bool):
    if not valid:
        return mock.Mock(returncode=1, stdout="")
    return mock.Mock(
        returncode=0,
        stdout=json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "audio",
                        "codec_name": "mp3",
                        "duration": "1.25",
                        "nb_read_frames": "48",
                    }
                ],
                "format": {"duration": "1.25"},
            }
        ),
    )


def synthesize_approved_text(
    text: str,
    output: Path,
    env: dict[str, str],
    cache: Path,
    *,
    project_root: Path | None = None,
    probe_results: bool | list[bool] = True,
) -> dict:
    config = {
        "narration": {
            "language": "Chinese",
            "scenes": [
                {"id": "scene-01", "text": text, "captions": [text]},
            ],
        }
    }
    approval = {
        "narration_sha256": common.narration_hash(config),
        "review_payload_sha256": "f" * 64,
    }
    probe_patch = (
        mock.patch.object(
            adapter,
            "run_process",
            side_effect=[fake_probe_result(result) for result in probe_results],
        )
        if isinstance(probe_results, list)
        else mock.patch.object(
            adapter, "run_process", return_value=fake_probe_result(probe_results)
        )
    )
    with mock.patch.object(
        review_gate, "verify_approval", return_value=approval
    ), mock.patch.object(
        adapter,
        "load_and_validate_config",
        return_value=(config, project_root or output.parent),
    ), mock.patch.object(
        adapter,
        "resolve_media_tools",
        return_value=(Path("/locked/ffmpeg"), Path("/locked/ffprobe")),
    ), probe_patch:
        return adapter.synthesize(
            (project_root or output.parent) / "approved-project.json",
            output,
            env,
            cache,
        )


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "audio/mpeg"}
        self._body = io.BytesIO(body)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class RecordingBytesIO(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class EndpointContractTests(unittest.TestCase):
    def test_endpoint_is_canonicalized_and_bound_into_cache_identity(self) -> None:
        canonical = adapter.cache_provenance("已审核口播", fake_env())
        explicit_443 = adapter.cache_provenance(
            "已审核口播",
            fake_env("https://openspeech.bytedance.com:443/api/v1/tts"),
        )
        self.assertEqual(adapter.OFFICIAL_ENDPOINT, canonical["endpoint"])
        self.assertEqual(adapter.ADAPTER_VERSION, canonical["adapter_version"])
        self.assertEqual(canonical, explicit_443)
        serialized = json.dumps(canonical, ensure_ascii=False)
        self.assertNotIn("test-access-token", serialized)
        self.assertNotIn("test-app-id", serialized)
        self.assertNotIn("test-cluster", serialized)
        self.assertNotIn("test-voice", serialized)

        first_key = adapter._cache_key("已审核口播", fake_env())
        with mock.patch.object(adapter, "ADAPTER_VERSION", "adapter-version-test"):
            self.assertNotEqual(first_key, adapter._cache_key("已审核口播", fake_env()))

    def test_malicious_or_different_endpoints_are_rejected_before_transport(self) -> None:
        invalid = (
            "http://openspeech.bytedance.com/api/v1/tts",
            "https://openspeech.bytedance.com/api/v1/tts/",
            "https://openspeech.bytedance.com/api/v1/tts?next=evil",
            "https://openspeech.bytedance.com/api/v1/tts#fragment",
            "https://openspeech.bytedance.com:444/api/v1/tts",
            "https://user@openspeech.bytedance.com/api/v1/tts",
            "https://openspeech.bytedance.com@evil.invalid/api/v1/tts",
            "https://openspeech.bytedance.com.evil.invalid/api/v1/tts",
        )
        with tempfile.TemporaryDirectory(prefix="tts-endpoint-") as raw_temp:
            root = Path(raw_temp)
            for index, endpoint in enumerate(invalid):
                with self.subTest(endpoint=endpoint), mock.patch.object(
                    adapter, "_open_request"
                ) as transport:
                    with self.assertRaises(common.WorkflowError) as raised:
                        synthesize_approved_text(
                            "已审核口播",
                            root / f"out-{index}.mp3",
                            fake_env(endpoint),
                            root / f"cache-{index}",
                        )
                    self.assertEqual("TTS_ENDPOINT", raised.exception.code)
                    transport.assert_not_called()

    def test_installer_uses_the_same_endpoint_validator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tts-install-") as raw_temp:
            source = Path(raw_temp) / "volc.env"
            source.write_text(
                "\n".join(
                    f"{key}={value}"
                    for key, value in fake_env(
                        "https://openspeech.bytedance.com:443/api/v1/tts"
                    ).items()
                )
                + "\n",
                encoding="utf-8",
            )
            source.chmod(0o600)
            values = install_volc_config.parse_env_file(source)
            self.assertEqual(adapter.OFFICIAL_ENDPOINT, values["VOLC_TTS_ENDPOINT"])

            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "https://openspeech.bytedance.com:443/api/v1/tts",
                    "https://evil.invalid/api/v1/tts",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(common.WorkflowError) as raised:
                install_volc_config.parse_env_file(source)
            self.assertEqual("TTS_ENDPOINT", raised.exception.code)

    def test_control_characters_in_credentials_are_rejected_without_leaking(self) -> None:
        poisoned = fake_env()
        poisoned["VOLC_TTS_ACCESS_TOKEN"] = "CANARY_TOKEN_LINE1\nCANARY_TOKEN_LINE2"
        with tempfile.TemporaryDirectory(prefix="tts-control-char-") as raw_temp:
            root = Path(raw_temp)
            with mock.patch.object(adapter, "_open_request") as transport:
                with self.assertRaises(common.WorkflowError) as raised:
                    synthesize_approved_text(
                        "已审核口播", root / "out.mp3", poisoned, root / "cache"
                    )
        self.assertEqual("TTS_CONFIG", raised.exception.code)
        self.assertNotIn("CANARY_TOKEN", str(raised.exception))
        transport.assert_not_called()

    def test_unexpected_transport_exception_is_redacted(self) -> None:
        canary = "CANARY_UNEXPECTED_TOKEN_42"
        env = fake_env()
        env["VOLC_TTS_ACCESS_TOKEN"] = canary
        with tempfile.TemporaryDirectory(prefix="tts-unexpected-") as raw_temp, mock.patch.object(
            adapter, "_open_request", side_effect=RuntimeError(canary)
        ):
            root = Path(raw_temp)
            with self.assertRaises(common.WorkflowError) as raised:
                synthesize_approved_text(
                    "已审核口播", root / "out.mp3", env, root / "cache"
                )
        self.assertEqual("TTS_INTERNAL", raised.exception.code)
        self.assertNotIn(canary, str(raised.exception))


class RedirectAndBodyLimitTests(unittest.TestCase):
    def test_production_redirect_handler_never_opens_location(self) -> None:
        seen: list[str] = []

        class RedirectingHTTPSHandler(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                seen.append(request.full_url)
                headers = Message()
                headers["Location"] = "https://attacker.invalid/collect"
                stream = io.BytesIO(b"redirect")
                stream.msg = "Found"
                return addinfourl(stream, headers, request.full_url, 302)

        opener = urllib.request.build_opener(
            adapter._NoRedirectHandler(), RedirectingHTTPSHandler()
        )
        request = urllib.request.Request(adapter.OFFICIAL_ENDPOINT, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(request)
        self.assertEqual(302, raised.exception.code)
        self.assertEqual([adapter.OFFICIAL_ENDPOINT], seen)

    def test_success_body_is_bounded_with_and_without_content_length(self) -> None:
        cases = (
            ({}, b"x" * 17, [17]),
            ({"Content-Length": "17"}, b"", []),
        )
        with tempfile.TemporaryDirectory(prefix="tts-success-cap-") as raw_temp:
            root = Path(raw_temp)
            for index, (headers, body, expected_reads) in enumerate(cases):
                response = FakeResponse(body, headers=headers)
                with self.subTest(headers=headers), mock.patch.object(
                    adapter, "MAX_SUCCESS_BODY_BYTES", 16
                ), mock.patch.object(
                    adapter, "_open_request", return_value=response
                ):
                    with self.assertRaises(common.WorkflowError) as raised:
                        synthesize_approved_text(
                            "已审核口播",
                            root / f"out-{index}.mp3",
                            fake_env(),
                            root / f"cache-{index}",
                        )
                    self.assertEqual("TTS_RESPONSE_SIZE", raised.exception.code)
                    self.assertEqual(expected_reads, response.read_sizes)

    def test_error_body_read_is_capped(self) -> None:
        stream = RecordingBytesIO(b"x" * 100)
        http_error = urllib.error.HTTPError(
            adapter.OFFICIAL_ENDPOINT,
            400,
            "bad request",
            {},
            stream,
        )
        with tempfile.TemporaryDirectory(prefix="tts-error-cap-") as raw_temp, mock.patch.object(
            adapter, "MAX_ERROR_BODY_BYTES", 16
        ), mock.patch.object(adapter, "_open_request", side_effect=http_error):
            root = Path(raw_temp)
            with self.assertRaises(common.WorkflowError) as raised:
                synthesize_approved_text(
                    "已审核口播", root / "out.mp3", fake_env(), root / "cache"
                )
        self.assertEqual("TTS_HTTP", raised.exception.code)
        self.assertEqual([16], stream.read_sizes)


class CacheIntegrityTests(unittest.TestCase):
    def test_valid_cache_hit_requires_metadata_digest_size_and_mp3_signature(self) -> None:
        first_audio = fake_mp3(b"A")
        second_audio = fake_mp3(b"B")
        with tempfile.TemporaryDirectory(prefix="tts-cache-") as raw_temp:
            root = Path(raw_temp)
            cache_root = root / "cache"
            first_response = FakeResponse(first_audio)
            with mock.patch.object(
                adapter, "_open_request", return_value=first_response
            ) as transport:
                first = synthesize_approved_text(
                    "已审核口播", root / "first.mp3", fake_env(), cache_root
                )
            self.assertEqual("miss", first["cache"])
            transport.assert_called_once()

            cache_file = cache_root / "tts" / f"{first['cache_key']}.mp3"
            metadata_file = cache_root / "tts" / f"{first['cache_key']}.json"
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            self.assertEqual(len(first_audio), metadata["size"])
            self.assertEqual(common.sha256_bytes(first_audio), metadata["audio_sha256"])
            self.assertEqual(adapter.OFFICIAL_ENDPOINT, metadata["endpoint"])
            self.assertEqual(adapter.ADAPTER_VERSION, metadata["adapter_version"])
            cache_artifacts = cache_file.read_bytes() + metadata_file.read_bytes()
            self.assertNotIn(b"test-access-token", cache_artifacts)
            self.assertNotIn(b"test-app-id", cache_artifacts)
            self.assertNotIn(b"test-cluster", cache_artifacts)
            self.assertNotIn(b"test-voice", cache_artifacts)

            with mock.patch.object(
                adapter, "_open_request", side_effect=AssertionError("network used")
            ) as transport:
                hit = synthesize_approved_text(
                    "已审核口播", root / "hit.mp3", fake_env(), cache_root
                )
            self.assertEqual("hit", hit["cache"])
            self.assertEqual(first_audio, (root / "hit.mp3").read_bytes())
            transport.assert_not_called()

            cache_file.write_bytes(b"ID3" + b"P" * (len(first_audio) - 3))
            replacement = FakeResponse(second_audio)
            with mock.patch.object(
                adapter, "_open_request", return_value=replacement
            ) as transport:
                refreshed = synthesize_approved_text(
                    "已审核口播", root / "refreshed.mp3", fake_env(), cache_root
                )
            self.assertEqual("miss", refreshed["cache"])
            self.assertEqual(second_audio, (root / "refreshed.mp3").read_bytes())
            transport.assert_called_once()

            metadata_file.unlink()
            third_audio = fake_mp3(b"C")
            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(third_audio)
            ) as transport:
                orphan_refresh = synthesize_approved_text(
                    "已审核口播", root / "orphan.mp3", fake_env(), cache_root
                )
            self.assertEqual("miss", orphan_refresh["cache"])
            self.assertEqual(third_audio, (root / "orphan.mp3").read_bytes())
            transport.assert_called_once()

    def test_signed_but_undecodable_network_audio_is_removed(self) -> None:
        body = fake_mp3(b"X")
        with tempfile.TemporaryDirectory(prefix="tts-undecodable-network-") as raw_temp:
            root = Path(raw_temp)
            cache_root = root / "cache"
            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(body)
            ):
                with self.assertRaises(common.WorkflowError) as raised:
                    synthesize_approved_text(
                        "已审核口播",
                        root / "out.mp3",
                        fake_env(),
                        cache_root,
                        probe_results=False,
                    )
            self.assertEqual("TTS_AUDIO", raised.exception.code)
            self.assertFalse((root / "out.mp3").exists())
            self.assertEqual([], list(cache_root.rglob("*.mp3")))
            self.assertEqual([], list(cache_root.rglob("*.json")))

    def test_undecodable_cache_is_removed_and_refreshed(self) -> None:
        first_audio = fake_mp3(b"A")
        replacement_audio = fake_mp3(b"B")
        with tempfile.TemporaryDirectory(prefix="tts-undecodable-cache-") as raw_temp:
            root = Path(raw_temp)
            cache_root = root / "cache"
            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(first_audio)
            ):
                first = synthesize_approved_text(
                    "已审核口播", root / "first.mp3", fake_env(), cache_root
                )

            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(replacement_audio)
            ) as transport:
                refreshed = synthesize_approved_text(
                    "已审核口播",
                    root / "refreshed.mp3",
                    fake_env(),
                    cache_root,
                    probe_results=[False, True],
                )
            self.assertEqual("miss", refreshed["cache"])
            self.assertEqual(replacement_audio, (root / "refreshed.mp3").read_bytes())
            transport.assert_called_once()
            cache_file = cache_root / "tts" / f"{first['cache_key']}.mp3"
            metadata_file = cache_root / "tts" / f"{first['cache_key']}.json"
            self.assertEqual(replacement_audio, cache_file.read_bytes())
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            self.assertEqual(common.sha256_bytes(replacement_audio), metadata["audio_sha256"])

    def test_non_mp3_success_is_not_cached_or_copied(self) -> None:
        body = b"RIFF" + b"x" * (adapter.MIN_AUDIO_BYTES + 32)
        with tempfile.TemporaryDirectory(prefix="tts-bad-audio-") as raw_temp:
            root = Path(raw_temp)
            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(body)
            ):
                with self.assertRaises(common.WorkflowError) as raised:
                    synthesize_approved_text(
                        "已审核口播", root / "out.mp3", fake_env(), root / "cache"
                    )
            self.assertEqual("TTS_AUDIO", raised.exception.code)
            self.assertFalse((root / "out.mp3").exists())
            self.assertEqual([], list((root / "cache").rglob("*.mp3")))
            self.assertEqual([], list((root / "cache").rglob("*.json")))

    def test_network_and_cache_outputs_use_project_confined_atomic_copy(self) -> None:
        audio = fake_mp3(b"S")
        with tempfile.TemporaryDirectory(prefix="tts-atomic-output-") as raw_temp:
            root = Path(raw_temp)
            project = root / "project"
            project.mkdir()
            cache = root / "cache"
            network_output = project / "audio" / "network.mp3"
            with mock.patch.object(
                adapter, "_open_request", return_value=FakeResponse(audio)
            ) as transport:
                miss = synthesize_approved_text(
                    "已审核口播",
                    network_output,
                    fake_env(),
                    cache,
                    project_root=project,
                )
            self.assertEqual("miss", miss["cache"])
            self.assertEqual(audio, network_output.read_bytes())
            transport.assert_called_once()

            cache_output = project / "audio" / "cache.mp3"
            with mock.patch.object(
                adapter, "_open_request", side_effect=AssertionError("network used")
            ) as transport:
                hit = synthesize_approved_text(
                    "已审核口播",
                    cache_output,
                    fake_env(),
                    cache,
                    project_root=project,
                )
            self.assertEqual("hit", hit["cache"])
            self.assertEqual(audio, cache_output.read_bytes())
            transport.assert_not_called()
            self.assertEqual([], list((project / "audio").glob(".source-led-tts-*.tmp")))


class DirectCliGateTests(unittest.TestCase):
    def test_library_transport_has_no_free_text_review_bypass(self) -> None:
        blocked = common.WorkflowError("REVIEW_BLOCKED", "approval missing")
        with tempfile.TemporaryDirectory(prefix="tts-library-gate-") as raw_temp, mock.patch.object(
            review_gate, "verify_approval", side_effect=blocked
        ), mock.patch.object(adapter, "_open_request") as transport:
            root = Path(raw_temp)
            with self.assertRaises(common.WorkflowError) as raised:
                adapter.synthesize(
                    root / "project.json",
                    root / "out.mp3",
                    fake_env(),
                    root / "cache",
                )
        self.assertEqual("REVIEW_BLOCKED", raised.exception.code)
        transport.assert_not_called()

    def test_old_free_text_arguments_are_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            direct_cli.main(["--text", "绕过审核", "--out", "/tmp/unused.mp3"])
        self.assertEqual(2, raised.exception.code)

    def test_pending_rights_approval_still_allows_exact_config_narration_tts(self) -> None:
        config = {
            "narration": {
                "scenes": [
                    {"id": "one", "text": "第一段。", "captions": ["第一段。"]},
                    {"id": "two", "text": "第二段。", "captions": ["第二段。"]},
                ]
            }
        }
        approval = {
            "narration_sha256": common.narration_hash(config),
            "rights_clearance": review_gate.RIGHTS_PENDING,
        }
        events: list[str] = []

        def verify(path: Path) -> dict:
            events.append("verify")
            self.assertTrue(path.is_absolute())
            return approval

        def load_config(_path: Path):
            events.append("load-config")
            return config, Path(raw_temp)

        def synthesize(config_path: Path, output: Path, env: dict[str, str], cache: Path):
            events.append("synthesize")
            self.assertTrue(config_path.is_absolute())
            self.assertEqual("project.json", config_path.name)
            self.assertEqual(fake_env(), env)
            return {"cache": "miss", "cache_key": "safe-test-key"}

        with tempfile.TemporaryDirectory(prefix="tts-cli-") as raw_temp, mock.patch.object(
            direct_cli.review_gate, "verify_approval", side_effect=verify
        ), mock.patch.object(
            direct_cli, "load_and_validate_config", side_effect=load_config
        ), mock.patch.object(
            direct_cli, "load_env_file", return_value=fake_env()
        ), mock.patch.object(
            direct_cli, "runtime_root", return_value=Path(raw_temp) / "cache"
        ), mock.patch.object(
            direct_cli, "synthesize", side_effect=synthesize
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result = direct_cli.main(
                    [
                        "--config",
                        str(Path(raw_temp) / "project.json"),
                        "--out",
                        str(Path(raw_temp) / "voice.mp3"),
                    ]
                )
        self.assertEqual(0, result)
        self.assertEqual(["verify", "load-config", "synthesize"], events)

    def test_output_leaf_symlink_is_rejected_before_env_or_synthesis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tts-cli-leaf-link-") as raw_temp:
            root = Path(raw_temp)
            project = root / "project"
            project.mkdir()
            protected = root / "protected.mp3"
            protected.write_bytes(b"unchanged")
            output = project / "voice.mp3"
            output.symlink_to(protected)
            self._assert_output_rejected(project, output, "PROJECT_OUTPUT_SYMLINK")
            self.assertEqual(b"unchanged", protected.read_bytes())

    def test_output_ancestor_symlink_is_rejected_before_synthesis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tts-cli-parent-link-") as raw_temp:
            root = Path(raw_temp)
            project = root / "project"
            project.mkdir()
            external = root / "external"
            external.mkdir()
            (project / "audio").symlink_to(external, target_is_directory=True)
            output = project / "audio" / "voice.mp3"
            self._assert_output_rejected(project, output, "PROJECT_OUTPUT_SYMLINK")
            self.assertFalse((external / "voice.mp3").exists())

    def test_output_outside_project_is_rejected_before_synthesis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tts-cli-outside-") as raw_temp:
            root = Path(raw_temp)
            project = root / "project"
            project.mkdir()
            output = root / "outside.mp3"
            self._assert_output_rejected(project, output, "PROJECT_OUTPUT")
            self.assertFalse(output.exists())

    def test_output_non_directory_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tts-cli-file-parent-") as raw_temp:
            root = Path(raw_temp)
            project = root / "project"
            project.mkdir()
            (project / "audio").write_bytes(b"not a directory")
            self._assert_output_rejected(
                project,
                project / "audio" / "voice.mp3",
                "PROJECT_OUTPUT",
            )

    def _assert_output_rejected(
        self, project: Path, output: Path, expected_code: str
    ) -> None:
        config = {
            "narration": {
                "scenes": [
                    {"id": "one", "text": "第一段。", "captions": ["第一段。"]},
                ]
            }
        }
        approval = {"narration_sha256": common.narration_hash(config)}
        with mock.patch.object(
            direct_cli.review_gate, "verify_approval", return_value=approval
        ), mock.patch.object(
            direct_cli,
            "load_and_validate_config",
            return_value=(config, project),
        ), mock.patch.object(
            direct_cli, "load_env_file"
        ) as load_env, mock.patch.object(
            direct_cli, "synthesize"
        ) as synthesize:
            with self.assertRaises(common.WorkflowError) as raised:
                direct_cli.main(
                    [
                        "--config",
                        str(project / "project.json"),
                        "--out",
                        str(output),
                    ]
                )
        self.assertEqual(expected_code, raised.exception.code)
        load_env.assert_not_called()
        synthesize.assert_not_called()

    def test_blocked_review_stops_before_config_env_and_synthesis(self) -> None:
        blocked = common.WorkflowError("REVIEW_BLOCKED", "approval missing")
        with mock.patch.object(
            direct_cli.review_gate, "verify_approval", side_effect=blocked
        ), mock.patch.object(
            direct_cli, "load_and_validate_config"
        ) as load_config, mock.patch.object(
            direct_cli, "load_env_file"
        ) as load_env, mock.patch.object(
            direct_cli, "synthesize"
        ) as synthesize:
            with self.assertRaises(common.WorkflowError) as raised:
                direct_cli.main(
                    ["--config", "/tmp/missing-project.json", "--out", "/tmp/unused.mp3"]
                )
        self.assertEqual("REVIEW_BLOCKED", raised.exception.code)
        load_config.assert_not_called()
        load_env.assert_not_called()
        synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()

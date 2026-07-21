from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SKILL_ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = SKILL_ROOT / "assets/fonts/NotoSansCJKsc-Regular.otf"
MODEL_ID = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
MODEL_REVISION = "0e1a68e91d815300c7c9754b2a7639378b23db15"
LOCKED_ALIGNER_PYTHON_VERSION = "3.11.15"
FIXED_MAX_DURATION_SECONDS = 90.0
MAX_NARRATION_CHARACTERS = 480
FIXED_FINAL_LUFS = -14.0
FIXED_RETIME_RATIO_LIMITS = (0.8, 1.2)
BGM_FADE_OUT_DEFAULT_SECONDS = 1.8
BGM_FADE_OUT_RANGE_SECONDS = (0.5, 5.0)
LOCKED_FFMPEG_SHA256 = "9a08d61f9328e8164ba560ee7a79958e357307fcfeea6fe626b7d66cdc287028"
LOCKED_FFPROBE_SHA256 = "aab17ac7379c1178aaf400c3ef36cdb67db0b75b1a23eeef2cb9f658be8844e6"
REQUIRED_VOLC_ENV = (
    "VOLC_TTS_ENDPOINT",
    "VOLC_TTS_APP_ID",
    "VOLC_TTS_ACCESS_TOKEN",
    "VOLC_TTS_CLUSTER",
    "VOLC_TTS_VOICE",
    "VOLC_TTS_ENCODING",
)
MANAGED_PROJECT_ROOTS = frozenset(
    {"review", ".source-led-ai-video", "review-runs", "deliverables"}
)


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write_bytes(path, canonical_json(value) + b"\n", mode=mode)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("CONFIG_READ", f"Cannot read JSON: {path.name}") from exc


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text))).strip()


def spoken_glyphs(text: str) -> str:
    return "".join(
        character
        for character in normalize_text(text)
        if unicodedata.category(character)[:1] in {"L", "N"}
    )


def safe_cover_hook_name(hook: str) -> str:
    if type(hook) is not str:
        raise WorkflowError("CONFIG_TYPE", "cover.hook must be a string")
    value = re.sub(r"[\\/:*?\"<>|\s，。；：、！？,.!?;:]+", "", normalize_text(hook))
    return value[:32]


def caption_objects(scene: dict[str, Any]) -> list[dict[str, Any]]:
    if type(scene) is not dict:
        raise WorkflowError("CONFIG_SCENE", "Narration scenes must be objects")
    raw_captions = scene.get("captions")
    if type(raw_captions) is not list or not raw_captions:
        raise WorkflowError("CONFIG_CAPTION", "Scene captions must be a non-empty list")
    result: list[dict[str, Any]] = []
    for item in raw_captions:
        if type(item) is str:
            text = normalize_text(item)
            show = True
        elif type(item) is dict:
            raw_text = item.get("text")
            if type(raw_text) is not str:
                raise WorkflowError("CONFIG_CAPTION", "Caption text must be a string")
            show = item.get("show", True)
            if type(show) is not bool:
                raise WorkflowError("CONFIG_CAPTION", "Caption show must be true or false")
            text = normalize_text(raw_text)
        else:
            raise WorkflowError("CONFIG_CAPTION", "Caption entries must be text or objects")
        if not text or not spoken_glyphs(text):
            raise WorkflowError("CONFIG_CAPTION", "Caption text must contain spoken characters")
        result.append({"text": text, "show": show})
    return result


def narration_payload(config: dict[str, Any]) -> dict[str, Any]:
    scenes = []
    for scene in config["narration"]["scenes"]:
        scenes.append(
            {
                "id": scene["id"],
                "text": normalize_text(scene["text"]),
                "captions": caption_objects(scene),
            }
        )
    return {
        "schema": 1,
        "platform": config.get("platform", "douyin"),
        "language": config["narration"].get("language", "Chinese"),
        "scenes": scenes,
    }


def narration_hash(config: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(narration_payload(config)))


def narration_text(config: dict[str, Any]) -> str:
    return "\n\n".join(normalize_text(scene["text"]) for scene in config["narration"]["scenes"])


def project_path(project_root: Path, raw: str, *, must_exist: bool = False) -> Path:
    if type(raw) is not str:
        raise WorkflowError("CONFIG_TYPE", "Project paths must be strings")
    if not raw.strip():
        raise WorkflowError("PATH_EMPTY", "Project path cannot be empty")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise WorkflowError("PATH_ABSOLUTE", "Project paths must be relative")
    if ".." in candidate.parts:
        raise WorkflowError("PATH_ESCAPE", "Project paths cannot contain '..'")
    if any(ord(character) < 32 for character in raw):
        raise WorkflowError("PATH_INVALID", "Project paths cannot contain control characters")
    resolved = (project_root / candidate).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise WorkflowError("PATH_ESCAPE", "Project path leaves the project directory") from exc
    if must_exist and not resolved.is_file():
        raise WorkflowError("PATH_MISSING", f"Missing project input: {raw}")
    if not must_exist and resolved.exists() and not resolved.is_file():
        raise WorkflowError("PATH_NOT_FILE", f"Project media path must be a file: {raw}")
    return resolved


def _project_output_relative(
    project_root: Path, candidate: Path | str
) -> tuple[Path, Path]:
    """Resolve only the project root; keep the output path lexical for lstat."""
    root = project_root.resolve()
    raw = Path(candidate)
    raw_text = os.fspath(raw)
    if ".." in raw.parts:
        raise WorkflowError("PROJECT_OUTPUT", "Output path cannot contain '..'")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_text):
        raise WorkflowError("PROJECT_OUTPUT", "Output path cannot contain control characters")
    lexical = (
        Path(os.path.abspath(raw_text))
        if raw.is_absolute()
        else root.joinpath(*raw.parts)
    )
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        # Preserve the two standard macOS aliases without resolving arbitrary
        # caller-controlled ancestors such as a symlink to the project root.
        alias_lexical: Path | None = None
        if (
            len(root.parts) > 2
            and root.parts[1] == "private"
            and len(lexical.parts) > 1
            and lexical.parts[1] in {"tmp", "var"}
        ):
            alias_lexical = Path("/private").joinpath(*lexical.parts[1:])
        try:
            relative = alias_lexical.relative_to(root) if alias_lexical else None
        except ValueError as alias_exc:
            raise WorkflowError(
                "PROJECT_OUTPUT", "Output path must stay inside the project"
            ) from alias_exc
        if relative is None:
            raise WorkflowError(
                "PROJECT_OUTPUT", "Output path must stay inside the project"
            ) from exc
    if not relative.parts:
        raise WorkflowError("PROJECT_OUTPUT", "Output path must name a file")
    return root, relative


def project_output_path(project_root: Path, candidate: Path | str) -> Path:
    """Validate a project-confined output without following existing links."""
    root, relative = _project_output_relative(project_root, candidate)
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise WorkflowError("PROJECT_OUTPUT", "Project directory is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkflowError("PROJECT_OUTPUT", "Project root must be a plain directory")

    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise WorkflowError("PROJECT_OUTPUT", "Output path cannot be inspected") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise WorkflowError("PROJECT_OUTPUT_SYMLINK", "Output path cannot contain symlinks")
        if index < len(relative.parts) - 1:
            if not stat.S_ISDIR(entry_stat.st_mode):
                raise WorkflowError(
                    "PROJECT_OUTPUT", "Output path parent must be a directory"
                )
        elif not stat.S_ISREG(entry_stat.st_mode):
            raise WorkflowError(
                "PROJECT_OUTPUT", "Existing output must be a regular file"
            )
    return root.joinpath(*relative.parts)


def project_atomic_copy_file(
    project_root: Path,
    source: Path,
    destination: Path | str,
    *,
    mode: int = 0o600,
) -> Path:
    """Copy to a project output through a pinned directory and atomic replace."""
    root, relative = _project_output_relative(project_root, destination)
    target = project_output_path(root, root.joinpath(*relative.parts))
    try:
        source_stat = os.lstat(source)
    except OSError as exc:
        raise WorkflowError("PROJECT_OUTPUT_WRITE", "Copy source is unavailable") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise WorkflowError("PROJECT_OUTPUT_WRITE", "Copy source must be a regular file")

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    output_flags |= getattr(os, "O_CLOEXEC", 0)
    output_flags |= getattr(os, "O_NOFOLLOW", 0)
    opened_directories: list[int] = []
    temp_fd: int | None = None
    temp_name: str | None = None
    try:
        current_fd = os.open(root, directory_flags)
        opened_directories.append(current_fd)
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            current_fd = next_fd
            opened_directories.append(current_fd)

        leaf_name = relative.parts[-1]

        def validate_leaf() -> None:
            try:
                leaf_stat = os.stat(
                    leaf_name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISLNK(leaf_stat.st_mode):
                raise WorkflowError(
                    "PROJECT_OUTPUT_SYMLINK", "Output leaf cannot be a symlink"
                )
            if not stat.S_ISREG(leaf_stat.st_mode):
                raise WorkflowError(
                    "PROJECT_OUTPUT", "Existing output must be a regular file"
                )

        validate_leaf()
        for _ in range(16):
            candidate_name = f".source-led-tts-{secrets.token_hex(12)}.tmp"
            try:
                temp_fd = os.open(
                    candidate_name,
                    output_flags,
                    mode,
                    dir_fd=current_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate_name
            break
        if temp_fd is None or temp_name is None:
            raise WorkflowError(
                "PROJECT_OUTPUT_WRITE", "Could not create a safe temporary output"
            )

        with source.open("rb") as source_handle, os.fdopen(temp_fd, "wb") as target_handle:
            temp_fd = None
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())

        validate_leaf()
        os.replace(
            temp_name,
            leaf_name,
            src_dir_fd=current_fd,
            dst_dir_fd=current_fd,
        )
        temp_name = None
        os.fsync(current_fd)
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(
            "PROJECT_OUTPUT_WRITE", "Could not safely publish the TTS output"
        ) from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None and opened_directories:
            try:
                os.unlink(temp_name, dir_fd=opened_directories[-1])
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for descriptor in reversed(opened_directories):
            os.close(descriptor)
    return project_output_path(root, target)


def normalized_path_identity(path: Path | str) -> str:
    """Filesystem-independent identity for case and Unicode-equivalent paths."""
    return unicodedata.normalize("NFC", os.fspath(path)).casefold()


def _managed_relative(project_root: Path, candidate: Path | str) -> tuple[Path, Path]:
    """Return a lexical managed path without following any candidate symlink."""
    root = project_root.resolve()
    raw = Path(candidate)
    if ".." in raw.parts:
        raise WorkflowError("MANAGED_PATH", "Managed paths cannot contain '..'")
    if any(ord(character) < 32 for character in os.fspath(raw)):
        raise WorkflowError("MANAGED_PATH", "Managed paths cannot contain control characters")
    if raw.is_absolute():
        lexical = Path(os.path.abspath(os.fspath(raw)))
    else:
        lexical = Path(os.path.abspath(os.fspath(root / raw)))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        # macOS commonly exposes the same temporary directory through /var and
        # /private/var. Locate an ancestor that is the project root by inode,
        # then keep checking every component below that root with lstat.
        alias_root: Path | None = None
        for ancestor in (lexical, *lexical.parents):
            try:
                if (
                    ancestor.exists()
                    and os.path.samefile(ancestor, root)
                    and ancestor.parent.exists()
                    and os.path.samefile(ancestor.parent, root.parent)
                ):
                    alias_root = ancestor
                    break
            except OSError:
                continue
        if alias_root is None:
            raise WorkflowError("MANAGED_PATH", "Managed path leaves the project directory") from exc
        try:
            relative = lexical.relative_to(alias_root)
        except ValueError as alias_exc:
            raise WorkflowError("MANAGED_PATH", "Managed path leaves the project directory") from alias_exc
        lexical = root.joinpath(*relative.parts)
    if not relative.parts or relative.parts[0] not in MANAGED_PROJECT_ROOTS:
        raise WorkflowError("MANAGED_PATH", "Path is outside the managed project directories")
    return root, relative


def managed_path(
    project_root: Path,
    candidate: Path | str,
    *,
    must_exist: bool = False,
    kind: str | None = None,
) -> Path:
    """Validate a managed path one component at a time using lstat.

    Existing symlinks are rejected at every level. Missing suffix components are
    allowed unless ``must_exist`` is true. ``kind`` may be ``file`` or ``dir``.
    """
    if kind not in {None, "file", "dir"}:
        raise ValueError("kind must be None, 'file', or 'dir'")
    root, relative = _managed_relative(project_root, candidate)
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise WorkflowError("MANAGED_PATH", "Project directory is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkflowError("MANAGED_PATH", "Project root must be a plain directory")

    current = root
    exists = True
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            exists = False
            break
        except OSError as exc:
            raise WorkflowError("MANAGED_PATH", "Managed path cannot be inspected") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise WorkflowError("MANAGED_SYMLINK", "Managed paths cannot contain symlinks")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(entry_stat.st_mode):
            raise WorkflowError("MANAGED_PATH", "Managed path parent is not a directory")

    lexical = root.joinpath(*relative.parts)
    try:
        lexical.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise WorkflowError("MANAGED_PATH", "Managed path resolves outside the project") from exc
    if must_exist and not exists:
        raise WorkflowError("MANAGED_MISSING", "Managed path is missing")
    if exists and kind is not None:
        try:
            leaf_stat = os.lstat(lexical)
        except OSError as exc:
            raise WorkflowError("MANAGED_PATH", "Managed path cannot be inspected") from exc
        if kind == "file" and not stat.S_ISREG(leaf_stat.st_mode):
            raise WorkflowError("MANAGED_PATH", "Managed path must be a regular file")
        if kind == "dir" and not stat.S_ISDIR(leaf_stat.st_mode):
            raise WorkflowError("MANAGED_PATH", "Managed path must be a plain directory")
    return lexical


def ensure_managed_dir(project_root: Path, candidate: Path | str) -> Path:
    """Create a managed directory without traversing symlink components."""
    root, relative = _managed_relative(project_root, candidate)
    managed_path(root, relative)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WorkflowError("MANAGED_CREATE", "Could not create managed directory") from exc
            try:
                entry_stat = os.lstat(current)
            except OSError as exc:
                raise WorkflowError("MANAGED_CREATE", "Managed directory was not created") from exc
        except OSError as exc:
            raise WorkflowError("MANAGED_PATH", "Managed directory cannot be inspected") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise WorkflowError("MANAGED_SYMLINK", "Managed paths cannot contain symlinks")
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise WorkflowError("MANAGED_PATH", "Managed directory component is not a directory")
    return managed_path(root, relative, must_exist=True, kind="dir")


def managed_atomic_write_bytes(
    project_root: Path,
    candidate: Path | str,
    data: bytes,
    *,
    mode: int = 0o600,
) -> Path:
    root, relative = _managed_relative(project_root, candidate)
    destination = root.joinpath(*relative.parts)
    ensure_managed_dir(root, destination.parent)
    managed_path(root, destination, kind="file")
    atomic_write_bytes(destination, data, mode=mode)
    return managed_path(root, destination, must_exist=True, kind="file")


def managed_atomic_write_json(
    project_root: Path,
    candidate: Path | str,
    value: Any,
    *,
    mode: int = 0o600,
) -> Path:
    return managed_atomic_write_bytes(
        project_root,
        candidate,
        canonical_json(value) + b"\n",
        mode=mode,
    )


def managed_atomic_write_text(
    project_root: Path,
    candidate: Path | str,
    text: str,
    *,
    mode: int = 0o600,
) -> Path:
    return managed_atomic_write_bytes(
        project_root,
        candidate,
        text.encode("utf-8"),
        mode=mode,
    )


def managed_copy_file(
    project_root: Path,
    source: Path,
    destination: Path | str,
    *,
    mode: int = 0o600,
) -> Path:
    """Atomically copy a file into a managed directory without following its leaf."""
    root, relative = _managed_relative(project_root, destination)
    target = root.joinpath(*relative.parts)
    ensure_managed_dir(root, target.parent)
    managed_path(root, target, kind="file")
    if not source.is_file():
        raise WorkflowError("MANAGED_COPY", "Copy source is missing")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        managed_path(root, target, kind="file")
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return managed_path(root, target, must_exist=True, kind="file")


def assert_managed_tree(project_root: Path, candidate: Path | str) -> Path:
    """Reject symlinks anywhere inside an existing managed directory tree."""
    directory = managed_path(project_root, candidate, must_exist=True, kind="dir")
    root = project_root.resolve()
    for walk_root, dir_names, file_names in os.walk(directory, topdown=True, followlinks=False):
        walk_path = Path(walk_root)
        managed_path(root, walk_path, must_exist=True, kind="dir")
        for name in [*dir_names, *file_names]:
            entry = walk_path / name
            try:
                entry_stat = os.lstat(entry)
            except OSError as exc:
                raise WorkflowError("MANAGED_PATH", "Managed tree cannot be inspected") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise WorkflowError("MANAGED_SYMLINK", "Managed trees cannot contain symlinks")
            managed_path(root, entry, must_exist=True)
    return directory


def remove_managed_tree(project_root: Path, candidate: Path | str) -> None:
    """Remove only a confirmed, project-contained, plain managed subdirectory."""
    root, relative = _managed_relative(project_root, candidate)
    if len(relative.parts) < 2:
        raise WorkflowError("MANAGED_DELETE", "Refusing to remove a managed root directory")
    directory = assert_managed_tree(root, root.joinpath(*relative.parts))
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise WorkflowError("MANAGED_DELETE", "Could not remove managed directory") from exc


def replace_managed_dir(
    project_root: Path,
    source: Path | str,
    destination: Path | str,
) -> Path:
    """Move a checked managed directory to a checked, absent managed destination."""
    root = project_root.resolve()
    source_path = assert_managed_tree(root, source)
    destination_path = managed_path(root, destination, kind="dir")
    ensure_managed_dir(root, destination_path.parent)
    destination_path = managed_path(root, destination_path, kind="dir")
    if destination_path.exists():
        raise WorkflowError("MANAGED_EXISTS", "Managed destination already exists")
    try:
        os.replace(source_path, destination_path)
    except OSError as exc:
        raise WorkflowError("MANAGED_MOVE", "Could not publish managed directory") from exc
    return assert_managed_tree(root, destination_path)


def open_managed_lock(project_root: Path, candidate: Path | str):
    """Open a regular managed lock file, using O_NOFOLLOW where available."""
    root, relative = _managed_relative(project_root, candidate)
    lock_path = root.joinpath(*relative.parts)
    ensure_managed_dir(root, lock_path.parent)
    managed_path(root, lock_path, kind="file")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorkflowError("MANAGED_LOCK", "Could not safely open the build lock") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise WorkflowError("MANAGED_LOCK", "Build lock must be a regular file")
        return os.fdopen(descriptor, "a+b")
    except Exception:
        os.close(descriptor)
        raise


def _finite_number(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise WorkflowError("CONFIG_NUMBER", f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not (-1e12 < number < 1e12):
        raise WorkflowError("CONFIG_NUMBER", f"{label} is not finite")
    return number


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise WorkflowError("CONFIG_TYPE", f"{label} must be an object")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise WorkflowError("CONFIG_TYPE", f"{label} must be a string")
    if not allow_empty and not normalize_text(value):
        raise WorkflowError("CONFIG_TYPE", f"{label} cannot be empty")
    return value


def load_and_validate_config(config_path: Path) -> tuple[dict[str, Any], Path]:
    config_path = config_path.resolve()
    project_root = config_path.parent
    config = read_json(config_path)
    if type(config) is not dict or type(config.get("version")) is not int or config.get("version") != 1:
        raise WorkflowError("CONFIG_VERSION", "Expected project config version 1")
    project_name = _text(config.get("project_name"), "project_name", allow_empty=True)
    if not normalize_text(project_name):
        raise WorkflowError("CONFIG_NAME", "project_name is required")
    if type(config.get("platform")) is not str or config.get("platform") != "douyin":
        raise WorkflowError("CONFIG_PLATFORM", "This workflow is fixed to douyin")
    if "commercial" in config and type(config["commercial"]) is not bool:
        raise WorkflowError("CONFIG_TYPE", "commercial must be true or false")
    if "review_scope" in config:
        review_scope = _mapping(config["review_scope"], "review_scope")
        industries = review_scope.get("industries", [])
        if (
            type(industries) is not list
            or any(type(item) is not str for item in industries)
            or industries != sorted(set(industries))
            or any(item not in {"medical", "finance"} for item in industries)
        ):
            raise WorkflowError(
                "CONFIG_REVIEW_SCOPE",
                "review_scope.industries must be a sorted unique list of medical/finance",
            )

    canvas = _mapping(config.get("canvas"), "canvas")
    canvas_values = (canvas.get("width"), canvas.get("height"), canvas.get("fps"))
    if any(type(value) is not int for value in canvas_values) or canvas_values != (1080, 1920, 30):
        raise WorkflowError("CONFIG_CANVAS", "Canvas must be 1080x1920 at 30 fps")

    paths = _mapping(config.get("paths"), "paths")
    base_video = _text(paths.get("base_video"), "paths.base_video", allow_empty=True)
    project_path(project_root, base_video, must_exist=True)
    if "bgm" in paths:
        bgm = _text(paths["bgm"], "paths.bgm", allow_empty=True)
        if bgm:
            project_path(project_root, bgm, must_exist=True)

    narration = _mapping(config.get("narration"), "narration")
    if "language" in narration:
        _text(narration["language"], "narration.language")
    if "caption_lead_ms" in narration:
        _finite_number(narration["caption_lead_ms"], "narration.caption_lead_ms")
    scenes = narration.get("scenes")
    if type(scenes) is not list or not scenes:
        raise WorkflowError("CONFIG_SCENES", "At least one narration scene is required")
    ids: set[str] = set()
    narration_characters = 0
    for index, scene in enumerate(scenes):
        if type(scene) is not dict:
            raise WorkflowError("CONFIG_SCENE", f"Scene at index {index} must be an object")
        raw_scene_id = scene.get("id")
        if type(raw_scene_id) is not str:
            raise WorkflowError("CONFIG_SCENE_ID", f"Invalid scene id at index {index}")
        scene_id = normalize_text(raw_scene_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", scene_id):
            raise WorkflowError("CONFIG_SCENE_ID", f"Invalid scene id at index {index}")
        if scene_id in ids:
            raise WorkflowError("CONFIG_SCENE_ID", f"Duplicate scene id: {scene_id}")
        ids.add(scene_id)
        raw_scene_text = scene.get("text")
        if type(raw_scene_text) is not str:
            raise WorkflowError("CONFIG_SCENE_TEXT", f"Scene {scene_id} text must be a string")
        text = normalize_text(raw_scene_text)
        narration_characters += len(text)
        captions = caption_objects(scene)
        if not text or not spoken_glyphs(text):
            raise WorkflowError("CONFIG_SCENE_TEXT", f"Scene {scene_id} needs spoken text")
        if spoken_glyphs("".join(item["text"] for item in captions)) != spoken_glyphs(text):
            raise WorkflowError("CONFIG_CAPTION_TEXT", f"Captions do not reconstruct scene {scene_id}")
    if narration_characters > MAX_NARRATION_CHARACTERS:
        raise WorkflowError(
            "CONFIG_NARRATION_LENGTH",
            f"Narration exceeds the {MAX_NARRATION_CHARACTERS}-character paid-TTS preflight limit",
        )

    source_timeline = _mapping(config.get("source_timeline"), "source_timeline")
    boundaries = source_timeline.get("scene_boundaries_seconds")
    if type(boundaries) is not list or len(boundaries) != len(scenes) + 1:
        raise WorkflowError("CONFIG_TIMELINE", "scene_boundaries_seconds must have scenes + 1 entries")
    numeric = [_finite_number(value, "scene boundary") for value in boundaries]
    if abs(numeric[0]) > 1e-6 or any(b <= a for a, b in zip(numeric, numeric[1:])):
        raise WorkflowError("CONFIG_TIMELINE", "Scene boundaries must start at zero and increase")
    if "retime_ratio_limits" in source_timeline:
        limits = source_timeline["retime_ratio_limits"]
        if type(limits) is not list or len(limits) != 2:
            raise WorkflowError("CONFIG_TIMELINE", "retime_ratio_limits must contain two numbers")
        low, high = (_finite_number(value, "retime ratio") for value in limits)
        if (low, high) != FIXED_RETIME_RATIO_LIMITS:
            raise WorkflowError(
                "CONFIG_TIMELINE",
                "retime_ratio_limits is fixed at [0.8, 1.2]",
            )

    cover = _mapping(config.get("cover"), "cover")
    raw_hook = _text(cover.get("hook"), "cover.hook", allow_empty=True)
    hook = normalize_text(raw_hook)
    if not (4 <= len(hook) <= 20):
        raise WorkflowError("CONFIG_COVER", "Cover hook must be 4-20 characters")
    if not safe_cover_hook_name(hook) or not spoken_glyphs(hook):
        raise WorkflowError("CONFIG_COVER", "Cover hook must contain filename-safe spoken characters")
    if "headline_lines" in cover:
        headline_lines = cover["headline_lines"]
        if type(headline_lines) is not list or not 1 <= len(headline_lines) <= 2:
            raise WorkflowError(
                "CONFIG_COVER", "cover.headline_lines must contain one or two strings"
            )
        normalized_lines: list[str] = []
        for index, line in enumerate(headline_lines):
            if type(line) is not str:
                raise WorkflowError(
                    "CONFIG_COVER", "cover.headline_lines entries must be strings"
                )
            normalized_line = normalize_text(
                _text(line, f"cover.headline_lines[{index}]", allow_empty=True)
            )
            if not normalized_line or not spoken_glyphs(normalized_line):
                raise WorkflowError(
                    "CONFIG_COVER", "cover.headline_lines cannot contain an empty line"
                )
            normalized_lines.append(normalized_line)
        if spoken_glyphs("".join(normalized_lines)) != spoken_glyphs(hook):
            raise WorkflowError(
                "CONFIG_COVER", "cover.headline_lines must reconstruct cover.hook"
            )
    if "visual_brief" in cover:
        _text(cover["visual_brief"], "cover.visual_brief", allow_empty=True)
    text_mode = cover.get("text_mode", "deterministic")
    if type(text_mode) is not str or text_mode not in {"imagegen", "deterministic"}:
        raise WorkflowError(
            "CONFIG_COVER",
            "cover.text_mode must be imagegen or deterministic",
        )
    allow_people = cover.get("allow_people", False)
    if type(allow_people) is not bool:
        raise WorkflowError("CONFIG_COVER", "cover.allow_people must be a boolean")
    cover_paths: list[Path] = []
    for ratio in ("3x4", "4x3"):
        entry = _mapping(cover.get(ratio), f"cover.{ratio}")
        ratio_text_mode = entry.get("text_mode", text_mode)
        if type(ratio_text_mode) is not str or ratio_text_mode not in {
            "imagegen",
            "deterministic",
        }:
            raise WorkflowError(
                "CONFIG_COVER",
                f"cover.{ratio}.text_mode must be imagegen or deterministic",
            )
        for field in ("generated_source", "final"):
            raw_path = _text(entry.get(field), f"cover.{ratio}.{field}", allow_empty=True)
            resolved_path = project_path(project_root, raw_path, must_exist=False)
            if Path(raw_path).suffix.lower() != ".png":
                raise WorkflowError("CONFIG_COVER", f"cover.{ratio}.{field} must end in .png")
            cover_paths.append(resolved_path)
    if len({normalized_path_identity(path) for path in cover_paths}) != len(cover_paths):
        raise WorkflowError("CONFIG_COVER", "All generated and final cover paths must be distinct")
    for index, first in enumerate(cover_paths):
        for second in cover_paths[index + 1 :]:
            try:
                same_existing_file = first.exists() and second.exists() and os.path.samefile(first, second)
            except OSError as exc:
                raise WorkflowError("CONFIG_COVER", "Cover paths cannot be compared safely") from exc
            if same_existing_file:
                raise WorkflowError(
                    "CONFIG_COVER", "All generated and final cover paths must be separate files"
                )

    if "caption_style" in config:
        style = _mapping(config["caption_style"], "caption_style")
        for field in ("font_size", "margin_v", "max_line_units"):
            if field in style:
                _finite_number(style[field], f"caption_style.{field}")
    if "audio" in config:
        audio = _mapping(config["audio"], "audio")
        if "final_lufs" in audio:
            final_lufs = _finite_number(audio["final_lufs"], "audio.final_lufs")
            if final_lufs != FIXED_FINAL_LUFS:
                raise WorkflowError("CONFIG_AUDIO", "audio.final_lufs is fixed at -14")
        if "bgm_fade_out_seconds" in audio:
            fade = _finite_number(
                audio["bgm_fade_out_seconds"], "audio.bgm_fade_out_seconds"
            )
            fade_low, fade_high = BGM_FADE_OUT_RANGE_SECONDS
            if not fade_low <= fade <= fade_high:
                raise WorkflowError(
                    "CONFIG_AUDIO",
                    f"audio.bgm_fade_out_seconds must be within {fade_low:.1f}-{fade_high:.1f}",
                )
    if "qa" in config:
        qa = _mapping(config["qa"], "qa")
        if "max_duration_seconds" in qa:
            maximum = _finite_number(
                qa["max_duration_seconds"], "qa.max_duration_seconds"
            )
            if maximum != FIXED_MAX_DURATION_SECONDS:
                raise WorkflowError(
                    "CONFIG_QA", "qa.max_duration_seconds is fixed at 90"
                )
        for field in ("allow_black", "allow_silence"):
            if field in qa and type(qa[field]) is not bool:
                raise WorkflowError("CONFIG_TYPE", f"qa.{field} must be true or false")
            if qa.get(field, False) is not False:
                raise WorkflowError("CONFIG_QA", f"qa.{field} must be false")

    return config, project_root


def load_env_file(path: Path | None) -> dict[str, str]:
    values = dict(os.environ)
    if path is None:
        env_hint = os.getenv("VOLC_TTS_ENV_FILE", "").strip()
        if env_hint:
            path = Path(env_hint).expanduser()
        else:
            default_path = Path.home() / ".config/source-led-ai-video/volc.env"
            if default_path.is_file():
                path = default_path
    if path is not None:
        path = path.expanduser()
        try:
            permissions = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise WorkflowError("TTS_ENV_READ", "Cannot read the Volcengine env file") from exc
        if permissions & 0o077:
            raise WorkflowError("TTS_ENV_PERMISSIONS", "Volcengine env file must use mode 0600")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WorkflowError("TTS_ENV_READ", "Cannot read the Volcengine env file") from exc
        file_values: dict[str, str] = {}
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if key in REQUIRED_VOLC_ENV:
                if key in file_values:
                    raise WorkflowError("TTS_CONFIG", f"Duplicate Volcengine setting: {key}")
                file_values[key] = value
        values.update(file_values)
    return values


def redact(message: Any, env: dict[str, str] | None = None) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(message or "unknown error")).strip()
    candidates = []
    source = env or os.environ
    for name in (*REQUIRED_VOLC_ENV, "VOLC_TTS_SECRET_KEY"):
        value = source.get(name, "")
        if value:
            candidates.extend((value, value.encode("utf-8").hex()))
    for secret in sorted(set(candidates), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text[:400] or "unknown error"


def run_process(
    args: Iterable[str | Path],
    *,
    cwd: Path | None = None,
    capture: bool = True,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 900,
) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in args]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("PROCESS_TIMEOUT", "A media or alignment process timed out") from exc


def runtime_root() -> Path:
    override = os.getenv("SOURCE_LED_RUNTIME_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".cache/source-led-ai-video"


def runtime_manifest() -> dict[str, Any]:
    path = runtime_root() / "runtime.json"
    return read_json(path) if path.exists() else {}


def resolve_media_tools(
    ffmpeg_override: str | None = None, ffprobe_override: str | None = None
) -> tuple[Path, Path]:
    import shutil

    manifest = runtime_manifest()
    ffmpeg = ffmpeg_override or os.getenv("SOURCE_LED_FFMPEG") or manifest.get("ffmpeg") or shutil.which("ffmpeg")
    ffprobe = ffprobe_override or os.getenv("SOURCE_LED_FFPROBE") or manifest.get("ffprobe") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise WorkflowError("RUNTIME_MEDIA", "Run bootstrap_runtime.py before production")
    ffmpeg_path, ffprobe_path = Path(ffmpeg), Path(ffprobe)
    if not (ffmpeg_path.is_file() and os.access(ffmpeg_path, os.X_OK)):
        raise WorkflowError("RUNTIME_MEDIA", "FFmpeg is missing or not executable")
    if not (ffprobe_path.is_file() and os.access(ffprobe_path, os.X_OK)):
        raise WorkflowError("RUNTIME_MEDIA", "FFprobe is missing or not executable")
    if sha256_file(ffmpeg_path) != LOCKED_FFMPEG_SHA256:
        raise WorkflowError("RUNTIME_MEDIA", "FFmpeg does not match the locked runtime")
    if sha256_file(ffprobe_path) != LOCKED_FFPROBE_SHA256:
        raise WorkflowError("RUNTIME_MEDIA", "FFprobe does not match the locked runtime")
    return ffmpeg_path, ffprobe_path


def resolve_aligner_python(override: str | None = None) -> Path:
    manifest = runtime_manifest()
    raw = override or os.getenv("SOURCE_LED_ALIGNER_PYTHON") or manifest.get("aligner_python")
    if not raw:
        raise WorkflowError("RUNTIME_ALIGNER", "Run bootstrap_runtime.py before alignment")
    path = Path(raw)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise WorkflowError("RUNTIME_ALIGNER", "Aligner Python is missing or not executable")
    return path

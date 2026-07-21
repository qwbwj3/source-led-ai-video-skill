#!/usr/bin/env python3
"""Post-publication share-document decisions and deterministic Feishu XML.

This module is deliberately operational.  It writes only below
``.source-led-ai-video/share-doc`` and never participates in production build,
release, or cache identities.  It does not call Feishu APIs.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from lib.common import (
    WorkflowError,
    canonical_json,
    ensure_managed_dir,
    managed_atomic_write_text,
    managed_path,
    normalize_text,
    project_path,
    redact,
    sha256_bytes,
    sha256_file,
)
from lib.telemetry import (
    TelemetryError,
    append_identity_record,
    identity_log_lock,
    read_events,
)


PROJECT_TYPES: dict[str, str] = {
    "open_source_project": "开源项目",
    "community_demo": "社区创作案例",
    "ai_visual_case": "AI 视觉案例",
    "model_capability_case": "模型能力案例",
    "tool_workflow": "工具与工作流",
    "prompt_method": "提示词与方法",
    "other": "其他",
}
DECISIONS = frozenset({"include", "skip"})
BUILD_KEY_PATTERN = re.compile(r"[0-9a-f]{20}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PUBLICATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,99}")
REMOTE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
CONTENT_KEYS = frozenset(
    {
        "title",
        "one_line_takeaway",
        "overview",
        "toolchain",
        "repository_url",
        "source_url",
        "prompt",
        "resources",
    }
)
RESOURCE_KEYS = frozenset({"label", "url", "note"})
PROMPT_KEYS = frozenset({"text", "provenance", "source_url", "title"})
PROMPT_LABELS = {
    "author_public": "原作者提示词",
    "author_summary": "作者说明整理",
    "editorial_reconstruction": "参考写法",
}
SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\b(?:sk|ak|token|secret)[-_][a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}\b"),
    re.compile(
        r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}(?:\.[a-zA-Z0-9_-]{8,})?\b"
    ),
    re.compile(r"(?i)\bgh[pousr]_[a-z0-9]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
)
SENSITIVE_QUERY_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "auth",
        "authorization",
        "credential",
        "key",
        "passwd",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
SENSITIVE_QUERY_SUFFIXES = (
    "_api_key",
    "access_token",
    "_credential",
    "_password",
    "_secret",
    "_signature",
    "_token",
)


def _reject_sensitive(value: str, label: str) -> None:
    if any(pattern.search(value) for pattern in SENSITIVE_TEXT_PATTERNS):
        raise WorkflowError("SHARE_DOC_SENSITIVE", f"{label} contains a secret-like value")


def _one_line(value: Any, label: str, *, limit: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} must be text")
    clean = normalize_text(value)
    if not clean:
        if optional:
            return None
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} is required")
    if len(clean) > limit:
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} contains control characters")
    _reject_sensitive(clean, label)
    return clean


def _multiline(value: Any, label: str, *, limit: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} must be text")
    clean = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    clean = "\n".join(line.rstrip() for line in clean.split("\n")).strip()
    if not clean:
        if optional:
            return None
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} is required")
    if len(clean) > limit:
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} is too long")
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in clean
    ):
        raise WorkflowError("SHARE_DOC_TEXT", f"{label} contains control characters")
    _reject_sensitive(clean, label)
    return clean


def _url(value: Any, label: str, *, optional: bool = True) -> str | None:
    clean = _one_line(value, label, limit=2048, optional=optional)
    if clean is None:
        return None
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError as exc:
        raise WorkflowError("SHARE_DOC_URL", f"{label} is invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise WorkflowError("SHARE_DOC_URL", f"{label} must be a public HTTP(S) URL")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise WorkflowError("SHARE_DOC_URL", f"{label} must not use a private hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise WorkflowError("SHARE_DOC_URL", f"{label} must not use a private IP address")
    try:
        legacy_packed = socket.inet_aton(hostname)
    except OSError:
        legacy_address = None
    else:
        legacy_address = ipaddress.ip_address(legacy_packed)
    if legacy_address is not None and (
        hostname != str(legacy_address) or not legacy_address.is_global
    ):
        raise WorkflowError(
            "SHARE_DOC_URL", f"{label} must use a canonical public IP address"
        )
    for raw_name, _ in parse_qsl(parsed.query, keep_blank_values=True):
        name = raw_name.casefold().replace("-", "_")
        if name in SENSITIVE_QUERY_NAMES or name.endswith(SENSITIVE_QUERY_SUFFIXES):
            raise WorkflowError(
                "SHARE_DOC_SENSITIVE", f"{label} contains a signed or secret URL"
            )
    return clean


def _identity(
    *, publication_id: Any, build_key: Any, video_sha256: Any
) -> dict[str, str | None]:
    if type(publication_id) is not str or PUBLICATION_ID_PATTERN.fullmatch(publication_id) is None:
        raise WorkflowError("SHARE_DOC_IDENTITY", "publication_id is invalid")
    if build_key is not None and (
        type(build_key) is not str or BUILD_KEY_PATTERN.fullmatch(build_key) is None
    ):
        raise WorkflowError(
            "SHARE_DOC_IDENTITY", "build_key must be null or 20 lowercase hex characters"
        )
    if type(video_sha256) is not str or SHA256_PATTERN.fullmatch(video_sha256) is None:
        raise WorkflowError("SHARE_DOC_IDENTITY", "video_sha256 must be lowercase SHA-256")
    return {
        "publication_id": publication_id,
        "build_key": build_key,
        "video_sha256": video_sha256,
    }


def validate_content(raw: Any) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) - CONTENT_KEYS:
        raise WorkflowError("SHARE_DOC_CONTENT", "Share-document content has unknown fields")
    title = _one_line(raw.get("title"), "title", limit=120)
    one_line_takeaway = _one_line(
        raw.get("one_line_takeaway"), "one_line_takeaway", limit=240
    )
    overview = _multiline(raw.get("overview"), "overview", limit=1200)
    toolchain_raw = raw.get("toolchain")
    if type(toolchain_raw) is not list or not 3 <= len(toolchain_raw) <= 5:
        raise WorkflowError("SHARE_DOC_CONTENT", "toolchain must contain 3 to 5 steps")
    toolchain = [
        _one_line(step, f"toolchain step {index}", limit=180)
        for index, step in enumerate(toolchain_raw, start=1)
    ]
    repository_url = _url(raw.get("repository_url"), "repository_url")
    source_url = _url(raw.get("source_url"), "source_url")
    prompt_raw = raw.get("prompt")
    if prompt_raw is None:
        prompt = None
    else:
        if type(prompt_raw) is not dict or set(prompt_raw) - PROMPT_KEYS:
            raise WorkflowError("SHARE_DOC_CONTENT", "prompt must be a provenance object")
        provenance = prompt_raw.get("provenance")
        if provenance not in PROMPT_LABELS:
            raise WorkflowError("SHARE_DOC_CONTENT", "prompt provenance is unsupported")
        prompt = {
            "text": _multiline(prompt_raw.get("text"), "prompt text", limit=12000),
            "provenance": provenance,
            "source_url": _url(prompt_raw.get("source_url"), "prompt source_url"),
            "title": _one_line(
                prompt_raw.get("title"), "prompt title", limit=160, optional=True
            ),
        }
    resources_raw = raw.get("resources", [])
    if type(resources_raw) is not list or len(resources_raw) > 8:
        raise WorkflowError("SHARE_DOC_CONTENT", "resources must be a list of at most 8 items")
    resources: list[dict[str, str | None]] = []
    seen_resources: set[bytes] = set()
    for index, item in enumerate(resources_raw, start=1):
        if type(item) is not dict or set(item) - RESOURCE_KEYS:
            raise WorkflowError("SHARE_DOC_CONTENT", f"resource {index} has unknown fields")
        resource = {
            "label": _one_line(item.get("label"), f"resource {index} label", limit=100),
            "url": _url(item.get("url"), f"resource {index} url"),
            "note": _multiline(
                item.get("note"), f"resource {index} note", limit=400, optional=True
            ),
        }
        if resource["url"] is None and resource["note"] is None:
            raise WorkflowError(
                "SHARE_DOC_CONTENT", f"resource {index} requires a URL or note"
            )
        fingerprint = canonical_json(resource)
        if fingerprint in seen_resources:
            raise WorkflowError("SHARE_DOC_CONTENT", "resources cannot contain duplicates")
        seen_resources.add(fingerprint)
        resources.append(resource)
    return {
        "title": title,
        "one_line_takeaway": one_line_takeaway,
        "overview": overview,
        "toolchain": toolchain,
        "repository_url": repository_url,
        "source_url": source_url,
        "prompt": prompt,
        "resources": resources,
    }


def _xml_text(value: str) -> str:
    return escape(value, {"\"": "&quot;", "'": "&apos;"}).replace("\n", "<br/>")


def _material_id(identity: dict[str, str | None]) -> str:
    return f"SLAV-{identity['video_sha256'][:12].upper()}"


def render_entry_xml(
    project_type: str,
    content: dict[str, Any],
    *,
    material_id: str,
) -> tuple[str, str]:
    if project_type not in PROJECT_TYPES:
        raise WorkflowError("SHARE_DOC_PROJECT_TYPE", "project_type is unsupported")
    clean = validate_content(content)
    if project_type == "open_source_project" and clean["repository_url"] is None:
        raise WorkflowError(
            "SHARE_DOC_CONTENT",
            "open_source_project entries require repository_url",
        )
    version_placeholder = "C-PENDING"
    lines = [
        f"<h3>{_xml_text(clean['title'])}</h3>",
        (
            f"<p><b>资料编号：</b>{_xml_text(material_id)}　"
            f"<b>内容版本：</b>{version_placeholder}</p>"
        ),
        f"<p><b>一句话结论：</b>{_xml_text(clean['one_line_takeaway'])}</p>",
        "<h4>项目内容</h4>",
        f"<p>{_xml_text(clean['overview'])}</p>",
        "<h4>简版工具链</h4>",
        "<ol>",
    ]
    lines.extend(
        f'<li seq="auto">{_xml_text(step)}</li>' for step in clean["toolchain"]
    )
    lines.append("</ol>")
    if clean["repository_url"] is not None:
        address = clean["repository_url"]
        lines.append(
            f"<p><b>仓库地址：</b><a href={quoteattr(address)}>{_xml_text(address)}</a></p>"
        )
    if clean["source_url"] is not None:
        address = clean["source_url"]
        lines.append(
            f"<p><b>原始资料：</b><a href={quoteattr(address)}>查看原始演示或资料</a></p>"
        )
    if clean["prompt"] is not None:
        prompt = clean["prompt"]
        prompt_label = PROMPT_LABELS[prompt["provenance"]]
        if prompt["title"] is not None:
            prompt_label += f"：{prompt['title']}"
        lines.extend(
            [
                f"<h4>{_xml_text(prompt_label)}</h4>",
                f"<pre lang=\"text\"><code>{_xml_text(prompt['text'])}</code></pre>",
            ]
        )
        if prompt["source_url"] is not None:
            lines.append(
                f'<p><a href={quoteattr(prompt["source_url"])}>查看提示词来源</a></p>'
            )
    if clean["resources"]:
        lines.append("<h4>补充资料</h4>")
        lines.append("<ul>")
        for item in clean["resources"]:
            label = _xml_text(item["label"])
            if item["url"] is not None:
                body = f"<a href={quoteattr(item['url'])}>{label}</a>"
            else:
                body = f"<b>{label}</b>"
            if item["note"] is not None:
                body += f"：{_xml_text(item['note'])}"
            lines.append(f"<li>{body}</li>")
        lines.append("</ul>")
    lines.append("<hr/>")
    versionless_xml = "\n".join(lines) + "\n"
    content_version = "C-" + sha256_bytes(versionless_xml.encode("utf-8"))[:12]
    xml = versionless_xml.replace(version_placeholder, content_version, 1)
    try:
        ElementTree.fromstring(f"<fragment>{xml}</fragment>")
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive invariant
        raise WorkflowError("SHARE_DOC_XML", "Generated Feishu XML is invalid") from exc
    return xml, content_version


def _state_paths(project_root: Path) -> tuple[Path, Path, Path]:
    lexical = Path(os.path.abspath(os.fspath(project_root)))
    try:
        root_stat = os.lstat(lexical)
    except OSError as exc:
        raise WorkflowError("SHARE_DOC_PROJECT", "Project directory is missing") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkflowError("SHARE_DOC_PROJECT", "Project root must be a plain directory")
    control = ensure_managed_dir(lexical, ".source-led-ai-video")
    ignore_path = control / ".gitignore"
    if not ignore_path.exists():
        managed_atomic_write_text(lexical, ignore_path, "*\n!.gitignore\n")
    state = ensure_managed_dir(lexical, control / "share-doc")
    entries = ensure_managed_dir(lexical, state / "entries")
    return lexical, state / "events.jsonl", entries


def _validate_log_event(event: Any, expected_sequence: int) -> None:
    if type(event) is not dict or event.get("sequence") != expected_sequence:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document log sequence is invalid")
    if event.get("schema") != 1 or event.get("event") not in {
        "share_doc_decision",
        "share_doc_synced",
    }:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document log contains an unsupported event")
    event_id = event.get("event_id")
    if type(event_id) is not str or SHA256_PATTERN.fullmatch(event_id) is None:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document event ID is invalid")
    identity = {
        key: value
        for key, value in event.items()
        if key not in {"sequence", "event_id", "recorded_at", "monotonic_ns"}
    }
    if sha256_bytes(canonical_json(identity)) != event_id:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document event was modified")


def _load_events(path: Path) -> list[dict[str, Any]]:
    try:
        events = read_events(path, repair_tail=True)
    except TelemetryError as exc:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document log cannot be read safely") from exc
    for sequence, event in enumerate(events, start=1):
        _validate_log_event(event, sequence)
    return events


def _artifact_events(
    events: list[dict[str, Any]], identity: dict[str, str | None]
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("video_sha256") == identity["video_sha256"]
    ]


def _entry_relative(identity: dict[str, str | None]) -> str:
    artifact_id = sha256_bytes(
        canonical_json({"video_sha256": identity["video_sha256"]})
    )[:24]
    return f".source-led-ai-video/share-doc/entries/{artifact_id}.xml"


def decide(
    project_root: Path,
    *,
    publication_id: str,
    build_key: str | None,
    video_sha256: str,
    decision: str,
    project_type: str,
    decided_by: str,
    decision_note: str | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = _identity(
        publication_id=publication_id,
        build_key=build_key,
        video_sha256=video_sha256,
    )
    if decision not in DECISIONS:
        raise WorkflowError("SHARE_DOC_DECISION", "decision must be include or skip")
    if project_type not in PROJECT_TYPES:
        raise WorkflowError("SHARE_DOC_PROJECT_TYPE", "project_type is unsupported")
    actor = _one_line(decided_by, "decided_by", limit=80)
    note = _multiline(decision_note, "decision_note", limit=500, optional=True)
    if decision == "include":
        if content is None:
            raise WorkflowError("SHARE_DOC_CONTENT", "include decisions require content")
        clean_content = validate_content(content)
        material_id = _material_id(identity)
        xml, content_version = render_entry_xml(
            project_type,
            clean_content,
            material_id=material_id,
        )
        entry_sha256: str | None = sha256_bytes(xml.encode("utf-8"))
        entry_path: str | None = _entry_relative(identity)
    else:
        if content is not None:
            raise WorkflowError("SHARE_DOC_CONTENT", "skip decisions cannot include content")
        clean_content = None
        material_id = None
        content_version = None
        xml = None
        entry_sha256 = None
        entry_path = None

    event_identity = {
        "schema": 1,
        "event": "share_doc_decision",
        **identity,
        "decision": decision,
        "project_type": project_type,
        "decided_by": actor,
        "decision_note": note,
        "content": clean_content,
        "material_id": material_id,
        "content_version": content_version,
        "entry_path": entry_path,
        "entry_sha256": entry_sha256,
    }
    project, events_path, _ = _state_paths(project_root)
    transaction = events_path.with_name("transaction")
    try:
        with identity_log_lock(transaction):
            events = _load_events(events_path)
            for existing in events:
                if (
                    existing.get("event") == "share_doc_decision"
                    and existing.get("publication_id") == publication_id
                    and existing.get("video_sha256") != video_sha256
                ):
                    raise WorkflowError(
                        "SHARE_DOC_DUPLICATE",
                        "publication_id is already bound to a different video",
                    )
            artifact_events = _artifact_events(events, identity)
            prior_decisions = [
                event for event in artifact_events if event.get("event") == "share_doc_decision"
            ]
            for prior in prior_decisions:
                if prior.get("publication_id") != publication_id:
                    raise WorkflowError(
                        "SHARE_DOC_DUPLICATE",
                        "This published artifact already has a share-document decision",
                    )
                prior_identity = {
                    key: value
                    for key, value in prior.items()
                    if key not in {"sequence", "event_id", "recorded_at", "monotonic_ns"}
                }
                if prior_identity == event_identity:
                    if decision == "include":
                        destination = managed_path(project, project / entry_path, kind="file")
                        if not destination.is_file() or sha256_file(destination) != entry_sha256:
                            managed_atomic_write_text(project, project / entry_path, xml)
                    return prior
                raise WorkflowError(
                    "SHARE_DOC_ALREADY_DECIDED",
                    "This publication already has a different share-document decision",
                )

            if decision == "include":
                managed_atomic_write_text(project, project / entry_path, xml)
            try:
                return append_identity_record(events_path, event_identity)
            except TelemetryError as exc:
                raise WorkflowError(
                    "SHARE_DOC_LOG", "Share-document decision cannot be recorded safely"
                ) from exc
    except TelemetryError as exc:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document transaction lock is unsafe") from exc


def mark_synced(
    project_root: Path,
    *,
    publication_id: str,
    build_key: str | None,
    video_sha256: str,
    document_id: str,
    block_id: str,
) -> dict[str, Any]:
    identity = _identity(
        publication_id=publication_id,
        build_key=build_key,
        video_sha256=video_sha256,
    )
    if type(document_id) is not str or REMOTE_ID_PATTERN.fullmatch(document_id) is None:
        raise WorkflowError("SHARE_DOC_REMOTE_ID", "document_id is invalid")
    if type(block_id) is not str or REMOTE_ID_PATTERN.fullmatch(block_id) is None:
        raise WorkflowError("SHARE_DOC_REMOTE_ID", "block_id is invalid")
    project, events_path, _ = _state_paths(project_root)
    transaction = events_path.with_name("transaction")
    try:
        with identity_log_lock(transaction):
            events = _load_events(events_path)
            artifact_events = _artifact_events(events, identity)
            decisions = [
                event
                for event in artifact_events
                if event.get("event") == "share_doc_decision"
                and event.get("publication_id") == publication_id
            ]
            if len(decisions) != 1:
                raise WorkflowError(
                    "SHARE_DOC_DECISION_MISSING",
                    "A unique share-document decision is required before sync",
                )
            decision_event = decisions[0]
            if decision_event.get("build_key") != build_key:
                raise WorkflowError(
                    "SHARE_DOC_IDENTITY",
                    "mark-synced must use the build_key recorded by the decision",
                )
            if decision_event.get("decision") != "include":
                raise WorkflowError("SHARE_DOC_SKIPPED", "Skipped content cannot be synced")
            entry_path = decision_event.get("entry_path")
            entry_sha256 = decision_event.get("entry_sha256")
            if type(entry_path) is not str or type(entry_sha256) is not str:
                raise WorkflowError("SHARE_DOC_ENTRY", "Included entry metadata is invalid")
            entry = managed_path(project, project / entry_path, must_exist=True, kind="file")
            if sha256_file(entry) != entry_sha256:
                raise WorkflowError("SHARE_DOC_ENTRY", "Share-document XML changed after decision")
            document_fingerprint = sha256_bytes(
                canonical_json({"document_id": document_id})
            )
            block_fingerprint = sha256_bytes(canonical_json({"block_id": block_id}))
            event_identity = {
                "schema": 1,
                "event": "share_doc_synced",
                **identity,
                "decision_event_id": decision_event["event_id"],
                "entry_sha256": entry_sha256,
                "document_fingerprint": document_fingerprint,
                "block_fingerprint": block_fingerprint,
            }
            prior_syncs = [
                event
                for event in artifact_events
                if event.get("event") == "share_doc_synced"
                and event.get("decision_event_id") == decision_event["event_id"]
            ]
            for prior in prior_syncs:
                prior_identity = {
                    key: value
                    for key, value in prior.items()
                    if key not in {"sequence", "event_id", "recorded_at", "monotonic_ns"}
                }
                if prior_identity == event_identity:
                    return prior
                raise WorkflowError(
                    "SHARE_DOC_ALREADY_SYNCED",
                    "This entry was already synced to a different document location",
                )
            try:
                return append_identity_record(events_path, event_identity)
            except TelemetryError as exc:
                raise WorkflowError(
                    "SHARE_DOC_LOG", "Share-document sync cannot be recorded safely"
                ) from exc
    except TelemetryError as exc:
        raise WorkflowError("SHARE_DOC_LOG", "Share-document transaction lock is unsafe") from exc


def status(project_root: Path) -> dict[str, Any]:
    _, events_path, _ = _state_paths(project_root)
    events = _load_events(events_path)
    sync_by_decision = {
        event["decision_event_id"]: event
        for event in events
        if event.get("event") == "share_doc_synced"
    }
    decisions = []
    for event in events:
        if event.get("event") != "share_doc_decision":
            continue
        synced = sync_by_decision.get(event["event_id"])
        decisions.append(
            {
                "publication_id": event["publication_id"],
                "build_key": event["build_key"],
                "video_sha256": event["video_sha256"],
                "decision": event["decision"],
                "project_type": event["project_type"],
                "category_heading": PROJECT_TYPES[event["project_type"]],
                "material_id": event.get("material_id"),
                "content_version": event.get("content_version"),
                "entry_path": event["entry_path"],
                "entry_sha256": event["entry_sha256"],
                "synced": synced is not None,
                "document_fingerprint": (
                    synced.get("document_fingerprint") if synced else None
                ),
                "block_fingerprint": (
                    synced.get("block_fingerprint") if synced else None
                ),
            }
        )
    return {"schema": 1, "decisions": decisions}


def _load_content(project_root: Path, raw_path: str) -> dict[str, Any]:
    path = project_path(project_root, raw_path, must_exist=True)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("SHARE_DOC_CONTENT", "Content JSON cannot be read") from exc
    return validate_content(value)


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: record.get(key)
        for key in (
            "event",
            "event_id",
            "sequence",
            "publication_id",
            "build_key",
            "video_sha256",
            "decision",
            "project_type",
            "material_id",
            "content_version",
            "entry_path",
            "entry_sha256",
            "document_fingerprint",
            "block_fingerprint",
            "recorded_at",
        )
        if key in record
    }
    project_type = record.get("project_type")
    if project_type in PROJECT_TYPES:
        summary["category_heading"] = PROJECT_TYPES[project_type]
    return summary


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument(
        "--build-key",
        help="Optional real production build key; omit for legacy published samples",
    )
    parser.add_argument("--video-sha256", required=True)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Record post-publication share-doc decisions; no Feishu API calls"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    decision_parser = sub.add_parser("decide")
    _add_identity_arguments(decision_parser)
    decision_parser.add_argument("--decision", choices=tuple(sorted(DECISIONS)), required=True)
    decision_parser.add_argument(
        "--project-type", choices=tuple(sorted(PROJECT_TYPES)), required=True
    )
    decision_parser.add_argument("--decided-by", required=True)
    decision_parser.add_argument("--decision-note")
    decision_parser.add_argument(
        "--content-json",
        help="Project-relative JSON; required for include and forbidden for skip",
    )
    synced_parser = sub.add_parser("mark-synced")
    _add_identity_arguments(synced_parser)
    synced_parser.add_argument("--document-id", required=True)
    synced_parser.add_argument("--block-id", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "decide":
        if args.decision == "include" and args.content_json is None:
            parser.error("--content-json is required for include")
        if args.decision == "skip" and args.content_json is not None:
            parser.error("--content-json is forbidden for skip")
        content = (
            _load_content(args.project, args.content_json)
            if args.content_json is not None
            else None
        )
        result = decide(
            args.project,
            publication_id=args.publication_id,
            build_key=args.build_key,
            video_sha256=args.video_sha256,
            decision=args.decision,
            project_type=args.project_type,
            decided_by=args.decided_by,
            decision_note=args.decision_note,
            content=content,
        )
        output = _summary(result)
    elif args.command == "mark-synced":
        result = mark_synced(
            args.project,
            publication_id=args.publication_id,
            build_key=args.build_key,
            video_sha256=args.video_sha256,
            document_id=args.document_id,
            block_id=args.block_id,
        )
        output = _summary(result)
    else:
        output = status(args.project)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"ERROR[{exc.code}]: {redact(exc)}", file=sys.stderr)
        raise SystemExit(2)

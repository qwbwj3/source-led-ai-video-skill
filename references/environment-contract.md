# Environment and portability contract

## Required agent capabilities

- Apple Silicon macOS.
- Codex with the built-in `imagegen` capability for two independent cover generations.
- Chrome-control capability when the source depends on the user's logged-in X session. Never substitute a headless browser.
- ChatCut plugin `0.2.18` or a compatible later release for source-led visual assembly and clean-base export.
- Network access on first bootstrap for the pinned forced-aligner model and for live Volcengine TTS.

HyperFrames is optional. Use it for a readable toolchain card when available; a locally rendered HTML information card is acceptable when it is not.

## What the Skill installs or carries

- Apple Silicon FFmpeg, FFprobe, and Astral uv with pinned hashes and license/provenance files.
- Noto Sans CJK SC regular font with its OFL license.
- Locked forced-aligner package versions and model revision.
- A bundled uv launcher that can install the locked CPython 3.11.15 runtime when the Agent's own Python is not an exact match; the aligner environment path is versioned by Python plus the requirements hash, so an older cached environment is ignored. No separate Homebrew, Python, or uv installation is required.
- The pinned `yuwen-publish-precheck` review references and lexical scanner.
- Cover rendering, TTS adapter, forced alignment, retiming, subtitle, audio mix, render, integrity manifests, and QA scripts.

## External state that cannot be bundled

- X/Chrome login and any 2FA or source access permission.
- ChatCut plugin installation and its account state.
- Built-in imagegen availability.
- Volcengine credentials and provider availability.
- Source-media and music rights.

After the listed Codex capabilities are present, Volcengine is the only credential file this Skill asks the user to configure. Do not describe the complete workflow as self-contained when Chrome, ChatCut, or imagegen is absent.

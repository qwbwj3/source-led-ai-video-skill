# source-led-ai-video

将 X 帖子或 GitHub 项目整理为抖音可用的中文 AI 短视频项目。工作流以原始演示素材为主，完成事实核实、口播审核、ChatCut 可编辑剪辑、火山引擎 TTS、字幕对齐、双尺寸封面、视听 QA 和交付验证。

## 安装

把整个仓库放到 Codex 的 Skills 目录：

```text
~/.codex/skills/source-led-ai-video
```

首次使用前阅读 `SKILL.md`，并按 `assets/volc-tts.env.example` 在项目外配置真实火山引擎 TTS 凭证。不要把真实 `.env`、Token 或密钥提交到仓库。

## 验证

```bash
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
python3 -m compileall -q scripts
```

运行环境、项目结构、制作标准和 QA 门禁分别记录在 `references/` 中。仓库内置的 FFmpeg、uv、字体及其许可证和来源说明位于 `assets/runtime/` 与 `assets/fonts/`。


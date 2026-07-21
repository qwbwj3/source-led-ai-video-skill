# source-led-ai-video

将一个已选定的 X 帖子、Builder 演示或 GitHub/开源项目，整理成抖音可用的中文 AI 短视频项目。V1.2 以原始演示素材为主，加入可缓存的 Source Package、ChatCut 语义剪辑计划、火山引擎 TTS、强制对齐字幕、两次独立 ImageGen 封面、实际证据帧审片、可撤销权利账本、断点状态和数据复盘。

## 安装

把整个仓库放到 Codex 的 Skills 目录：

```text
~/.codex/skills/source-led-ai-video
```

首次使用前阅读 `SKILL.md`。按 `assets/volc-tts.env.example` 在项目外配置火山引擎 TTS；其余外部能力来自 Codex 的 Chrome、ChatCut 和 ImageGen。不要把真实 `.env`、Token 或密钥提交到仓库。

新项目默认使用 V2：

```bash
python3 scripts/init_project.py --project "/absolute/project" --name "项目名"
python3 scripts/workflow.py status --config "/absolute/project/project.json"
```

初始化会同时生成可直接填写的 `source-package-draft.json` 和待同步的封面调用记录。

`review-runs/` 仅供私下审核。只有通过文案审核、完整视听 QA、素材权利确认并由 `finalize` 原子发布的 `deliverables/<release-key>/` 才能上传；`latest.json` 会同时记录对应的 build 与 release，授权账本变化不会覆盖旧交付物。

## 验证

```bash
env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p 'test_*.py'
PYTHONPYCACHEPREFIX=/private/tmp/source-led-pyc python3 -m compileall -q scripts
```

完整生产顺序、项目结构、证据包、剪辑计划、封面、QA、权利、运行恢复和复盘字段分别记录在 `references/`。仓库内置的 FFmpeg、uv、字体及其许可证和来源说明位于 `assets/runtime/` 与 `assets/fonts/`。

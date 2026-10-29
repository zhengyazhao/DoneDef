# 定标 DoneDef

先把「做完是什么样」（完成标准）定下来，再让本机已登录的 claude / codex / grok CLI 按 **需求 → 开发 → 验证** 三个关卡干活。每个关卡结束都会停下来等人审核，人只做确认和验收。

截图待补。

## 工作流程

1. **需求**：AI 只读代码，输出完成目标、完成标准和方案；有疑问的地方单独列出并给出默认做法。你确认，或者写一句话让 AI 修改。
2. **开发**：在独立的 git worktree + feature 分支里改代码，改完由程序自己跑构建 / 测试。
3. **验证**：AI 逐条对照完成标准打 ✓ / ✗，你确认通过。
4. 可选：把交付记录追加到 Azure DevOps Task 的描述里。

## 要求

- macOS（停止任务用的是进程组，Windows 未适配）
- Python 3.13+、PySide6 6.12+
- claude / codex / grok 至少装一个，并且已经在终端里登录好

## 安装与运行

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m donedef
```

数据（SQLite、worktree、日志）放在仓库下的 `data/`。

## 它怎么调用 CLI

不经过终端，而是起后台无头子进程：一张卡一个进程，cwd 是这张卡的 worktree，输出按 JSON 流解析。下面只列主要参数，权限档、模型等可选参数省略。

- claude：`claude -p <提示词> --permission-mode <档位> --output-format stream-json`，续聊加 `--resume <会话ID>`
- codex：`codex exec --json …`，续聊用 `codex exec resume <会话ID>`
- grok：`grok -p <提示词> --output-format streaming-messages-json`，续聊用 `-r <会话ID>`

命令从 PATH 里查找（`shutil.which`）。从 Dock / Finder 启动时 PATH 可能不含这些命令，建议从终端启动。

## 安全边界

- 程序不读取任何 AI 凭证，使用各家 CLI 自己的登录状态；子进程环境变量走白名单，并清掉各家 API key。
- 需求阶段强制只读；改代码只发生在 worktree 里。
- 不自动 merge / push / 建 PR。
- Azure PAT 只从 `~/.azure-devops/personal-pat.txt` 读取，且只在你明确允许的组织上运行 AI。
- 「放开」权限档下 AI 能执行任意命令，请自行判断风险。

## 开发

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
.venv/bin/python -m pyflakes donedef tests
```

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。

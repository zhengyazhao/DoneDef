"""三家 CLI：命令拼装、流式输出解析、需求 JSON 解析、子进程环境。
「测试连接」相关的 build_test_cmd / parse_reply / ProcessJob 从 prototype_qt/cli.py 原样复制。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

TEST_PROMPT = "只回复两个字母 OK，不要做任何其他事"
TEST_TIMEOUT = 90  # 秒
# 子进程环境白名单（代理变量大小写都放行）
ENV_KEEP = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
LOCAL_NO_PROXY = ("127.0.0.1", "localhost")

# 权限档 → 各家参数
CLAUDE_PERM = {"read": "dontAsk", "edit": "acceptEdits", "full": "bypassPermissions"}
CODEX_SANDBOX = {"read": "read-only", "edit": "workspace-write", "full": "danger-full-access"}
GROK_PERM = {"read": "dontAsk", "edit": "acceptEdits", "full": "bypassPermissions"}
GROK_READ_TOOLS = "read_file,list_dir,grep"  # grok 的 dontAsk 会写文件，只读靠只放行读类工具

# 需求阶段：程序在模板提示词后固定追加
REQ_JSON_SUFFIX = (
    "\n\n---\n最后，把你的结论按下面的 JSON 格式放在一个 ```json 代码块里输出（字段名不要改，plan 的元素是中文字符串）：\n"
    "```json\n"
    '{"goal": "这张卡要干嘛（1 句）", "done": ["可验证的完成标准 1", "…"], '
    '"plan": ["步骤 1", "步骤 2（待你确认：…，默认 …）"]}\n'
    "```\n"
    "done 是这张卡做完的完成标准，每条一句话、必须能验证（别人照着能判断做没做到），"
    "例如「点导出能下载 CSV，表头是 日期/订单号/金额」。\n"
    "需求里没说清或可能遗漏的地方不要单独列，写进对应步骤，用「（待你确认：…，默认 …）」标出，并给出你的默认做法。")
REQ_ANSWER_SUFFIX = ("\n\n按同样的 JSON 格式（goal / done / plan 三个字段，放在一个 ```json 代码块里）重新输出完整方案。")
REQ_FIELDS = ("goal", "plan")  # done（完成标准）可选，缺省 []
REQ_OLD_FIELDS = ("understanding", "todo", "not_todo", "questions")

QUOTA_RE = re.compile(r"usage limit|rate limit", re.I)


def _safe_prompt(prompt: str) -> str:
    """提示词以 - 开头会被 CLI 当成参数，前面垫一个换行"""
    return "\n" + prompt if prompt.startswith("-") else prompt


# 模板能力 → MCP 服务；名字加 ab_ 前缀，不和你自己全局配的同名服务冲突
MCP_PREFIX = "ab_"
NO_INDEX = "代码检索没挂上：仓库 {path} 还没建 codegraph 索引（在仓库根运行 codegraph init 后生效）"
NO_REPO_INDEX = "代码检索没挂上：这张卡没关联代码仓库"


def mcp_servers(caps: list[str], repo_path: str | None) -> tuple[dict[str, dict], list[str]]:
    """按能力拼 MCP 服务：{名字: {command, args}}，以及没挂上的原因（显示在输出里，不拦运行）。
    代码检索指向原仓库的 .codegraph 索引（worktree 里没有索引）；没有索引只提示，不自动建"""
    servers, notes = {}, []
    if "codegraph" in caps:
        if not repo_path:
            notes.append(NO_REPO_INDEX)
        elif not (Path(repo_path) / ".codegraph").is_dir():
            notes.append(NO_INDEX.format(path=repo_path))
        else:
            servers[MCP_PREFIX + "codegraph"] = {
                "command": "codegraph", "args": ["serve", "--mcp", "--path", repo_path, "--no-watch"]}
    if "playwright" in caps:
        servers[MCP_PREFIX + "playwright"] = {
            "command": "npx", "args": ["@playwright/mcp@latest", "--headless", "--isolated"]}
    return servers, notes


def _toml(v) -> str:
    """codex -c 的值按 TOML 解析：字符串 / 字符串数组用 JSON 写法正好合法"""
    return json.dumps(v, ensure_ascii=False)


def build_cmd(cli: str, perm: str, model: str, effort: str, prompt: str,
              session_id: str = "", out_file: str = "", mcp: dict[str, dict] | None = None) -> list[str]:
    """拼装一轮的命令。session_id 非空 = 续聊；model / effort 为空就不传。codex 需要 out_file（-o）。
    mcp = mcp_servers() 的服务表；grok 没有单次运行挂 MCP 的参数，忽略"""
    if perm not in CLAUDE_PERM:
        raise ValueError(f"未知权限档：{perm}")
    prompt = _safe_prompt(prompt)
    if cli == "claude":
        cmd = ["claude", "-p", prompt, "--permission-mode", CLAUDE_PERM[perm],
               "--output-format", "stream-json", "--verbose"]
        if session_id:
            cmd += ["--resume", session_id]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if mcp:
            # 两个参数都吃多个值，放最后，避免把别的参数吞进去；只读档（dontAsk）下 MCP 工具要显式放行
            cmd += ["--mcp-config", json.dumps({"mcpServers": mcp}, ensure_ascii=False),
                    "--allowedTools", *[f"mcp__{name}" for name in mcp]]
        return cmd
    if cli == "codex":
        if not out_file:
            raise ValueError("codex 需要 -o 输出文件")
        if session_id:
            # 续聊不接受 -s，沙箱用 -c 显式带；-m / -c 都要重新带
            cmd = ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
                   "-c", f'sandbox_mode="{CODEX_SANDBOX[perm]}"']
        else:
            cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", CODEX_SANDBOX[perm]]
        cmd += ["-c", 'approval_policy="never"']
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        for name, srv in (mcp or {}).items():
            cmd += ["-c", f"mcp_servers.{name}.command={_toml(srv['command'])}",
                    "-c", f"mcp_servers.{name}.args={_toml(srv['args'])}"]
        cmd += ["-o", out_file]
        cmd += [session_id, prompt] if session_id else [prompt]
        return cmd
    if cli == "grok":
        cmd = ["grok", "-p", prompt]
        if session_id:
            cmd = ["grok", "-r", session_id, "-p", prompt]
        cmd += ["--permission-mode", GROK_PERM[perm]]
        if perm == "read":
            cmd += ["--tools", GROK_READ_TOOLS]
        cmd += ["--output-format", "streaming-messages-json"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--reasoning-effort", effort]
        return cmd
    raise ValueError(f"未知 CLI：{cli}")


# ---------- 流式输出解析 ----------
# 工具名 → 动作动词（界面上 humanize 再翻成「正在阅读 / 修改 / 运行…」）
TOOL_VERB = {
    # claude
    "Read": "Read", "Edit": "Edit", "MultiEdit": "Edit", "Write": "Write", "NotebookEdit": "Edit",
    "Bash": "Bash", "Grep": "Grep", "Glob": "Glob",
    # grok
    "read_file": "Read", "write": "Write", "search_replace": "Edit", "run_terminal_command": "Bash",
    "grep": "Grep", "list_dir": "Glob",
}
_SHELL_WRAP = re.compile(r"^/bin/(?:ba|z)?sh -lc (.*)$", re.S)


def _one_line(s: str, n: int = 120) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s if len(s) <= n else s[:n] + "…"


def _unwrap_shell(cmd: str) -> str:
    """codex 的命令形如 /bin/zsh -lc 'echo hi'，去掉外壳只留真正的命令"""
    m = _SHELL_WRAP.match(cmd.strip())
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner


def _tool_action(name: str, inp: dict) -> str:
    verb = TOOL_VERB.get(name, name)
    obj = ""
    for key in ("file_path", "target_file", "path", "command", "pattern", "query", "url", "description"):
        v = inp.get(key) if isinstance(inp, dict) else None
        if isinstance(v, str) and v.strip():
            obj = v
            break
    return f"{verb} {_one_line(obj)}".strip()


@dataclass
class Outcome:
    kind: str  # ok / failed / quota
    reply: str = ""
    error: str = ""  # 失败原因（人话）


class StreamParser:
    """逐行喂 stdout：feed() 返回这一行产生的动作（可能为空）；结束后 finish() 判定结果"""

    def __init__(self, cli: str):
        if cli not in ("claude", "codex", "grok"):
            raise ValueError(f"未知 CLI：{cli}")
        self.cli = cli
        self.session_id = ""
        self.result: str | None = None  # claude / grok 的 result 事件
        self.is_error = False
        self.last_text = ""  # 最近一条 AI 文字（codex 的 agent_message）
        self.errors: list[str] = []  # 事件里的报错文字
        self.rejected = False  # claude rate_limit_event 的 status = rejected
        self.rate_info: dict | None = None  # 最近一条 rate_limit_event 的 rate_limit_info（额度栏用）
        self.tail: list[str] = []  # 最后若干行原文，判额度用

    def feed(self, line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        self.tail = (self.tail + [line])[-40:]
        try:
            ev = json.loads(line)
        except ValueError:
            return []  # 非 JSON 行（极少见）只落盘，不解析
        if not isinstance(ev, dict):
            return []
        return self._codex(ev) if self.cli == "codex" else self._claude_like(ev)

    def _claude_like(self, ev: dict) -> list[str]:
        """claude stream-json 与 grok streaming-messages-json 结构相同"""
        t = ev.get("type")
        if not self.session_id and isinstance(ev.get("session_id"), str) and ev.get("subtype") != "hook_started":
            self.session_id = ev["session_id"]
        if t == "system" and ev.get("subtype") == "init" and isinstance(ev.get("session_id"), str):
            self.session_id = ev["session_id"]
        if t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            self.rate_info = info
            if info.get("status") == "rejected":
                self.rejected = True
            return []
        if t == "result":
            self.is_error = bool(ev.get("is_error"))
            if isinstance(ev.get("result"), str):
                self.result = ev["result"]
            if isinstance(ev.get("session_id"), str):
                self.session_id = ev["session_id"]
            if self.is_error:
                self.errors.append(str(ev.get("result") or ev.get("subtype") or "is_error"))
            return []
        if t != "assistant":
            return []
        acts = []
        for c in (ev.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                acts.append(_tool_action(str(c.get("name")), c.get("input") or {}))
            elif c.get("type") == "text" and (c.get("text") or "").strip():
                self.last_text = c["text"]
                acts.append("说：" + _one_line(c["text"], 80))
        return acts

    def _codex(self, ev: dict) -> list[str]:
        t = ev.get("type")
        if t == "thread.started" and isinstance(ev.get("thread_id"), str):
            self.session_id = ev["thread_id"]
            return []
        if t == "error":
            self.errors.append(str(ev.get("message", "")))
            return []
        if t == "turn.failed":
            self.errors.append(str((ev.get("error") or {}).get("message", "turn.failed")))
            return []
        item = ev.get("item") or {}
        it = item.get("type")
        if t == "item.started" and it == "command_execution":
            return ["Bash " + _one_line(_unwrap_shell(str(item.get("command", ""))))]
        if t != "item.completed":
            return []
        if it == "file_change":
            verb = {"add": "Write", "update": "Edit", "delete": "删除"}
            return [f"{verb.get(ch.get('kind'), 'Edit')} {ch.get('path', '')}" for ch in item.get("changes") or []]
        if it == "agent_message" and (item.get("text") or "").strip():
            self.last_text = item["text"]
            return ["说：" + _one_line(item["text"], 80)]
        if it == "error":
            self.errors.append(str(item.get("message", "")))
        return []

    def reply(self, out_file: str = "") -> str:
        if self.cli == "codex":
            p = Path(out_file) if out_file else None
            if p and p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return text
            return self.last_text.strip()
        return (self.result or "").strip()

    def finish(self, code: int, stderr: str, out_file: str = "") -> Outcome:
        """结束判定：退出码 0 且取到回复 → ok；否则失败；失败时输出含 usage limit / rate limit → 额度耗尽"""
        reply = self.reply(out_file)
        if code == 0 and not self.is_error and reply:
            return Outcome("ok", reply)
        hay = "\n".join([stderr, *self.errors, reply, *self.tail])
        if self.rejected or QUOTA_RE.search(hay):
            return Outcome("quota", reply, "额度耗尽：" + _one_line(next(
                (x for x in [*self.errors, stderr, reply] if QUOTA_RE.search(x or "")), "rate_limit_event rejected"), 160))
        if code != 0:
            why = _one_line(self.errors[-1] if self.errors else tail(stderr, 160), 160)
            return Outcome("failed", reply, f"进程退出码 {code}" + (f"：{why}" if why else ""))
        if self.is_error:
            return Outcome("failed", reply, "CLI 报错：" + _one_line(self.errors[-1] if self.errors else "", 160))
        return Outcome("failed", reply, "取不到最终回复")


# ---------- 需求 JSON ----------
_JSON_BLOCK = re.compile(r"```json[ \t]*\n(.*?)```", re.S)


def _str_list(data: dict, k: str) -> list[str]:
    v = data[k]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{k} 不是字符串数组")
    return [x.strip() for x in v if x.strip()]


def _old_to_new(p: dict) -> dict:
    """旧格式（理解 / 要做 / 不做 / 疑问）转新格式：疑问变成「待你确认」步骤，不做丢弃"""
    return {"goal": p["understanding"], "done": [],
            "plan": list(p["todo"]) + [f"（待你确认：{q}）" for q in p["questions"]]}


def req_view(parsed: dict | None) -> dict | None:
    """库里旧轮次的 parsed 可能是旧格式 / 没有 done，读取时统一成 {goal, done, plan}（总带 done 键）"""
    if parsed is None:
        return None
    if "goal" in parsed:
        return parsed if "done" in parsed else {**parsed, "done": []}
    return _old_to_new(parsed)


def parse_req_json(reply: str) -> dict:
    """取回复里最后一个 ```json 代码块，没有就整段当 JSON；新旧格式都接受，统一返回 {goal, done, plan}；都不符抛 ValueError"""
    blocks = _JSON_BLOCK.findall(reply or "")
    text = blocks[-1] if blocks else (reply or "")
    try:
        data = json.loads(text.strip())
    except ValueError as e:
        raise ValueError(f"不是合法 JSON（{e}）") from e
    if not isinstance(data, dict):
        raise ValueError("JSON 不是对象")
    if all(k in data for k in REQ_FIELDS):
        if not isinstance(data["goal"], str):
            raise ValueError("goal 不是字符串")
        done = _str_list(data, "done") if "done" in data else []
        return {"goal": data["goal"].strip(), "done": done, "plan": _str_list(data, "plan")}
    if all(k in data for k in REQ_OLD_FIELDS):
        if not isinstance(data["understanding"], str):
            raise ValueError("understanding 不是字符串")
        old = {"understanding": data["understanding"].strip()}
        for k in REQ_OLD_FIELDS[1:]:
            old[k] = _str_list(data, k)
        return _old_to_new(old)
    raise ValueError("缺少字段 " + "、".join(k for k in REQ_FIELDS if k not in data))


# ---------- 「测试连接」（原样复制自 prototype_qt/cli.py） ----------
def build_test_cmd(cli: str, model: str, effort: str, prompt: str, out_file: str) -> list[str]:
    """无头单轮、固定只读档。model / effort 为空就不传对应参数"""
    if cli == "claude":
        cmd = ["claude", "-p", prompt, "--permission-mode", "dontAsk", "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
    elif cli == "codex":
        cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-c", 'approval_policy="never"']
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += ["-o", str(out_file), prompt]
    elif cli == "grok":
        cmd = ["grok", "-p", prompt, "--permission-mode", "dontAsk", "--tools", GROK_READ_TOOLS,
               "--output-format", "json"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--reasoning-effort", effort]
    else:
        raise ValueError(f"未知 CLI：{cli}")
    return cmd


def parse_reply(cli: str, stdout: str, out_file: str) -> str:
    """取出 CLI 的回复文本；取不到就抛 ValueError，由调用方显示失败"""
    if cli == "codex":
        p = Path(out_file)
        if not p.is_file():
            raise ValueError("codex 没有写出回复文件")
        return p.read_text(encoding="utf-8")
    if cli not in ("claude", "grok"):
        raise ValueError(f"未知 CLI：{cli}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"stdout 不是 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("stdout 的 JSON 不是对象")
    for key in ("text", "result"):
        if isinstance(data.get(key), str):
            return data[key]
    raise ValueError("JSON 里没有 text / result 字段")


def child_env(os_environ) -> dict[str, str]:
    """白名单过滤：CLAUDECODE、CLAUDE_CODE_*、各家 API_KEY 都不在白名单内；no_proxy 至少含本机地址"""
    env = {k: v for k, v in os_environ.items() if k in ENV_KEEP}
    parts = [x for x in (env.get("no_proxy") or env.get("NO_PROXY") or "").split(",") if x.strip()]
    parts += [x for x in LOCAL_NO_PROXY if x not in parts]
    env["no_proxy"] = env["NO_PROXY"] = ",".join(parts)
    return env


def tail(text: str, n: int = 300) -> str:
    return (text or "").strip()[-n:]


class ProcessJob(QObject):
    """异步执行一条命令：done(退出码, stdout, stderr, 是否超时)。超时杀进程；stdin 接 /dev/null"""
    done = Signal(int, str, str, bool)

    def __init__(self, cmd: list[str], env: dict[str, str], cwd: str, timeout: int = TEST_TIMEOUT, parent=None):
        super().__init__(parent)
        self._timed_out = False
        self._finished = False
        self._cmd = cmd
        self.proc = QProcess(self)
        pe = QProcessEnvironment()
        for k, v in env.items():
            pe.insert(k, v)
        self.proc.setProcessEnvironment(pe)
        self.proc.setWorkingDirectory(cwd)
        self.proc.setStandardInputFile(QProcess.nullDevice())
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(timeout * 1000)
        self.timer.timeout.connect(self._on_timeout)

    def start(self):
        self.timer.start()
        self.proc.start(self._cmd[0], self._cmd[1:])

    def _emit(self, code: int, err_extra: str = ""):
        if self._finished:
            return
        self._finished = True
        self.timer.stop()
        out = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace") or err_extra
        self.done.emit(code, out, err, self._timed_out)

    def _on_finished(self, code, _status):
        self._emit(code)

    def _on_error(self, error):
        if error == QProcess.FailedToStart:  # 其他错误（崩溃等）会跟着 finished 信号
            self._emit(-1, self.proc.errorString())

    def _on_timeout(self):
        self._timed_out = True
        self.proc.kill()

    def kill(self):
        """关弹窗 / 换 CLI：杀进程，丢弃结果（不再发 done）"""
        self._finished = True
        self.timer.stop()
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)


def run_process(cmd, env, cwd, timeout=TEST_TIMEOUT):
    """默认执行器；测试里换成假的。返回 job，调用方连 job.done 后再 job.start()"""
    return ProcessJob(cmd, env, cwd, timeout)

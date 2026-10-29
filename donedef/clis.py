"""本机 CLI 检测、模型列表、额度。全程不调模型。
codex 的模型列表和额度走 `codex app-server`（JSON-RPC over stdio，登录态由 codex 自己带，程序不读凭证文件）；
grok 用 `grok models`；claude 没有列表命令，从 `claude --help` 里取 --model 的别名和 --effort 的可选值。
命令在后台线程跑（BgJob），结果经 Qt 排队信号回主线程。"""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
from pathlib import Path

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal

CLIS = ("claude", "codex", "grok")
CMD_TIMEOUT = 30  # 秒
GROK_EFFORTS = ["low", "medium", "high"]  # grok --help 没列可选值，用常见的三档


# ---------- 公共 ----------
def _run(args: list[str], env: dict[str, str], timeout: int = CMD_TIMEOUT) -> str:
    """跑一条命令返回 stdout；启动失败、超时、退出码非 0 都抛 RuntimeError（带原因）"""
    try:
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{' '.join(args[1:]) or args[0]} 超时（{timeout} 秒）") from e
    except OSError as e:
        raise RuntimeError(f"启动失败：{e}") from e
    if r.returncode != 0:
        raise RuntimeError(f"退出码 {r.returncode}：{(r.stderr or r.stdout).strip()[-200:]}")
    return r.stdout


def load_json(path: Path) -> dict:
    """读缓存文件；不存在返回空；坏文件抛 ValueError，由调用方提示"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"{path.name} 不是合法 JSON：{e}") from e
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------- CLI 检测 ----------
_VER = re.compile(r"\d+\.\d+(?:\.\d+)?")


def parse_version(text: str) -> str:
    """「codex-cli 0.152.1」「2.1.311 (Claude Code)」「grok 1.0.70 (…)」→ 版本号；认不出就原样截一段"""
    m = _VER.search(text or "")
    return m.group(0) if m else (text or "").strip()[:40]


def detect(env: dict[str, str]) -> dict[str, dict]:
    """{cli: {"path": 可执行文件或 "", "version": 版本号, "error": 原因}}；path 为空 = 没装"""
    out = {}
    for cli in CLIS:
        path = shutil.which(cli, path=env.get("PATH")) or ""
        info = {"path": path, "version": "", "error": ""}
        if path:
            try:
                info["version"] = parse_version(_run([path, "--version"], env))
            except RuntimeError as e:
                info["error"] = f"--version 失败：{e}"
        out[cli] = info
    return out


# ---------- codex app-server ----------
def app_server(path: str, requests: list[dict], env: dict[str, str], timeout: int = CMD_TIMEOUT) -> dict[int, dict]:
    """启动 `codex app-server`，initialize 后发 requests，收齐各 id 的 result 返回 {id: result}。
    返回 error、提前退出、超时都抛 RuntimeError。用 os.read 自己切行，避免缓冲区里有行而 select 不再触发"""
    p = subprocess.Popen([path, "app-server"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        msgs = [{"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "donedef", "version": "0"}}},
                {"method": "initialized"}, *requests]
        p.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))
        p.stdin.flush()
        want = {r["id"] for r in requests}
        got: dict[int, dict] = {}
        buf = b""
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while want - got.keys():
            if b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # 非 JSON 行忽略
                if isinstance(d, dict) and d.get("id") in want:
                    if "error" in d:
                        raise RuntimeError(f"app-server 报错：{(d['error'] or {}).get('message', d['error'])}")
                    got[d["id"]] = d.get("result") or {}
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError(f"codex app-server 超时（{timeout} 秒）")
            if not sel.select(left):
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"codex app-server 提前退出（退出码 {p.poll()}）")
            buf += chunk
        return got
    finally:
        p.kill()
        p.wait()


# ---------- 模型列表 ----------
def parse_codex_models(result: dict) -> dict:
    """model/list 的 result → {"models", "default", "efforts": {模型: [推理强度]}, "note"}；去掉 hidden"""
    data = [m for m in (result.get("data") or []) if isinstance(m, dict) and not m.get("hidden")]
    if not data:
        raise ValueError("model/list 没有返回可用模型")
    models = [str(m.get("id") or m.get("model")) for m in data]
    default = next((str(m.get("id")) for m in data if m.get("isDefault")), "")
    efforts = {str(m.get("id")): [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []
                                  if isinstance(e, dict) and e.get("reasoningEffort")] for m in data}
    note = "codex app-server model/list"
    if result.get("nextCursor"):
        note += "（只取了第一页）"
    return {"models": models, "default": default, "efforts": efforts, "note": note}


_GROK_LINE = re.compile(r"^\s*[*-]\s+(\S+)(\s+\(default\))?\s*$")


def parse_grok_models(text: str) -> dict:
    """`grok models` 的「* grok-4.7 (default)」「- grok-4.6」行"""
    models, default = [], ""
    for line in (text or "").splitlines():
        m = _GROK_LINE.match(line)
        if m:
            models.append(m.group(1))
            if m.group(2):
                default = m.group(1)
    if not models:
        raise ValueError("grok models 的输出里没找到模型")
    return {"models": models, "default": default, "efforts": {m: GROK_EFFORTS for m in models},
            "note": "grok models"}


def _option_block(help_text: str, opt: str) -> str:
    """取 --help 里某个选项的说明（到下一个以 - 开头的选项行为止）"""
    i = help_text.find(opt)
    if i < 0:
        return ""
    rest = help_text[i + len(opt):]
    m = re.search(r"\n\s{0,6}-", rest)
    return rest[: m.start()] if m else rest


def parse_claude_help(help_text: str) -> dict:
    """claude 没有列模型的命令：--model 说明里举例的别名 + --effort 括号里的可选值"""
    aliases = re.findall(r"'([A-Za-z0-9._-]+)'", _option_block(help_text, "--model <model>"))
    if not aliases:
        raise ValueError("claude --help 的 --model 说明里没找到别名")
    m = re.search(r"\(([a-z, ]+)\)", _option_block(help_text, "--effort <level>"))
    efforts = [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []
    return {"models": aliases, "default": "", "efforts": {a: efforts for a in aliases}, "any_efforts": efforts,
            "note": "claude --help 里的别名（没有列表命令；可手填完整模型名）"}


def list_models(cli: str, path: str, env: dict[str, str]) -> dict:
    if cli == "codex":
        return parse_codex_models(app_server(path, [{"id": 1, "method": "model/list", "params": {}}], env)[1])
    if cli == "grok":
        return parse_grok_models(_run([path, "models"], env))
    if cli == "claude":
        return parse_claude_help(_run([path, "--help"], env))
    raise ValueError(f"未知 CLI：{cli}")


def probe_all(env: dict[str, str]) -> dict:
    """Agent 模板窗口用：检测三家 + 已装的各查一次模型。单家失败只记原因，不影响别家"""
    clis = detect(env)
    models = {}
    for cli, info in clis.items():
        if not info["path"]:
            continue
        try:
            models[cli] = list_models(cli, info["path"], env)
        except (RuntimeError, ValueError) as e:
            models[cli] = {"error": str(e)}
    return {"clis": clis, "models": models, "at": time.time()}


# ---------- 额度 ----------
def window_name(mins) -> str:
    try:
        mins = int(mins)
    except (TypeError, ValueError):
        return "?"
    if mins % 1440 == 0:
        return f"{mins // 1440} 天"
    return f"{mins // 60} 小时" if mins % 60 == 0 else f"{mins} 分钟"


def parse_codex_quota(result: dict) -> list[dict]:
    """account/rateLimits/read → [{"name", "pct", "resets_at"}]；usedPercent 已是百分数"""
    rl = result.get("rateLimits") or {}
    out = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("usedPercent") is not None:
            out.append({"name": window_name(w.get("windowDurationMins")), "pct": float(w["usedPercent"]),
                        "resets_at": w.get("resetsAt")})
    if not out:
        raise ValueError("rateLimits 里没有 primary / secondary")
    return out


CLAUDE_WINDOWS = (("five_hour", "5 小时"), ("seven_day", "7 天"))


def parse_claude_quota(info: dict) -> list[dict]:
    """rate_limit_event.rate_limit_info → 窗口列表；utilization 是 0~1 小数"""
    uw = (info or {}).get("unifiedWindows") or {}
    out = []
    for key, name in CLAUDE_WINDOWS:
        w = uw.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            out.append({"name": name, "pct": float(w["utilization"]) * 100, "resets_at": w.get("resetsAt")})
    return out


def codex_quota(env: dict[str, str]) -> list[dict]:
    path = shutil.which("codex", path=env.get("PATH"))
    if not path:
        raise RuntimeError("本机没装 codex")
    return parse_codex_quota(app_server(path, [{"id": 1, "method": "account/rateLimits/read"}], env)[1])


# ---------- 后台执行 ----------
class BgJob(QObject):
    """在后台线程跑 fn()，done(结果) 在主线程发；fn 抛异常时结果为 {"error": 原因}"""
    done = Signal(object)
    _bg = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._bg.connect(self.done.emit, Qt.QueuedConnection)

    def start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            res = self._fn()
        except Exception as e:  # noqa: BLE001 —— 后台线程里的任何异常都要变成界面上的原因，不能吞掉
            res = {"error": f"{type(e).__name__}：{e}"}
        if shiboken6.isValid(self):  # 窗口已关、对象已销毁就不发：结果没人要了
            self._bg.emit(res)

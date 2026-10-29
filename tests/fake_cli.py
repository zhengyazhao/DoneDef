"""假 claude / codex / grok：回放 tests/samples 里的真实流式样本（取自 probes/out，已脱敏），不调真 CLI。
用法：bin 目录里的包装脚本执行 `python fake_cli.py <cli 名> <原参数…>`。
同目录 plan.json = {cli: [每次调用的剧本, …]}，按调用顺序逐个取用；每次调用的参数追加到 calls.jsonl。
剧本字段：reply 最终回复、session 会话 ID、write {相对路径: 内容}、sleep 秒、exit 退出码、
stderr、is_error、grandchild（起一个 sleep 孙进程并把两个 pid 写到 pids.txt）、ignore_term（忽略 SIGTERM）、stubborn_child（不占管道且忽略 SIGTERM 的孙进程）、
no_output（不输出流式事件）。"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "samples"
SAMPLE = {"claude": "edit-claude.jsonl", "codex": "edit-codex.jsonl", "grok": "edit-grok.jsonl"}


def take_step(bin_dir: Path, cli: str) -> dict:
    plan_file = bin_dir / "plan.json"
    plan = json.loads(plan_file.read_text())
    queue = plan.get(cli) or []
    if not queue:
        print(f"fake {cli}: plan.json 里没有剧本了", file=sys.stderr)
        sys.exit(97)
    step = queue.pop(0)
    plan_file.write_text(json.dumps(plan, ensure_ascii=False))
    return step


def replay(cli: str, step: dict, out_file: str):
    sid = step.get("session", "S-" + cli)
    reply = step.get("reply", "完成")
    lines = (SAMPLES / SAMPLE[cli]).read_text().splitlines()
    for raw in lines:
        ev = json.loads(raw)
        for k in ("session_id", "thread_id", "sessionId"):
            if k in ev:
                ev[k] = sid
        if ev.get("type") == "result":
            ev["result"] = reply
            if step.get("is_error"):
                ev["is_error"] = True
        if cli == "codex" and ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            ev["item"]["text"] = reply
        print(json.dumps(ev, ensure_ascii=False), flush=True)
    if cli == "codex" and out_file:
        Path(out_file).write_text(reply)


def main():
    cli, args = sys.argv[1], sys.argv[2:]
    bin_dir = Path(os.environ.get("FAKE_CLI_DIR") or Path(sys.argv[0]).resolve().parent)
    with open(bin_dir / "calls.jsonl", "a") as f:
        f.write(json.dumps({"cli": cli, "args": args, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    step = take_step(bin_dir, cli)
    out_file = args[args.index("-o") + 1] if "-o" in args else ""
    if step.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if step.get("grandchild"):
        gc = subprocess.Popen(["sleep", "300"])
        (bin_dir / "pids.txt").write_text(f"{os.getpid()} {gc.pid}")
    if step.get("stubborn_child"):
        # 不占管道、忽略 SIGTERM 的孙进程（像 AI 用 Bash 起的后台服务）
        gc = subprocess.Popen(["sh", "-c", 'trap "" TERM; sleep 300'], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        (bin_dir / "pids.txt").write_text(f"{os.getpid()} {gc.pid}")
    for rel, content in (step.get("write") or {}).items():
        p = Path(os.getcwd()) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if step.get("sleep"):
        time.sleep(step["sleep"])
    if step.get("stderr"):
        print(step["stderr"], file=sys.stderr, flush=True)
    if not step.get("no_output"):
        replay(cli, step, out_file)
    sys.exit(step.get("exit", 0))


if __name__ == "__main__":
    main()

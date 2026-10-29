"""核心流转测试：不调真 CLI，用 tests/fake_cli.py 回放 tests/samples 的真实样本；git 用真的（临时目录）。"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtWidgets import QMessageBox

from donedef import worktree
from donedef.board import RESTART_LINE, SUPPLEMENT_HEAD, Board
from donedef.cli import REQ_ANSWER_SUFFIX, REQ_JSON_SUFFIX, StreamParser, build_cmd, parse_req_json
from donedef.model import Project
from donedef.ui import PENDING_MARK_STYLE, MainWindow, status_of
from donedef.worktree import GitError

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
SHOTS = ROOT / "shots"
SHOTS.mkdir(exist_ok=True)
WAIT = 20000  # 毫秒

REQ1 = ("好的。\n```json\n{\"understanding\": \"新建 hello.txt\", \"todo\": [\"写 hello.txt\"], "
        "\"not_todo\": [\"不改别的文件\"], \"questions\": [\"末尾要换行吗\"]}\n```")
REQ2 = ("好的。\n```json\n{\"understanding\": \"新建 hello.txt，末尾换行\", "
        "\"todo\": [\"写 hello.txt\"], \"not_todo\": [], \"questions\": []}\n```")
IDS = ["a5", "a2", "a4"]  # 需求 claude 只读 / 开发 grok 可改 / 验证 codex 只读


# ---------- 环境 ----------
@pytest.fixture(autouse=True)
def pat_isolated(monkeypatch, tmp_path):
    """测试绝不读写你真正的 ~/.azure-devops/personal-pat.txt"""
    from donedef import azure
    monkeypatch.setattr(azure, "PAT_FILE", tmp_path / "no-real-pat" / "personal-pat.txt")


@pytest.fixture(autouse=True)
def git_isolated(monkeypatch, tmp_path):
    """git 不读你的全局 / 系统配置，用户名只在临时仓库里配"""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path) -> Path:
    path.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=path)
    sh("git", "config", "user.name", "Test", cwd=path)
    sh("git", "config", "user.email", "test@example.com", cwd=path)
    sh("git", "config", "user.useConfigOnly", "true", cwd=path)
    (path / "README.md").write_text("demo\n")
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-q", "-m", "init", cwd=path)
    return path


class Fake:
    def __init__(self, d: Path):
        self.dir = d

    def plan(self, **queues):
        self.dir.joinpath("plan.json").write_text(json.dumps(queues, ensure_ascii=False))

    def calls(self, cli=None):
        f = self.dir / "calls.jsonl"
        rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [r for r in rows if cli is None or r["cli"] == cli]

    def pids(self):
        f = self.dir / "pids.txt"
        return [int(x) for x in f.read_text().split()] if f.exists() else []


@pytest.fixture
def fake(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("claude", "codex", "grok"):
        p = d / name
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_cli.py"}" {name} "$@"\n')
        p.chmod(0o755)
    f = Fake(d)
    f.plan()
    return f


def env_for(fake: Fake) -> dict:
    return {"PATH": f"{fake.dir}:{os.environ['PATH']}", "HOME": os.environ["HOME"], "FAKE_CLI_DIR": str(fake.dir),
            "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"], "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture
def board(qtbot, tmp_path, fake):
    b = Board(tmp_path / "data", env=env_for(fake))
    yield b
    b.stop_all()
    dispose(b)


def dispose(b):
    """Board 有引用环，交给循环 GC 可能在后台线程里析构 Qt 对象而崩溃：测试里在主线程显式销毁"""
    b.close()
    shiboken6.delete(b)


def add_project(board, repo, build="", test=""):
    p = Project(uuid.uuid4().hex[:12], "demo", str(repo), "local", str(repo), base_branch="main",
                build_cmd=build, test_cmd=test)
    board.projects.append(p)
    return p


def start(board, proj, title="新建 hello.txt", ids=IDS):
    item = board.new_manual_item(title, "在仓库根目录新建 hello.txt，内容 hi", "")
    card, err = board.start_card(item, proj.id, list(ids))
    assert err is None, err
    return card


def wait_review(qtbot, card, stage=None):
    qtbot.waitUntil(lambda: card.stage.status == "review" and (stage is None or card.stage.stage == stage), timeout=WAIT)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 已退出但没被回收的僵尸也算死了
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def prompt_of(args):
    return args[args.index("-p") + 1]


def git_out(cwd, *args):
    return sh("git", *args, cwd=cwd).strip()


# ---------- 命令拼装 ----------
@pytest.mark.parametrize("perm,claude,codex_new,grok", [
    ("read", "dontAsk", "read-only", "dontAsk"),
    ("edit", "acceptEdits", "workspace-write", "acceptEdits"),
    ("full", "bypassPermissions", "danger-full-access", "bypassPermissions"),
])
def test_build_cmd_matrix(perm, claude, codex_new, grok):
    assert build_cmd("claude", perm, "", "", "做事") == [
        "claude", "-p", "做事", "--permission-mode", claude, "--output-format", "stream-json", "--verbose"]
    assert build_cmd("claude", perm, "opus", "low", "做事", session_id="S1") == [
        "claude", "-p", "做事", "--permission-mode", claude, "--output-format", "stream-json", "--verbose",
        "--resume", "S1", "--model", "opus", "--effort", "low"]
    assert build_cmd("codex", perm, "", "", "做事", out_file="/o.txt") == [
        "codex", "exec", "--json", "--skip-git-repo-check", "-s", codex_new, "-c", 'approval_policy="never"',
        "-o", "/o.txt", "做事"]
    assert build_cmd("codex", perm, "gpt-5.6-sol", "low", "做事", session_id="T1", out_file="/o.txt") == [
        "codex", "exec", "resume", "--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{codex_new}"',
        "-c", 'approval_policy="never"', "-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="low"',
        "-o", "/o.txt", "T1", "做事"]
    tools = ["--tools", "read_file,list_dir,grep"] if perm == "read" else []
    assert build_cmd("grok", perm, "", "", "做事") == [
        "grok", "-p", "做事", "--permission-mode", grok, *tools, "--output-format", "streaming-messages-json"]
    assert build_cmd("grok", perm, "grok-4.6", "low", "做事", session_id="G1") == [
        "grok", "-r", "G1", "-p", "做事", "--permission-mode", grok, *tools,
        "--output-format", "streaming-messages-json", "-m", "grok-4.6", "--reasoning-effort", "low"]


def test_build_cmd_guards():
    assert build_cmd("claude", "read", "", "", "-x 开头")[2] == "\n-x 开头"
    with pytest.raises(ValueError):
        build_cmd("codex", "read", "", "", "做事")  # 没有 -o 文件
    with pytest.raises(ValueError):
        build_cmd("claude", "root", "", "", "做事")


# ---------- 流式解析 ----------
def feed_sample(cli, name):
    p = StreamParser(cli)
    acts = []
    for ln in (SAMPLES / name).read_text().splitlines():
        acts += p.feed(ln)
    return p, acts


def test_parse_claude_sample():
    p, acts = feed_sample("claude", "edit-claude.jsonl")
    assert p.session_id == "822e3402…"
    assert "Write <redacted>" in acts
    assert any(a.startswith("Bash echo ran > ran.txt") for a in acts)
    assert any(a.startswith("说：") for a in acts)
    p.result = "改好了"  # 样本里的 result 已脱敏删掉
    res = p.finish(0, "")
    assert (res.kind, res.reply) == ("ok", "改好了")


def test_parse_codex_sample(tmp_path):
    p, acts = feed_sample("codex", "edit-codex.jsonl")
    assert p.session_id == "01a17c43…"
    assert acts[:3] == ["说：我会分别按指定方式创建两个文件，并核对内容。", "Write <redacted>", "Bash echo ran > ran.txt"]
    # -o 文件优先，没有就用最后一条 agent_message
    assert p.finish(0, "").reply == "已完成：`hello.txt` 内容为 `hi`，`ran.txt` 内容为 `ran`。"
    out = tmp_path / "last.txt"
    out.write_text("文件里的回复")
    assert p.finish(0, "", str(out)).reply == "文件里的回复"


def test_parse_grok_sample():
    p, acts = feed_sample("grok", "edit-grok.jsonl")
    assert p.session_id == "01a17c43…"
    assert any(a.startswith(("Edit", "Write")) for a in acts)
    res = p.finish(0, "")
    assert res.kind == "ok" and res.reply.startswith("已用文件编辑工具新建")


def test_finish_failed_and_quota():
    p = StreamParser("claude")
    assert p.finish(0, "").error == "取不到最终回复"
    r = p.finish(3, "boom\n")
    assert (r.kind, r.error) == ("failed", "进程退出码 3：boom")
    q = StreamParser("codex")
    q.feed(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
    r = q.finish(1, "")
    assert r.kind == "quota" and "usage limit" in r.error
    c = StreamParser("claude")
    c.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}))
    assert c.finish(1, "").kind == "quota"
    e = StreamParser("grok")
    e.feed(json.dumps({"type": "result", "is_error": True, "result": "bad things"}))
    r = e.finish(0, "")
    assert (r.kind, r.error) == ("failed", "CLI 报错：bad things")
    # 成功的运行里提到 rate limit 不算额度耗尽
    ok = StreamParser("grok")
    ok.feed(json.dumps({"type": "result", "is_error": False, "result": "已加 rate limit 中间件"}))
    assert ok.finish(0, "").kind == "ok"


# ---------- 需求 JSON ----------
def test_parse_req_json():
    # 旧格式（REQ1）：疑问转成「待你确认」步骤，不做丢弃
    assert parse_req_json(REQ1) == {"goal": "新建 hello.txt", "done": [],
                                  "plan": ["写 hello.txt", "（待你确认：末尾要换行吗）"]}
    two = "```json\n{\"a\": 1}\n```\n后面才是最终版\n" + REQ2
    assert parse_req_json(two) == {"goal": "新建 hello.txt，末尾换行", "done": [], "plan": ["写 hello.txt"]}
    bare = '{"understanding": "u", "todo": [], "not_todo": [], "questions": [" q "]}'
    assert parse_req_json(bare)["plan"] == ["（待你确认：q）"]
    with pytest.raises(ValueError, match="缺少字段 goal、plan"):
        parse_req_json('{"understanding": "u", "todo": []}')
    with pytest.raises(ValueError, match="不是合法 JSON"):
        parse_req_json("我理解的是……")
    with pytest.raises(ValueError, match="todo 不是字符串数组"):
        parse_req_json('{"understanding": "u", "todo": "x", "not_todo": [], "questions": []}')


# ---------- worktree ----------
def test_worktree_add_commit_diff_remove(tmp_path, repo):
    wt = tmp_path / "wts" / "demo" / "L1"
    worktree.add(str(repo), str(wt), "feature/task-l1", "main")
    assert git_out(wt, "branch", "--show-current") == "feature/task-l1"
    with pytest.raises(GitError, match="目录已存在"):
        worktree.add(str(repo), str(wt), "feature/task-l9", "main")
    with pytest.raises(GitError, match="already exists"):
        worktree.add(str(repo), str(tmp_path / "wts" / "demo" / "L2"), "feature/task-l1", "main")
    (wt / "a.txt").write_text("A\n")
    assert worktree.changed_files(str(wt)) == ["?? a.txt"]
    worktree.commit(str(wt), "L1 开发：写 a")
    assert git_out(wt, "log", "-1", "--format=%s") == "L1 开发：写 a"
    (wt / "README.md").write_text("changed\n")  # 未提交的改动也在 diff 里
    stat, full = worktree.diff(str(wt), "main")
    assert "a.txt" in stat and "README.md" in stat and "+changed" in full
    with pytest.raises(GitError, match="contains modified or untracked files"):
        worktree.remove(str(repo), str(wt))  # 不加 --force：有改动就失败
    sh("git", "checkout", "README.md", cwd=wt)
    worktree.remove(str(repo), str(wt))
    assert not wt.exists()
    assert git_out(repo, "branch", "--list", "feature/task-l1")  # 分支保留


def test_diff_truncated(tmp_path, repo, monkeypatch):
    wt = tmp_path / "wt"
    worktree.add(str(repo), str(wt), "feature/x", "main")
    (wt / "README.md").write_text("x" * 500 + "\n")
    monkeypatch.setattr(worktree, "DIFF_LIMIT", 100)
    _, full = worktree.diff(str(wt), "main")
    assert full.endswith("超过 100，已截断）") and len(full) < 200


# ---------- 完整流转 ----------
def test_full_pipeline(qtbot, board, fake, repo):
    fake.plan(
        claude=[{"reply": REQ1, "session": "C1"}, {"reply": REQ2, "session": "C1"}],
        grok=[{"reply": "开发完成", "session": "G1",
               "write": {"hello.txt": "hi\n", "test_hello.sh": "test -f hello.txt\n"}},
              {"reply": "按退回改好了", "session": "G1", "write": {"hello.txt": "hi!\n"}}],
        codex=[{"reply": "验证不通过：少感叹号", "session": "X1"},
               {"reply": "验证通过", "session": "X2"}],
    )
    proj = add_project(board, repo, build="true", test="sh test_hello.sh")
    card = start(board, proj)
    wt = Path(card.wt_path)
    assert wt == board.data_dir / "worktrees" / "demo" / "L1"
    assert git_out(wt, "branch", "--show-current") == "feature/task-l1"

    # 需求：只读 + 追加 JSON 要求；回复里取最后一个 json 块
    wait_review(qtbot, card, "req")
    st = card.stages[0]
    assert st.rounds[0].parsed["plan"] == ["写 hello.txt", "（待你确认：末尾要换行吗）"]
    assert st.session_id == "C1"
    c1 = fake.calls("claude")[0]
    assert c1["cwd"] == str(wt)
    assert c1["args"][c1["args"].index("--permission-mode") + 1] == "dontAsk"
    assert prompt_of(c1["args"]).endswith(REQ_JSON_SUFFIX)
    assert (board.log_dir(card) / "req-1.jsonl").read_text().count("\n") == 18  # 原始 stdout 逐行落盘

    # 回答疑问 = 续聊原会话 + 追加「按同样 JSON 重新输出」
    assert board.ask(card, "要换行")
    wait_review(qtbot, card, "req")
    c2 = fake.calls("claude")[1]["args"]
    assert c2[c2.index("--resume") + 1] == "C1"
    assert prompt_of(c2) == "要换行" + REQ_ANSWER_SUFFIX
    assert board.open_questions(card) == []
    assert board.confirm_spec(card, board.spec_draft(card))
    assert card.spec == "【完成目标】\n新建 hello.txt，末尾换行\n\n【方案】\n1. 写 hello.txt"

    # 开发：grok 可改；跑完程序自检；有改动 → 不能直接通过，必须提交
    wait_review(qtbot, card, "dev")
    g1 = fake.calls("grok")[0]
    assert g1["args"][g1["args"].index("--permission-mode") + 1] == "acceptEdits"
    assert "新建 hello.txt，末尾换行" in prompt_of(g1["args"])  # {spec}
    assert card.stage.checks == {"构建": ("true", 0), "测试": ("sh test_hello.sh", 0)}
    assert (board.log_dir(card) / "dev-1-check-测试.txt").exists()
    files, msg = board.pending_commit(card)
    assert files == ["?? hello.txt", "?? test_hello.sh"] and msg == "L1 开发：新建 hello.txt"
    assert board.pass_stage(card) is False
    assert board.commit_and_pass(card) is None
    assert git_out(wt, "log", "-1", "--format=%s") == "L1 开发：新建 hello.txt"

    # 开发之后直接验证（没有测试阶段）：codex 只读，{diff} 是相对 merge-base 的完整 diff；验证后也自检
    wait_review(qtbot, card, "verify")
    assert [s.stage for s in card.stages] == ["req", "dev", "verify"]
    x1 = fake.calls("codex")[0]["args"]
    assert "resume" not in x1 and x1[x1.index("-s") + 1] == "read-only"
    assert "+hi" in x1[-1] and "测试结果" not in x1[-1]  # 预置验证模板不再引用 {test_reply}
    assert card.stage.checks == {"构建": ("true", 0), "测试": ("sh test_hello.sh", 0)}

    # 从验证退回开发：续聊开发原会话；验证会话清空
    spec_before = card.spec
    assert board.can_send_back(card)
    assert board.send_back(card, "加个感叹号")
    assert card.stage.stage == "dev" and card.stages[2].session_id == "" and card.stages[2].checks == {}
    assert card.spec == spec_before + "\n\n" + SUPPLEMENT_HEAD + "\n加个感叹号"
    wait_review(qtbot, card, "dev")
    g2 = fake.calls("grok")[1]["args"]
    assert g2[:2] == ["-r", "G1"]
    assert "【从验证阶段退回】" in prompt_of(g2) and "加个感叹号" in prompt_of(g2) and "少感叹号" in prompt_of(g2)
    assert board.commit_and_pass(card) is None

    # 退回后重跑验证 = 新开会话
    wait_review(qtbot, card, "verify")
    x2 = fake.calls("codex")[1]["args"]
    assert "resume" not in x2 and card.stage.session_id == "X2"
    assert "+hi!" in x2[-1] and SUPPLEMENT_HEAD + "\n加个感叹号" in x2[-1]
    assert board.pass_stage(card)  # 没改动直接通过
    assert card.column == "done" and card.comment == "skip"

    # 清理：git worktree remove，分支保留
    assert board.cleanup(card) is None
    assert not wt.exists() and card.worktree_cleaned
    assert git_out(repo, "log", "-1", "--format=%s", "feature/task-l1") == "L1 开发：新建 hello.txt"
    kinds = [k for k, _ in board.db.events(card.id)]
    assert kinds.count("commit") == 2 and "send_back" in kinds and kinds[-1] == "cleanup"


def test_old_four_stage_card_finishes(qtbot, board, fake, repo, monkeypatch):
    """去掉测试阶段之前建的四阶段老卡：照原样走完；{test_reply} 取测试阶段回复"""
    import donedef.board as board_mod
    monkeypatch.setattr(board_mod, "STAGES", ("req", "dev", "test", "verify"))
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}], grok=[{"reply": "开发完成", "session": "G1"}],
              codex=[{"reply": "测试写好了", "session": "X1"}, {"reply": "验证通过", "session": "X2"}])
    board.agent("a4").prompt = "测试结果：{test_reply}"
    card = start(board, add_project(board, repo), ids=["a5", "a2", "a3", "a4"])
    monkeypatch.setattr(board_mod, "STAGES", ("req", "dev", "verify"))
    assert [s.stage for s in card.stages] == ["req", "dev", "test", "verify"]
    wait_review(qtbot, card)
    assert board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")
    assert "通过后进入测试" in status_of(board, card)[2]
    assert board.pass_stage(card)
    wait_review(qtbot, card, "test")
    assert "通过后进入验证" in status_of(board, card)[2]
    assert board.can_send_back(card)
    assert board.pass_stage(card)
    wait_review(qtbot, card, "verify")
    assert fake.calls("codex")[1]["args"][-1] == "测试结果：测试写好了"
    assert board.pass_stage(card)
    assert card.column == "done"


def test_new_card_test_reply_placeholder(qtbot, board, fake, repo):
    """新卡没有测试阶段：自建模板里的 {test_reply} 填固定文字，不报错"""
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了"}], codex=[{"reply": "验证通过"}])
    board.agent("a4").prompt = "测试结果：{test_reply}"
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card)
    assert board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")
    assert "通过后进入验证" in status_of(board, card)[2]
    assert board.pass_stage(card)
    wait_review(qtbot, card, "verify")
    assert "通过即完成" in status_of(board, card)[2]
    assert fake.calls("codex")[0]["args"][-1] == "测试结果：（本流程没有测试阶段）"


def test_failed_check_blocks_pass(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了"}], codex=[{"reply": "测完"}])
    proj = add_project(board, repo, build="true", test="exit 3")
    card = start(board, proj)
    wait_review(qtbot, card)
    board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")  # 开发结束就自检
    assert card.stage.checks["测试"] == ("exit 3", 3)
    assert not board.can_pass(card) and not board.pass_stage(card)
    assert status_of(board, card)[:2] == ("bad", "自检失败")
    assert not board.can_send_back(card)  # 开发阶段没有「退回开发」，用「发给 AI 接着改」
    log = board.log_dir(card) / "dev-1-check-测试.txt"
    assert log.exists()


def test_req_bad_json_fails(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": "我觉得应该这么做……"}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card)
    assert card.stage.flag == "failed"
    assert card.stage.error.startswith("需求理解格式不对：不是合法 JSON")
    assert card.stage.rounds[-1].end == "failed"


def test_req_forced_readonly(qtbot, board, fake, repo):
    """需求阶段选了可改档的模板，也按只读跑"""
    fake.plan(grok=[{"reply": REQ2}])
    card = start(board, add_project(board, repo), ids=["a2", "a2", "a4"])
    wait_review(qtbot, card)
    args = fake.calls("grok")[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--tools") + 1] == "read_file,list_dir,grep"


@pytest.mark.parametrize("step,flag,error", [
    ({"exit": 2, "stderr": "boom", "no_output": True}, "failed", "进程退出码 2：boom"),
    ({"is_error": True, "reply": "oops"}, "failed", "CLI 报错：oops"),
    ({"exit": 1, "stderr": "Claude AI usage limit reached", "no_output": True}, "quota",
     "额度耗尽：Claude AI usage limit reached"),
])
def test_end_flags(qtbot, board, fake, repo, step, flag, error):
    fake.plan(claude=[step])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card)
    assert (card.stage.flag, card.stage.error) == (flag, error)
    assert board.notifications[-1][0].startswith("L1 需求阶段")


def test_spawn_failure(qtbot, tmp_path, fake, repo):
    b = Board(tmp_path / "data2", env={"PATH": "/nonexistent", "HOME": os.environ["HOME"]})
    try:
        card = start(b, add_project(b, repo))
        assert card.stage.flag == "failed" and card.stage.error.startswith("启动失败")
    finally:
        dispose(b)


def test_start_refused_on_git_error(board, fake, repo):
    sh("git", "branch", "feature/task-l1", cwd=repo)
    proj = add_project(board, repo)
    item = board.new_manual_item("t", "", "")
    card, err = board.start_card(item, proj.id, IDS)
    assert card is None and "already exists" in err and err.startswith("$ git worktree add")
    assert board.cards == [] and fake.calls() == []


def test_commit_failure_keeps_stage(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了", "write": {"x.txt": "x"}}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card)
    board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")
    sh("git", "config", "--unset", "user.name", cwd=repo)
    sh("git", "config", "--unset", "user.email", cwd=repo)
    err = board.commit_and_pass(card)
    assert err and "Author identity unknown" in err
    assert card.stage.stage == "dev" and card.stage.status == "review"


# ---------- 停止 ----------
def test_stop_kills_process_group(qtbot, board, fake, repo):
    fake.plan(claude=[{"grandchild": True, "sleep": 60}])
    card = start(board, add_project(board, repo))
    qtbot.waitUntil(lambda: len(fake.pids()) == 2, timeout=WAIT)
    leader, grandchild = fake.pids()
    assert alive(grandchild)
    assert board.stop(card)
    wait_review(qtbot, card)
    assert card.stage.flag == "stopped"
    qtbot.waitUntil(lambda: not alive(leader) and not alive(grandchild), timeout=WAIT)


def test_stop_escalates_to_sigkill(qtbot, board, fake, repo):
    fake.plan(claude=[{"ignore_term": True, "grandchild": True, "sleep": 60}])
    card = start(board, add_project(board, repo))
    qtbot.waitUntil(lambda: len(fake.pids()) == 2, timeout=WAIT)
    leader, _ = fake.pids()
    board.stop(card)
    qtbot.wait(1500)
    assert alive(leader) and card.stage.status == "running"  # SIGTERM 被忽略
    wait_review(qtbot, card)  # 5 秒后 SIGKILL
    assert card.stage.flag == "stopped" and not alive(leader)


def test_stop_kills_stubborn_grandchild(qtbot, board, fake, repo):
    """组长很快退出，但孙进程不占管道、忽略 SIGTERM：仍要在 5 秒后 SIGKILL 整个组，之后才算停止"""
    fake.plan(claude=[{"stubborn_child": True, "sleep": 60}])
    card = start(board, add_project(board, repo))
    qtbot.waitUntil(lambda: len(fake.pids()) == 2, timeout=WAIT)
    leader, grandchild = fake.pids()
    qtbot.wait(300)  # 等孙进程装好 trap
    board.stop(card)
    qtbot.waitUntil(lambda: not alive(leader), timeout=WAIT)
    qtbot.wait(1000)
    assert alive(grandchild) and card.stage.status == "running"  # 组还没死干净，不报已停止
    wait_review(qtbot, card)
    assert card.stage.flag == "stopped" and not alive(grandchild)


def test_stop_during_check(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了"}], codex=[{"reply": "测完"}])
    card = start(board, add_project(board, repo, test="sleep 60"))
    wait_review(qtbot, card)
    board.confirm_spec(card, "spec")
    qtbot.waitUntil(lambda: card.stage.status == "checking", timeout=WAIT)  # 开发结束后自检
    assert card.stage.stage == "dev" and card.stage.checking == "sleep 60"
    board.stop(card)
    wait_review(qtbot, card, "dev")
    assert card.stage.flag == "stopped" and "自检 sleep 60 被终止" in card.stage.error


# ---------- 落库与重启 ----------
@pytest.mark.real_seed
def test_first_start_seeds(board):
    """新库只种 6 个预置角色模板"""
    assert [(a.id, a.name, a.cli, a.perm, a.caps) for a in board.agents] == [
        ("p1", "架构师", "claude", "read", ["codegraph"]), ("p2", "测试", "codex", "edit", ["codegraph"]),
        ("p3", "需求找茬", "claude", "read", []), ("p4", "开发", "claude", "edit", ["codegraph"]),
        ("p5", "代码审查", "codex", "read", ["codegraph"]), ("p6", "原型审核", "claude", "read", ["playwright"])]
    assert board.agent("p2").model == "gpt-5.6-sol" and board.agent("p5").model == "gpt-5.6-sol"
    assert all("{" not in a.prompt for a in board.agents)  # 只写角色说明，不引用阶段变量
    assert board.projects == [] and board.cards == []


def test_no_reseed_after_agents_deleted(board, repo, tmp_path, fake):
    """模板删光后重启：不算第一次启动，不重新预置，更不能清掉项目"""
    proj = add_project(board, repo)
    board.agents.clear()
    board.save()
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        assert b2.agents == [] and b2.projects == [proj]
    finally:
        dispose(b2)


def test_persist_roundtrip(qtbot, board, fake, repo, tmp_path):
    fake.plan(claude=[{"reply": REQ1, "session": "C1"}, {"reply": REQ2, "session": "C1"}])
    proj = add_project(board, repo, build="make")
    card = start(board, proj)
    wait_review(qtbot, card)
    board.ask(card, "要换行")
    wait_review(qtbot, card)
    board.agents[0].model = "opus"
    board.save()
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        assert b2.projects == board.projects and b2.agents == board.agents
        c2 = b2.card("L1")
        assert c2.wt_path == card.wt_path and c2.cur == 0
        assert [(r.by, r.ask, r.reply, r.end, r.parsed, r.session_id, r.log) for r in c2.stages[0].rounds] == \
               [(r.by, r.ask, r.reply, r.end, r.parsed, r.session_id, r.log) for r in card.stages[0].rounds]
        assert c2.stages[0].session_id == "C1" and c2.stages[0].output == card.stages[0].output
        assert b2.new_manual_item("x", "", "").id == "L2"
    finally:
        dispose(b2)


def test_restart_marks_stopped(qtbot, board, fake, repo, tmp_path):
    fake.plan(claude=[{"sleep": 60}])
    card = start(board, add_project(board, repo))
    board.save()
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        st = b2.card("L1").stage
        assert (st.status, st.flag, st.output[-1]) == ("review", "stopped", RESTART_LINE)
        assert st.rounds[-1].end == "stopped"
        assert ("recover", f"需求阶段：{RESTART_LINE}") in b2.db.events("L1")
    finally:
        dispose(b2)
    assert card.stage.status == "running"  # 原来那个还在跑，fixture 收尾时停掉


def test_stop_all_on_quit(qtbot, board, fake, repo):
    fake.plan(claude=[{"sleep": 60}, {"sleep": 60}])
    proj = add_project(board, repo)
    c1, c2 = start(board, proj), start(board, proj, "第二张")
    qtbot.waitUntil(lambda: len(fake.calls("claude")) == 2, timeout=WAIT)
    board.stop_all()
    assert c1.stage.flag == c2.stage.flag == "stopped"
    assert board.running_cards() == []


# ---------- 界面 ----------
@pytest.fixture
def win(qtbot, board):
    w = MainWindow(board, autorun=False)
    qtbot.addWidget(w)
    w.resize(1440, 900)
    w.show()
    yield w
    w._quitting = True


def grab(w, name):
    w.grab().save(str(SHOTS / f"donedef-{name}.png"))


def test_ui_empty_state(win):
    """第一次启动：没配 Azure 不发请求，说明行提示去设置；三栏为空；手写任务照样能加"""
    assert win.fetch_label.text() == "还没配 Azure：点「设置」→ Azure"
    assert win.fetch_label.property("kind") == "warn" and win._job is None
    assert [c.count.text() for c in win.columns.values()] == ["0", "0", "0"]
    assert not win.iter_cb.isEnabled() and win.add_btn.isEnabled()
    grab(win, "0-empty")


def test_ui_flow_with_commit_box(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ1}], grok=[{"reply": "好了", "write": {f"f{i:02d}.txt": "x" for i in range(35)}}],
              codex=[{"reply": "测完", "sleep": 30}])
    proj = add_project(board, repo)
    card = start(board, proj)
    win.select(card.id)
    qtbot.waitUntil(lambda: card.stage.output and card.stage.output[-1].startswith("说："), timeout=WAIT)
    win.render()
    grab(win, "1-running")
    wait_review(qtbot, card)
    win.render()
    qtbot.wait(300)  # 等抽屉滑入动画
    # 疑问挪到方案上面的橙色框，步骤里只留 ❓1 小标记
    assert "末尾要换行吗" in win.drawer.pend.text() and "❓1" in win.drawer.plan.text()
    assert "待你确认" not in win.drawer.plan.text()
    grab(win, "2-req-review")
    win.drawer.confirm_btn.click()
    wait_review(qtbot, card, "dev")
    win.render()

    # 卡片上的「通过」→ 提交确认框：最多列 30 个文件
    btn = win.card_widget(card.id).buttons["pass"]
    btn.click()
    box = win.active_dialog
    assert isinstance(box, QMessageBox)
    text = box.text()
    assert "f29.txt" in text and "f30.txt" not in text and "等 35 个" in text
    assert "feature/task-l1" in text and "L1 开发：新建 hello.txt" in text
    grab(box, "3-commit-box")
    # 取消 = 不通过
    box.reject()
    assert card.stage.stage == "dev" and len(board.pending_commit(card)[0]) == 35
    win.request_pass(card)
    win.active_dialog.ok_btn.click()
    assert card.stage.stage == "verify" and card.stage.status == "running"
    assert git_out(card.wt_path, "log", "-1", "--format=%s") == "L1 开发：新建 hello.txt"


def test_ui_quit_box_stops_all(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"sleep": 60}])
    proj = add_project(board, repo)
    card = start(board, proj)
    win.close()
    box = win.active_dialog
    assert box.stop_btn.text() == "停掉全部再退出"
    box.stop_btn.click()
    assert card.stage.flag == "stopped"


# ---------- CLI 检测 / 模型列表 / 额度 / 模板窗口 ----------
from donedef import clis  # noqa: E402
from donedef.dialogs import AgentDialog  # noqa: E402


def test_parse_version():
    assert clis.parse_version("codex-cli 0.152.1") == "0.152.1"
    assert clis.parse_version("2.1.311 (Claude Code)") == "2.1.311"
    assert clis.parse_version("grok 1.0.70 (abc)") == "1.0.70"
    assert clis.parse_version("nightly") == "nightly"


def test_parse_models_real_samples():
    """三家模型列表都用本机真实输出（tests/samples）"""
    g = clis.parse_grok_models((SAMPLES / "grok-models.txt").read_text())
    assert g["models"] == ["grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"] and g["default"] == "grok-4.7"
    c = clis.parse_codex_models(json.loads((SAMPLES / "codex-models.json").read_text()))
    assert c["models"] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"] and c["default"] == "gpt-5.6-sol"
    assert "ultra" in c["efforts"]["gpt-5.6-sol"] and "ultra" not in c["efforts"]["gpt-5.6-luna"]
    a = clis.parse_claude_help((SAMPLES / "claude-help.txt").read_text())
    assert a["models"] == ["fable", "opus", "sonnet"]
    assert a["any_efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_parse_codex_models_hidden_and_paging():
    r = {"data": [{"id": "m1", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
                  {"id": "secret", "hidden": True}], "nextCursor": "x"}
    c = clis.parse_codex_models(r)
    assert c["models"] == ["m1"] and c["efforts"] == {"m1": ["low"]} and "只取了第一页" in c["note"]
    with pytest.raises(ValueError):
        clis.parse_codex_models({"data": [{"id": "secret", "hidden": True}]})
    with pytest.raises(ValueError):
        clis.parse_grok_models("You are logged in.\n")
    with pytest.raises(ValueError):
        clis.parse_claude_help("Usage: claude [options]\n")


def test_parse_quota_samples():
    line = (SAMPLES / "codex-quota.jsonl").read_text().splitlines()[0]
    q = clis.parse_codex_quota(json.loads(line)["result"])
    assert [(w["name"], w["pct"]) for w in q] == [("5 小时", 2.0), ("7 天", 3.0)]
    assert q[0]["resets_at"] == 1793100157
    with pytest.raises(ValueError):
        clis.parse_codex_quota({"rateLimits": {}})
    info = next(json.loads(x)["rate_limit_info"] for x in (SAMPLES / "edit-claude.jsonl").read_text().splitlines()
                if '"rate_limit_event"' in x)
    cq = clis.parse_claude_quota(info)
    assert [(w["name"], round(w["pct"])) for w in cq] == [("5 小时", 42), ("7 天", 53)]
    assert clis.window_name(90) == "90 分钟" and clis.window_name(None) == "?"


def test_set_quota_persist_and_error_keeps_old(qtbot, board, tmp_path, fake):
    with qtbot.waitSignal(board.quota_changed):
        board.set_quota("codex", [{"name": "5 小时", "pct": 12.0, "resets_at": 1}])
    board.set_quota("codex", error="超时")
    q = board.quota["codex"]
    assert q["windows"][0]["pct"] == 12.0 and q["error"] == "超时" and q["err_at"]
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        assert b2.quota["codex"]["windows"][0]["pct"] == 12.0 and b2.quota["codex"]["error"] == "超时"
    finally:
        dispose(b2)
    # 成功一次就清掉原因
    board.set_quota("codex", [{"name": "5 小时", "pct": 13.0, "resets_at": 1}])
    assert board.quota["codex"]["error"] == ""


def test_bad_cache_file_reported(tmp_path, fake):
    d = tmp_path / "data"
    d.mkdir()
    (d / "quota.json").write_text("{坏")
    b = Board(d, env=env_for(fake))
    try:
        assert b.quota == {} and "quota.json" in b.cache_error
    finally:
        dispose(b)


def test_claude_quota_from_run(qtbot, board, fake, repo):
    """需求阶段 claude 跑完：额度取自流里的 rate_limit_event，并发 ai_done"""
    fake.plan(claude=[{"reply": REQ1}])
    proj = add_project(board, repo)
    with qtbot.waitSignal(board.ai_done, timeout=WAIT) as sig:
        card = start(board, proj)
    assert sig.args == ["claude"]
    wait_review(qtbot, card)
    # 流里有多个 rate_limit_event，取最后一个（样本第 17 行 0.43）
    assert [round(w["pct"]) for w in board.quota["claude"]["windows"]] == [43, 53]


def test_bgjob_error_becomes_reason(qtbot):
    def boom():
        raise RuntimeError("连不上")
    job = clis.BgJob(boom)
    with qtbot.waitSignal(job.done, timeout=WAIT) as sig:
        job.start()
    assert sig.args == [{"error": "RuntimeError：连不上"}]


def test_ui_header_and_quota_bar(qtbot, board, win, repo):
    add_project(board, repo)
    chips = win.quota_bar.chips
    assert chips["grok"].text() == "Grok 额度不可用"
    assert chips["claude"].text() == "Claude 跑一次后显示"
    board.set_quota("claude", [{"name": "5 小时", "pct": 42.0, "resets_at": 1793099400},
                               {"name": "7 天", "pct": 91.0, "resets_at": 1793314800}])
    assert chips["claude"].text() == "Claude 5 小时 剩 58% · 7 天 剩 9%" and chips["claude"].property("kind") == "bad"
    assert "重置" in chips["claude"].toolTip()
    # codex：经注入的查询函数，先失败再成功
    win._quota_fetch = lambda env: (_ for _ in ()).throw(RuntimeError("app-server 超时"))
    win.refresh_codex_quota()
    qtbot.waitUntil(lambda: win._quota_job is None, timeout=WAIT)
    assert chips["codex"].text() == "Codex 查询失败" and "app-server 超时" in chips["codex"].toolTip()
    win._quota_fetch = lambda env: [{"name": "5 小时", "pct": 75.0, "resets_at": 1}]
    win.refresh_codex_quota()
    qtbot.waitUntil(lambda: win._quota_job is None, timeout=WAIT)
    assert chips["codex"].text() == "Codex 5 小时 剩 25%" and chips["codex"].property("kind") == "warn"
    grab(win, "4-header-quota")


def test_ui_req_sections(qtbot, board, win, fake, repo):
    """需求页分区：要干嘛 / 方案（待你确认高亮；不显示需求原文）；旧的理解、疑问区、任务书编辑框已删"""
    fake.plan(claude=[{"reply": REQ1}, {"reply": REQ2}])
    proj = add_project(board, repo)
    card = start(board, proj)
    win.select(card.id)
    wait_review(qtbot, card)
    win.render()
    qtbot.wait(300)
    d = win.drawer
    assert not hasattr(d, "orig")  # 需求原文已删
    assert "🎯 完成目标" in d.goal.text() and "新建 hello.txt" in d.goal.text()
    assert "🛠 方案" in d.plan.text() and "① 写 hello.txt" in d.plan.text()
    assert "② " in d.plan.text() and "❓1" in d.plan.text() and PENDING_MARK_STYLE in d.plan.text()
    assert "末尾要换行吗" in d.pend.text() and "待你确认" not in d.plan.text()
    assert not any(hasattr(d, n) for n in ("und", "q_box", "qs", "warn", "spec_edit"))
    grab(win, "5-req-sections")
    d.answer.setPlainText("要换行")
    d.reply_btn.click()
    qtbot.waitUntil(lambda: card.stage.status == "review" and len(card.stage.rounds) == 2, timeout=WAIT)
    win.render()
    assert "待你确认" not in d.plan.text() and "新建 hello.txt，末尾换行" in d.goal.text()


def fake_probe(env):
    return {"clis": {"claude": {"path": "/x/claude", "version": "2.1.311", "error": ""},
                     "codex": {"path": "/x/codex", "version": "0.152.1", "error": ""},
                     "grok": {"path": "", "version": "", "error": ""}},
            "models": {"claude": clis.parse_claude_help((SAMPLES / "claude-help.txt").read_text()),
                       "codex": clis.parse_codex_models(json.loads((SAMPLES / "codex-models.json").read_text()))},
            "at": 1793000000}


def test_agent_dialog_probe(qtbot, board):
    dlg = AgentDialog(board, prober=fake_probe)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: not dlg._probing, timeout=WAIT)
    texts = [dlg.cli.itemText(i) for i in range(dlg.cli.count())]
    assert texts == ["Claude · 2.1.311", "Codex · 0.152.1", "Grok · 未安装"]
    assert not dlg.cli.model().item(2).isEnabled()
    assert json.loads((board.data_dir / "clis.json").read_text())["clis"]["codex"]["version"] == "0.152.1"
    # 选到 codex 模板：实时模型列表，默认项带后缀，推理强度随模型变
    row = next(i for i, a in enumerate(board.agents) if a.cli == "codex")
    dlg.list.setCurrentRow(row)
    items = [dlg.model.itemText(i) for i in range(dlg.model.count())]
    assert items == ["gpt-5.6-sol（默认）", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert "codex app-server" in dlg.models_note.text()
    dlg.model.setCurrentIndex(0)
    assert dlg._model_text() == "gpt-5.6-sol"
    assert dlg.effort.findData("ultra") >= 0
    dlg.model.setCurrentIndex(2)
    assert dlg._model_text() == "gpt-5.6-luna" and dlg.effort.findData("ultra") < 0
    # 按钮文字完整：宽度不小于文字宽度
    assert dlg.conn_btn.width() >= dlg.conn_btn.fontMetrics().horizontalAdvance("测试连接")
    grab(dlg, "6-agent-dialog")
    # 切到没装的 grok（代码里切）：提示本机没装
    dlg.cli.setCurrentIndex(2)
    assert "本机没装 Grok" in dlg.models_note.text()


def test_agent_dialog_keeps_unknown_effort(qtbot, board):
    """模板里存的强度不在实时列表里：保留并标出来，不悄悄丢掉"""
    board.set_cli_cache(fake_probe({}))
    a = next(a for a in board.agents if a.cli == "codex")
    a.model, a.effort = "gpt-5.6-luna", "ultra"
    dlg = AgentDialog(board, prober=None)
    qtbot.addWidget(dlg)
    dlg.list.setCurrentRow(board.agents.index(a))
    assert dlg.effort.currentData() == "ultra" and "该模型列表里没有" in dlg.effort.currentText()


def test_agent_dialog_probe_failure(qtbot, board):
    def bad(env):
        raise RuntimeError("PATH 坏了")
    dlg = AgentDialog(board, prober=bad)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg._probing, timeout=WAIT)
    assert "检测本机 CLI 失败" in dlg.models_note.text() and "PATH 坏了" in dlg.models_note.text()


def test_agent_dialog_macos_style_not_squeezed(qtbot, board, win):
    """macOS 原生风格下（控件更高）表单行不被压扁，「测试连接」按钮完整"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    old = app.style().name()
    app.setStyle("macOS")
    try:
        board.set_cli_cache(fake_probe({}))
        dlg = AgentDialog(board, win, prober=None)
        qtbot.addWidget(dlg)
        dlg.show()
        qtbot.wait(100)
        for w in (dlg.conn_btn, dlg.cli, dlg.model, dlg.effort, dlg.perm, dlg.models_note):
            assert w.height() >= w.minimumSizeHint().height(), w
        assert dlg.models_note.height() >= dlg.models_note.heightForWidth(dlg.models_note.width())
        assert dlg.conn_btn.width() >= dlg.conn_btn.sizeHint().width()
        grab(dlg, "7-agent-dialog-macos")
    finally:
        app.setStyle(old)

"""主 agent 三阶段续聊、需求固定进卡、额外角色、交付记录写回的组织核对、
MCP 能力挂载、模板能力往返与老库补列、预置模板补种、自动拉取节流、点抽屉外收起。
不连真 Azure（假 HTTP）；CLI 用假脚本、git 用真的。"""
import sqlite3
import tomllib
import time

import pytest
from PySide6.QtCore import QPropertyAnimation, Qt
from PySide6.QtTest import QTest

from donedef.azure import text_to_html
from donedef.board import AI_OFF, DB_FILENAME, NO_ADO, NOT_TASK, RESTART_LINE, Board
from donedef.cli import NO_INDEX, NO_REPO_INDEX, build_cmd, mcp_servers
from donedef.dialogs import AgentDialog
from donedef.model import Agent, WorkItem
from donedef.ui import AUTO_PULL_MS, MainWindow
from tests.test_core import REQ2, WAIT, add_project, dispose, env_for, git_out, grab, wait_review
from tests.test_azure import ORG, PAT, PROJ, FakeAzure, azure_board
from tests.test_lanes import PBI, choose_all, connect, keys, lanes_router

pytest_plugins = ["tests.test_core"]

ROLE = "你是严谨的开发。"


@pytest.fixture
def pat(tmp_path):
    f = tmp_path / "personal-pat.txt"
    f.write_text(PAT + "\n")
    return f


@pytest.fixture
def lwin(qtbot, board):
    w = MainWindow(board, autorun=False, vscode=lambda path: None)
    qtbot.addWidget(w)
    w.resize(1440, 900)
    w.show()
    yield w
    w._quitting = True


def main_agent(board):
    """主 agent 用的模板：claude、可以改、只有角色说明"""
    board.agents.append(Agent("m1", "主开发", "claude", "edit", ROLE))
    return "m1"


def arg(args, flag):
    return args[args.index(flag) + 1]


def prompt_of(call):
    return arg(call["args"], "-p")


# ---------- 主 agent 三阶段 + 续聊 ----------
def test_main_agent_three_stages_resume(qtbot, board, fake, repo):
    fake.plan(claude=[
        {"reply": REQ2, "session": "C1"},
        {"reply": "做好了\n改了什么：\n- 新建 hello.txt", "session": "C1", "write": {"hello.txt": "hi\n"}},
        {"reply": "都做到了\n结论：通过", "session": "C1"}])
    proj = add_project(board, repo)
    item = board.new_manual_item("新建 hello.txt", "在仓库根目录新建 hello.txt，内容 hi", "")
    card, err = board.start_card(item, proj.id, main_agent(board))
    assert err is None, err
    assert board.main_mode(card) and [s.agent_id for s in card.stages] == ["m1", "m1", "m1"]

    # 需求：强制只读、新会话；角色说明 + 内置需求指令 + 需求 + JSON 要求
    wait_review(qtbot, card, "req")
    c1 = fake.calls("claude")[0]
    assert arg(c1["args"], "--permission-mode") == "dontAsk" and "--resume" not in c1["args"]
    p1 = prompt_of(c1)
    assert p1.startswith(f"【你的角色】\n{ROLE}\n\n【当前阶段：需求分析】只读，不要改任何文件。")
    assert "【任务】L1 新建 hello.txt" in p1 and "【需求】\n在仓库根目录新建 hello.txt，内容 hi" in p1
    assert "【验收标准】\n（没写验收标准，请在方案里用（待你确认：…）问我做到什么程度算完成）" in p1 and '"plan"' in p1 and '"goal"' in p1
    assert board.confirm_spec(card, "新建 hello.txt，内容 hi")

    # 开发：续聊需求阶段的会话，用模板的权限档；要求最后列「改了什么」
    wait_review(qtbot, card, "dev")
    c2 = fake.calls("claude")[1]
    assert arg(c2["args"], "--resume") == "C1" and arg(c2["args"], "--permission-mode") == "acceptEdits"
    p2 = prompt_of(c2)
    assert "【当前阶段：开发】照下面的需求和确认的方案实现" in p2 and "【确认的方案】\n新建 hello.txt，内容 hi" in p2 and "任务书" not in p2
    assert p2.endswith("回复最后用「改了什么：」开头，每行「- 」列出一项改动。")
    assert board.commit_and_pass(card) is None

    # 验证：继续续聊；给基准分支和改动统计；要求最后一行给结论
    wait_review(qtbot, card, "verify")
    c3 = fake.calls("claude")[2]
    assert arg(c3["args"], "--resume") == "C1" and arg(c3["args"], "--permission-mode") == "acceptEdits"
    p3 = prompt_of(c3)
    assert "【当前阶段：验证】照需求和确认的方案核对本卡的改动" in p3
    assert "改动在当前分支 feature/task-l1，基准分支 main，可以运行 git diff main...HEAD 查看。" in p3
    assert "【改动统计】\n" in p3 and "hello.txt" in p3.split("【改动统计】")[1]
    assert p3.endswith("回复最后一行只写「结论：通过」或「结论：不通过」。")
    assert board.pass_stage(card) and card.column == "done" and card.comment == "skip"  # 手写卡不写回
    assert git_out(repo, "log", "-1", "--format=%s", "feature/task-l1") == "L1 开发：新建 hello.txt"


def test_main_agent_new_session_without_id(qtbot, board, fake, repo):
    """上一阶段拿不到会话 ID：下一阶段新开会话，不带 --resume"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"reply": "做好了", "session": "C2"}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, main_agent(board))
    wait_review(qtbot, card, "req")
    card.stages[0].session_id = ""
    assert board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")
    assert "--resume" not in fake.calls("claude")[1]["args"] and card.stage.session_id == "C2"


def test_legacy_card_new_session_per_stage(qtbot, board, fake, repo):
    """老方式（三阶段各一个模板）：进下一阶段不续聊"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}], grok=[{"reply": "好了", "session": "G1"}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, ["a5", "a2", "a4"])
    assert not board.main_mode(card)
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "spec")
    wait_review(qtbot, card, "dev")
    assert "-r" not in fake.calls("grok")[0]["args"]


# ---------- 只有 Task 跑 AI、需求固定进卡、写回前核对组织 ----------
def test_parent_requirement_frozen_and_writeback_org_check(qtbot, tmp_path, fake, pat):
    http = FakeAzure()
    b = azure_board(tmp_path, fake, pat, http)
    try:
        b.set_azure(ORG, PROJ)
        b.set_ai_allowed(ORG, True)
        aid = main_agent(b)
        parent = WorkItem("30", PBI, "订单记录", "Committed", "用户要能导出订单记录", "- 能按月导出", "azure")
        # PBI / Issue 不能开始（程序层）
        for t in (PBI, "Issue"):
            assert b.start_card(WorkItem("30", t, "订单记录", "Committed", "x", "", "azure"), None, aid) == (
                None, NOT_TASK.format(type=t))
        assert b.cards == []

        fake.plan(claude=[{"reply": REQ2, "session": "C1"}])
        item = WorkItem("40", "Task", "写导出接口", "To Do", "Task 自己的描述", "", "azure")
        card, err = b.start_card(item, None, aid, parent=parent)
        assert err is None, err
        # 需求取自父项并固定进本卡；父项之后再改不影响
        want = ("（来自 Product Backlog Item #30 订单记录）\n用户要能导出订单记录", "- 能按月导出")
        assert (card.item.description, card.item.acceptance) == want
        parent.description = "父项后来改了"
        assert card.item.description == want[0]
        assert b.meta(card) == {"main": True, "ado": f"{ORG}/{PROJ}"}
        wait_review(qtbot, card, "req")
        p = prompt_of(fake.calls("claude")[0])
        assert f"【需求】\n{want[0]}" in p and "【验收标准】\n- 能按月导出" in p and "Task 自己的描述" not in p
        assert "【任务】#40 写导出接口" in p

        # 重开程序：固定的需求和开始时的组织都还在
        b.save()
        b2 = Board(tmp_path / "data", env=env_for(fake))
        try:
            c2 = b2.find_card("40")
            assert (c2.item.description, c2.item.acceptance) == want and b2.meta(c2)["ado"] == f"{ORG}/{PROJ}"
        finally:
            dispose(b2)

        # 不关联仓库的卡确认需求并结束 → 只含方案的交付记录
        assert b.finish_after_req(card, "做按月导出")
        assert card.comment_text.splitlines() == [
            f"—— 定标 DoneDef 交付记录 · {time.strftime('%Y-%m-%d')} ——",
            "结论：需求已确认，未进入开发", "方案：", "做按月导出"]

        # 切到别的组织：拒绝写回，不发请求
        b.set_azure("other-org", "x")
        assert b.writeback_block(card) == (f"这张卡是在 {ORG}/{PROJ} 开始的，当前 Azure 设置是 other-org/x，"
                                           "不能写回（切回原组织 / 项目再写）")
        assert not b.send_writeback(card, card.comment_text)
        assert b.writeback_errors[card.id] == b.writeback_block(card) and http.requests == []
        # 切回来但组织不允许 AI：也拒绝
        b.set_azure(ORG, PROJ)
        b.set_ai_allowed(ORG, False)
        assert b.writeback_block(card) == AI_OFF and not b.send_writeback(card, card.comment_text)
        assert http.requests == []
        # 都对上了才写
        b.set_ai_allowed(ORG, True)
        assert b.writeback_block(card) == ""
        http.push(("ok", 200, {"id": 40, "rev": 3, "fields": {"System.Description": "<p>原描述</p>"}}),
                  ("ok", 200, {"id": 40, "rev": 4, "fields": {}}))
        assert b.send_writeback(card, card.comment_text)
        qtbot.waitUntil(lambda: card.id not in b.sending, timeout=WAIT)
        assert card.comment == "sent" and [r["method"] for r in http.requests] == ["GET", "PATCH"]
        assert http.requests[1]["body"][1]["value"] == "<p>原描述</p><br><br>" + text_to_html(card.comment_text)
    finally:
        b.stop_all()
        dispose(b)


# ---------- 额外角色 ----------
def test_helpers_order_readonly_failure_rerun(qtbot, board, fake, repo):
    fake.plan(
        claude=[{"reply": REQ2, "session": "C1"}, {"exit": 1, "stderr": "boom", "no_output": True},
                {"reply": REQ2, "session": "C1"}, {"reply": "找茬意见 2", "session": "H2"}],
        codex=[{"reply": "审查意见 1", "session": "X1"}, {"reply": "审查意见 2", "session": "X2"}])
    proj = add_project(board, repo)
    card, err = board.start_card(board.new_manual_item("t", "在根目录新建 a.txt", ""), proj.id, main_agent(board),
                                 helpers=[("req", "p3"), ("req", "p5")])
    assert err is None, err
    wait_review(qtbot, card, "req")
    st = card.stage
    # 按添加顺序：主 agent → 需求找茬（claude）→ 代码审查（codex）
    assert [c["cli"] for c in fake.calls()] == ["claude", "claude", "codex"]
    # 第一个额外角色失败：记原因，后面的照跑，不拦确认需求
    assert [h.status for h in st.helpers] == ["failed", "done"] and st.flag is None
    assert st.helpers[0].error and st.helpers[1].reply == "审查意见 1"
    assert board.can_confirm_req(card)
    # 额外角色：只读、新会话；提示词 = 角色说明 + 只读给意见 + 需求 + 主 agent 最新回复
    h1 = fake.calls("claude")[1]["args"]
    assert arg(h1, "--permission-mode") == "dontAsk" and "--resume" not in h1
    ph = arg(h1, "-p")
    assert ph.startswith("【你的角色】\n你是挑剔的需求评审。")
    assert "主 agent 刚完成需求阶段。你只读，不要改任何文件，从你的角色给出意见。" in ph
    assert "【需求】\n在根目录新建 a.txt" in ph and "【主 agent 最新回复】\n" + REQ2 in ph
    x1 = fake.calls("codex")[0]["args"]
    assert arg(x1, "-s") == "read-only" and "resume" not in x1
    # 代码审查模板勾了代码检索，但仓库没建索引：不挂，输出里提示
    assert not any("mcp_servers" in a for a in x1)
    assert f"[代码审查] {NO_INDEX.format(path=str(repo))}" in st.output

    # 回答疑问 = 主 agent 再跑一轮 → 额外角色清掉重跑，只保留最新一次
    assert board.ask(card, "要换行")
    wait_review(qtbot, card, "req")
    assert [c["cli"] for c in fake.calls()] == ["claude", "claude", "codex", "claude", "claude", "codex"]
    assert [(h.status, h.reply, h.error) for h in st.helpers] == [("done", "找茬意见 2", ""), ("done", "审查意见 2", "")]


def test_helper_stop_skips_rest(qtbot, board, fake, repo):
    """额外角色在跑时点停止：它停、后面的不跑，卡进待确认，主 agent 这一轮不算失败"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"reply": "x", "sleep": 30}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, main_agent(board),
                               helpers=[("req", "p3"), ("req", "p5")])
    qtbot.waitUntil(lambda: card.stage.running_helper() is not None and len(fake.calls("claude")) == 2, timeout=WAIT)
    assert card.stage.status == "checking"
    assert board.stop(card)
    wait_review(qtbot, card, "req")
    st = card.stage
    assert [h.status for h in st.helpers] == ["stopped", "pending"] and fake.calls("codex") == []
    assert st.flag is None and st.rounds[0].end is None and board.can_confirm_req(card)


# ---------- MCP 能力挂载 ----------
def test_mcp_servers_and_cli_args(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    # 没建索引：不挂，给原因；不关联仓库：不挂代码检索
    assert mcp_servers(["codegraph"], str(repo)) == ({}, [NO_INDEX.format(path=str(repo))])
    assert mcp_servers(["codegraph"], None) == ({}, [NO_REPO_INDEX])
    (repo / ".codegraph").mkdir()
    servers, notes = mcp_servers(["codegraph", "playwright"], str(repo))
    assert notes == [] and servers == {
        "ab_codegraph": {"command": "codegraph", "args": ["serve", "--mcp", "--path", str(repo), "--no-watch"]},
        "ab_playwright": {"command": "npx", "args": ["@playwright/mcp@latest", "--headless", "--isolated"]}}

    # claude：--mcp-config JSON + 显式放行 MCP 工具（只读档下也要）
    c = build_cmd("claude", "read", "", "", "hi", mcp=servers)
    i = c.index("--mcp-config")
    import json
    assert json.loads(c[i + 1]) == {"mcpServers": servers}
    assert c[i + 2:] == ["--allowedTools", "mcp__ab_codegraph", "mcp__ab_playwright"]
    # codex：-c mcp_servers.<名>.command / args，值是合法 TOML；续聊也带
    for sid in ("", "T1"):
        x = build_cmd("codex", "read", "", "", "hi", sid, "/tmp/o.txt", servers)
        cfg = {}
        for k, v in zip(x, x[1:]):
            if k == "-c" and v.startswith("mcp_servers."):
                part = tomllib.loads(v)["mcp_servers"]
                for name, d in part.items():
                    cfg.setdefault(name, {}).update(d)
        assert cfg == servers
    # grok：没有单次运行挂 MCP 的参数，不挂
    g = build_cmd("grok", "read", "", "", "hi", mcp=servers)
    assert not any("mcp" in a for a in g)
    # 没有能力：命令里没有 MCP 参数
    assert "--mcp-config" not in build_cmd("claude", "read", "", "", "hi")


def test_mcp_for_card(board, repo):
    """按模板能力挂：grok 不挂；不关联仓库的卡不挂代码检索；仓库有索引才挂"""
    proj = add_project(board, repo)
    (repo / ".codegraph").mkdir()
    card = type("C", (), {"project_id": proj.id})()
    no_repo = type("C", (), {"project_id": None})()
    cg = Agent("g", "g", "grok", "read", "", caps=["codegraph"])
    cc = Agent("c", "c", "claude", "read", "", caps=["codegraph"])
    assert board.mcp_for(card, cg) == ({}, [])
    assert list(board.mcp_for(card, cc)[0]) == ["ab_codegraph"]
    assert board.mcp_for(no_repo, cc) == ({}, [NO_REPO_INDEX])
    assert board.mcp_for(card, Agent("n", "n", "claude", "read", "")) == ({}, [])


# ---------- 模板能力往返、老库补列 / 补预置模板 ----------
def test_agent_dialog_caps_roundtrip(qtbot, board, tmp_path, fake):
    dlg = AgentDialog(board, prober=None)
    qtbot.addWidget(dlg)
    row = next(i for i, a in enumerate(board.agents) if a.id == "p3")  # 需求找茬：没有能力
    dlg.list.setCurrentRow(row)
    assert not dlg.caps["codegraph"].isChecked() and dlg.caps["codegraph"].isEnabled()
    dlg.caps["codegraph"].setChecked(True)
    dlg.caps["playwright"].setChecked(True)
    dlg._save()
    assert board.agent("p3").caps == ["codegraph", "playwright"]
    # 换到别的模板再回来：勾选照模板显示
    dlg.list.setCurrentRow(next(i for i, a in enumerate(board.agents) if a.id == "p6"))
    assert [k for k, box in dlg.caps.items() if box.isChecked()] == ["playwright"]
    dlg.list.setCurrentRow(row)
    assert all(box.isChecked() for box in dlg.caps.values())
    # 选 Grok：能力不可勾，写明原因
    dlg.cli.setCurrentIndex(dlg.cli.findData("grok"))
    assert not any(box.isEnabled() for box in dlg.caps.values()) and "Grok 暂不支持" in dlg.caps_note.text()
    dlg.cli.setCurrentIndex(dlg.cli.findData("claude"))
    assert all(box.isEnabled() for box in dlg.caps.values()) and dlg.caps_note.text() == ""
    grab(dlg, "p12-agent-caps")
    # 落库往返
    board.save()
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        assert b2.agent("p3").caps == ["codegraph", "playwright"] and b2.agent("p1").caps == ["codegraph"]
    finally:
        dispose(b2)


@pytest.mark.real_seed
def test_old_db_gets_caps_column_and_presets(qtbot, tmp_path, fake):
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(str(data / DB_FILENAME))
    with conn:
        conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, ord INTEGER, name TEXT, cli TEXT, perm TEXT, "
                     "prompt TEXT, model TEXT, effort TEXT)")  # 早期的表结构
        conn.executemany("INSERT INTO agents VALUES (?,?,?,?,?,?,?,?)", [
            ("p2", 0, "我的模板", "grok", "edit", "随便写", "", ""),
            ("k", 1, "架构师", "codex", "read", "我自己写的架构师", "", "")])
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    b = Board(data, env=env_for(fake))
    try:
        # 同名的「架构师」不覆盖；「测试」的 ID p2 被占了就换一个；老模板能力为空
        assert [(a.id, a.name) for a in b.agents] == [
            ("p2", "我的模板"), ("k", "架构师"), ("p7", "测试"), ("p3", "需求找茬"), ("p4", "开发"),
            ("p5", "代码审查"), ("p6", "原型审核")]
        assert b.agent("k").prompt == "我自己写的架构师" and b.agent("k").caps == [] and b.agent("p2").caps == []
        assert b.agent("p7").caps == ["codegraph"]
        b.agents = [a for a in b.agents if a.name != "测试"]  # 你删掉一个预置模板
        b.save()
    finally:
        dispose(b)
    cols = [r[1] for r in sqlite3.connect(str(data / DB_FILENAME)).execute("PRAGMA table_info(agents)")]
    assert cols[-1] == "caps"
    b2 = Board(data, env=env_for(fake))
    try:
        # 只补一次：删掉的不会再冒出来
        assert "测试" not in [a.name for a in b2.agents] and len(b2.agents) == 6
        assert b2.agent("p6").caps == ["playwright"]
    finally:
        dispose(b2)


# ---------- 界面：从 PBI 卡里的 Task 开始、自动拉取、点抽屉外收起 ----------
def test_ui_start_task_with_parent_requirement(qtbot, board, lwin, fake, pat, repo):
    win = lwin
    proj = add_project(board, repo)
    connect(board, win, pat, lanes_router(), ai=True)
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}])
    win.select("22")
    d = win.drawer
    qtbot.waitUntil(lambda: "能按月导出" in d.preview.text(), timeout=WAIT)
    choose_all(d, proj.id)
    assert d.start_btn.isEnabled(), d.start_hint.text()
    d.start_btn.click()
    card = board.find_card("22")
    assert card is not None and board.main_mode(card)
    assert card.item.description == f"（来自 {PBI} #30 订单记录）\n用户要能导出订单记录"
    assert card.item.acceptance == "- 能按月导出\n- CSV 格式"
    wait_review(qtbot, card, "req")
    # 开始后 Task 行带 AI 徽标，仍在 PBI #30 卡里
    assert [r.key for r in win.card_widget("30").rows] == ["22"] and win.card_widget("22").card is card
    assert "需求待确认" in win.card_widget("22").badge.text()


def test_auto_pull_throttle(qtbot, board, lwin, pat):
    win = lwin
    t = [1000.0]
    win.clock = lambda: t[0]
    assert win.pull_timer.interval() == AUTO_PULL_MS == 5 * 60 * 1000 and not win.pull_timer.isActive()
    assert not win.auto_pull()  # 没配 Azure 不拉
    r = lanes_router()
    connect(board, win, pat, r)
    assert win._job is not None and not win.auto_pull()  # 正在拉不重复
    qtbot.waitUntil(lambda: win._job is None and keys(win, "todo") == ["23"], timeout=WAIT)

    def wiql():
        return sum("/wiql" in q["url"] for q in r.requests)
    n = wiql()
    t[0] += 59
    assert not win.auto_pull() and wiql() == n  # 距上次拉取不到 60 秒
    t[0] += 2
    assert win.auto_pull()
    qtbot.waitUntil(lambda: win._job is None, timeout=WAIT)
    assert wiql() == n + 1
    # 回到前台：同样受 60 秒限制；切到后台不拉
    win.on_app_state(Qt.ApplicationActive)
    assert win._job is None and wiql() == n + 1
    t[0] += 61
    win.on_app_state(Qt.ApplicationInactive)
    assert win._job is None
    win.on_app_state(Qt.ApplicationActive)
    assert win._job is not None
    qtbot.waitUntil(lambda: win._job is None, timeout=WAIT)
    assert wiql() == n + 2


def test_click_outside_closes_drawer(qtbot, board, lwin, pat):
    win = lwin
    connect(board, win, pat, lanes_router())
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    d = win.drawer

    def click(w):
        QTest.mouseClick(w, Qt.LeftButton)
        qtbot.waitUntil(lambda: d.anim.state() != QPropertyAnimation.Running, timeout=WAIT)  # 等滑入 / 滑出动画走完

    click(win.card_widget("22"))
    assert win.selected == "22" and d.is_open and d.x() < win.main.width()
    # 点抽屉里面：不收
    click(d.title)
    assert win.selected == "22" and d.is_open
    # 点另一张卡 / PBI 标题：切过去，不收
    click(win.card_widget("23"))
    assert win.selected == "23" and d.is_open
    click(win.card_widget("30").head)
    assert win.selected == "30" and d.is_open
    # 点列头（To Do 在左边，没被抽屉盖住）：收起
    head = win.columns["todo"]
    assert head.mapTo(win.main, head.rect().center()).x() < d.x()
    click(head)
    assert win.selected is None and not d.is_open
    # 点看板空白处：收起
    click(win.card_widget("21"))
    assert d.is_open
    click(win.board_scroll.widget())
    assert win.selected is None and not d.is_open


def test_req_page_shows_helper_opinions(qtbot, board, lwin, fake, repo):
    """需求阶段没有「AI 的反馈」页签：额外角色的意见（含失败原因）显示在需求页"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"exit": 1, "stderr": "boom"}],
              codex=[{"reply": "接口名建议统一", "session": "X1"}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, main_agent(board),
                               helpers=[("req", "p3"), ("req", "p5")])
    wait_review(qtbot, card, "req")
    lwin.select(card.id)
    d = lwin.drawer
    assert d.req_helpers_box.isVisibleTo(d)
    text = d.req_helpers.text()
    assert "需求找茬" in text and "失败" in text and "代码审查" in text and "接口名建议统一" in text
    # 没加额外角色的卡不显示这块
    fake.plan(claude=[{"reply": REQ2, "session": "C2"}])
    c2, _ = board.start_card(board.new_manual_item("t2", "d", ""), proj.id, "m1")
    wait_review(qtbot, c2, "req")
    lwin.select(c2.id)
    assert not d.req_helpers_box.isVisibleTo(d)



# ---------- 续聊失败回退 / 额外角色在跑时退出 ----------
def test_resume_failure_falls_back_to_new_session(qtbot, board, fake, repo):
    """续聊失败又没拿到新会话（会话被清理 / 换了目录）：清掉会话 ID，下次发话新开会话、带完整提示词，不再反复续"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"},
                      {"exit": 1, "no_output": True, "stderr": "No conversation found with session ID: C1"},
                      {"reply": "做好了", "session": "C9"}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, main_agent(board))
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "新建 a.txt")
    wait_review(qtbot, card, "dev")
    assert arg(fake.calls("claude")[1]["args"], "--resume") == "C1"
    assert card.stage.flag == "failed" and card.stage.session_id == ""
    assert board.ask(card, "再试一次")
    wait_review(qtbot, card, "dev")
    c3 = fake.calls("claude")[2]["args"]
    p3 = arg(c3, "-p")
    assert "--resume" not in c3 and "【确认的方案】\n新建 a.txt" in p3 and p3.endswith("补充：再试一次")
    assert card.stage.session_id == "C9" and card.stage.flag is None


def test_attach_repo_starts_new_session(qtbot, board, fake, repo):
    """不关联仓库的卡补选仓库：工作目录从 scratch 换到 worktree，claude 会话按目录存续不上 → 开发阶段新开会话"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"reply": "做好了", "session": "C2"}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), None, main_agent(board))
    wait_review(qtbot, card, "req")
    assert board.attach_repo(card, proj.id, "新建 a.txt") is None
    wait_review(qtbot, card, "dev")
    c2 = fake.calls("claude")[1]
    assert "--resume" not in c2["args"] and "【确认的方案】\n新建 a.txt" in arg(c2["args"], "-p")
    assert card.stage.session_id == "C2"


def test_writeback_refuses_card_without_org_and_is_idempotent(qtbot, tmp_path, fake, pat):
    http = FakeAzure()
    b = azure_board(tmp_path, fake, pat, http)
    try:
        b.set_azure(ORG, PROJ)
        b.set_ai_allowed(ORG, True)
        fake.plan(claude=[{"reply": REQ2, "session": "C1"}])
        card, _ = b.start_card(WorkItem("40", "Task", "写导出接口", "To Do", "d", "", "azure"), None, main_agent(b))
        wait_review(qtbot, card, "req")
        assert b.finish_after_req(card, "做按月导出")
        # 没记下开始时的组织（早期开始的老卡）：确认不了写回目标 → 拒绝、不发请求
        b.db.set_setting(f"card_meta:{card.id}", "{}")
        b._meta.pop(card.id)
        assert b.writeback_block(card) == NO_ADO and not b.send_writeback(card, card.comment_text)
        assert http.requests == []
        b.db.set_setting(f"card_meta:{card.id}", '{"main": true, "ado": "%s/%s"}' % (ORG, PROJ))
        b._meta.pop(card.id)
        # 上次 PATCH 其实成功了（描述里已有这份记录）：再发不重复追加，按已写回处理
        done = "<p>原描述</p><br><br>" + text_to_html(card.comment_text)
        http.push(("ok", 200, {"id": 40, "rev": 5, "fields": {"System.Description": done}}))
        assert b.send_writeback(card, card.comment_text)
        qtbot.waitUntil(lambda: card.id not in b.sending, timeout=WAIT)
        assert card.comment == "sent" and [r["method"] for r in http.requests] == ["GET"]
    finally:
        b.stop_all()
        dispose(b)


def test_quit_or_restart_while_helper_running(qtbot, board, fake, repo, tmp_path):
    """额外角色在跑时退出程序 / 重启：只停这个角色，主 agent 这一轮不算停止，照样能确认需求"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"reply": "x", "sleep": 30}])
    proj = add_project(board, repo)
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), proj.id, main_agent(board),
                               helpers=[("req", "p3"), ("req", "p5")])
    qtbot.waitUntil(lambda: card.stage.running_helper() is not None and len(fake.calls("claude")) == 2, timeout=WAIT)
    board.stop_all()
    st = card.stage
    assert [h.status for h in st.helpers] == ["stopped", "pending"] and st.status == "review"
    assert st.flag is None and st.rounds[0].end is None and board.can_confirm_req(card)

    # 重启恢复：上次退出时额外角色还在跑（直接改库里的状态模拟崩溃）
    st.status, st.helpers[0].status, st.helpers[0].error = "checking", "running", ""
    board.save()
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        s2 = b2.find_card(card.id).stage
        assert [h.status for h in s2.helpers] == ["stopped", "pending"] and s2.helpers[0].error == RESTART_LINE
        assert s2.status == "review" and s2.flag is None and s2.rounds[0].end is None
        assert b2.can_confirm_req(b2.find_card(card.id))
    finally:
        dispose(b2)


def test_start_after_details_cleared_shows_hint(qtbot, board, lwin, fake, pat, repo):
    """自动拉取清掉了需求详情、开始按钮还没刷新：点开始不静默，写明在等详情并补拉"""
    win = lwin
    proj = add_project(board, repo)
    connect(board, win, pat, lanes_router(), ai=True)
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    win.select("22")
    d = win.drawer
    qtbot.waitUntil(lambda: "能按月导出" in d.preview.text(), timeout=WAIT)
    choose_all(d, proj.id)
    win.clear_details()
    d._start()
    assert board.find_card("22") is None and "等需求详情取到" in d.start_hint.text()
    qtbot.waitUntil(lambda: "30" in win.details, timeout=WAIT)  # 补拉回来后能开始
    qtbot.waitUntil(lambda: d.start_btn.isEnabled(), timeout=WAIT)

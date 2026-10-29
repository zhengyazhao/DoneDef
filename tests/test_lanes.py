"""看板泳道测试：AI 开关按组织、PBI 泳道、改 Azure 状态、任务书四节、四问工作页、代码改动、VS Code。
不连真 Azure（假 HTTP）；CLI 用假脚本、git 用真的。"""
import subprocess
import urllib.error

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QLabel

from donedef import azure, worktree
from donedef.azure import CONFLICT, AzureError, Brief, Client, ConflictError
from donedef.board import AI_OFF, NOT_TASK
from donedef.model import state_column
from donedef.ui import ASK_FOLD, MainWindow, open_vscode
from tests.test_core import IDS, REQ2, WAIT, add_project, git_out, grab, make_repo, sh, wait_review
from tests.test_azure import ITERS, ORG, PROJ, PAT, PROJECT, Router, client, wiql_of

pytest_plugins = ["tests.test_core"]

# Scrum 流程 Task 的默认状态；Ready 是为测「按分类归列」加的：按名会落到 Doing
TASK_STATES = {"count": 6, "value": [
    {"name": "To Do", "category": "Proposed"}, {"name": "Ready", "category": "Proposed"},
    {"name": "In Progress", "category": "InProgress"}, {"name": "Test", "category": "InProgress"},
    {"name": "Done", "category": "Completed"}, {"name": "Removed", "category": "Removed"}]}
PBI = "Product Backlog Item"
ITEM30 = {"id": 30, "rev": 4, "fields": {
    "System.Title": "订单记录", "System.WorkItemType": PBI, "System.State": "Committed",
    "System.Description": "<p>用户要能导出订单记录</p>",
    "Microsoft.VSTS.Common.AcceptanceCriteria": "<ul><li>能按月导出</li><li>CSV 格式</li></ul>"}}


def batch(*rows):
    """workitemsbatch 的返回：rows = (卡号, 类型, 状态, 标题, 父项号, rev)"""
    return {"count": len(rows), "value": [
        {"id": i, "rev": rev, "fields": {"System.Id": i, "System.WorkItemType": t, "System.State": st, "System.Title": ti,
                                         **({"System.Parent": p} if p else {}), "System.Rev": rev}}
        for i, t, st, ti, p, rev in rows]}


def item(num, title, state, wtype="Task"):
    return {"id": num, "rev": 1, "fields": {"System.Title": title, "System.WorkItemType": wtype, "System.State": state,
                                            "System.Description": f"<p>{title}的描述</p>"}}


def lanes_router():
    """我的卡：PBI #20（是 #21 的父项）、Task #21（父 #20）、Task #22（父 #30，不是指派给我的）、Task #23（没有父项）。
    PBI 的状态列表取失败（500），Task 的取到"""
    return Router(
        ("/_apis/projects/", PROJECT, None), ("teamsettings/iterations", ITERS, None), ("Sprint 1", wiql_of(20, 21, 22, 23), None),
        ('"ids": [30]', batch((30, PBI, "Committed", "订单记录", None, 4)), None),
        ("workitemsbatch", batch((20, PBI, "Committed", "导出功能", None, 2), (21, "Task", "In Progress", "写导出接口", 20, 3),
                                 (22, "Task", "Test", "导出页面", 30, 7), (23, "Task", "Ready", "整理文档", None, 5)), None),
        ("workitemtypes/Task/states", TASK_STATES, None),
        ("Product%20Backlog%20Item/states", (500, b'{"message": "VS000: boom"}'), None),
        ("/workitems/30?", ITEM30, None), ("/workitems/20?", item(20, "导出功能", "Committed", PBI), None),
        ("/workitems/21?", item(21, "写导出接口", "In Progress"), None), ("/workitems/22?", item(22, "导出页面", "Test"), None),
        ("/workitems/23?", item(23, "整理文档", "Ready"), None))


@pytest.fixture
def pat(tmp_path):
    f = tmp_path / "personal-pat.txt"
    f.write_text(PAT + "\n")
    return f


@pytest.fixture
def vs_calls():
    return []


@pytest.fixture
def lwin(qtbot, board, vs_calls):
    """VS Code 启动函数注入：记下打开的目录，不真开"""
    def fake_vscode(path):
        vs_calls.append(path)
        return "打不开 VS Code：找不到应用程序" if path.endswith("broken") else None
    w = MainWindow(board, autorun=False, vscode=fake_vscode)
    qtbot.addWidget(w)
    w.resize(1440, 900)
    w.show()
    yield w
    w._quitting = True


def connect(board, win, pat, http, ai=False):
    board.ado = Client(pat, opener=http)
    board.set_azure(ORG, PROJ)
    board.set_ai_allowed(ORG, ai)
    win.load_iterations()


def keys(win, col):
    return [w.key for w in win.columns[col].cards]


def pick_state(win, name):
    cb = win.drawer.state_cb
    i = cb.findText(name)
    assert i >= 0, name
    cb.setCurrentIndex(i)
    cb.activated.emit(i)


def pick(cb, value):
    i = cb.findData(value)
    assert i >= 0, value
    cb.setCurrentIndex(i)
    cb.activated.emit(i)


def choose_all(d, repo_id):
    pick(d.repo, repo_id)
    d.main_cb.setCurrentIndex(d.main_cb.findData("a5"))


def groups(win):
    """看板分组的摘要：(列, 键, 是否 PBI 卡, 卡里的行)"""
    return [(g.col, g.key, g.pbi, [r[0] for r in g.rows]) for g in win.board_groups()]


def row_keys(win, pbi):
    return [r.key for r in win.card_widget(pbi).rows]


# ---------- 请求构造 ----------
def test_states_request(pat):
    c, http = client(pat, ("ok", 200, TASK_STATES))
    assert c.states(ORG, PROJ, PBI)[:2] == [("To Do", "Proposed"), ("Ready", "Proposed")]
    r = http.requests[0]
    assert (r["method"], r["url"]) == (
        "GET", "https://dev.azure.com/myorg/myproject/_apis/wit/workitemtypes/Product%20Backlog%20Item/states?api-version=7.1")


def test_set_state_request(pat):
    c, http = client(pat, ("ok", 200, {"id": 22, "rev": 8, "fields": {"System.WorkItemType": "Task", "System.State": "Done",
                                                                       "System.Title": "导出页面", "System.Parent": 30}}))
    assert c.set_state(ORG, PROJ, 22, "Done", 7) == Brief(22, "Task", "Done", "导出页面", 30, 8)
    r = http.requests[0]
    assert (r["method"], r["url"]) == ("PATCH", "https://dev.azure.com/myorg/myproject/_apis/wit/workitems/22?api-version=7.1")
    assert r["headers"]["content-type"] == "application/json-patch+json"
    # 只改状态，先 test rev：别人改过就失败，不覆盖
    assert r["body"] == [{"op": "test", "path": "/rev", "value": 7},
                         {"op": "replace", "path": "/fields/System.State", "value": "Done"}]


@pytest.mark.parametrize("status, body, conflict", [
    (412, b"{}", True), (409, b"{}", True),
    (400, b'{"message": "TF26071: This work item has been changed by someone else since you opened it."}', True),
    (400, b'{"message": "Test operation failed for /rev"}', True),
    (400, b'{"message": "TF401320: Rule Error for field State"}', False)])
def test_set_state_conflict(pat, status, body, conflict):
    c, _ = client(pat, ("http", status, body))
    with pytest.raises(AzureError) as ei:
        c.set_state(ORG, PROJ, 22, "Done", 7)
    assert isinstance(ei.value, ConflictError) is conflict
    if conflict:
        assert str(ei.value) == CONFLICT
    else:
        assert str(ei.value) == "Azure 返回 400：TF401320: Rule Error for field State"  # 其他失败给原话


def test_board_fetch_parents_and_states(pat):
    r = lanes_router()
    got = Client(pat, opener=r).board(ORG, PROJ, "myproject\\Sprint 1")
    assert [b.id for b in got.rows] == [20, 21, 22, 23] and got.rows[2] == Brief(22, "Task", "Test", "导出页面", 30, 7)
    assert got.parents == [Brief(30, PBI, "Committed", "订单记录", None, 4)]  # #20 是我的，不再取
    batches = [q["body"]["ids"] for q in r.requests if "workitemsbatch" in q["url"]]
    assert batches == [[20, 21, 22, 23], [30]]
    assert got.states == {"Task": [(x["name"], x["category"]) for x in TASK_STATES["value"]]}
    assert got.state_errors == {PBI: "Azure 返回 500：VS000: boom"}  # 取不到不让整个看板失败
    # 已经取过的类型不再取
    r2 = lanes_router()
    Client(pat, opener=r2).board(ORG, PROJ, "myproject\\Sprint 1", known_types=("Task", PBI))
    assert not any("/states?" in q["url"] for q in r2.requests)


def test_board_no_parents_no_extra_batch(pat):
    c, http = client(pat, ("ok", 200, wiql_of(5)), ("ok", 200, batch((5, "Issue", "To Do", "x", None, 1))),
                     ("ok", 200, TASK_STATES))
    got = c.board(ORG, PROJ, "myproject\\Sprint 1")
    assert got.parents == [] and sum("workitemsbatch" in q["url"] for q in http.requests) == 1


def test_fetch_item_acceptance(pat):
    c, _ = client(pat, ("ok", 200, ITEM30))
    w = c.fetch_item(ORG, PROJ, 30)
    assert (w.description, w.acceptance) == ("用户要能导出订单记录", "- 能按月导出\n- CSV 格式")


def test_state_column_by_category():
    # 有分类按分类；没有按名
    assert [state_column(s, c) for s, c in [("Ready", "Proposed"), ("Approved", "Proposed"), ("Committed", "InProgress"),
                                            ("Test", "InProgress"), ("Resolved", "Resolved"), ("Done", "Completed"),
                                            ("Removed", "Removed")]] == \
        ["todo", "todo", "doing", "doing", "done", "done", "done"]
    assert [state_column(s) for s in ("Ready", "New", "Committed", "Done")] == ["doing", "todo", "doing", "done"]


# ---------- PBI 卡 ----------
def test_lanes(qtbot, board, lwin, pat):
    win = lwin
    board.add_manual_task("手写一", "写个脚本", "")
    connect(board, win, pat, lanes_router())
    qtbot.waitUntil(lambda: win.fetch_label.text().startswith(ORG), timeout=WAIT)
    # #20 是我的卡也是 #21 的父项 → 只当 PBI 卡、不重复成行；#30 不是指派给我的父项也成 PBI 卡；
    # PBI 的状态列表没取到 → Committed 按名归 Doing；Ready 的分类是 Proposed → To Do；独立卡排在 PBI 卡后面
    assert groups(win) == [("doing", "20", True, ["21"]), ("doing", "30", True, ["22"]),
                           ("todo", "23", False, ["23"]), ("todo", "L1", False, ["L1"])]
    assert (keys(win, "todo"), keys(win, "doing"), keys(win, "done")) == (["23", "L1"], ["20", "30"], [])
    assert [c.count.text() for c in win.columns.values()] == ["2", "2", "0"]
    assert win.card_widget("20").head.title.text() == f"{PBI} #20 导出功能 · Committed"
    assert win.card_widget("30").head.title.text() == f"{PBI} #30 订单记录 · Committed"
    assert row_keys(win, "20") == ["21"] and row_keys(win, "30") == ["22"]
    assert win.card_widget("22") is win.card_widget("30").rows[0]
    qtbot.wait(200)
    grab(win, "p1-pbi-cards")

    # 点 Task #22：需求区是父项 PBI 的描述 + 验收（不显示 Task 原文）；状态下拉是 Task 的状态列表
    win.card_widget("22").clicked.emit("22")
    d = win.drawer
    assert win.card_widget("22").property("sel") == "true"
    qtbot.waitUntil(lambda: "能按月导出" in d.preview.text(), timeout=WAIT)
    assert f"📋 需求（来自 {PBI} #30 订单记录）" in d.preview.text() and "用户要能导出订单记录" in d.preview.text()
    assert "导出页面的描述" not in d.preview.text()
    assert [d.state_cb.itemText(i) for i in range(d.state_cb.count())] == [x["name"] for x in TASK_STATES["value"]]
    assert d.state_cb.currentText() == "Test" and d.state_cb.isEnabled() and d.state_msg.text() == ""
    assert d.pick_box.isVisible() and d.start_footer.isVisible()
    # 没有父项的 Task：需求区用它自己的描述
    win.select("23")
    qtbot.waitUntil(lambda: "整理文档的描述" in d.preview.text(), timeout=WAIT)
    assert "📋 需求（Task 自己的描述）" in d.preview.text()

    # 点 PBI 卡标题 #30：抽屉显示 PBI 全文，不能开始；PBI 的状态列表没取到 → 下拉不可用，写明原因
    win.card_widget("30").head.clicked.emit("30")
    assert d.card_id == "30" and win.card_widget("30").head.property("sel") == "true"
    assert f"📦 {PBI} 全文" in d.preview.text()
    assert not d.pick_box.isVisible() and not d.start_footer.isVisible()
    qtbot.waitUntil(lambda: "用户要能导出订单记录" in d.preview.text(), timeout=WAIT)
    assert "能按月导出" in d.preview.text()
    assert d.state_cb.currentText() == "Committed" and not d.state_cb.isEnabled()
    assert d.state_msg.text() == f"取不到「{PBI}」的状态列表：Azure 返回 500：VS000: boom（点「刷新」重试）"
    qtbot.wait(300)
    grab(win, "b5-pbi-drawer")


def test_change_state(qtbot, board, lwin, pat):
    win = lwin
    r = lanes_router()
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    win.select("22")
    d = win.drawer
    qtbot.waitUntil(lambda: "30" in win.details, timeout=WAIT)
    gate = __import__("threading").Event()
    r.add(("/workitems/22?", {"id": 22, "rev": 8, "fields": {"System.WorkItemType": "Task", "System.State": "Done",
                                                              "System.Title": "导出页面", "System.Parent": 30}}, gate))
    pick_state(win, "Done")
    # 发送中：下拉不可用、保持你选的值；同一张卡不重复发
    assert d.state_msg.text() == "正在改成「Done」…" and not d.state_cb.isEnabled() and d.state_cb.currentText() == "Done"
    win.change_state("22", "Removed")
    gate.set()
    qtbot.waitUntil(lambda: "22" not in win.state_jobs, timeout=WAIT)
    patches = [q for q in r.requests if q["method"] == "PATCH"]
    assert len(patches) == 1 and patches[0]["url"].endswith("/workitems/22?api-version=7.1")
    assert patches[0]["body"] == [{"op": "test", "path": "/rev", "value": 7},
                                  {"op": "replace", "path": "/fields/System.State", "value": "Done"}]
    # 成功：Task 行显示新状态（PBI 卡按 PBI 的状态归列，不动），rev 更新（下次改用新 rev）
    assert win.brief("22").state == "Done" and win.brief("22").rev == 8
    assert keys(win, "doing") == ["20", "30"] and row_keys(win, "30") == ["22"]
    assert win.card_widget("22").findChild(QLabel, "cardMeta").text() == "Done"
    assert d.state_msg.text() == "已改成「Done」" and d.state_cb.currentText() == "Done" and d.state_cb.isEnabled()
    qtbot.wait(200)
    grab(win, "b4-state-changed")

    # 冲突：提示刷新；下拉回到原状态、卡不移列
    win.select("23")
    qtbot.waitUntil(lambda: "23" in win.details, timeout=WAIT)
    r.add(("/workitems/23?", (412, b'{"message": "rev mismatch"}'), None))
    pick_state(win, "In Progress")
    qtbot.waitUntil(lambda: "23" not in win.state_jobs, timeout=WAIT)
    assert d.state_msg.text() == f"没改成：{CONFLICT}" and d.state_cb.currentText() == "Ready"
    assert keys(win, "todo") == ["23"] and win.brief("23").state == "Ready"
    # 其他失败给原话
    r.add(("/workitems/23?", (400, b'{"message": "TF401320: Rule Error"}'), None))
    pick_state(win, "Done")
    qtbot.waitUntil(lambda: "23" not in win.state_jobs, timeout=WAIT)
    assert d.state_msg.text() == "没改成：Azure 返回 400：TF401320: Rule Error"
    # 选回当前状态不发请求
    n = len(r.requests)
    pick_state(win, "Ready")
    assert "23" not in win.state_jobs and len(r.requests) == n
    # 刷新后旧提示清掉
    win.refresh_btn.click()
    qtbot.waitUntil(lambda: win.refresh_btn.isEnabled() and win.fetch_label.text().startswith(ORG), timeout=WAIT)
    assert d.state_msg.text() == ""


def test_state_result_dropped_after_settings_change(qtbot, board, lwin, pat):
    win = lwin
    r = lanes_router()
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["23"], timeout=WAIT)
    gate = __import__("threading").Event()
    r.add(("/workitems/23?", {"id": 23, "rev": 6, "fields": {"System.WorkItemType": "Task", "System.State": "Done",
                                                              "System.Title": "整理文档"}}, gate))
    win.change_state("23", "Done")
    assert "23" in win.state_jobs
    reload_gate = __import__("threading").Event()
    r.add(("teamsettings/iterations", ITERS, reload_gate))
    win.load_iterations()  # 改了 Azure 设置会重拉；让重拉挂着，只看改状态的旧结果
    gate.set()
    qtbot.waitUntil(lambda: sum(q["method"] == "PATCH" for q in r.requests) == 1, timeout=WAIT)
    qtbot.wait(300)
    # 旧结果丢弃：没有提示、没有记本地状态（否则下一次拉取会被它覆盖）
    assert win.state_msgs == {} and win.local_states == {} and win.state_jobs == {}
    reload_gate.set()
    qtbot.waitUntil(lambda: win.brief("23") is not None, timeout=WAIT)
    assert win.brief("23").state == "Ready" and win.state_msgs == {}


def test_refresh_in_flight_does_not_undo_state_change(qtbot, board, lwin, pat):
    """刷新发出后、回来前改状态成功：刷新带回的旧数据不覆盖这张卡；再下一次刷新以 Azure 为准"""
    win = lwin
    r = lanes_router()
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["23"], timeout=WAIT)
    load_gate = __import__("threading").Event()
    r.add(("Sprint 1", wiql_of(20, 21, 22, 23), load_gate))
    win.refresh()
    r.add(("/workitems/23?", {"id": 23, "rev": 6, "fields": {"System.WorkItemType": "Task", "System.State": "Done",
                                                              "System.Title": "整理文档"}}, None))
    win.change_state("23", "Done")
    qtbot.waitUntil(lambda: win.state_jobs == {}, timeout=WAIT)
    assert win.brief("23").state == "Done"
    load_gate.set()  # 刷新回来：批量取的还是改之前的 Ready / rev 5
    qtbot.waitUntil(lambda: win._job is None, timeout=WAIT)
    assert (win.brief("23").state, win.brief("23").rev) == ("Done", 6) and keys(win, "done") == ["23"]
    assert win.state_msgs["23"] == ("已改成「Done」", False)
    # 改成功之后才发出的刷新：以拉回来的为准，提示清掉
    win.refresh()
    qtbot.waitUntil(lambda: win._job is None, timeout=WAIT)
    assert (win.brief("23").state, win.brief("23").rev) == ("Ready", 5) and "23" not in win.state_msgs
    assert win.local_states == {}


def test_parent_batch_fails_board_still_loads(qtbot, board, lwin, pat):
    """父项批量取失败：看板照常，泳道标题写「#号（取不到父项详情）」，说明行给原因；刷新可恢复"""
    r = lanes_router()
    r.add(('"ids": [30]', (500, b'{"message": "VS000: down"}'), None))
    got = Client(pat, opener=r).board(ORG, PROJ, "myproject\\Sprint 1")
    assert len(got.rows) == 4 and got.parents == [] and got.parent_error == "Azure 返回 500：VS000: down"
    win = lwin
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: win.fetch_label.text().startswith(ORG), timeout=WAIT)
    # 父项取不到：PBI 卡标题写「#号（取不到父项详情）」，按卡里第一行（#22 Test → Doing）归列
    assert win.card_widget("30").head.title.text() == "#30（取不到父项详情）"
    assert groups(win)[:2] == [("doing", "20", True, ["21"]), ("doing", "30", True, ["22"])]
    assert "父项详情取不到：Azure 返回 500：VS000: down（可以点「刷新」重试）" in win.fetch_label.text()
    assert win.fetch_kind == "bad"
    with r.lock:
        r.routes.pop(0)
    win.refresh()
    qtbot.waitUntil(lambda: win._job is None, timeout=WAIT)
    assert win.card_widget("30").head.title.text() == f"{PBI} #30 订单记录 · Committed"
    assert "父项详情" not in win.fetch_label.text() and win.fetch_kind == ""


def test_parent_detail_retry_on_reselect(qtbot, board, lwin, pat):
    """需求来自的 PBI 详情取失败：按提示再点这张卡，父项详情会重取；没取到前不能开始"""
    win = lwin
    r = lanes_router()
    r.add(("/workitems/30?", (500, b'{"message": "VS000: flaky"}'), None))
    connect(board, win, pat, r, ai=True)
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    win.select("22")
    d = win.drawer
    qtbot.waitUntil(lambda: "取详情失败" in d.preview.text(), timeout=WAIT)
    assert "再点一下这张卡可重试" in d.preview.text() and "Azure 返回 500：VS000: flaky" in d.preview.text()
    assert d.start_item() is None and "等需求详情取到" in d.start_hint.text()
    with r.lock:
        r.routes.pop(0)
    win.select("22")
    qtbot.waitUntil(lambda: "能按月导出" in d.preview.text(), timeout=WAIT)
    assert sum("/workitems/30?" in q["url"] for q in r.requests) == 2


def test_foreign_parent_drawer_has_no_start(qtbot, board, lwin, pat):
    """点 PBI 卡标题：只看全文、改状态，没有选仓库 / agent / 开始；
    不是指派给我的父项和指派给我的 PBI 说明不同"""
    win = lwin
    connect(board, win, pat, lanes_router(), ai=True)
    qtbot.waitUntil(lambda: keys(win, "doing") == ["20", "30"], timeout=WAIT)
    d = win.drawer
    win.card_widget("30").head.clicked.emit("30")
    assert d.meta.text().endswith("父项，不是指派给我的，只能看和改状态")
    assert not d.pick_box.isVisible() and not d.start_footer.isVisible() and d.state_cb.isVisible()
    win.card_widget("20").head.clicked.emit("20")
    assert d.meta.text().endswith(NOT_TASK.format(type=PBI))
    assert not d.pick_box.isVisible() and not d.start_footer.isVisible() and d.state_cb.isVisible()
    # 换到我的卡：开始区回来
    win.select("22")
    assert d.meta.text().endswith("未开始") and d.pick_box.isVisible() and d.start_footer.isVisible()


def test_manual_card_has_no_state_cb(qtbot, board, lwin):
    win = lwin
    board.add_manual_task("手写一", "", "")
    win.select("L1")
    assert not win.drawer.state_cb.isVisible()


# ---------- AI 开关 ----------
def test_ai_switch_per_org(qtbot, board, lwin, pat, repo):
    win = lwin
    proj = add_project(board, repo)
    board.add_manual_task("手写一", "", "")
    connect(board, win, pat, lanes_router(), ai=False)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["23", "L1"], timeout=WAIT)
    assert not board.ai_allowed() and not board.ai_allowed("") and not board.ai_allowed("other")
    win.select("23")
    d = win.drawer
    qtbot.waitUntil(lambda: "23" in win.details, timeout=WAIT)
    choose_all(d, proj.id)
    assert not d.start_btn.isEnabled() and d.start_hint.text() == AI_OFF
    # 状态照样能改（只看板不跑 AI）
    assert d.state_cb.isEnabled()
    qtbot.wait(300)
    grab(win, "b1-ai-off")
    # 绕过界面直接开始也被拒
    assert board.start_card(win.details["23"], proj.id, "a5") == (None, AI_OFF)
    # 手写任务不受影响
    win.select("L1")
    choose_all(d, proj.id)
    assert d.start_btn.isEnabled() and d.start_hint.text() == ""
    # 开关按组织记
    board.set_ai_allowed("other", True)
    assert not board.ai_allowed()
    board.set_ai_allowed(ORG, True)
    assert board.ai_allowed() and board.db.setting(f"ai_allowed:{ORG}") == "1"
    win.select("23")
    choose_all(d, proj.id)
    assert d.start_btn.isEnabled() and d.start_hint.text() == ""


def test_ai_switch_dialog(qtbot, board, lwin):
    win = lwin
    offline = lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("离线"))  # 保存后会重拉，不连真 Azure
    board.ado = Client(azure.PAT_FILE, opener=offline)
    sdlg = win.open_azure_settings()
    dlg = sdlg.azure  # 设置窗口里的 Azure 页
    assert not dlg.ai.isChecked() and not dlg.ai.isEnabled()  # 没填地址不能勾
    dlg.ado.setText("https://dev.azure.com/myorg/myproject")
    assert dlg.ai.isEnabled() and not dlg.ai.isChecked()  # 默认不勾
    dlg.ai.setChecked(True)
    dlg.pat.setText("pat-for-test")
    grab(dlg, "b1-ai-dialog")
    dlg._save()
    assert sdlg.isVisible() and board.ai_allowed(ORG)  # 保存后留在设置窗口里
    win._job = None
    dlg2 = win.open_azure_settings().azure
    assert dlg2.ai.isChecked()
    dlg2.ado.setText("https://dev.azure.com/otherorg/项目")  # 换组织 → 显示那个组织的（没允许）
    assert not dlg2.ai.isChecked()
    dlg2.ado.setText("https://dev.azure.com/myorg/myproject2")  # 换回原组织（项目不同）→ 显示它记下的「允许」
    assert dlg2.ai.isChecked()
    dlg2.ado.setText("https://dev.azure.com/otherorg/项目")
    dlg2._save()
    win._job = None
    assert not board.ai_allowed("otherorg") and board.ai_allowed(ORG) and board.azure_org == "otherorg"


# ---------- VS Code ----------
def test_vscode_button(qtbot, board, lwin, vs_calls, repo, tmp_path):
    win = lwin
    proj = add_project(board, repo)
    board.add_manual_task("手写一", "", "")
    win.select("L1")
    d = win.drawer
    assert not d.vscode_btn.isVisible()  # 没选仓库不显示
    pick(d.repo, proj.id)
    assert d.vscode_btn.isVisible()
    d.vscode_btn.click()
    assert vs_calls == [str(repo)] and not d.vs_msg.isVisible()
    # 失败给原话
    broken = add_project(board, make_repo(tmp_path / "broken"))
    win.render()  # 仓库下拉重新加载
    pick(d.repo, broken.id)
    d.vscode_btn.click()
    assert vs_calls[-1].endswith("broken") and d.vs_msg.isVisible()
    assert d.vs_msg.text() == "打不开 VS Code：找不到应用程序"
    pick(d.repo, proj.id)  # 换了仓库，旧的报错清掉
    assert not d.vs_msg.isVisible()


def test_open_vscode_command(monkeypatch):
    seen = []

    def run(args, **kw):
        seen.append((args, kw.get("stdin")))
        code = 1 if args[-1] == "/bad" else 0
        return subprocess.CompletedProcess(args, code, "", "Unable to find application named 'Visual Studio Code'" if code else "")
    monkeypatch.setattr(subprocess, "run", run)
    assert open_vscode("/repo/demo") is None
    assert seen[0] == (["open", "-b", "com.microsoft.VSCode", "/repo/demo"], subprocess.DEVNULL)
    assert open_vscode("/bad") == "打不开 VS Code：Unable to find application named 'Visual Studio Code'"


# ---------- 四问工作页 + 代码改动 ----------
def test_work_page_four_questions(qtbot, board, lwin, fake, repo, vs_calls):
    win = lwin
    proj = add_project(board, repo)
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "开发完成：新建了 hello.txt", "write": {"hello.txt": "hi\n"}}])
    it = board.new_manual_item("新建 hello.txt", "在仓库根目录新建 hello.txt。" + "背景说明。" * 200, "")
    card, err = board.start_card(it, proj.id, IDS)
    assert err is None, err
    wait_review(qtbot, card, "req")
    win.select(card.id)
    d = win.drawer
    assert not hasattr(d, "spec_edit") and "🎯 完成目标" in d.goal.text() and "🛠 方案" in d.plan.text()
    assert "【完成目标】" in board.spec_draft(card) and "【方案】" in board.spec_draft(card)
    assert board.confirm_spec(card)
    wait_review(qtbot, card, "dev")
    win.render()
    assert d.pages.currentIndex() == 1 and not d.banner.isVisible()
    # VS Code 打开的是原仓库，不是 worktree
    d.vscode_btn.click()
    assert vs_calls == [str(repo)]
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ["结果", "改了哪些代码", "方案", "对话", "更多"]
    assert "AI 现在在干什么" in d.now_box.text() and "AI 没在跑" in d.now_box.text()
    result = d.views["result"].toPlainText()
    assert "开发做完了" in result and "开发完成：新建了 hello.txt" not in result  # 原话默认折叠
    d._toggle_ask(QUrl("#raw-1-1"))
    assert "开发完成：新建了 hello.txt" in d.views["result"].toPlainText()
    talk = d.views["talk"].toPlainText()
    assert talk.index("需求") < talk.index("开发") and "程序 → AI：发了阶段指令" in talk and "开发完成：新建了 hello.txt" in talk
    # 长提示词默认只一行摘要，点「展开」看全文
    first = card.stages[0].rounds[0].ask
    assert len(first) > ASK_FOLD and f"发了阶段指令（{len(first)} 字）" in talk and first not in talk.replace("\n", "")
    d._toggle_ask(QUrl("#0-1"))
    assert "▾收起" in d.views["talk"].toPlainText() and "背景说明。" * 200 in d.views["talk"].toPlainText()
    d._toggle_ask(QUrl("#0-1"))
    assert "▸展开" in d.views["talk"].toPlainText()
    spec = d.views["spec"].toPlainText()
    assert "已确认的方案" not in spec and "🎯 完成目标" in spec and "🛠 方案" in spec and "新建 hello.txt，末尾换行" in spec
    assert "验收标准" not in spec and "任务书" not in spec
    assert "自检" in d.views["more"].toPlainText() and "原始输出" in d.views["more"].toPlainText()
    # 改了哪些代码：页签可见才跑 git
    assert d.change_list.count() == 0
    d.tabs.setCurrentIndex(1)
    assert [d.change_list.item(i).text() for i in range(d.change_list.count())] == ["+  hello.txt  新增  +1 −0（未提交）"]
    assert "改了 1 个文件（其中 1 个还没提交） · 0 个 commit" in d.changes_note.text()
    d.change_list.setCurrentRow(0)
    assert "hi" in d.views["changes"].toPlainText() and "新文件" in d.views["changes"].toPlainText()
    qtbot.wait(300)
    grab(win, "b7-changes")
    d.tabs.setCurrentIndex(3)
    grab(win, "b7-talk")
    d.tabs.setCurrentIndex(0)
    grab(win, "b7-feedback")
    d.tabs.setCurrentIndex(2)
    grab(win, "b6-spec")
    board.stop_all()


def test_card_changes_and_file_diff(tmp_path):
    repo = make_repo(tmp_path / "r")
    wt = tmp_path / "wt"
    worktree.add(str(repo), str(wt), "feature/ado-9", "main")
    (wt / "README.md").write_text("demo\n改了\n")
    sh("git", "commit", "-qam", "#9 开发：改 README", cwd=wt)
    (wt / "中文.txt").write_text("新文件内容\n")  # 未跟踪
    (wt / "README.md").write_text("demo\n改了\n又改\n")  # 已提交的文件又有未提交改动
    sh("git", "commit", "-q", "--allow-empty", "-m", "main 上别人的提交", cwd=repo)  # 基准往前走了也只看本卡分支的
    ch = worktree.card_changes(str(wt), "main")
    assert ch["files"] == [("M", "README.md", True), ("?", "中文.txt", True)]
    assert [c.split(" ", 1)[1] for c in ch["commits"]] == ["#9 开发：改 README"]
    assert "+又改" in worktree.file_diff(str(wt), "main", "README.md")
    assert worktree.file_diff(str(wt), "main", "中文.txt", untracked=True) == "（新文件，还没提交）\n新文件内容\n"
    # worktree 清理后：在原仓库里看本卡分支，只有已提交的
    sh("git", "checkout", "--", "README.md", cwd=wt)
    (wt / "中文.txt").unlink()
    worktree.remove(str(repo), str(wt))
    ch2 = worktree.card_changes(str(repo), "main", "feature/ado-9")
    assert ch2["files"] == [("M", "README.md", False)] and len(ch2["commits"]) == 1
    d = worktree.file_diff(str(repo), "main", "README.md", "feature/ado-9")
    assert "+改了" in d and "又改" not in d
    assert git_out(repo, "branch", "--show-current") == "main"


def test_card_changes_odd_names(tmp_path):
    """带空格 / 引号的文件名不被 git 加引号；本卡提交里新增、又在工作区删掉的标 D"""
    repo = make_repo(tmp_path / "r")
    wt = tmp_path / "wt"
    worktree.add(str(repo), str(wt), "feature/ado-9", "main")
    (wt / "a b.txt").write_text("一\n")
    (wt / "gone.txt").write_text("x\n")
    sh("git", "add", "-A", cwd=wt)
    sh("git", "commit", "-qm", "#9 加文件", cwd=wt)
    (wt / "a b.txt").write_text("一\n二\n")
    (wt / "gone.txt").unlink()
    (wt / 'q"x.txt').write_text("引号\n")
    ch = worktree.card_changes(str(wt), "main")
    assert ch["files"] == [("A", "a b.txt", True), ("D", "gone.txt", True), ("?", 'q"x.txt', True)]
    assert "+二" in worktree.file_diff(str(wt), "main", "a b.txt")
    assert worktree.file_diff(str(wt), "main", 'q"x.txt', untracked=True) == "（新文件，还没提交）\n引号\n"

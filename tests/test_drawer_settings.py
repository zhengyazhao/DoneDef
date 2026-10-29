"""抽屉界面测试：抽屉顶部一行、需求原文删除、待确认那句话删除、
在跑条（暂停 / 继续 / 中止 / 重新开始 / 停止）、设置合并成一个窗口、窗口缩小不乱。CLI 用假脚本、git 用真的。"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QSize
from PySide6.QtWidgets import QDialogButtonBox, QFrame, QLabel, QMessageBox, QPushButton

from donedef import azure
from donedef.board import ABORTED_LINE, RESUME_TEXT
from donedef.dialogs import ABORT_LIST_MAX, AgentDialog, AzureSettingsDialog, RepoDialog, SettingsDialog, abort_box
from donedef.model import WorkItem
from donedef.ui import COL_MIN, Drawer, ElidedLabel, status_of
from tests.test_core import IDS, REQ2, WAIT, add_project, start, wait_review
from tests.test_kanban import allow_ai
from tests.test_main_agent import arg, prompt_of
from tests.test_pause_abort import SLOW, enter_dev, wait_running

pytest_plugins = ["tests.test_core"]


def settle(qtbot, win, card_id):
    """选中卡、重画、等抽屉滑入动画结束"""
    win.select(card_id)
    win.render()
    qtbot.waitUntil(lambda: not win.drawer.anim.state() == win.drawer.anim.State.Running, timeout=WAIT)
    return win.drawer


def shown(d, name):
    """抽屉在跑条上当前可见的按钮键"""
    return sorted(k for k, b in d.run_buttons.items() if b.isVisibleTo(d.run_bar))


def lines_of(w):
    return " ".join(x.text() for x in w.findChildren(QLabel))


# ---------- 需求页没有需求原文 ----------
def test_req_page_has_no_requirement_original(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ2}])
    item = board.new_manual_item("导出", "独特描述句子ZZQ必须不出现", "独特验收标准YYX也不出现")
    card, err = board.start_card(item, add_project(board, repo).id, list(IDS))
    assert err is None
    wait_review(qtbot, card, "req")
    d = settle(qtbot, win, card.id)
    page = d.pages.widget(0)
    assert d.pages.currentWidget() is page
    assert not hasattr(d, "orig") and not hasattr(d, "req_parent")
    text = lines_of(page)
    assert "ZZQ" not in text and "YYX" not in text and "📋 需求" not in text and "验收标准" not in text
    assert "🎯 完成目标" in text  # 其余分区还在
    # 提示词照旧带需求原文（board 的活，这里只确认没被界面改动误伤）
    assert "独特描述句子ZZQ必须不出现" in prompt_of(fake.calls("claude")[0])


# ---------- 需求待确认那句话删掉，徽标保留 ----------
def test_req_wait_has_no_status_line(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ2}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    assert status_of(board, card) == ("wait", "需求待确认", "")
    d = settle(qtbot, win, card.id)
    w = win.card_widget(card.id)
    assert "需求待确认" in w.badge.text()
    assert not w.now_label.isVisibleTo(w)  # 看板卡片上那行黄字隐藏
    assert d.banner.text() == "" and not d.banner.isVisibleTo(d) and not d.run_bar.isVisibleTo(d)


def test_failed_line_still_shown(qtbot, board, win, fake, repo):
    """失败提示照旧：卡片上和抽屉 banner 都显示"""
    fake.plan(claude=[{"exit": 1, "stderr": "boom", "no_output": True}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    kind, stext, now = status_of(board, card)
    assert kind == "bad" and now.startswith("✖")
    d = settle(qtbot, win, card.id)
    w = win.card_widget(card.id)
    assert w.now_label.isVisibleTo(w) and w.now_label.text() == now
    assert d.banner.isVisibleTo(d) and d.banner.text() == now


# ---------- 抽屉顶部一行 ----------
def test_drawer_top_row(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ2}])
    proj = add_project(board, repo)
    allow_ai(board)
    item = WorkItem("23", "Task", "Azure 的卡", "To Do", "描述", "", "azure")
    card, err = board.start_card(item, proj.id, list(IDS))
    assert err is None
    wait_review(qtbot, card, "req")
    win.rows = [azure.Brief(23, "Task", "To Do", "Azure 的卡")]
    win.state_lists = {"Task": [("To Do", "Proposed"), ("Doing", "InProgress"), ("Done", "Completed")]}
    d = settle(qtbot, win, card.id)
    close = next(b for b in d.findChildren(QPushButton) if b.text() == "✕")
    row = [d.meta, d.state_cb, d.vscode_btn, close]
    assert all(w.isVisibleTo(d) for w in row)
    # 同属抽屉第一个 layout（一行），y 方向居中对齐
    top = d.layout().itemAt(0).layout()
    assert {top.itemAt(i).widget() for i in range(top.count())} == set(row)
    centers = {w.mapTo(d, QPoint(0, 0)).y() + w.height() // 2 for w in row}
    assert max(centers) - min(centers) <= 3, centers
    # 标题单独一行，在这一行下面；阶段条保留
    assert d.title.mapTo(d, QPoint(0, 0)).y() >= max(w.mapTo(d, QPoint(0, 0)).y() + w.height() for w in row)
    assert d.stages.isVisibleTo(d) and "需求" in d.stages.text()
    assert not d.hint_box.isVisibleTo(d)  # 没有提示时标题下面那一行隐藏
    assert not hasattr(d, "state_row")
    # 改状态的提示和 VS Code 的错误合并在标题下一行
    d.state_msg.setText("没改成：xx")
    d._sync_hint()
    assert d.hint_box.isVisibleTo(d) and d.state_msg.isVisibleTo(d)
    # meta 过长：省略号 + tooltip 全文
    long = "Task #23 · Azure · " + "很长的仓库名" * 30
    d.meta.setText(long)  # 不等定时器重画，免得被刷新成真实内容
    assert d.meta.toolTip() == long and d.meta.text() == long
    d.meta._elide()
    assert QLabel.text(d.meta).endswith("…") and d.meta.width() <= d.width()


def test_manual_card_top_row_has_no_state_cb(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ2}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    d = settle(qtbot, win, card.id)
    assert not d.state_cb.isVisibleTo(d) and d.vscode_btn.isVisibleTo(d)


# ---------- 在跑条 + 看板卡片按钮 ----------
def test_pause_resume_flow(qtbot, board, win, fake, repo):
    fake.plan(claude=[SLOW, {"reply": REQ2, "session": "S1"}])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    d = settle(qtbot, win, card.id)
    assert d.run_bar.isVisibleTo(d) and shown(d, "run") == ["abort", "pause"]
    assert not d.banner.isVisibleTo(d)  # 在跑条替代 banner
    assert d.run_label.text().startswith("▶ ")
    w = win.card_widget(card.id)
    assert w.buttons["pause"].text() == "⏸ 暂停" and "stop" not in w.buttons and "abort" not in w.buttons
    assert "stop" not in d.buttons and "pause" not in d.buttons  # 抽屉底部不重复
    d.run_buttons["pause"].click()
    wait_review(qtbot, card, "req")
    win.render()
    assert status_of(board, card) == ("gray", "已暂停", "⏸ 已暂停，点「▶ 继续」接着做")
    assert shown(d, "paused") == ["abort", "resume"] and "已暂停" in d.run_label.text()
    assert not d.banner.isVisibleTo(d)
    assert win.card_widget(card.id).badge.text() == "⏸ 已暂停"  # 主动暂停不用「!」
    assert "被你暂停了" in d.goal.text() and "没有产出方案" not in d.goal.text()  # 方案框不重复报错原话
    d.run_buttons["resume"].click()
    qtbot.waitUntil(lambda: len(fake.calls("claude")) == 2, timeout=WAIT)
    c2 = fake.calls("claude")[1]
    assert arg(c2["args"], "--resume") == "S1" and RESUME_TEXT in prompt_of(c2)
    wait_review(qtbot, card, "req")
    win.render()
    assert not d.run_bar.isVisibleTo(d)  # 跑完回到待确认：没有在跑条、也没有那句话


# ---------- 中止确认 ----------
def test_abort_cancel_does_nothing_then_confirm_then_restart(qtbot, board, win, fake, repo):
    fake.plan(claude=[SLOW, {"reply": REQ2, "session": "S2"}])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    d = settle(qtbot, win, card.id)
    d.run_buttons["abort"].click()
    box = win.active_dialog
    assert isinstance(box, QMessageBox) and "本轮作废" in box.text() and "AI 会话清空" in box.text()
    box.cancel_btn.click()  # 取消：什么都不发生
    qtbot.wait(300)
    assert card.stage.status == "running" and card.stage.session_id == "S1" and not board.is_aborted(card)
    assert shown(d, "run") == ["abort", "pause"] and len(fake.calls("claude")) == 1
    d.run_buttons["abort"].click()
    win.active_dialog.ok_btn.click()
    wait_review(qtbot, card, "req")
    win.render()
    assert board.is_aborted(card) and card.stage.session_id == ""
    assert status_of(board, card) == ("gray", "已中止", "■ " + ABORTED_LINE)
    assert shown(d, "aborted") == ["restart"] and ABORTED_LINE in d.run_label.text()
    assert d.run_buttons["restart"].text() == "↻ 重新开始本阶段"
    assert win.card_widget(card.id).badge.text() == "■ 已中止"
    assert "已中止作废" in d.goal.text() and "没有产出方案" not in d.goal.text()
    d.run_buttons["restart"].click()
    qtbot.waitUntil(lambda: len(fake.calls("claude")) == 2, timeout=WAIT)
    assert "--resume" not in fake.calls("claude")[1]["args"]  # 新开会话
    wait_review(qtbot, card, "req")


def test_abort_from_paused(qtbot, board, win, fake, repo):
    fake.plan(claude=[SLOW])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    d = settle(qtbot, win, card.id)
    d.run_buttons["pause"].click()
    wait_review(qtbot, card, "req")
    win.render()
    d.run_buttons["abort"].click()
    win.active_dialog.ok_btn.click()
    win.render()
    assert board.is_aborted(card) and shown(d, "aborted") == ["restart"]


def test_abort_box_lists_dev_files_and_caps_at_15(qtbot, board, win, fake, repo):
    card, wt = enter_dev(qtbot, board, fake, repo)
    d = settle(qtbot, win, card.id)
    d.run_buttons["abort"].click()
    box = win.active_dialog
    for f in ("README.md", "new.txt", "staged.txt"):
        assert f in box.text()
    assert "3 个未提交的文件" in box.text() and "已提交的不受影响，原仓库不动" in box.text()
    assert "keep.txt" not in box.text()  # 已提交的不在丢弃清单里
    box.cancel_btn.click()
    assert (wt / "new.txt").exists()  # 取消：文件还在
    board.stop_all()


def test_abort_box_text_cap_and_read_failure():
    dev = SimpleNamespace(stage=SimpleNamespace(stage="dev"))
    files = [f"f{i:02d}.txt" for i in range(20)]
    box = abort_box(dev, files, lambda: None, None)
    t = box.text()
    assert "f14.txt" in t and "f15.txt" not in t and "…等 20 个" in t and ABORT_LIST_MAX == 15
    assert "20 个未提交的文件" in t
    bad = abort_box(dev, ["（读取改动清单失败：boom）"], lambda: None, None).text()
    assert "（读取改动清单失败：boom）" in bad and "个未提交的文件" not in bad
    none = abort_box(dev, [], lambda: None, None).text()
    assert "没有未提交的改动" in none
    req = abort_box(SimpleNamespace(stage=SimpleNamespace(stage="req")), [], lambda: None, None).text()
    assert "本轮作废" in req and "worktree" not in req


# ---------- 自检 / 额外角色在跑只有「停止」 ----------
def test_checking_shows_only_stop(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "做完了", "write": {"x.txt": "x\n"}}])
    card = start(board, add_project(board, repo, build="sleep 30"))
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "spec")
    qtbot.waitUntil(lambda: card.stage.status == "checking", timeout=WAIT)
    d = settle(qtbot, win, card.id)
    assert not board.can_pause(card)
    assert shown(d, "stop") == ["stop"] and d.run_bar.isVisibleTo(d)
    w = win.card_widget(card.id)
    assert "stop" in w.buttons and "pause" not in w.buttons
    d.run_buttons["stop"].click()
    qtbot.waitUntil(lambda: card.stage.status != "checking", timeout=WAIT)
    assert not board.is_paused(card) and not board.is_aborted(card)


# ---------- 设置合并 ----------
def test_settings_button_and_nav(qtbot, board, win):
    assert win.prober is None  # 测试里不真检测本机 CLI
    assert win.settings_btn.text() == "设置" and win.settings_btn.menu() is None
    win.settings_btn.click()
    dlg = win.active_dialog
    assert isinstance(dlg, SettingsDialog) and dlg.isVisible()
    assert [dlg.nav.item(i).text() for i in range(dlg.nav.count())] == ["Azure", "仓库", "Agent 模板"]
    assert dlg.nav.width() == 140
    pages = [AzureSettingsDialog, RepoDialog, AgentDialog]
    for i, cls in enumerate(pages):
        dlg.nav.setCurrentRow(i)
        cur = dlg.stack.currentWidget()
        assert isinstance(cur, cls) and cur.isVisible() and dlg.stack.currentIndex() == i
        assert not cur.isWindow()  # 嵌入成子控件，不是独立窗口
    # 三个入口定位到对应页
    for i, fn in enumerate((win.open_azure_settings, win.open_repos, win.open_agents)):
        d2 = fn()
        assert isinstance(d2, SettingsDialog) and d2.stack.currentIndex() == i
        d2.close()
    assert win.open_settings().stack.currentIndex() == 0 and win.open_settings(2).stack.currentIndex() == 2


def test_settings_azure_save_stays_and_reloads(qtbot, board, win):
    calls = []
    win.load_iterations = lambda: calls.append(1)  # 不连真 Azure，只数「重拉迭代」被触发几次
    dlg = win.open_settings()
    dlg.azure.ado.setText("https://dev.azure.com/myorg/myproject")
    dlg.azure.pat.setText("pat-for-test")
    dlg.azure.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()
    assert calls == [1]
    assert (board.azure_org, board.azure_project) == ("myorg", "myproject")
    assert dlg.isVisible() and dlg.azure.isVisible() and dlg.stack.currentWidget() is dlg.azure
    assert "已保存" in dlg.msg.text()
    dlg.nav.setCurrentRow(1)  # 切页后提示清掉
    assert dlg.msg.text() == ""


def test_settings_azure_save_failure_does_not_reload(qtbot, board, win):
    calls = []
    win.load_iterations = lambda: calls.append(1)
    dlg = win.open_settings()
    dlg.azure.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()  # 没填地址
    assert calls == [] and dlg.azure.msg.text() and dlg.isVisible() and "已保存" not in dlg.msg.text()


@pytest.mark.parametrize("page", [0, 2])
def test_settings_cancel_closes_whole_window(qtbot, board, win, page):
    dlg = win.open_settings(page)
    cur = dlg.stack.currentWidget()
    bb = cur.findChild(QDialogButtonBox)
    # Azure 页是「取消」，Agent 页是「关闭」，都是 RejectRole
    rej = next(b for b in bb.buttons() if bb.buttonRole(b) == QDialogButtonBox.RejectRole)
    rej.click()
    assert not dlg.isVisible()


def test_settings_repo_close_closes_whole_window(qtbot, board, win):
    dlg = win.open_settings(1)
    next(b for b in dlg.repos.findChildren(QPushButton) if b.text() == "关闭").click()
    assert not dlg.isVisible()


def test_settings_repo_page_uses_window_chooser(qtbot, board, win, repo):
    win.chooser = lambda parent: str(repo)
    dlg = win.open_settings(1)
    dlg.repos._add()
    assert dlg.repos.list.count() == 1 and board.projects[0].path == str(repo.resolve())


# ---------- 缩小不乱 ----------
def header_of(win):
    return win.main.findChild(QFrame, "header")


def test_min_size_and_header_stays_one_row(qtbot, board, win):
    assert win.minimumSize() == QSize(720, 520)
    win.resize(400, 300)
    qtbot.wait(100)
    assert win.width() >= 720 and win.height() >= 520
    win.resize(720, 600)
    qtbot.wait(100)
    h = header_of(win)
    base = h.height()
    # 依据：单行顶栏 = 控件高度 + 上下边距 24，实测约 50；64 留了 ~14px 余量，换行撑高会超出
    assert base <= 64, base
    long = "myorg/myproject · 指派给我的 3 张 · 父项详情取不到：" + "Azure 返回 500：很长的错误原话" * 20
    win._set_fetch(long, "bad")
    qtbot.wait(100)
    f = win.fetch_label
    assert isinstance(f, ElidedLabel) and not f.wordWrap()
    assert h.height() == base  # 长文字不把顶栏撑高
    assert f.text() == long and f.toolTip() == long and QLabel.text(f).endswith("…")
    assert f.height() <= f.fontMetrics().height() + 12
    # 顶栏各控件互不重叠、右边的按钮都在可见范围内（额度条被压缩，不把按钮挤没）
    items = [win.iter_cb, win.refresh_btn, f, win.add_btn, win.quota_bar, win.settings_btn]
    rects = [w.geometry() for w in items]
    for a, b in zip(rects, rects[1:]):
        assert a.right() < b.left(), (a, b)
    assert win.settings_btn.isVisible() and win.settings_btn.geometry().right() <= h.width()
    assert win.add_btn.width() >= win.add_btn.minimumSizeHint().width()


def test_quota_chips_shrink_not_overflow(qtbot, board, win):
    for cli in ("claude", "codex"):
        board.set_quota(cli, [{"name": "5h", "pct": 40, "resets_at": 0}, {"name": "7d", "pct": 20, "resets_at": 0}])
    win.quota_bar.refresh()
    win.resize(1440, 900)
    qtbot.wait(100)
    wide = win.quota_bar.width()
    win.resize(720, 600)
    qtbot.wait(100)
    assert win.quota_bar.width() < wide
    chip = win.quota_bar.chips["claude"]
    assert chip.text().startswith("Claude") and chip.toolTip()
    assert win.settings_btn.geometry().right() <= header_of(win).width()


def test_drawer_width_follows_window(qtbot, board, win, fake, repo):
    for w, want in [(1440, 500), (1000, 420), (900, 378), (760, 360), (759, 759), (720, 720)]:
        assert Drawer.width_for(w) == want, w
    fake.plan(claude=[{"reply": REQ2}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    d = settle(qtbot, win, card.id)
    for w, want in [(1440, 500), (1000, 420), (720, None)]:
        win.resize(w, 700)
        qtbot.wait(150)
        pw = win.main.width()
        want = pw if want is None else want
        assert d.width() == want and d.x() == pw - want, (w, d.width(), d.x(), pw)
    win.resize(1440, 900)
    qtbot.wait(150)
    assert d.width() == 500


def test_board_scrolls_horizontally_and_stays_aligned(qtbot, board, win):
    states = [("To Do", "Proposed"), ("Doing", "InProgress"), ("Done", "Completed")]
    win.state_lists = {"Task": states}
    win.rows = [azure.Brief(1, "Task", "To Do", "甲"), azure.Brief(2, "Task", "Doing", "乙"),
                azure.Brief(3, "Task", "Done", "丙")]
    win.render()
    win.resize(1440, 900)
    qtbot.wait(150)
    assert win.board_hscroll.horizontalScrollBar().maximum() == 0  # 宽窗口不滚
    win.resize(720, 600)
    qtbot.wait(200)
    hs = win.board_hscroll.horizontalScrollBar()
    assert hs.maximum() > 0, "720 宽放不下三列最小宽，应该能横向滚动"
    assert all(c.width() >= COL_MIN for c in win.columns.values())

    def aligned():
        for key, col in zip(("1", "2", "3"), win.columns.values()):
            cw = win.card_widget(key)
            hx = col.mapTo(win.board_inner, QPoint(0, 0)).x()
            cx = cw.mapTo(win.board_inner, QPoint(0, 0)).x()
            assert abs(hx - cx) <= 1, (key, hx, cx)
            assert abs(col.width() - cw.width()) <= 1, (key, col.width(), cw.width())

    aligned()
    hs.setValue(hs.maximum())  # 滚到最右：列头和卡片列在同一个容器里一起动
    qtbot.wait(100)
    aligned()
    assert win.board_inner.mapTo(win, QPoint(0, 0)).x() < 0

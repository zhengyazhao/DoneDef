"""迭代看板测试：不关联仓库、两条确认路径、补选仓库、记住的仓库、手写任务归栏、老库兼容。
CLI 用假脚本、git 用真的；看板拉取和 Azure 设置的测试在 test_azure.py。"""
import sqlite3
import subprocess
from pathlib import Path

from donedef.azure import Brief
from donedef.board import DB_FILENAME, Board
from donedef.dialogs import BROWSE, NO_REPO, AttachRepoDialog, RepoDialog, TaskDialog
from donedef.model import WorkItem
from donedef.ui import badge_of
from tests.test_core import IDS, REQ1, REQ2, WAIT, add_project, dispose, env_for, git_out, grab, make_repo, sh, \
    start, wait_review

pytest_plugins = ["tests.test_core"]  # 复用那边的夹具（fake / board / repo / win …）

NOT_CONFIGURED = "还没配 Azure：点「设置」→ Azure"


def azure_item(num, title="调研导出方案"):
    return WorkItem(str(num), "Task", title, "To Do", "看看导出怎么做", "", "azure")


def allow_ai(board, org="myorg"):
    """Azure 卡要先在设置里允许交给 AI"""
    if not board.azure_org:
        board.set_azure(org, "myproject")
    board.set_ai_allowed(board.azure_org, True)


def start_no_repo(board, item):
    if item.source == "azure":
        allow_ai(board)
    card, err = board.start_card(item, None, IDS)
    assert err is None, err
    return card


def column_of(win, key):
    return next(k for k, col in win.columns.items() if any(w.key == key for w in col.cards))


def pick(cb, value):
    """模拟在仓库下拉里选一项（activated 才触发「选择本地文件夹…」）"""
    i = cb.findData(value)
    assert i >= 0, value
    cb.setCurrentIndex(i)
    cb.activated.emit(i)


# ---------- 手写任务 ----------
def test_manual_todo_persist_and_numbering(qtbot, board, fake, repo, tmp_path):
    fake.plan(claude=[{"sleep": 60}])
    a = board.add_manual_task("  手写一 ", " 写个脚本 ", "")
    b = board.add_manual_task("手写二", "", "能跑")
    assert (a.id, a.title, a.description, b.id, b.acceptance) == ("L1", "手写一", "写个脚本", "L2", "能跑")
    assert [w.id for w in board.todo_items] == ["L1", "L2"]
    # 开始了就不再是未开始；编号不复用
    card, err = board.start_card(a, add_project(board, repo).id, IDS)
    assert err is None and card.id == "L1"
    assert [w.id for w in board.todo_items] == ["L2"]
    assert board.add_manual_task("手写三", "", "").id == "L3"
    board.stop_all()
    board.save()
    b2 = Board(board.data_dir, env=env_for(fake))
    try:
        assert [(w.id, w.title, w.acceptance) for w in b2.todo_items] == [("L2", "手写二", "能跑"), ("L3", "手写三", "")]
        assert b2.find_card("L1") is not None and b2.add_manual_task("手写四", "", "").id == "L4"
    finally:
        dispose(b2)


def test_manual_columns_by_local_progress(qtbot, board, win, fake, repo):
    """手写卡不看 Azure：未开始 → To Do，已开始（含等确认）→ Doing，完成 → Done"""
    fake.plan(claude=[{"reply": REQ2}, {"reply": REQ2}, {"sleep": 60}])
    board.add_manual_task("未开始的", "", "")
    done = start_no_repo(board, board.new_manual_item("做完的", "", ""))
    wait_review(qtbot, done)
    assert board.finish_after_req(done, "结论")
    review = start_no_repo(board, board.new_manual_item("等确认的", "", ""))
    wait_review(qtbot, review)
    running = start_no_repo(board, board.new_manual_item("在跑的", "", ""))
    win.render()
    assert column_of(win, "L1") == "todo" and win.card_widget("L1").badge.text() == "○ 未开始"
    assert column_of(win, done.id) == "done" and win.card_widget(done.id).badge.text() == "✓ 已完成"
    assert column_of(win, review.id) == "doing" and win.card_widget(review.id).badge.text() == "● 需求待确认"
    assert column_of(win, running.id) == "doing" and win.card_widget(running.id).badge.text() == "▶ 理解需求中"
    board.stop_all()
    win.render()
    assert win.card_widget(running.id).badge.text() == "! 已停止"
    assert badge_of(board, running) == ("gray", "! 已停止")


# ---------- 不关联仓库 ----------
def test_no_repo_start_runs_readonly_in_scratch(qtbot, board, fake):
    fake.plan(claude=[{"reply": REQ1}])
    card = start_no_repo(board, azure_item(8))
    scratch = board.data_dir / "scratch" / "8"
    assert card.project_id is None and Path(card.wt_path) == scratch and scratch.is_dir()
    assert list(scratch.iterdir()) == []
    wait_review(qtbot, card, "req")
    c = fake.calls("claude")[0]
    assert c["cwd"] == str(scratch)
    assert c["args"][c["args"].index("--permission-mode") + 1] == "dontAsk"
    assert (board.data_dir / "logs" / "8" / "req-1.jsonl").exists()
    assert board.can_confirm_req(card)
    # 不关联仓库：不能直接「确认需求，开始开发」，也没有 worktree 可清理
    assert not board.confirm_spec(card, "spec") and card.stage.stage == "req"
    assert board.cleanup(card) == "现在不能清理"


def test_no_repo_git_isolated(qtbot, tmp_path, fake):
    """data/ 在别的 git 仓库里（比如本程序自己的仓库）：不关联仓库的卡，git 不往上找到那个仓库"""
    outer = make_repo(tmp_path / "outer")
    b = Board(outer / "data", env=env_for(fake))
    try:
        card = start_no_repo(b, azure_item(8))
        env = b.card_env(card)
        assert env["GIT_CEILING_DIRECTORIES"] == str(outer.resolve() / "data" / "scratch")
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=card.wt_path, env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "not a git repository" in r.stderr
        assert git_out(card.wt_path, "rev-parse", "--show-toplevel") == str(outer.resolve())  # 不加就会找到外层
        assert "GIT_CEILING_DIRECTORIES" not in b.card_env(type(card)(card.item, "p1", card.stages))
    finally:
        b.stop_all()
        dispose(b)


def test_no_repo_cannot_start_twice(board, fake):
    start_no_repo(board, azure_item(8))
    card, err = board.start_card(azure_item(8), None, IDS)
    assert card is None and err == "#8 已经在看板上了"
    board.stop_all()


def test_finish_after_req_azure_draft(qtbot, board, fake):
    """Azure 卡确认需求并结束 → 只含结论和方案的交付记录；没配 Azure 写不回去、草稿留着"""
    fake.plan(claude=[{"reply": REQ2}])
    card = start_no_repo(board, azure_item(8))
    assert not board.finish_after_req(card, "结论")  # 还在跑
    wait_review(qtbot, card)
    assert board.finish_after_req(card, " 结论：用 CSV 导出 ")
    assert card.spec == "结论：用 CSV 导出" and card.stages[0].status == "passed" and card.comment == "draft"
    assert card.comment_text.splitlines()[1:] == ["结论：需求已确认，未进入开发", "方案：", "结论：用 CSV 导出"]
    assert card.comment_text.startswith("—— 定标 DoneDef 交付记录 · ")
    assert card.column == "review" and badge_of(board, card) == ("wait", "● 交付记录待确认")
    assert len(fake.calls()) == 1  # 没进开发
    board.set_azure("", "")  # 开始时得配 Azure，这里清掉看「没配 Azure 发不出去」
    assert not board.send_writeback(card, card.comment_text)
    assert board.writeback_errors["8"] == NOT_CONFIGURED and card.comment == "draft"
    assert badge_of(board, card) == ("bad", "! 写回失败")


def test_finish_after_req_manual_done(qtbot, board, fake):
    fake.plan(claude=[{"reply": REQ1}])
    card = start_no_repo(board, board.new_manual_item("调研", "", ""))
    wait_review(qtbot, card)
    assert board.finish_after_req(card, "结论")
    assert card.comment == "skip" and card.column == "done"
    assert badge_of(board, card) == ("ok", "✓ 已完成")


def test_finish_refused_for_repo_card_and_bad_json(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}, {"reply": "没有 JSON"}])
    with_repo = start(board, add_project(board, repo))
    wait_review(qtbot, with_repo)
    assert not board.finish_after_req(with_repo, "结论") and with_repo.comment == "none"
    bad = start_no_repo(board, azure_item(8))
    wait_review(qtbot, bad)
    assert bad.stage.flag == "failed" and not board.can_confirm_req(bad)
    assert not board.finish_after_req(bad, "结论")
    assert board.attach_repo(bad, with_repo.project_id, "结论") == "现在不能进入开发"


def test_attach_repo_enters_dev(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "开发完成", "sleep": 60}])
    card = start_no_repo(board, azure_item(8))
    wait_review(qtbot, card)
    proj = add_project(board, repo)
    assert board.attach_repo(card, "nope", "spec") == "关联的仓库不存在"
    # git 出错：不进开发，原话返回，卡还是不关联仓库
    sh("git", "branch", "feature/ado-8", cwd=repo)
    err = board.attach_repo(card, proj.id, "spec")
    assert err.startswith("$ git worktree add") and "already exists" in err
    assert card.project_id is None and card.stage.stage == "req" and len(fake.calls()) == 1
    sh("git", "branch", "-D", "feature/ado-8", cwd=repo)
    assert board.attach_repo(card, proj.id, " 用 CSV 导出 ") is None
    wt = board.data_dir / "worktrees" / "demo" / "8"
    assert card.project_id == proj.id and Path(card.wt_path) == wt and card.spec == "用 CSV 导出"
    assert git_out(wt, "branch", "--show-current") == "feature/ado-8"
    assert card.stage.stage == "dev" and card.stage.status == "running"
    qtbot.waitUntil(lambda: len(fake.calls("grok")) == 1, timeout=WAIT)
    assert fake.calls("grok")[0]["cwd"] == str(wt)
    assert board.attach_repo(card, proj.id, "spec") == "现在不能进入开发"
    board.stop_all()


# ---------- 记住的仓库 ----------
def test_remember_repo(board, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    got, err = board.remember_repo(str(plain))
    assert got is None and err.startswith("不是 git 仓库：") and board.projects == []
    (repo / "sub").mkdir()
    p1, err = board.remember_repo(str(repo / "sub"))  # 选子目录 → 取仓库根
    assert err is None and p1.name == "repo" and p1.path == str(repo.resolve())
    assert (p1.base_branch, p1.build_cmd, p1.test_cmd) == ("main", "", "")
    assert board.remember_repo(str(repo))[0] is p1 and len(board.projects) == 1
    # 另一个同名仓库：加 -2；基准分支 / 测试命令自动识别
    (tmp_path / "x").mkdir()
    other = make_repo(tmp_path / "x" / "repo")
    sh("git", "checkout", "-q", "-b", "develop", cwd=other)
    (other / "pyproject.toml").write_text("[project]\nname='x'\n")
    p2, err = board.remember_repo(str(other))
    assert err is None and p2.name == "repo-2" and (p2.base_branch, p2.test_cmd) == ("develop", "pytest")
    board.save()
    b2 = Board(board.data_dir)
    try:
        assert [(p.name, p.path) for p in b2.projects] == [("repo", p1.path), ("repo-2", p2.path)]
    finally:
        dispose(b2)


# ---------- 设置 / 老库兼容 ----------
def test_settings_roundtrip(board):
    assert not board.azure_ready and board.db.setting("azure_org") == "" and board.db.setting("x", "默认") == "默认"
    board.set_azure("myorg", "myproject")
    board.set_azure("myorg", "myproject2")  # 覆盖写
    b2 = Board(board.data_dir)
    try:
        assert b2.azure_ready and (b2.azure_org, b2.azure_project) == ("myorg", "myproject2")
    finally:
        dispose(b2)


def test_old_db_compat(qtbot, tmp_path, fake, repo):
    """老库（没有 settings 表、项目行带别的组织）：自动建表；Azure 设置不从老项目迁移；老项目当记住的仓库；老卡照常走"""
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了", "sleep": 60}])
    b = Board(tmp_path / "data", env=env_for(fake))
    p = add_project(b, repo)
    p.tasks, p.org, p.ado_project = "azure", "company-org", "Course"
    card = start(b, p)
    wait_review(qtbot, card)
    b.save()
    dispose(b)
    db = tmp_path / "data" / DB_FILENAME
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE settings")
    b = Board(tmp_path / "data", env=env_for(fake))
    try:
        with sqlite3.connect(db) as c:
            # 只多了「预置模板已补过」的标记，Azure 设置不从老项目迁移
            assert c.execute("SELECT k, v FROM settings").fetchall() == [("presets_v6", "1")]
        assert not b.azure_ready and b.todo_items == []
        assert [(x.name, x.path, x.org) for x in b.projects] == [("demo", str(repo), "company-org")]
        old = b.find_card("L1")
        assert old.project_id == p.id and old.stage.status == "review" and b.can_confirm_req(old)
        assert b.confirm_spec(old, "spec") and old.stage.stage == "dev"
        qtbot.waitUntil(lambda: len(fake.calls("grok")) == 1, timeout=WAIT)
        assert fake.calls("grok")[0]["cwd"] == old.wt_path
    finally:
        b.stop_all()
        dispose(b)


# ---------- 界面 ----------
def test_ui_task_dialog(qtbot, board, win):
    dlg = win.open_add()
    assert isinstance(dlg, TaskDialog) and not dlg.ok_btn.isEnabled()
    dlg.title.setText("  ")
    assert not dlg.ok_btn.isEnabled()
    dlg.title.setText("整理导出脚本")
    dlg.desc.setPlainText("把导出脚本挪到 tools/")
    assert dlg.ok_btn.isEnabled() and dlg.ok_btn.text() == "保存到 To Do"
    grab(dlg, "k9-task-dialog")
    dlg.ok_btn.click()
    assert not dlg.isVisible() and dlg.saved_item.id == "L1"
    assert board.todo_items[0].description == "把导出脚本挪到 tools/"
    assert column_of(win, "L1") == "todo" and win.card_widget("L1").badge.text() == "○ 未开始"
    assert win.columns["todo"].count.text() == "1"


def test_ui_repo_dialog(qtbot, board, win, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    win.chooser = lambda parent: str(plain)
    dlg = win.open_repos().repos  # 设置窗口里的仓库页
    assert isinstance(dlg, RepoDialog) and dlg.list.count() == 0 and not dlg.save_btn.isEnabled()
    assert dlg.name.text() == "还没有记住的仓库：点「＋ 添加文件夹…」"
    dlg._add()
    assert dlg.msg.text().startswith("不是 git 仓库：") and board.projects == []
    dlg.chooser = lambda parent: str(repo)
    dlg._add()
    assert dlg.msg.text().startswith("已记住 repo") and dlg.list.count() == 1 and dlg.current() is board.projects[0]
    assert dlg.path.text() == str(repo.resolve()) and dlg.edits["base_branch"].text() == "main"
    dlg.edits["base_branch"].setText(" ")
    dlg.edits["test_cmd"].setText("pytest -q")
    dlg.save_btn.click()
    p = board.projects[0]
    assert (p.base_branch, p.test_cmd) == ("main", "pytest -q") and dlg.edits["base_branch"].text() == "main"
    qtbot.wait(50)
    grab(dlg, "k10-repo-settings")
    dlg.chooser = lambda parent: ""  # 取消选文件夹：什么都不做
    dlg._add()
    assert dlg.list.count() == 1


def test_ui_start_drawer_manual_browse(qtbot, board, win, fake, repo, tmp_path):
    """未开始的手写卡：仓库下拉「选择本地文件夹…」→ 记住并选中；不是 git 仓库提示原因、不选中"""
    fake.plan(claude=[{"sleep": 60}])
    board.add_manual_task("整理导出脚本", "挪到 tools/", "能跑")
    win.select("L1")
    d = win.drawer
    assert d.pages.currentIndex() == 2 and "挪到 tools/" in d.preview.text() and "能跑" in d.preview.text()
    assert d.repo.value() is None and not d.start_btn.isEnabled()
    assert d.start_hint.text() == "还差：选关联代码仓库、选主 agent"
    assert "📋 需求（手写任务）" in d.preview.text()
    plain = tmp_path / "plain"
    plain.mkdir()
    win.chooser = lambda parent: str(plain)
    d._build_pickers()  # 按新的 chooser 重建（正常使用时 chooser 不会变）
    pick(d.repo, BROWSE)
    assert d.repo.value() is None and board.projects == [] and "不是 git 仓库" in d.repo_err.text()
    win.chooser = lambda parent: str(repo / ".")
    d._build_pickers()
    pick(d.repo, BROWSE)
    assert board.projects[0].name == "repo" and d.repo.value() == board.projects[0].id
    d.main_cb.setCurrentIndex(d.main_cb.findData("a5"))
    assert d.start_btn.isEnabled()
    d.start_btn.click()
    card = board.find_card("L1")
    assert card.project_id == board.projects[0].id and Path(card.wt_path) == board.data_dir / "worktrees" / "repo" / "L1"
    assert board.todo_items == [] and column_of(win, "L1") == "doing"
    board.stop_all()


def test_ui_no_repo_confirm_paths(qtbot, board, win, fake, repo):
    """不关联仓库的卡：需求待确认时两个按钮；补选仓库弹窗 git 出错原话留在窗口里，成功进入开发"""
    fake.plan(claude=[{"reply": REQ1}, {"reply": REQ2}], grok=[{"reply": "好了", "sleep": 60}])
    proj = add_project(board, repo)
    board.add_manual_task("调研导出", "看看导出怎么做", "")
    win.select("L1")
    d = win.drawer
    pick(d.repo, NO_REPO)
    assert "data/scratch/L1/" in d.repo_note.text()
    d.main_cb.setCurrentIndex(d.main_cb.findData("a5"))
    d.start_btn.click()
    card = board.find_card("L1")
    assert card.project_id is None and d.pages.currentIndex() == 0 and "未关联仓库" in d.meta.text()
    wait_review(qtbot, card)
    win.render()
    qtbot.wait(300)
    assert d.finish_btn.isVisible() and d.attach_btn.isVisible() and not d.confirm_btn.isVisible()
    assert d.finish_btn.text() == "确认需求并结束" and d.finish_btn.isEnabled() and d.attach_btn.isEnabled()
    assert "末尾要换行吗" in d.pend.text()
    grab(win, "k8-no-repo-req")

    # 补选仓库进入开发：先让 git 出错
    sh("git", "branch", "feature/task-l1", cwd=repo)
    d.attach_btn.click()
    dlg = win.active_dialog
    assert isinstance(dlg, AttachRepoDialog) and dlg.repo.value() is None and not dlg.ok_btn.isEnabled()
    assert [dlg.repo.itemText(i) for i in range(dlg.repo.count())] == ["— 请选择 —", f"demo  ·  {repo}", "选择本地文件夹…"]
    pick(dlg.repo, proj.id)
    assert dlg.ok_btn.isEnabled() and dlg.wt.text() == \
        "将创建 worktree：data/worktrees/demo/L1 · 分支 feature/task-l1（从 main 切出）"
    qtbot.wait(50)
    grab(dlg, "k8-attach-repo")
    dlg.ok_btn.click()
    assert dlg.isVisible() and dlg.err.text().startswith("没有进入开发：\n$ git worktree add")
    assert card.project_id is None and card.stage.stage == "req"
    sh("git", "branch", "-D", "feature/task-l1", cwd=repo)
    dlg.ok_btn.click()
    assert not dlg.isVisible() and card.project_id == proj.id and card.stage.stage == "dev"
    win.render()
    assert "demo · ⎇ feature/task-l1" in d.meta.text() and d.pages.currentIndex() == 1
    board.stop_all()


def test_ui_no_repo_azure_finish(qtbot, board, win, fake):
    fake.plan(claude=[{"reply": REQ2}])
    card = start_no_repo(board, azure_item(8))
    win.rows = [Brief(8, "Task", "To Do", "调研导出方案")]  # 当作迭代里拉到了 #8
    win.select("8")
    wait_review(qtbot, card)
    win.render()
    d = win.drawer
    assert d.finish_btn.text() == "确认需求并结束（生成交付记录）"
    d.finish_btn.click()
    assert card.comment == "draft" and "未进入开发" in card.comment_text and "【完成目标】" in card.comment_text
    win.render()
    assert column_of(win, "8") == "todo" and win.card_widget("8").badge.text() == "● 交付记录待确认"

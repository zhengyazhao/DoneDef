"""需求页只留「要干嘛 + 方案」，遗漏写在步骤里的「（待你确认：…）」高亮；
不再有 AI 的理解 / 疑问区 / 任务书编辑框；确认直接冻结 spec_draft。CLI 用假脚本、git 用真的。"""

from donedef.dialogs import AttachRepoDialog
from donedef.ui import PENDING_MARK_STYLE, plan_html, status_of
from tests.test_core import WAIT, add_project, start, wait_review
from tests.test_kanban import azure_item, pick, start_no_repo

pytest_plugins = ["tests.test_core"]

PENDING = "（待你确认：导出哪些字段？默认 日期 / 订单号 / 金额）"
NEW_REPLY = ('好的。\n```json\n{"goal": "给订单页加导出 CSV", "plan": ["读现有订单页数据", "导出成 CSV' + PENDING
             + '", "加一个导出按钮"]}\n```')
NO_PENDING_REPLY = '```json\n{"goal": "新建 hello.txt", "plan": ["写 hello.txt", "提交"]}\n```'
OLD_PARSED = {"understanding": "新建 hello.txt", "todo": ["写 hello.txt"], "not_todo": ["不改别的文件"],
              "questions": ["末尾要换行吗"]}


def open_req(qtbot, win, card):
    wait_review(qtbot, card, "req")
    win.select(card.id)
    win.render()
    qtbot.wait(300)  # 等抽屉滑入动画
    return win.drawer


def test_plan_html_replaces_pending_with_marker_and_escapes():
    out = plan_html(["普通步骤 <b>x</b>", "导出" + PENDING])
    assert out.count(PENDING_MARK_STYLE) == 1 and "❓1" in out  # 疑问整段换成小标记
    assert out.index("普通步骤") < out.index("❓1") and "导出" in out.split(PENDING_MARK_STYLE)[0]
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "<b>x</b>" not in out
    assert "待你确认" not in out and "默认" not in out
    assert "（无）" in plan_html([])


def test_req_page_shows_goal_and_plan_with_highlight(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NEW_REPLY}])
    card = start(board, add_project(board, repo))
    d = open_req(qtbot, win, card)
    assert not hasattr(d, "orig")  # 需求原文已删
    assert "🎯 完成目标" in d.goal.text() and "给订单页加导出 CSV" in d.goal.text()
    plan = d.plan.text()
    assert "🛠 方案" in plan and "① 读现有订单页数据" in plan and "③ 加一个导出按钮" in plan
    # 只有带「待你确认」的步骤带 ❓1 小标记，疑问原文挪到上面的橙色框
    assert plan.count(PENDING_MARK_STYLE) == 1 and "❓1" in plan and "待你确认" not in plan
    pend = d.pend.text()
    assert "需要你确认（1）" in pend and "导出哪些字段？" in pend and "日期 / 订单号 / 金额" in pend
    assert "读现有订单页数据</div>" in plan
    assert d.plan_box.isVisible()
    assert "发给 AI 改方案" == d.reply_btn.text()
    assert "例如：字段只要日期和金额" in d.answer.placeholderText()


def test_req_page_has_no_old_blocks(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NEW_REPLY}])
    card = start(board, add_project(board, repo))
    d = open_req(qtbot, win, card)
    for name in ("und", "q_box", "qs", "warn", "spec_edit", "orig", "req_parent"):
        assert not hasattr(d, name), name
    all_text = " ".join(x.text() for x in (d.goal, d.plan))
    assert "任务书" not in all_text and "还有" not in all_text and "需要你回答" not in all_text
    assert "AI 没有疑问" not in all_text
    assert d.confirm_btn.text() == "✓ 确认方案，开始开发"


def test_req_page_no_pending_has_no_highlight(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NO_PENDING_REPLY}])
    card = start(board, add_project(board, repo))
    d = open_req(qtbot, win, card)
    assert "② 提交" in d.plan.text() and PENDING_MARK_STYLE not in d.plan.text() and "待你确认" not in d.plan.text()
    assert not d.pend_box.isVisible()  # 没有疑问：橙色框不显示
    assert board.open_questions(card) == []


def test_badge_text_with_and_without_pending(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": NEW_REPLY}, {"reply": NO_PENDING_REPLY}])
    proj = add_project(board, repo)
    c1 = start(board, proj, "统计导出")
    wait_review(qtbot, c1, "req")
    # 徽标保留，「方案里有 N 处待你确认」那句话删掉，有没有待确认项都一样
    assert status_of(board, c1)[:3] == ("wait", "需求待确认", "")
    c1.stages[0].rounds[-1].parsed["plan"].append("加筛选" + PENDING)
    assert status_of(board, c1)[:3] == ("wait", "需求待确认", "")
    c2 = start(board, proj, "写 hello")
    wait_review(qtbot, c2, "req")
    assert status_of(board, c2)[:3] == ("wait", "需求待确认", "")


def test_confirm_button_freezes_spec_draft_and_work_tab(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NEW_REPLY}], grok=[{"reply": "开发完成", "sleep": 60}])
    card = start(board, add_project(board, repo))
    d = open_req(qtbot, win, card)
    draft = board.spec_draft(card)
    assert draft.startswith("【完成目标】\n给订单页加导出 CSV\n\n【方案】\n1. 读现有订单页数据")
    assert PENDING in draft
    d.confirm_btn.click()
    assert card.spec == draft and card.stage.stage == "dev"
    win.render()
    assert d.pages.currentIndex() == 1
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ["结果", "改了哪些代码", "方案", "对话", "更多"]
    spec = d.views["spec"].toPlainText()
    assert "已确认的方案" not in spec and "🎯 完成目标" in spec and "给订单页加导出 CSV" in spec
    assert "验收标准" not in spec and "在仓库根目录新建 hello.txt" not in spec and "任务书" not in spec
    board.stop_all()


def test_no_repo_finish_uses_spec_draft(qtbot, board, win, fake):
    fake.plan(claude=[{"reply": NEW_REPLY}])
    card = start_no_repo(board, azure_item(8))
    d = open_req(qtbot, win, card)
    assert d.finish_btn.isVisible() and d.attach_btn.isVisible() and not d.confirm_btn.isVisible()
    draft = board.spec_draft(card)
    d.finish_btn.click()
    assert card.spec == draft and card.comment == "draft"
    assert "未进入开发" in card.comment_text and "【完成目标】" in card.comment_text and PENDING in card.comment_text


def test_no_repo_attach_dialog_uses_spec_draft(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NEW_REPLY}], grok=[{"reply": "好了", "sleep": 60}])
    proj = add_project(board, repo)
    card = start_no_repo(board, azure_item(9))
    d = open_req(qtbot, win, card)
    draft = board.spec_draft(card)
    d.attach_btn.click()
    dlg = win.active_dialog
    assert isinstance(dlg, AttachRepoDialog) and dlg.spec_text is None
    pick(dlg.repo, proj.id)
    dlg.ok_btn.click()
    assert card.project_id == proj.id and card.stage.stage == "dev" and card.spec == draft
    board.stop_all()


def test_old_format_round_opens_without_error(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NO_PENDING_REPLY}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    card.stages[0].rounds[-1].parsed = dict(OLD_PARSED)  # 模拟库里旧格式轮次
    win.select(card.id)
    win.render()
    qtbot.wait(300)
    d = win.drawer
    assert "新建 hello.txt" in d.goal.text()
    plan = d.plan.text()
    assert "① 写 hello.txt" in plan and "❓1" in plan and "待你确认" not in plan
    assert "末尾要换行吗" in d.pend.text()
    assert "不改别的文件" not in plan  # 旧的「不做」不再显示
    assert status_of(board, card)[:3] == ("wait", "需求待确认", "")
    assert "【方案】" in board.spec_draft(card)


def test_failed_round_shows_retry_hint(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NO_PENDING_REPLY}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    card.stages[0].rounds[-1].parsed = None
    win.select(card.id)
    win.render()
    qtbot.wait(300)
    d = win.drawer
    assert "重新出方案" in d.goal.text() and "重新理解" not in d.goal.text()
    assert not d.plan_box.isVisible()


def test_running_hint_mentions_goal_and_plan(qtbot, board, win, fake, repo):
    fake.plan(claude=[{"reply": NO_PENDING_REPLY, "sleep": 30}])
    card = start(board, add_project(board, repo))
    win.select(card.id)
    qtbot.waitUntil(lambda: card.stage.status == "running", timeout=WAIT)
    win.render()
    assert "完成目标 / 方案" in win.drawer.goal.text() and "疑问" not in win.drawer.goal.text()
    board.stop_all()

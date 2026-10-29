"""工作页五个页签 结果 / 改了哪些代码 / 方案 / 对话 / 更多。CLI 用假脚本、git 用真的。"""
import html
import re
from pathlib import Path

from PySide6.QtCore import QUrl

from donedef import worktree
from donedef.board import SUPPLEMENT_HEAD
from donedef.model import Round
from donedef.ui import ASK_FOLD, diff_html, result_html, talk_html, verdict_of
from tests.test_core import IDS, WAIT, add_project, make_repo, sh, wait_review

pytest_plugins = ["tests.test_core"]

REQ_NEW = ('```json\n{"goal": "新建 hello.txt 并写入 hi", "plan": ["在仓库根目录新建 hello.txt", "内容写 hi"]}\n```')
DEV_REPLY = "开发完成。\n过程说明QQ-RAW。\n改了什么：\n- 新增 hello.txt，内容 hi\n- 改了 README 的第一行\n"


def to_dev(qtbot, board, win, fake, repo, dev_reply=DEV_REPLY, desc="独特需求描述ZZQ", acc="独特验收标准YYX"):
    """需求跑完 → 确认 → 开发阶段跑完等你确认；返回 (卡, 抽屉)"""
    fake.plan(claude=[{"reply": REQ_NEW}], grok=[{"reply": dev_reply, "write": {"hello.txt": "hi\n"}}])
    item = board.new_manual_item("新建 hello.txt", desc, acc)
    card, err = board.start_card(item, add_project(board, repo).id, list(IDS))
    assert err is None, err
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card)
    wait_review(qtbot, card, "dev")
    win.select(card.id)
    win.render()
    qtbot.waitUntil(lambda: not win.drawer.anim.state() == win.drawer.anim.State.Running, timeout=WAIT)
    return card, win.drawer


def test_tab_names_and_order(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ["结果", "改了哪些代码", "方案", "对话", "更多"]
    assert d.tab_keys == ["result", "changes", "spec", "talk", "more"]
    assert "AI 现在在干什么" in d.now_box.text()  # 顶部那条保留


# ---------- 结果 ----------
def test_result_card_changes_listed_and_raw_folded(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    text = d.views["result"].toPlainText()
    assert re.search(r"开发 · 第 1 轮 · Grok 开发 · 用时 \d+:\d\d", text)
    assert "✓ 开发做完了" in text
    assert "• 新增 hello.txt，内容 hi" in text and "• 改了 README 的第一行" in text
    assert "- 新增" not in text  # 去掉「- 」前缀换成圆点
    # 原话默认折叠：只有「看 AI 原话」链接，正文里的过程说明不出现
    assert "▸ 看 AI 原话" in text and "过程说明QQ-RAW" not in text
    d._toggle_ask(QUrl("#raw-1-1"))
    text = d.views["result"].toPlainText()
    assert "▾ 收起 AI 原话" in text and "过程说明QQ-RAW" in text
    d._toggle_ask(QUrl("#raw-1-1"))
    assert "过程说明QQ-RAW" not in d.views["result"].toPlainText()


def test_result_without_parsable_changes_hides_block(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo, dev_reply="开发完成，没按格式写。")
    text = d.views["result"].toPlainText()
    assert "✓ 开发做完了" in text and "改了什么" not in text
    # 「改了什么：」后面没有「- 」行也不显示这块
    card.stage.rounds[-1].reply = "好了。\n改了什么：\n没有列表"
    assert "改了什么" not in result_html(board, card, set()).replace("改了什么：", "")


def test_result_checks_line_and_helper_kept(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    card.stage.checks = {"构建": ("make", 0), "测试": ("pytest", 1)}
    text = result_html(board, card, set())
    plain = re.sub(r"<[^>]+>", "", text)
    assert "自检：构建 ✓　测试 ✗(exit 1)" in plain
    assert "#c4363a" in text  # 失败项红色
    # 自检没过时结论不能写「✓ 做完了」
    assert "⚠ AI 说开发做完了，但自检没过" in plain and "✓ 开发做完了" not in plain
    card.stage.checks = {"构建": ("make", 0), "测试": ("pytest", 0)}
    assert "✓ 开发做完了" in re.sub(r"<[^>]+>", "", result_html(board, card, set()))
    card.stage.checks = {}
    assert "自检" not in result_html(board, card, set())


def test_result_verdict_texts(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    assert verdict_of("blah\n结论：通过") == "通过" and verdict_of("结论：不通过") == "不通过"
    assert verdict_of("没有结论") is None
    card.cur = 2  # 验证阶段
    ver = card.stage
    assert ver.stage == "verify"
    ver.status = "review"

    def plain():
        return re.sub(r"<[^>]+>", "", result_html(board, card, set()))

    ver.rounds.append(Round("流水线", "p", reply="都看过了\n结论：通过"))
    assert "验证 · 第 1 轮" in plain() and "✓ AI 验证：通过" in plain()
    ver.rounds[-1].reply = "结论：不通过，缺测试"
    assert "✖ AI 验证：不通过" in plain()
    ver.rounds[-1].reply = "看起来还行"
    assert "AI 没给明确结论" in plain()
    ver.rounds[-1].end, ver.flag, ver.error = "failed", "failed", "boom"
    assert "✖ 这一轮失败了：boom" in plain()
    ver.rounds[-1].end, ver.flag = "quota", "quota"
    assert "额度用完了" in plain()
    ver.rounds[-1].end, ver.flag, ver.error = "stopped", "stopped", ""
    assert "已停止" in plain()
    ver.rounds[-1].end, ver.flag, ver.status = None, None, "running"
    assert "AI 还在跑" in plain()


def test_result_req_stage_shows_plan_not_json(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    card.cur = 0  # 保险路径：需求阶段的结果页
    text = result_html(board, card, set())
    plain = re.sub(r"<[^>]+>", "", text)
    assert "需求 · 第 1 轮" in plain and "方案已出" in plain
    assert "🎯 完成目标" in plain and "新建 hello.txt 并写入 hi" in plain and "🛠 方案" in plain
    assert '"goal"' not in plain and "```" not in plain


# ---------- 改了哪些代码 ----------
def make_changes(wt):
    """已提交 a.txt、未提交修改 README、未跟踪 new.txt"""
    (wt / "a.txt").write_text("1\n2\n3\n")
    sh("git", "add", "a.txt", cwd=wt)
    sh("git", "commit", "-q", "-m", "加 a.txt", cwd=wt)
    (wt / "README.md").write_text("demo2\nmore\n")
    (wt / "new.txt").write_text("x\ny\n")


def test_changes_page_rows_and_commits(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    make_changes(Path(card.wt_path))
    d.tabs.setCurrentIndex(1)
    d._show_changes(card, force=True)
    rows = [d.change_list.item(i).text() for i in range(d.change_list.count())]
    assert rows == ["~  README.md  修改  +2 −1（未提交）",
                    "+  a.txt  新增  +3 −0",
                    "+  hello.txt  新增  +1 −0（未提交）",
                    "+  new.txt  新增  +2 −0（未提交）"]
    assert "改了 4 个文件（其中 3 个还没提交） · 1 个 commit" in d.changes_note.text()
    assert d.changes_btn.text() == "刷新"
    # 没选文件时下面列本卡 commit
    assert "本卡的 commit" in d.views["changes"].toPlainText() and "加 a.txt" in d.views["changes"].toPlainText()


def test_changes_page_diff_is_colored(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    make_changes(Path(card.wt_path))
    d.tabs.setCurrentIndex(1)
    d._show_changes(card, force=True)
    d.change_list.setCurrentRow(0)  # README.md
    h = d.views["changes"].property("h")
    assert re.search(r"background-color:#e6f6ea[^>]*>\+demo2<", h) and re.search(r"background-color:#e6f6ea[^>]*>\+more<", h)
    assert re.search(r"background-color:#fdeaea[^>]*>-demo<", h)
    assert re.search(r"background-color:#e8eefe[^>]*>@@", h)
    assert re.search(r"color:#6b7280[^>]*>diff --git", h) and "Menlo" in h
    assert "-demo" in d.views["changes"].toPlainText()


def test_diff_html_pure():
    text = "diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n ctx <b>\n--x\n+++y\n+<i>"
    out = diff_html(text)
    divs = re.findall(r"<div style='([^']*)'>(.*?)</div>", out)
    style = dict((body, st) for st, body in divs)
    assert "color:#6b7280" in style["--- a/f"] and "color:#6b7280" in style["+++ b/f"] and "color:#6b7280" in style["index 1..2 100644"]
    assert "#e8eefe" in style["@@ -1,2 +1,2 @@"]
    assert "#fdeaea" in style["--x"] and "#e6f6ea" in style["+++y"]  # 进了 @@ 之后按前缀算，不当头部
    assert "#e6f6ea" in style["+&lt;i&gt;"] and "&lt;b&gt;" in out and "<i>" not in out and "<b>" not in out
    assert "background" not in style[" ctx &lt;b&gt;"]
    # 未跟踪的新文件：首行说明灰字，其余绿底
    u = diff_html("（新文件，还没提交）\nline1\n\nline3", untracked=True)
    assert u.count("#e6f6ea") == 3 and u.count("color:#6b7280") == 1


def test_card_numstat_real_git(tmp_path):
    repo = make_repo(tmp_path / "r")
    (repo / "gone.txt").write_text("a\nb\nc\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    wt = tmp_path / "wt"
    worktree.add(str(repo), str(wt), "feat/x", "main")
    (wt / "c.txt").write_text("1\n2\n")  # 已提交
    (wt / "README.md").write_text("demo\nplus\n")  # 已提交的修改
    sh("git", "add", "-A", cwd=wt)
    sh("git", "commit", "-q", "-m", "c", cwd=wt)
    (wt / "gone.txt").unlink()  # 未提交的删除
    (wt / "u.txt").write_text("l1\nl2\nl3")  # 未跟踪，末行没有换行
    (wt / "bin.dat").write_bytes(b"\x00\x01\x02")  # 未跟踪的二进制
    (wt / "empty.txt").write_text("")  # 未跟踪的空文件
    st = worktree.card_numstat(str(wt), "main")
    assert st == {"c.txt": (2, 0), "README.md": (1, 0), "gone.txt": (0, 3), "u.txt": (3, 0),
                  "bin.dat": (None, None), "empty.txt": (0, 0)}
    # worktree 清理后在原仓库里按分支看：只含已提交的
    sh("git", "add", "-A", cwd=wt)
    sh("git", "commit", "-q", "-m", "all", cwd=wt)
    assert worktree.card_numstat(str(repo), "main", "feat/x") == {
        "c.txt": (2, 0), "README.md": (1, 0), "gone.txt": (0, 3), "u.txt": (3, 0), "bin.dat": (None, None),
        "empty.txt": (0, 0)}


def test_changes_page_binary_and_numstat_failure(qtbot, board, win, fake, repo, monkeypatch):
    card, d = to_dev(qtbot, board, win, fake, repo)
    (Path(card.wt_path) / "pic.bin").write_bytes(b"\x00\xff")
    d.tabs.setCurrentIndex(1)
    d._show_changes(card, force=True)
    rows = [d.change_list.item(i).text() for i in range(d.change_list.count())]
    assert "+  pic.bin  新增  二进制（未提交）" in rows
    # 行数取不到时只少了 +N −M，清单照样出
    monkeypatch.setattr(worktree, "card_numstat", lambda *a: (_ for _ in ()).throw(worktree.GitError("boom")))
    d._show_changes(card, force=True)
    rows = [d.change_list.item(i).text() for i in range(d.change_list.count())]
    assert "+  hello.txt  新增（未提交）" in rows


# ---------- 方案 ----------
def test_spec_page_cards_no_requirement_text(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    spec = d.views["spec"].toPlainText()
    assert "已确认的方案" not in spec and "🎯 完成目标" in spec and "新建 hello.txt 并写入 hi" in spec
    assert "🛠 方案" in spec and "① 在仓库根目录新建 hello.txt" in spec and "② 内容写 hi" in spec
    assert "ZZQ" not in spec and "YYX" not in spec and "验收标准" not in spec and "父" not in spec
    assert "退回时补充" not in spec and card.wt_path not in spec  # worktree 路径挪到「更多」页签
    assert card.wt_path in d.views["more"].toPlainText()
    h = d.views["spec"].property("h")
    assert "#f5f8ff" in h and "#f0faf4" in h  # 同需求页的蓝 / 绿卡片底色


def test_spec_page_supplement_note(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    card.spec += f"\n\n{SUPPLEMENT_HEAD}\n补充要求XYZ：别动 README"
    d._show_work(card)
    spec = d.views["spec"].toPlainText()
    assert "↩ 退回时补充的要求" in spec and "补充要求XYZ：别动 README" in spec
    assert SUPPLEMENT_HEAD not in spec and spec.index("🛠 方案") < spec.index("↩ 退回时补充的要求")


def test_spec_page_falls_back_to_spec_text(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    card.stages[0].rounds[-1].parsed = None
    card.spec = "【完成目标】\n只剩原文QQ\n\n【方案】\n1. 步骤"
    d._show_work(card)
    spec = d.views["spec"].toPlainText()
    assert "只剩原文QQ" in spec and "🎯 完成目标" not in spec


# ---------- 对话 ----------
def test_talk_req_stage_has_no_json_and_prompt_is_one_line(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    talk = d.views["talk"].toPlainText()
    assert "── 需求 · " in talk and "── 开发 · " in talk
    assert '{"goal"' not in talk and "```" not in talk
    assert "🎯 完成目标" in talk and "新建 hello.txt 并写入 hi" in talk and "🛠 方案" in talk
    first = card.stages[0].rounds[0].ask
    assert f"第 1 轮 · 程序 → AI：发了阶段指令（{len(first)} 字） ▸展开" in talk
    assert first[:40] not in talk
    d._toggle_ask(QUrl("#0-1"))
    t2 = d.views["talk"].toPlainText()
    assert first[:40] in t2 and "▾收起" in t2
    d._toggle_ask(QUrl("#0-1"))
    assert first[:40] not in d.views["talk"].toPlainText()


def test_talk_req_stage_without_parsed_shows_raw_reply(qtbot, board, win, fake, repo):
    card, d = to_dev(qtbot, board, win, fake, repo)
    card.stages[0].rounds[-1].parsed = None
    assert '"goal"' in html.unescape(re.sub(r"<[^>]+>", "", talk_html(board, card, set())))


def test_talk_long_reply_folds_and_user_bubble(qtbot, board, win, fake, repo):
    long_reply = "很长的回复。" * 150 + "尾巴TAIL"
    assert len(long_reply) > ASK_FOLD
    card, d = to_dev(qtbot, board, win, fake, repo, dev_reply=long_reply)
    talk = d.views["talk"].toPlainText()
    assert f"展开（共 {len(long_reply)} 字）" in talk and "尾巴TAIL" not in talk
    d._toggle_ask(QUrl("#1-1r"))
    t2 = d.views["talk"].toPlainText()
    assert "尾巴TAIL" in t2 and "收起" in t2
    # 你发的话：显示原文、靠右
    card.stages[1].rounds.append(Round("你（退回）", "请改一下QQQ", reply="好的，改了"))
    h = talk_html(board, card, set())
    plain = re.sub(r"<[^>]+>", "", h)
    assert "你（退回）" in plain and "请改一下QQQ" in plain and "好的，改了" in plain
    assert re.search(r"align=.right.", h) and "程序 → AI" in plain
    # 短回复不折叠
    assert "展开（共" not in re.sub(r"<[^>]+>", "", talk_html(board, card, set())).split("你（退回）")[1]

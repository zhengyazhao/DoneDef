"""完成目标（done 完成标准）+ 方案页：解析 / 提示词 / spec_draft / 验证逐条解析，
界面的三块布局（完成目标 / 需要你确认 / 方案）、方案页签、验证结果页。CLI 用假脚本、git 用真的。"""
import json
import re

import pytest

from donedef.board import STAGE_GUIDE, VERIFY_TAIL, VERDICT_RE, parse_done_checks
from donedef.cli import REQ_ANSWER_SUFFIX, REQ_JSON_SUFFIX, parse_req_json, req_view
from donedef.model import Round
from donedef.ui import (PENDING_MARK_STYLE, goal_plan_html, plan_html, plan_questions, result_html,
                           split_pending)
from tests.test_core import IDS, WAIT, add_project, wait_review

pytest_plugins = ["tests.test_core"]

Q1 = "（待你确认：导出哪些字段？，默认 日期/订单号/金额）"
Q2 = "（待你确认：文件名用什么格式）"
REQ9 = json.dumps({
    "goal": "用户能按月导出订单记录为 CSV",
    "done": ["点「导出」能下载 CSV，表头 日期/订单号/金额", "只导出所选月份的记录"],
    "plan": ["新增接口 GET /api/export?month=", "加导出函数写 CSV" + Q1, "订单页加「导出」按钮" + Q2],
}, ensure_ascii=False)
REQ9_REPLY = "好的。\n```json\n" + REQ9 + "\n```"
REQ9_NO_Q = "```json\n" + json.dumps({"goal": "g", "done": ["d1"], "plan": ["a", "b"]}, ensure_ascii=False) + "\n```"
VERIFY_REPLY = ("逐条核对：\n- ✓ 点「导出」能下载 CSV：接口返回 200 且表头正确\n- ✓ 只导出所选月份：按 month 过滤\n"
                "- ✗ 文件名带月份：现在固定叫 export.csv\n结论：不通过")


def plain(h):
    return re.sub(r"<[^>]+>", "", h)


def to_review(qtbot, board, win, fake, repo, reply):
    fake.plan(claude=[{"reply": reply}])
    item = board.new_manual_item("导出订单记录", "描述", "验收")
    card, err = board.start_card(item, add_project(board, repo).id, list(IDS))
    assert err is None, err
    wait_review(qtbot, card, "req")
    win.select(card.id)
    win.render()
    qtbot.waitUntil(lambda: not win.drawer.anim.state() == win.drawer.anim.State.Running, timeout=WAIT)
    return card, win.drawer


# ---------- 后端：解析 ----------
def test_parse_new_format_with_done():
    out = parse_req_json(REQ9_REPLY)
    assert out["goal"] == "用户能按月导出订单记录为 CSV"
    assert out["done"] == ["点「导出」能下载 CSV，表头 日期/订单号/金额", "只导出所选月份的记录"]
    assert out["plan"][1] == "加导出函数写 CSV" + Q1
    bare = '{"goal": " g ", "done": [" a ", "", "b"], "plan": ["x"]}'  # 去空白、去空项
    assert parse_req_json(bare) == {"goal": "g", "done": ["a", "b"], "plan": ["x"]}


def test_parse_goal_plan_without_done_and_old_format():
    assert parse_req_json('{"goal": "g", "plan": ["a"]}') == {"goal": "g", "done": [], "plan": ["a"]}
    old = {"understanding": "做导出", "todo": ["写接口"], "not_todo": [], "questions": ["要分页吗"]}
    assert parse_req_json(json.dumps(old, ensure_ascii=False)) == {
        "goal": "做导出", "done": [], "plan": ["写接口", "（待你确认：要分页吗）"]}


def test_parse_done_must_be_string_list():
    with pytest.raises(ValueError, match="done 不是字符串数组"):
        parse_req_json('{"goal": "g", "done": "x", "plan": []}')
    with pytest.raises(ValueError, match="done 不是字符串数组"):
        parse_req_json('{"goal": "g", "done": [1], "plan": []}')
    with pytest.raises(ValueError, match="缺少字段 plan"):  # done 可选、plan 仍必需
        parse_req_json('{"goal": "g", "done": ["a"]}')


def test_req_view_fills_done_for_old_parsed():
    legacy = {"goal": "g", "plan": ["a"]}  # 旧库里的 parsed 没有 done
    v = req_view(legacy)
    assert v == {"goal": "g", "plan": ["a"], "done": []} and "done" not in legacy  # 不改库里的原对象
    full = {"goal": "g", "done": ["d"], "plan": ["a"]}
    assert req_view(full) is full
    old = {"understanding": "u", "todo": ["t"], "not_todo": [], "questions": []}
    assert req_view(old)["done"] == []
    assert req_view(None) is None


# ---------- 后端：提示词 / spec ----------
def test_prompts_ask_for_done():
    assert ('"goal": "这张卡要干嘛（1 句）", "done": ["可验证的完成标准 1", "…"], '
            '"plan": ["步骤 1", "步骤 2（待你确认：…，默认 …）"]') in REQ_JSON_SUFFIX
    assert "能下载 CSV，表头是 日期/订单号/金额" in REQ_JSON_SUFFIX and "每条一句话" in REQ_JSON_SUFFIX
    assert "完成标准" in STAGE_GUIDE["req"] and "可验证" in STAGE_GUIDE["req"]
    assert "goal / done / plan" in REQ_ANSWER_SUFFIX and "done" in REQ_ANSWER_SUFFIX


def test_verify_tail_asks_per_criterion():
    assert "✓" in VERIFY_TAIL and "✗" in VERIFY_TAIL and "完成标准" in VERIFY_TAIL
    assert VERDICT_RE.findall("x\n结论：通过") == ["通过"]
    assert VERIFY_TAIL.rstrip().endswith("「结论：通过」或「结论：不通过」。")


def test_spec_draft_has_goal_done_plan_and_dev_prompt_carries_it(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    draft = board.spec_draft(card)
    assert draft == ("【完成目标】\n用户能按月导出订单记录为 CSV\n\n"
                     "【完成标准】\n- 点「导出」能下载 CSV，表头 日期/订单号/金额\n- 只导出所选月份的记录\n\n"
                     "【方案】\n1. 新增接口 GET /api/export?month=\n2. 加导出函数写 CSV" + Q1 + "\n3. 订单页加「导出」按钮" + Q2)
    assert board.confirm_spec(card)
    assert card.spec == draft
    wait_review(qtbot, card, "dev")  # 等开发跑完再收尾，避免停掉半路的子进程
    assert card.cur == 1  # 开发阶段的提示词用 card.spec，自然带上完成标准
    assert "【完成标准】\n- 点「导出」能下载 CSV" in board.main_prompt(card)
    card.cur = 2  # 验证阶段同样带上，并要求逐条核对
    vp = board.main_prompt(card)
    assert "【完成标准】" in vp and vp.endswith(VERIFY_TAIL)
    board.stop_all()


def test_spec_draft_without_done_omits_section(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, '```json\n{"goal": "g", "plan": ["a"]}\n```')
    assert board.spec_draft(card) == "【完成目标】\ng\n\n【方案】\n1. a"
    assert "【完成标准】" not in board.spec_draft(card)
    assert "☐" not in d.goal.text()


# ---------- 后端：逐条解析 ----------
def test_parse_done_checks_positive():
    assert parse_done_checks(VERIFY_REPLY) == [
        (True, "点「导出」能下载 CSV：接口返回 200 且表头正确"),
        (True, "只导出所选月份：按 month 过滤"),
        (False, "文件名带月份：现在固定叫 export.csv")]
    variants = "- ✔ a\n-　✘　b\n  - × c\n* ✓ d\n- ✗x"  # 常见变体：✔/✘/×、全角空格、缩进、*、符号后无空格
    assert parse_done_checks(variants) == [(True, "a"), (False, "b"), (False, "c"), (True, "d"), (False, "x")]


def test_parse_done_checks_negative():
    assert parse_done_checks("") == [] and parse_done_checks(None) == []
    assert parse_done_checks("看起来还行\n结论：通过") == []
    assert parse_done_checks("- 普通列表项\n✓ 没有短横线开头\n- ✓") == []  # 无符号 / 无文本都不算
    assert parse_done_checks("改了什么：\n- 新增 hello.txt") == []


# ---------- 界面：疑问拆分 ----------
def test_split_pending_with_default_variants():
    assert split_pending(Q1) == ("导出哪些字段？", "日期/订单号/金额")
    assert split_pending("（待你确认：要分页吗，默认 不分页）") == ("要分页吗", "不分页")
    assert split_pending("(待你确认: 要分页吗;默认:不分页)") == ("要分页吗", "不分页")
    assert split_pending("（待你确认：导出哪些字段？默认 日期 / 订单号）") == ("导出哪些字段？", "日期 / 订单号")
    assert split_pending("（待你确认：文件名格式？默认：订单记录-2026-10.csv）") == ("文件名格式？", "订单记录-2026-10.csv")


def test_split_pending_without_default_keeps_text():
    assert split_pending(Q2) == ("文件名用什么格式", None)
    assert split_pending("（待你确认：要分页吗）") == ("要分页吗", None)
    q, d = split_pending("（待你确认：是否默认开启？）")  # 「默认」出现在句中、不是分隔写法：整句保留
    assert (q, d) == ("是否默认开启？", None)


def test_plan_questions_global_numbering_and_markers():
    steps = ["步骤一" + Q1, "没有疑问", "步骤三" + Q2 + "，后面还有字", "步骤四（待你确认：a，默认 b）"]
    assert plan_questions(steps) == [("导出哪些字段？", "日期/订单号/金额"), ("文件名用什么格式", None), ("a", "b")]
    out = plan_html(steps)
    assert [out.index(f"❓{i}") for i in (1, 2, 3)] == sorted(out.index(f"❓{i}") for i in (1, 2, 3))
    assert "待你确认" not in out and "默认" not in out and "导出哪些字段" not in out
    assert out.count(PENDING_MARK_STYLE) == 3
    assert "，后面还有字" in out and "步骤一" in out


def test_goal_plan_html_blocks_and_confirmed_mode():
    p = parse_req_json(REQ9_REPLY)
    h = goal_plan_html(p)
    txt = plain(h)
    assert txt.index("🎯 完成目标") < txt.index("❓ 需要你确认（2）") < txt.index("🛠 方案")  # 疑问框在方案上面
    assert "☐ 点「导出」能下载 CSV" in txt and "☐ 只导出所选月份的记录" in txt
    assert "1. 导出哪些字段？" in txt and "默认：日期/订单号/金额" in txt and "2. 文件名用什么格式" in txt
    assert txt.count("默认：") == 1  # 拆不出默认的只显示原句
    assert "#f5dfa6" in h and "#fff8e6" in h and "#9a6200" in h
    c = goal_plan_html(p, confirmed=True)
    assert "✓ 已确认的疑问（按默认做）" in plain(c) and "需要你确认" not in plain(c)
    assert "#e3e6eb" in c and "#fafbfc" in c and "#6b7280" in c
    # 没有 done / 没有疑问：只显示 goal，疑问框整个不出现
    bare = plain(goal_plan_html({"goal": "只有目标", "done": [], "plan": ["a"]}))
    assert "☐" not in bare and "只有目标" in bare and "需要你确认" not in bare and "已确认的疑问" not in bare
    assert "☐" not in plain(goal_plan_html({"goal": "g", "plan": ["a"]}))  # 缺 done 键也不报错


# ---------- 界面：需求页 ----------
def test_req_page_three_blocks(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    goal = plain(d.goal.text())
    assert "🎯 完成目标" in goal and "用户能按月导出订单记录为 CSV" in goal and "要干嘛" not in goal
    assert "☐ 点「导出」能下载 CSV，表头 日期/订单号/金额" in goal and "☐ 只导出所选月份的记录" in goal
    assert d.pend_box.isVisible()
    pend = plain(d.pend.text())
    assert "❓ 需要你确认（2）" in pend
    assert "1. 导出哪些字段？" in pend and "默认：日期/订单号/金额" in pend
    assert "2. 文件名用什么格式" in pend and pend.count("默认：") == 1
    plan = plain(d.plan.text())
    assert "② 加导出函数写 CSV ❓1" in plan and "③ 订单页加「导出」按钮 ❓2" in plan and "待你确认" not in plan
    assert "① 新增接口 GET /api/export?month=" in plan
    # 疑问框在完成目标和方案之间
    top = lambda w: w.mapTo(d, w.rect().topLeft()).y()
    assert top(d.goal) < top(d.pend_box) < top(d.plan_box)


def test_req_page_no_questions_hides_box(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_NO_Q)
    assert not d.pend_box.isVisible() and d.pend.text() == ""
    assert "☐ d1" in plain(d.goal.text()) and d.plan_box.isVisible()


def test_req_page_failed_round_hides_pend_box(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    assert d.pend_box.isVisible()
    card.stages[0].rounds[-1].parsed = None  # 这一轮没有可识别的方案
    d._show_req(card)
    assert not d.pend_box.isVisible() and not d.plan_box.isVisible()
    assert "🎯 完成目标" in d.goal.text()


# ---------- 界面：方案页签 / 更多 ----------
def test_spec_tab_clean_and_worktree_in_more(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    assert board.confirm_spec(card)
    wait_review(qtbot, card, "dev")
    win.select(card.id)
    win.render()
    spec = d.views["spec"].toPlainText()
    assert "已确认的方案（开发和验证都照这个做）" not in spec and "开发和验证都照这个做" not in spec
    assert card.wt_path not in spec and "worktree" not in spec
    assert "🎯 完成目标" in spec and "☐ 只导出所选月份的记录" in spec and "🛠 方案" in spec
    assert "已确认的疑问（按默认做）" in spec and "需要你确认" not in spec
    assert "1. 导出哪些字段？" in spec and "② 加导出函数写 CSV ❓1" in spec
    more = d.views["more"].toPlainText()
    assert "worktree：" in more and card.wt_path in more
    board.stop_all()


def test_spec_tab_keeps_supplement(qtbot, board, win, fake, repo):
    from donedef.board import SUPPLEMENT_HEAD
    from donedef.ui import spec_html
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    card.spec = board.spec_draft(card) + SUPPLEMENT_HEAD + "还要支持按周导出"
    out = plain(spec_html(board, card))
    assert "↩ 退回时补充的要求" in out and "还要支持按周导出" in out and "已确认的疑问" in out


# ---------- 界面：验证结果页 ----------
def verify_card(qtbot, board, win, fake, repo):
    card, d = to_review(qtbot, board, win, fake, repo, REQ9_REPLY)
    card.cur = 2
    st = card.stage
    assert st.stage == "verify"
    st.status = "review"
    return card, st


def test_verify_result_lists_criteria(qtbot, board, win, fake, repo):
    card, st = verify_card(qtbot, board, win, fake, repo)
    st.rounds.append(Round("流水线", "p", reply=VERIFY_REPLY))
    h = result_html(board, card, set())
    txt = plain(h)
    assert "✖ AI 验证：不通过" in txt
    assert txt.index("AI 验证：不通过") < txt.index("✓ 点「导出」能下载 CSV：接口返回 200 且表头正确")
    assert "✓ 只导出所选月份：按 month 过滤" in txt and "✗ 文件名带月份：现在固定叫 export.csv" in txt
    assert h.count("color:#23784f'>✓ ") == 2 and h.count("color:#c4363a'>✗ ") == 1
    assert "看 AI 原话" in txt  # 原话入口还在


def test_verify_result_falls_back_to_verdict_only(qtbot, board, win, fake, repo):
    card, st = verify_card(qtbot, board, win, fake, repo)
    st.rounds.append(Round("流水线", "p", reply="都看过了，没问题\n结论：通过"))
    txt = plain(result_html(board, card, set()))
    assert "✓ AI 验证：通过" in txt and "▸ 看 AI 原话" in txt
    assert "✗" not in txt and "- ✓" not in txt and "✓ 都看过了" not in txt

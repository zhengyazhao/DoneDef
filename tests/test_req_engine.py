"""需求阶段只出「要干嘛 + 方案」，确认时程序自动存进 card.spec，不再有可编辑任务书。
不连真 CLI（假脚本）、git 用真的。"""
import json

import pytest

from donedef.cli import REQ_ANSWER_SUFFIX, REQ_JSON_SUFFIX, parse_req_json, req_view
from donedef.model import WorkItem
from tests.test_core import IDS, WAIT, add_project, git_out, wait_review
from tests.test_kanban import start_no_repo
from tests.test_main_agent import arg, main_agent, prompt_of

pytest_plugins = ["tests.test_core"]


def reply(goal, plan):
    return "好的。\n```json\n" + json.dumps({"goal": goal, "plan": plan}, ensure_ascii=False) + "\n```"


R1 = reply("新建 hello.txt", ["写 hello.txt", "内容 hi（待你确认：末尾要换行吗，默认 换行）"])
R2 = reply("新建 hello.txt，末尾换行", ["写 hello.txt，内容 hi 加换行"])
DRAFT1 = "【完成目标】\n新建 hello.txt\n\n【方案】\n1. 写 hello.txt\n2. 内容 hi（待你确认：末尾要换行吗，默认 换行）"
OLD = {"understanding": "做导出", "todo": ["写接口", " ", "写页面"], "not_todo": ["不改权限"], "questions": ["要分页吗"]}


# ---------- 解析 ----------
def test_parse_new_format():
    assert parse_req_json(R1) == {"goal": "新建 hello.txt", "done": [],
                                  "plan": ["写 hello.txt", "内容 hi（待你确认：末尾要换行吗，默认 换行）"]}
    bare = '{"goal": "  g ", "plan": [" a ", "", "  "]}'  # 无代码块也行；去空白、去空项
    assert parse_req_json(bare) == {"goal": "g", "done": [], "plan": ["a"]}
    assert parse_req_json('{"goal": "g", "plan": []}') == {"goal": "g", "done": [], "plan": []}


def test_parse_old_format_converts():
    out = parse_req_json(json.dumps(OLD, ensure_ascii=False))
    assert out == {"goal": "做导出", "done": [], "plan": ["写接口", "写页面", "（待你确认：要分页吗）"]}  # 不做丢弃
    assert "not_todo" not in out and "questions" not in out


def test_req_view():
    assert req_view(None) is None
    new = {"goal": "g", "done": ["d"], "plan": ["a"]}
    assert req_view(new) is new  # 已是新格式原样返回
    # 库里的旧 parsed 已被旧版 parse_req_json 清洗过；两条路对同一份输入结果一致
    old = {"understanding": "做导出", "todo": ["写接口", "写页面"], "not_todo": ["不改权限"], "questions": ["要分页吗"]}
    assert req_view(old) == {"goal": "做导出", "done": [], "plan": ["写接口", "写页面", "（待你确认：要分页吗）"]}
    assert req_view(old) == parse_req_json(json.dumps(old, ensure_ascii=False))


def test_parse_invalid():
    with pytest.raises(ValueError, match="缺少字段 goal、plan"):
        parse_req_json('{"foo": 1}')
    with pytest.raises(ValueError, match="缺少字段 plan"):
        parse_req_json('{"goal": "g"}')
    with pytest.raises(ValueError, match="缺少字段 goal"):
        parse_req_json('{"plan": ["a"]}')
    with pytest.raises(ValueError, match="goal 不是字符串"):
        parse_req_json('{"goal": 1, "plan": []}')
    with pytest.raises(ValueError, match="plan 不是字符串数组"):
        parse_req_json('{"goal": "g", "plan": "x"}')
    with pytest.raises(ValueError, match="plan 不是字符串数组"):
        parse_req_json('{"goal": "g", "plan": ["a", 2]}')
    with pytest.raises(ValueError, match="不是合法 JSON"):
        parse_req_json("我理解的是……")
    with pytest.raises(ValueError, match="JSON 不是对象"):
        parse_req_json("[1]")
    with pytest.raises(ValueError, match="understanding 不是字符串"):
        parse_req_json('{"understanding": 1, "todo": [], "not_todo": [], "questions": []}')
    with pytest.raises(ValueError, match="questions 不是字符串数组"):
        parse_req_json('{"understanding": "u", "todo": [], "not_todo": [], "questions": "q"}')


def test_suffix_text():
    assert ('{"goal": "这张卡要干嘛（1 句）", "done": ["可验证的完成标准 1", "…"], '
            '"plan": ["步骤 1", "步骤 2（待你确认：…，默认 …）"]}') in REQ_JSON_SUFFIX
    assert "```json" in REQ_JSON_SUFFIX and "待你确认" in REQ_JSON_SUFFIX and "默认" in REQ_JSON_SUFFIX
    assert "goal" in REQ_ANSWER_SUFFIX and "plan" in REQ_ANSWER_SUFFIX and "understanding" not in REQ_ANSWER_SUFFIX


# ---------- 主 agent：需求 → 确认 → 开发 → 验证 ----------
def test_main_agent_flow_auto_spec(qtbot, board, fake, repo):
    fake.plan(claude=[
        {"reply": R1, "session": "C1"},
        {"reply": "做好了\n改了什么：\n- 新建 hello.txt", "session": "C1", "write": {"hello.txt": "hi\n"}},
        {"reply": "都做到了\n结论：通过", "session": "C1"}])
    proj = add_project(board, repo)
    item = board.new_manual_item("新建 hello.txt", "在仓库根目录新建 hello.txt，内容 hi", "")
    card, err = board.start_card(item, proj.id, main_agent(board))
    assert err is None, err
    wait_review(qtbot, card, "req")

    p1 = prompt_of(fake.calls("claude")[0])
    assert "检查需求有没有遗漏或没说清的地方" in p1 and "给出实现方案" in p1
    assert p1.endswith(REQ_JSON_SUFFIX) and '"goal"' in p1 and '"plan"' in p1 and "（待你确认：…，默认 …）" in p1
    assert "任务书" not in p1 and '"questions"' not in p1 and "（待你确认：…）问我做到什么程度算完成" in p1

    # 存进库的 parsed 是新格式；待确认步骤被数出来
    assert card.stages[0].rounds[-1].parsed == parse_req_json(R1)
    assert board.open_questions(card) == ["内容 hi（待你确认：末尾要换行吗，默认 换行）"]
    assert board.spec_draft(card) == DRAFT1

    # 确认不传 text：自动存「要干嘛 + 方案」
    assert board.confirm_spec(card)
    assert card.spec == DRAFT1
    wait_review(qtbot, card, "dev")
    p2 = prompt_of(fake.calls("claude")[1])
    assert "【需求】\n在仓库根目录新建 hello.txt，内容 hi" in p2
    assert "【确认的方案】\n" + DRAFT1 in p2
    assert p2.index("【需求】") < p2.index("【确认的方案】") and "任务书" not in p2
    assert "照下面的需求和确认的方案实现" in p2 and "照其中的默认做法做，不要再停下来问" in p2

    assert board.commit_and_pass(card) is None
    wait_review(qtbot, card, "verify")
    p3 = prompt_of(fake.calls("claude")[2])
    assert "【需求】\n在仓库根目录新建 hello.txt，内容 hi" in p3 and "【确认的方案】\n" + DRAFT1 in p3
    assert "任务书" not in p3 and "照需求和确认的方案核对" in p3 and "按其中的默认做法核对" in p3
    assert board.pass_stage(card) and card.column == "done"
    assert git_out(repo, "log", "-1", "--format=%s", "feature/task-l1") == "L1 开发：新建 hello.txt"


def test_reply_reruns_with_new_format_and_open_questions(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": R1, "session": "C1"}, {"reply": R2, "session": "C1"}])
    proj = add_project(board, repo)
    item = board.new_manual_item("新建 hello.txt", "d", "")
    card, err = board.start_card(item, proj.id, main_agent(board))
    assert err is None, err
    wait_review(qtbot, card, "req")
    assert len(board.open_questions(card)) == 1
    assert board.ask(card, "要换行")
    wait_review(qtbot, card, "req")
    c2 = fake.calls("claude")[1]["args"]
    assert arg(c2, "--resume") == "C1" and prompt_of(fake.calls("claude")[1]) == "要换行" + REQ_ANSWER_SUFFIX
    assert board.open_questions(card) == []  # 最新一轮没有待确认步骤
    assert board.spec_draft(card) == "【完成目标】\n新建 hello.txt，末尾换行\n\n【方案】\n1. 写 hello.txt，内容 hi 加换行"


def test_confirm_spec_explicit_text_and_blank(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": R2}, {"reply": "开发完成"}], grok=[{"reply": "开发完成"}])
    proj = add_project(board, repo)
    item = board.new_manual_item("t", "d", "")
    card, err = board.start_card(item, proj.id, IDS)
    assert err is None, err
    assert not board.confirm_spec(card)  # 还在跑
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "   ")  # 空白 = 用草稿
    assert card.spec == board.spec_draft(card) and card.spec.startswith("【完成目标】\n新建 hello.txt，末尾换行")
    board.stop_all()


def test_old_round_in_db_still_readable(qtbot, board, fake, repo):
    """库里旧轮次的 parsed 是旧格式：open_questions / spec_draft 经 req_view 读出"""
    fake.plan(claude=[{"reply": R2}])
    proj = add_project(board, repo)
    card, err = board.start_card(board.new_manual_item("t", "d", ""), proj.id, IDS)
    assert err is None, err
    wait_review(qtbot, card, "req")
    card.stages[0].rounds[-1].parsed = dict(OLD, todo=["写接口", "写页面"])
    assert board.open_questions(card) == ["（待你确认：要分页吗）"]
    assert board.spec_draft(card) == "【完成目标】\n做导出\n\n【方案】\n1. 写接口\n2. 写页面\n3. （待你确认：要分页吗）"
    card.stages[0].rounds[-1].parsed = None
    assert board.open_questions(card) == [] and board.spec_draft(card) == ""


def test_helper_prompt_uses_requirement_and_plan(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": R2, "session": "C1"}, {"reply": "开发完成", "session": "C1"}])
    proj = add_project(board, repo)
    item = board.new_manual_item("t", "需求原文", "")
    card, err = board.start_card(item, proj.id, main_agent(board))
    assert err is None, err
    wait_review(qtbot, card, "req")
    helper = board.agents[0]
    assert "【需求】\n需求原文" in board.helper_prompt(card, helper) and "【确认的方案】" not in board.helper_prompt(card, helper)
    assert board.confirm_spec(card)
    wait_review(qtbot, card, "dev")
    hp = board.helper_prompt(card, helper)
    assert "【需求】\n需求原文" in hp and "【确认的方案】\n" + card.spec in hp and "任务书" not in hp
    board.stop_all()


# ---------- 不关联仓库的卡 ----------
def test_finish_after_req_default_draft(qtbot, board, fake):
    fake.plan(claude=[{"reply": R1}])
    item = WorkItem("8", "Task", "调研", "To Do", "看看导出怎么做", "", "azure")
    card = start_no_repo(board, item)
    wait_review(qtbot, card)
    assert board.finish_after_req(card)
    assert card.spec == DRAFT1 and card.stages[0].status == "passed" and card.comment == "draft"
    assert card.comment_text.splitlines()[2:4] == ["方案：", "【完成目标】"] and "任务书" not in card.comment_text
    assert DRAFT1 in card.comment_text


def test_finish_after_req_blank_text_uses_draft_and_manual_done(qtbot, board, fake):
    fake.plan(claude=[{"reply": R2}])
    card = start_no_repo(board, board.new_manual_item("调研", "", ""))
    assert not board.finish_after_req(card)  # 还在跑
    wait_review(qtbot, card)
    assert board.finish_after_req(card, "  \n")
    assert card.spec.startswith("【完成目标】\n新建 hello.txt，末尾换行") and card.comment == "skip" and card.column == "done"


def test_attach_repo_default_draft(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": R1}], grok=[{"reply": "开发完成", "sleep": 60}])
    item = WorkItem("8", "Task", "调研", "To Do", "看看导出怎么做", "", "azure")
    card = start_no_repo(board, item)
    wait_review(qtbot, card)
    proj = add_project(board, repo)
    assert board.attach_repo(card, proj.id) is None
    assert card.project_id == proj.id and card.spec == DRAFT1
    assert card.stage.stage == "dev" and card.stage.status == "running"
    qtbot.waitUntil(lambda: len(fake.calls("grok")) == 1, timeout=WAIT)
    # 旧模式卡的开发提示词走模板渲染，{spec} 仍是 card.spec；这里只确认 spec 已冻结且没有任务书字样
    assert "任务书" not in card.spec
    board.stop_all()


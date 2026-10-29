"""新库只种 6 个预置角色模板。按阶段写的 a1～a5 老模板（早期版本的种子）在这里补进新库，
继续覆盖「老卡三阶段各用各的模板」这条路径；要测真实种子的测试打 @pytest.mark.real_seed。"""
import pytest

from donedef import board as board_mod
from donedef import model
from donedef.model import Agent


def legacy_agents() -> list[Agent]:
    return [
        Agent("a1", "Claude 开发", "claude", "edit",
              "按下面已确认的需求说明实现，只改必要的文件，完成后用中文总结改了什么。\n需求说明：\n{spec}"),
        Agent("a2", "Grok 开发", "grok", "edit", "实现下面的需求：\n{spec}"),
        Agent("a3", "Codex 补测试", "codex", "edit",
              "开发已完成：{dev_reply}\n改动：{diff_stat}\n请按需求说明补测试并实际运行：\n{spec}",
              model="gpt-5.6-sol", effort="medium"),
        Agent("a4", "Codex 审查(只读)", "codex", "read",
              "对照需求说明逐条审查改动，给出通过/不通过及理由。\n需求说明：\n{spec}\n改动：{diff}",
              model="gpt-5.6-sol", effort="medium"),
        Agent("a5", "Claude 理解需求", "claude", "read",
              "阅读下面的任务和相关代码，不要改任何文件。用中文按「我的理解 / 要做 / 不做 / 疑问」输出。\n"
              "标题：{title}\n描述：{description}\n验收标准：{acceptance}\n"
              "没有验收标准时，在疑问里问我做到什么程度算完成。"),
    ]


def pytest_configure(config):
    config.addinivalue_line("markers", "real_seed: 用真实的预置模板种子，不补老测试的 a1～a5")


@pytest.fixture(autouse=True)
def legacy_seed(request, monkeypatch):
    if request.node.get_closest_marker("real_seed"):
        return
    monkeypatch.setattr(board_mod, "seed_agents", lambda: legacy_agents() + model.seed_agents())

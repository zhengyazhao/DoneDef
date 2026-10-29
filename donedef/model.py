"""数据模型（从 prototype_qt/model.py 改造）：去掉假数据，加会话 ID、失败原因、worktree 绝对路径。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .repo import short_label

# 流水线三段：先让 AI 理解需求、你确认后冻结成需求说明，再开发 → 验证
STAGES = ("req", "dev", "verify")
# "test" 只留给数据库里已有的四阶段老卡显示用
STAGE_NAME = {"req": "需求", "dev": "开发", "test": "测试", "verify": "验证"}
NO_TEST_REPLY = "（本流程没有测试阶段）"
CLI_NAME = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}
PERM_NAME = {"read": "只读", "edit": "可改", "full": "放开"}
# 模板可勾选的能力：开始 / 续聊时以 MCP 挂给 CLI；grok 没有单次运行挂 MCP 的参数，不支持
CAPS = {"codegraph": "代码检索", "playwright": "看原型"}
CAP_CLIS = ("claude", "codex")
# 模型候选（可编辑下拉框）；grok 取自 `grok models` 实测
MODEL_CHOICES = {
    "claude": ("sonnet", "opus", "haiku"),
    "codex": ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    "grok": ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"),
}
EFFORTS = ("low", "medium", "high")
EFFORTS_EXTRA = {"claude": ("xhigh", "max")}


def effort_choices(cli: str) -> tuple[str, ...]:
    return EFFORTS + EFFORTS_EXTRA.get(cli, ())


# 提示词模板可用的变量（阶段之间传递的内容）
PROMPT_VARS = {
    "{title}": "卡片标题",
    "{description}": "卡片描述",
    "{acceptance}": "验收标准（手写任务；Azure 卡片取 Microsoft.VSTS.Common.AcceptanceCriteria，Basic 流程没有就为空）",
    "{spec}": "已确认的需求说明（需求阶段之后才有）",
    "{dev_reply}": "开发阶段最终回复",
    "{diff_stat}": "git diff --stat",
    "{diff}": "完整 diff",
    "{test_reply}": "测试阶段最终回复（新卡没有测试阶段，填「（本流程没有测试阶段）」）",
}


@dataclass
class Agent:
    id: str
    name: str
    cli: str  # claude / codex / grok
    perm: str  # read / edit / full
    prompt: str
    model: str = ""  # 空 = 用 CLI 自己的默认模型，不传参数
    effort: str = ""  # 空 = 用 CLI 默认推理强度，不传参数
    caps: list[str] = field(default_factory=list)  # 能力：CAPS 的键


@dataclass
class Project:
    id: str
    name: str
    repo: str  # 你粘贴的原文；目前只接受本地文件夹
    repo_kind: str  # azure / github / local
    path: str  # 本地仓库目录
    tasks: str = "manual"  # 任务来源：目前只有 manual（手写）
    org: str = ""
    ado_project: str = ""
    base_branch: str = "main"  # 以下三项自动识别，高级设置里可改
    build_cmd: str = ""
    test_cmd: str = ""
    cloned: bool = True

    @property
    def repo_short(self) -> str:
        return short_label(self.repo)


@dataclass
class Round:
    by: str  # "流水线" / "你" / "你（退回）"
    ask: str  # 实际发给 CLI 的提示词
    reply: str | None = None
    end: str | None = None  # failed / quota / stopped
    parsed: dict | None = None  # 需求阶段：{goal, plan}
    session_id: str = ""  # 这一轮跑在哪个会话里
    log: str = ""  # 原始 stdout 落盘位置


@dataclass
class HelperRun:
    """某阶段主 agent 跑完后依次只读跑的额外角色；只保留最近一次的结果"""
    agent_id: str
    status: str = "pending"  # pending / running / done / failed / stopped
    session_id: str = ""
    reply: str = ""
    log: str = ""
    elapsed: int = 0
    error: str = ""


@dataclass
class StageRun:
    stage: str
    agent_id: str
    status: str = "pending"  # pending / running / checking / review / passed
    flag: str | None = None  # failed / quota / stopped
    rounds: list[Round] = field(default_factory=list)
    output: list[str] = field(default_factory=list)  # 人能看懂的动作行（读 / 改 / 跑命令…）
    checks: dict[str, tuple[str, int]] = field(default_factory=dict)  # 类型 → (命令, 退出码)
    elapsed: int = 0  # 秒，本阶段累计
    session_id: str = ""  # 当前会话；空 = 下一轮新开会话
    error: str = ""  # 失败原因（显示在卡片上）
    checking: str = ""  # 正在跑的自检命令
    helpers: list[HelperRun] = field(default_factory=list)  # 本阶段的额外角色

    def running_helper(self) -> HelperRun | None:
        return next((h for h in self.helpers if h.status == "running"), None)

    def last_reply(self) -> str:
        """最近一次有回复的轮次的回复；都没有就空串"""
        for r in reversed(self.rounds):
            if r.reply:
                return r.reply
        return ""


@dataclass
class WorkItem:
    """任务：手写任务编号 L1、L2…"""
    id: int | str
    type: str
    title: str
    state: str
    description: str
    acceptance: str
    source: str = "manual"  # azure / manual

    @property
    def label(self) -> str:
        return f"#{self.id}" if self.source == "azure" else str(self.id)


# 看板按 Azure 状态分栏：完成态 → Done，未开始态 → To Do，其余 → Doing
TODO_STATES = ("To Do", "New", "Proposed", "")
DONE_STATES = ("Done", "Closed", "Removed")
BOARD_COLUMNS = (("todo", "To Do"), ("doing", "Doing"), ("done", "Done"))


# 状态分类（workitemtypes/{类型}/states 的 category）→ 列；取不到分类时按名映射
CATEGORY_COLUMN = {"Proposed": "todo", "InProgress": "doing", "Resolved": "done", "Completed": "done", "Removed": "done"}


def state_column(state: str, category: str = "") -> str:
    if category in CATEGORY_COLUMN:
        return CATEGORY_COLUMN[category]
    if state in DONE_STATES:
        return "done"
    if state in TODO_STATES:
        return "todo"
    return "doing"


@dataclass
class Card:
    item: WorkItem
    project_id: str | None  # 关联仓库（projects 表的行）；None = 不关联仓库（调研 / 建模）
    stages: list[StageRun]
    cur: int = 0
    spec: str = ""  # 确认后冻结的需求说明
    comment: str = "none"  # none / draft / sent / skip（手写任务不回写）
    comment_text: str = ""
    worktree_cleaned: bool = False
    wt_path: str = ""  # worktree 绝对路径

    @property
    def id(self):
        return self.item.id

    @property
    def stage(self) -> StageRun:
        return self.stages[self.cur]

    @property
    def branch(self) -> str:
        if self.item.source == "azure":
            return f"feature/ado-{self.item.id}"
        return f"feature/task-{str(self.item.id).lower()}"

    @property
    def column(self) -> str:
        """本地进度：running / review / done，只由运行状态和用户操作决定（看板分栏另按 Azure 状态，见 state_column）"""
        if self.comment in ("sent", "skip"):
            return "done"
        if self.stage.status in ("running", "checking"):
            return "running"
        return "review"


def seed_agents() -> list[Agent]:
    """新库预置的 6 个角色模板：提示词只写角色说明，阶段指令由程序内置"""
    return [Agent(f"p{i}", *row) for i, row in enumerate(PRESETS, 1)]


# (名字, CLI, 权限, 角色说明, 模型, 推理强度, 能力)；codex 的默认模型 ChatGPT 账号用不了，显式指定
PRESETS = (
    ("架构师", "claude", "read",
     "你是资深软件架构师。关注模块边界、依赖方向、数据模型和接口设计是否合理，改动是否和现有架构一致、有没有更简单的方案；"
     "指出风险时写明具体文件和理由。", "", "", ["codegraph"]),
    ("测试", "codex", "edit",
     "你是测试工程师。关注验收标准能不能被测试证明：补齐缺失的单元 / 集成测试并实际运行，覆盖边界和异常路径；"
     "不写没有断言的假测试，测试失败如实报告。", "gpt-5.6-sol", "medium", ["codegraph"]),
    ("需求找茬", "claude", "read",
     "你是挑剔的需求评审。找出需求里的歧义、矛盾、遗漏的场景和边界条件、没写清的验收标准，逐条列出，"
     "并说明为什么会导致做错；不替需求方做决定。", "", "", []),
    ("开发", "claude", "edit",
     "你是严谨的开发工程师。先读懂相关代码再动手，只改完成任务必需的文件，沿用项目现有的写法和命名，改完自己跑构建和测试。",
     "", "", ["codegraph"]),
    ("代码审查", "codex", "read",
     "你是代码审查员。逐个看改动：正确性、异常处理、边界条件、安全、可读性，以及有没有超出需求范围；"
     "按「必须修 / 建议 / 可选」分级列出问题，附文件和行号。", "gpt-5.6-sol", "medium", ["codegraph"]),
    ("原型审核", "claude", "read",
     "你是交互和视觉审核。用浏览器打开原型或页面，检查布局、文案、交互流程是否符合需求，窄屏下是否可用；"
     "每个问题写清在哪一页、怎么复现。", "", "", ["playwright"]),
)


def missing_presets(agents: list[Agent]) -> list[Agent]:
    """老库要补的预置模板：按名字去重，已存在的同名模板不覆盖；ID 避开已有的"""
    names, ids = {a.name for a in agents}, {a.id for a in agents}
    out = []
    for a in seed_agents():
        if a.name in names:
            continue
        n = 1
        while a.id in ids:
            a.id, n = f"p{len(PRESETS) + n}", n + 1
        ids.add(a.id)
        out.append(a)
    return out

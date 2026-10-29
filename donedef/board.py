"""真实流转引擎：接口沿用 prototype_qt/board.py，阶段由真实 CLI 子进程驱动、数据落 SQLite。
迭代看板：全局 Azure 设置、记住的仓库、卡片可不关联仓库；Azure 卡片的取卡与交付记录回写。
一个主 agent 跑完三阶段 + 额外角色、只有 Task 跑 AI、交付记录追加到 Task 描述、MCP 能力。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from . import azure, worktree
from .clis import BgJob, load_json, parse_claude_quota, save_json
from .cli import (
    REQ_ANSWER_SUFFIX, REQ_JSON_SUFFIX, StreamParser, build_cmd, child_env, mcp_servers, parse_req_json, req_view,
)
from .db import DB
from .model import (
    CAP_CLIS, NO_TEST_REPLY, STAGE_NAME, STAGES, Agent, Card, HelperRun, Project, Round, StageRun, WorkItem,
    missing_presets, seed_agents,
)
from .repo import detect
from .runner import StageProcess
from .worktree import GitError

RESTART_LINE = "程序重启，上次的进程已不在"
SUPPLEMENT_HEAD = "【补充要求（退回时追加）】"
PAUSED_LINE = "已暂停"
ABORTED_LINE = "已中止，本轮作废"
RESUME_TEXT = "刚才被暂停了，接着把没做完的做完"
DB_FILENAME = "agentboard.db"  # 早期版本用的文件名，保留以兼容已有数据


AI_OFF = "这个组织没允许交给 AI（点「设置」→ Azure 里勾选）"
NO_ADO = "这张卡开始时没记下组织 / 项目，确认不了写回目标，不能写回（可以复制交付记录手动贴到 Azure）"
NOT_TASK = "只有 Task 能交给 AI；{type} 只能看和改状态"
PRESETS_FLAG = "presets_v6"  # 老库补过预置模板就记下，之后你删掉的预置模板不会再冒出来
WRITEBACK_RETRY = "卡片刚被别人改过，再点一次重试"
CHANGES_HEAD = "改了什么："


def parse_changes(reply: str) -> list[str]:
    """回复里最后一个「改了什么：」之后以「- 」开头的行（交付记录和工作页「结果」共用）；解析不到返回 []"""
    if CHANGES_HEAD not in reply:
        return []
    return [x.strip() for x in reply.rsplit(CHANGES_HEAD, 1)[1].splitlines() if x.strip().startswith("- ")]
VERDICT_RE = re.compile(r"结论[:：]\s*(不通过|通过)")
# 验证回复里逐条对照完成标准的行：「- ✓ 标准：依据」「- ✗ 标准：原因」（兼容 ✔/✘/×/❌ 与全角空格）
CHECK_LINE_RE = re.compile(r"^\s*[-－•*]\s*([✓✔☑]|[✗✘×❌✖])[\s\u3000]*(.+)$")


def parse_done_checks(reply: str) -> list[tuple[bool, str]]:
    """解析验证回复里逐条完成标准的行，返回 [(是否通过, 文本)]；一行都没有就是 []"""
    out = []
    for line in (reply or "").splitlines():
        m = CHECK_LINE_RE.match(line)
        if m and m.group(2).strip():
            out.append((m.group(1) in "✓✔☑", m.group(2).strip()))
    return out
NO_ACCEPTANCE_HINT = "（没写验收标准，请在方案里用（待你确认：…）问我做到什么程度算完成）"

# 主 agent 的阶段指令：程序内置，模板提示词只写角色说明
STAGE_GUIDE = {
    "req": "【当前阶段：需求分析】只读，不要改任何文件。读懂下面的需求（关联了代码仓库就结合代码看），"
           "先定义这张卡做完是什么样（完成标准，每条必须可验证），再梳理要干嘛，检查需求有没有遗漏或没说清的地方，给出实现方案；遗漏的地方写进对应步骤并给出默认做法。",
    "dev": "【当前阶段：开发】照下面的需求和确认的方案实现，只改完成任务必需的文件，沿用项目现有写法，改完自己跑构建和测试。"
           "方案里标「待你确认」的地方已经确认过，照其中的默认做法做，不要再停下来问。",
    "verify": "【当前阶段：验证】照需求和确认的方案核对本卡的改动：是否都做到了、有没有超出范围、有没有明显缺陷。"
              "方案里标「待你确认」的地方按其中的默认做法核对。以核对为主，不要改实现代码。",
}
DEV_TAIL = f"\n\n回复最后用「{CHANGES_HEAD}」开头，每行「- 」列出一项改动。"
VERIFY_TAIL = ("\n\n请逐条对照方案里的【完成标准】（老卡没有就对照方案步骤）核对，每条一行：通过写「- ✓ 标准：依据」，没做到写「- ✗ 标准：原因」。"
               "回复最后一行只写「结论：通过」或「结论：不通过」。")


class Board(QObject):
    changed = Signal()
    notify = Signal(str, str)  # 标题, 内容
    quota_changed = Signal()
    ai_done = Signal(str)  # 一轮 AI 跑完（含失败 / 停止），参数 = CLI；界面据此刷新 codex 额度

    def __init__(self, data_dir: Path, env: dict[str, str] | None = None, ado: azure.Client | None = None):
        """data_dir 下放数据库文件、worktrees/、logs/；env 为空 = 每次按白名单从 os.environ 取；
        ado = Azure 客户端（测试里注入假 HTTP）"""
        super().__init__()
        self.data_dir = Path(data_dir).resolve()
        self.db = DB(self.data_dir / DB_FILENAME)
        self._env = env
        self.ado = ado or azure.Client()
        self.sending: dict[str, BgJob] = {}  # 卡号 → 正在写回 Task 的请求
        self.writeback_errors: dict[str, str] = {}  # 卡号 → 上次写回失败的原因（不落库，重开后草稿还在、可再发）
        self._meta: dict[str, dict] = {}  # 卡号 → 开始时记下的 {main: 主 agent 模式, ado: 组织/项目}（settings 表）
        self._h0: dict[str, float] = {}  # 卡号 → 当前额外角色的开始时刻
        if self.db.is_empty():
            self.projects, self.agents, self.cards = [], seed_agents(), []
            self.db.set_setting(PRESETS_FLAG, "1")
            self.db.save_all(self.projects, self.agents, self.cards)
        else:
            self.projects, self.agents, self.cards = self.db.load()
            if self.db.setting(PRESETS_FLAG) != "1":  # 早期的老库：补预置模板，只补一次
                self.agents += missing_presets(self.agents)
                self.db.set_setting(PRESETS_FLAG, "1")
            self._recover()
        # 全局 Azure 设置；未开始的手写任务，都在 settings 表
        self.azure_org = self.db.setting("azure_org")
        self.azure_project = self.db.setting("azure_project")
        self.todo_items: list[WorkItem] = [
            WorkItem(d["id"], "任务", d["title"], "本地", d["description"], d["acceptance"], "manual")
            for d in json.loads(self.db.setting("manual_todo", "[]"))]
        self.procs: dict[str, StageProcess] = {}  # 卡号 → 正在跑的进程（AI 或自检）
        self._stop_intent: dict[str, str] = {}  # 卡号 → 这次停止的意图（pause / abort），进程退出后取用并清掉
        self._t0: dict[str, tuple[float, int]] = {}  # 卡号 → (开始时刻, 开始时已累计秒数)
        self.notifications: list[tuple[str, str]] = []
        # 额度最近值与 CLI / 模型检测缓存；坏文件不拦启动，原因记下来给界面显示
        self.cache_error = ""
        self.quota = self._load_cache("quota.json")
        self.cli_cache = self._load_cache("clis.json")
        # 落库：有变化后 1 秒内保存一次（不随每行输出写库）
        self._save_timer = QTimer(self, singleShot=True, interval=1000)
        self._save_timer.timeout.connect(self.save)
        self.changed.connect(self._schedule_save)
        # 流式输出很密：合并成 250ms 刷新一次界面
        self._ui_timer = QTimer(self, singleShot=True, interval=250)
        self._ui_timer.timeout.connect(self.changed.emit)

    # ---------- 落库 ----------
    def _schedule_save(self):
        if not self._save_timer.isActive():
            self._save_timer.start()

    def save(self):
        self._save_timer.stop()
        self.db.save_all(self.projects, self.agents, self.cards)

    def close(self):
        self.save()
        self.db.close()

    # ---------- 额度 / CLI 缓存 ----------
    def _load_cache(self, name: str) -> dict:
        try:
            return load_json(self.data_dir / name)
        except ValueError as e:
            self.cache_error = f"{e}（已忽略，重新获取后覆盖）"
            return {}

    def set_quota(self, cli: str, windows: list[dict] | None = None, error: str = ""):
        """成功 = 换成新窗口；失败 = 保留上次的窗口，只记原因"""
        old = self.quota.get(cli) or {}
        if error:
            self.quota[cli] = {**old, "error": error, "err_at": time.time()}
        else:
            self.quota[cli] = {"windows": windows or [], "at": time.time(), "error": ""}
        save_json(self.data_dir / "quota.json", self.quota)
        self.quota_changed.emit()

    def set_cli_cache(self, data: dict):
        self.cli_cache = data
        save_json(self.data_dir / "clis.json", data)

    def _recover(self):
        """上次还在跑的阶段：标已停止，不接回进程"""
        for card in self.cards:
            st = card.stage
            if st.status == "checking" and st.running_helper() is not None:
                # 额外角色在跑：主 agent 这一轮已正常结束，只停这个角色，卡进待确认、不拦通过
                h = st.running_helper()
                h.status, h.error = "stopped", RESTART_LINE
                st.status = "review"
                st.output.append(RESTART_LINE)
                self.db.event(card.id, "recover", f"{STAGE_NAME[st.stage]}阶段额外角色：{RESTART_LINE}")
            elif st.status in ("running", "checking"):
                st.status, st.flag, st.error, st.checking = "review", "stopped", RESTART_LINE, ""
                if st.rounds and st.rounds[-1].end is None:
                    st.rounds[-1].end = "stopped"
                for h in st.helpers:
                    if h.status == "running":
                        h.status, h.error = "stopped", RESTART_LINE
                st.output.append(RESTART_LINE)
                self.db.event(card.id, "recover", f"{STAGE_NAME[st.stage]}阶段：{RESTART_LINE}")
        self.db.save_all(self.projects, self.agents, self.cards)

    # ---------- 查询 ----------
    def project(self, pid):
        return next((p for p in self.projects if p.id == pid), None)

    def agent(self, aid):
        return next((a for a in self.agents if a.id == aid), None)

    def card(self, cid):
        return next(c for c in self.cards if c.id == cid)

    def find_card(self, cid):
        return next((c for c in self.cards if c.id == cid), None)

    def find_todo(self, tid):
        return next((w for w in self.todo_items if w.id == tid), None)

    def review_count(self):
        return sum(1 for c in self.cards if c.column == "review")

    def running_cards(self):
        return [c for c in self.cards if c.column == "running"]

    def new_manual_item(self, title, desc, ac):
        n = 1 + sum(1 for c in self.cards if c.item.source == "manual") + len(self.todo_items)
        while self.find_card(f"L{n}") or self.find_todo(f"L{n}"):
            n += 1
        return WorkItem(f"L{n}", "任务", title, "本地", desc, ac, "manual")

    # ---------- 全局设置 / 手写任务 / 记住的仓库 ----------
    @property
    def azure_ready(self) -> bool:
        return bool(self.azure_org and self.azure_project)

    def set_azure(self, org: str, project: str):
        self.azure_org, self.azure_project = org, project
        self.db.set_setting("azure_org", org)
        self.db.set_setting("azure_project", project)
        self._emit()

    def ai_allowed(self, org: str | None = None) -> bool:
        """这个组织的卡能不能交给 AI，默认不能；按组织分别记在 settings 的 ai_allowed:<组织>"""
        org = self.azure_org if org is None else org
        return bool(org) and self.db.setting(f"ai_allowed:{org}") == "1"

    def set_ai_allowed(self, org: str, allowed: bool):
        self.db.set_setting(f"ai_allowed:{org}", "1" if allowed else "0")
        self._emit()

    def _save_todo(self):
        self.db.set_setting("manual_todo", json.dumps(
            [{"id": w.id, "title": w.title, "description": w.description, "acceptance": w.acceptance}
             for w in self.todo_items], ensure_ascii=False))

    def add_manual_task(self, title: str, desc: str, ac: str) -> WorkItem:
        """只记内容，出现在 To Do；开始走抽屉"""
        item = self.new_manual_item(title.strip(), desc.strip(), ac.strip())
        self.todo_items.append(item)
        self._save_todo()
        self._emit()
        return item

    def remember_repo(self, folder: str) -> tuple[Project | None, str]:
        """选本地文件夹 → 记成仓库：必须是 git 仓库（取仓库根）；同一路径不重复记；重名加 -2、-3；
        基准分支和构建 / 测试命令自动识别。返回 (仓库, None) 或 (None, 原因)"""
        try:
            top = worktree.git("rev-parse", "--show-toplevel", cwd=str(Path(folder).expanduser())).strip()
        except GitError as e:
            return None, f"不是 git 仓库：\n{e}"
        top = str(Path(top).resolve())
        old = next((p for p in self.projects if str(Path(p.path).expanduser().resolve()) == top), None)
        if old:
            return old, None
        base = Path(top).name or "repo"
        name, n = base, 2
        while any(p.name == name for p in self.projects):  # 仓库名是 worktree 目录名的一部分，不能重
            name, n = f"{base}-{n}", n + 1
        d = detect(top)
        proj = Project(uuid.uuid4().hex[:12], name, top, "local", top, base_branch=d.base_branch or "main",
                       build_cmd=d.build_cmd, test_cmd=d.test_cmd)
        self.projects.append(proj)
        self.db.event(None, "repo", f"记住仓库 {name} · {top}")
        self._emit()
        return proj, None

    @staticmethod
    def checks_ok(stage: StageRun) -> bool:
        return all(code == 0 for _, code in stage.checks.values())

    def can_pass(self, card: Card) -> bool:
        st = card.stage
        return (card.column == "review" and card.comment == "none" and st.status == "review"
                and st.flag is None and self.checks_ok(st))

    def open_questions(self, card: Card) -> list[str]:
        p = self._req_parsed(card)
        return [x for x in p["plan"] if "待你确认" in x] if p else []

    @staticmethod
    def _req_parsed(card: Card) -> dict | None:
        st = card.stages[0]
        return req_view(st.rounds[-1].parsed) if st.rounds else None

    def spec_draft(self, card: Card) -> str:
        p = self._req_parsed(card)
        if not p:
            return ""
        steps = "\n".join(f"{i}. {x}" for i, x in enumerate(p["plan"], 1))
        done = "".join(f"- {x}\n" for x in p.get("done") or [])
        done_part = f"【完成标准】\n{done}\n" if done else ""
        return f"【完成目标】\n{p['goal']}\n\n{done_part}【方案】\n{steps}"

    def log_dir(self, card: Card) -> Path:
        return self.data_dir / "logs" / str(card.id)

    def meta(self, card: Card) -> dict:
        """开始时记下的卡片信息（存 settings 的 card_meta:<卡号>，不改表结构）；早期开始的老卡为空"""
        if card.id not in self._meta:
            try:
                self._meta[card.id] = json.loads(self.db.setting(f"card_meta:{card.id}", "{}"))
            except ValueError:
                self._meta[card.id] = {}
        return self._meta[card.id]

    def _set_meta(self, card: Card, data: dict):
        self._meta[card.id] = data
        self.db.set_setting(f"card_meta:{card.id}", json.dumps(data, ensure_ascii=False))

    def main_mode(self, card: Card) -> bool:
        """一个主 agent 跑完三阶段；老卡三阶段各自的模板，照原来跑"""
        return bool(self.meta(card).get("main"))

    def mcp_for(self, card: Card, agent: Agent) -> tuple[dict, list[str]]:
        """按模板能力拼 MCP：代码检索指向原仓库；grok 不挂；不关联仓库的卡不挂代码检索"""
        if not agent.caps or agent.cli not in CAP_CLIS:
            return {}, []
        proj = self.project(card.project_id)
        return mcp_servers(agent.caps, proj.path if proj else None)

    # ---------- 用户操作 ----------
    def start_card(self, item, pid, agents, helpers=(), parent=None):
        """开始需求阶段。pid = 关联仓库：建 worktree；None = 不关联仓库，在 data/scratch/<卡号>/ 空目录里跑。
        agents = 主 agent 的模板 ID；传三个 ID 的列表 = 老方式，三阶段各用各的。
        helpers = [(阶段, 模板 ID)] 额外角色；parent = 父项详情（WorkItem），需求固定进本卡。
        返回 (卡片, None) 或 (None, 原因 / git 的原话)"""
        main = isinstance(agents, str)
        ids = [agents] * len(STAGES) if main else list(agents)
        assert len(ids) == len(STAGES) and all(ids), "每个阶段都必须选 agent"
        if any(self.agent(a) is None for a in ids) or any(self.agent(a) is None for _, a in helpers):
            return None, "选的模板不存在了，重新选一下"
        if self.find_card(item.id):
            return None, f"{item.label} 已经在看板上了"
        if item.source == "azure":
            if item.type != "Task":
                return None, NOT_TASK.format(type=item.type)
            if not self.ai_allowed():
                return None, AI_OFF
        if parent is not None:
            head = f"（来自 {parent.type} #{parent.id} {parent.title}）"
            item = WorkItem(item.id, item.type, item.title, item.state,
                            f"{head}\n{parent.description}".rstrip(), parent.acceptance, item.source)
        card = Card(item, pid, [StageRun(s, a) for s, a in zip(STAGES, ids)])
        for stage, aid in helpers:
            card.stages[STAGES.index(stage)].helpers.append(HelperRun(aid))
        if pid is None:
            card.wt_path = str(self.data_dir / "scratch" / str(item.id))
            try:
                Path(card.wt_path).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return None, f"建工作目录失败：{e}"
            where = f"不关联仓库 · 工作目录 {card.wt_path}"
        else:
            proj = self.project(pid)
            if proj is None:
                return None, "关联的仓库不存在"
            card.wt_path = str(self.data_dir / "worktrees" / proj.name / str(item.id))
            try:
                worktree.add(proj.path, card.wt_path, card.branch, proj.base_branch)
            except GitError as e:
                return None, str(e)
            where = f"worktree {card.wt_path} · 分支 {card.branch}"
        self.cards.insert(0, card)
        if self.find_todo(item.id):  # 手写任务开始了就不再是「未开始」：卡片和待办一起马上落库，不等 1 秒的延迟保存
            self.todo_items.remove(self.find_todo(item.id))
            self._save_todo()
            self.save()
        if main or item.source == "azure":
            meta = {"main": main}
            if item.source == "azure":
                meta["ado"] = f"{self.azure_org}/{self.azure_project}"  # 写回时核对
            self._set_meta(card, meta)
        self.db.event(card.id, "start", where)
        self._run_round(card, "流水线", self._pipeline_prompt(card), resume=False)
        self._emit()
        return card, None

    def confirm_spec(self, card: Card, text: str | None = None):
        """确认需求：把「完成目标 + 方案」冻结进 card.spec，进入开发。text 为空就用 spec_draft。不关联仓库的卡要先 attach_repo"""
        text = (text or "").strip() or self.spec_draft(card)
        if card.stage.stage != "req" or not self.can_pass(card) or not text.strip() or card.project_id is None:
            return False
        card.spec = text.strip()
        card.stage.status = "passed"
        card.cur = 1
        self.db.event(card.id, "spec", card.spec)
        self._start_stage(card)
        self._emit()
        return True

    def _start_stage(self, card: Card):
        """进入新阶段的首轮：主 agent 模式续聊上一阶段的会话，拿不到会话 ID 就新开；老卡一律新开"""
        sid = card.stages[card.cur - 1].session_id if self.main_mode(card) else ""
        card.stage.session_id = sid
        self._run_round(card, "流水线", self._pipeline_prompt(card), resume=bool(sid))

    def can_confirm_req(self, card: Card) -> bool:
        return card.stage.stage == "req" and self.can_pass(card) and bool(card.stage.rounds and card.stage.rounds[-1].parsed)

    def finish_after_req(self, card: Card, text: str | None = None) -> bool:
        """不关联仓库的卡：确认需求并结束。Azure 卡出只含方案的交付记录草稿，手写卡直接完成"""
        text = (text or "").strip() or self.spec_draft(card)
        if card.project_id is not None or not self.can_confirm_req(card) or not text.strip():
            return False
        card.spec = text.strip()
        card.stage.status = "passed"
        self.db.event(card.id, "spec", card.spec)
        if card.item.source == "azure":
            card.comment, card.comment_text = "draft", self.writeback_draft(card)
            self.db.event(card.id, "draft", "交付记录草稿已生成（只做了需求分析）")
            self._notify(card, "需求已确认，交付记录等你确认", stage=False)
        else:
            card.comment = "skip"
        self._emit()
        return True

    def attach_repo(self, card: Card, pid: str, text: str | None = None) -> str | None:
        """不关联仓库的卡：确认需求后补选仓库进入开发。建 worktree，工作目录切过去，再冻结需求说明开始开发。
        返回原因 / git 原话，成功返回 None"""
        proj = self.project(pid)
        text = (text or "").strip() or self.spec_draft(card)
        if card.project_id is not None or not self.can_confirm_req(card) or not text.strip():
            return "现在不能进入开发"
        if proj is None:
            return "关联的仓库不存在"
        wt = str(self.data_dir / "worktrees" / proj.name / str(card.id))
        try:
            worktree.add(proj.path, wt, card.branch, proj.base_branch)
        except GitError as e:
            return str(e)
        card.project_id, card.wt_path = pid, wt
        card.stages[0].session_id = ""  # 工作目录换了，需求阶段的会话续不上（claude 按目录存会话），开发阶段新开
        self.db.event(card.id, "attach", f"关联仓库 {proj.name} · worktree {wt} · 分支 {card.branch}")
        self.confirm_spec(card, text)
        return None

    def pending_commit(self, card: Card) -> tuple[list[str], str]:
        """通过前要提交的改动：(git status 行, 提交信息)；没有改动返回空列表。git 出错抛 GitError"""
        return worktree.changed_files(card.wt_path), self.commit_message(card)

    @staticmethod
    def commit_message(card: Card) -> str:
        return f"{card.item.label} {STAGE_NAME[card.stage.stage]}：{card.item.title}"

    def pass_stage(self, card: Card):
        """没有改动时直接通过；有改动必须走 commit_and_pass（先弹窗确认）"""
        if card.stage.stage == "req" or not self.can_pass(card):
            return False
        try:
            if worktree.changed_files(card.wt_path):
                return False
        except GitError:
            return False
        self._advance(card)
        return True

    def commit_and_pass(self, card: Card) -> str | None:
        """你在确认框里点了「确认提交并通过」：git add -A + commit，成功再通过。返回错误原话或 None"""
        if card.stage.stage == "req" or not self.can_pass(card):
            return "当前阶段不能通过"
        try:
            if worktree.changed_files(card.wt_path):
                msg = self.commit_message(card)
                worktree.commit(card.wt_path, msg)
                self.db.event(card.id, "commit", msg)
        except GitError as e:
            return str(e)
        self._advance(card)
        return None

    def _advance(self, card: Card):
        card.stage.status = "passed"
        self.db.event(card.id, "pass", f"{STAGE_NAME[card.stage.stage]}阶段通过")
        if card.cur < len(card.stages) - 1:  # 按卡自己的阶段数：老卡四段、新卡三段
            card.cur += 1
            self._start_stage(card)
        elif card.item.source == "azure":
            # Azure 卡片：验证通过后先出交付记录草稿，你确认后追加到 Task 描述才完成
            card.comment, card.comment_text = "draft", self.writeback_draft(card)
            self.db.event(card.id, "draft", "交付记录草稿已生成")
            self._notify(card, "通过，交付记录等你确认")
        else:
            card.comment = "skip"  # 手写任务：验证通过即完成，不回写
        self._emit()

    # ---------- 写回 Task：交付记录追加到描述 ----------
    def writeback_draft(self, card: Card) -> str:
        """程序拼交付记录，不额外调 AI：改了什么 + 分支 + 验证；不关联仓库的卡只有结论和方案"""
        lines = [f"—— 定标 DoneDef 交付记录 · {time.strftime('%Y-%m-%d')} ——"]
        proj = self.project(card.project_id)
        if proj is None:
            return "\n".join(lines + ["结论：需求已确认，未进入开发", "方案：", card.spec])
        lines += [CHANGES_HEAD] + (self._changes(card, proj) or ["- （没找到改动）"])
        try:
            n = int(worktree.git("rev-list", "--count", f"{proj.base_branch}..{card.branch}", cwd=proj.path).strip())
            commits = f"{n} 个 commit"
        except (GitError, ValueError) as e:
            commits = f"commit 数取不到：{str(e).strip().splitlines()[0] if str(e).strip() else e}"
        lines.append(f"分支：{card.branch}（基于 {proj.base_branch}，{commits}，未合并）")
        last = card.stages[-1]
        checks = []
        for kind, cmd in (("构建", proj.build_cmd), ("测试", proj.test_cmd)):
            if not cmd.strip():
                checks.append(f"{kind}未配置")
            elif kind in last.checks:
                checks.append(f"{kind} {'✓' if last.checks[kind][1] == 0 else '✗'}")
            else:
                checks.append(f"{kind}没跑")
        m = VERDICT_RE.findall(last.last_reply())
        verdict = m[-1] if m else "（AI 没给明确结论）"
        lines.append(f"验证：{' '.join(checks)}；AI 验证结论：{verdict}")
        return "\n".join(lines)

    def _changes(self, card: Card, proj: Project) -> list[str]:
        """开发阶段最后一轮回复里「改了什么：」下的「- 」行；没有就列本卡改动的文件"""
        items = parse_changes(card.stages[1].last_reply())
        if items:
            return items
        try:
            ch = worktree.card_changes(card.wt_path if not card.worktree_cleaned else proj.path, proj.base_branch,
                                       "HEAD" if not card.worktree_cleaned else card.branch)
        except GitError:
            return []
        return [f"- {path}" for _, path, _ in ch["files"]]

    def can_writeback(self, card: Card) -> bool:
        return card.comment == "draft" and card.id not in self.sending

    def writeback_block(self, card: Card) -> str:
        """写回前的安全核对：当前 Azure 设置必须是本卡开始时的组织 / 项目，且该组织允许 AI；空串 = 可以写"""
        if not self.azure_ready:
            return "还没配 Azure：点「设置」→ Azure"
        ado = self.meta(card).get("ado")
        now = f"{self.azure_org}/{self.azure_project}"
        if not ado and card.item.source == "azure":
            return NO_ADO
        if ado and ado != now:
            return f"这张卡是在 {ado} 开始的，当前 Azure 设置是 {now}，不能写回（切回原组织 / 项目再写）"
        if not self.ai_allowed():
            return AI_OFF
        return ""

    def send_writeback(self, card: Card, text: str) -> bool:
        """后台追加到 Task 描述；成功才标「已写回」（卡片完成），失败保留草稿（含你的修改）并记原因，可以再发"""
        if not self.can_writeback(card) or not text.strip():
            return False
        card.comment_text = text
        self.writeback_errors.pop(card.id, None)
        block = self.writeback_block(card)
        if block:
            self.writeback_errors[card.id] = block
            self._emit()
            return False
        org, ado_project, num = self.azure_org, self.azure_project, int(card.id)
        job = BgJob(azure.safe(lambda: self.ado.append_description(org, ado_project, num, text)), self)
        job.done.connect(lambda res: self._writeback_done(card, job, res))
        self.sending[card.id] = job
        self.db.event(card.id, "writeback_send", f"交付记录追加到 {org}/{ado_project} #{num} 的描述")
        job.start()
        self._emit()
        return True

    def _writeback_done(self, card: Card, job: BgJob, res: dict):
        self.sending.pop(card.id, None)
        job.deleteLater()
        if "error" in res:
            err = WRITEBACK_RETRY if res["error"] == azure.CONFLICT else res["error"]
            self.writeback_errors[card.id] = err
            self.db.event(card.id, "writeback_failed", err)
            self._notify(card, "交付记录写回失败", stage=False)
        else:
            card.comment = "sent"
            self.db.event(card.id, "writeback_sent", f"已追加到 Task 描述（rev {res['ok']}）")
            self._notify(card, "交付记录已写回 Task", stage=False)
        self._emit()

    def ask(self, card: Card, text: str):
        """续聊当前阶段原会话；需求阶段 = 回答疑问 / 纠正理解，并要求按同样 JSON 重新输出"""
        if card.column != "review" or card.comment != "none" or not text.strip():
            return False
        text = text.strip()
        prompt = text + (REQ_ANSWER_SUFFIX if card.stage.stage == "req" else "")
        if card.stage.session_id:
            self._run_round(card, "你", prompt, resume=True)
        else:
            # 上一轮没拿到会话 ID（例如启动即失败）：只能新开，把模板提示词带上
            self._run_round(card, "你", self._pipeline_prompt(card) + "\n\n补充：" + prompt, resume=False)
        self._emit()
        return True

    def can_send_back(self, card: Card) -> bool:
        """验证阶段（老卡还有测试阶段）待确认时可以退回开发（失败、自检不过、额度耗尽、已停止也可以）"""
        return (card.column == "review" and card.comment == "none"
                and card.stage.stage in ("test", "verify") and card.stage.status == "review")

    def send_back(self, card: Card, note: str):
        """退回开发：续聊开发阶段原会话；之后阶段（验证，老卡还有测试）的结果丢弃（历史轮次保留），重跑时新开会话"""
        if not self.can_send_back(card):
            return False
        src = card.stage
        last = src.rounds[-1].reply if src.rounds else None
        src.output.append("── 已退回开发，本阶段结果作废 ──")
        for s in card.stages[2:]:  # 开发之后的阶段：验证（老卡还有测试）
            s.status, s.flag, s.error, s.checking = "pending", None, "", ""
            s.checks = {}
            s.session_id = ""
        card.cur = 1
        self.append_supplement(card, note)
        text = f"【从{STAGE_NAME[src.stage]}阶段退回】"
        if note.strip():
            text += f"\n我的备注：{note.strip()}"
        text += f"\n{STAGE_NAME[src.stage]} AI 的回复：{last or '无回复'}"
        self.db.event(card.id, "send_back", text)
        self._run_round(card, "你（退回）", text, resume=bool(card.stage.session_id))
        self._emit()
        return True

    def append_supplement(self, card: Card, note: str):
        """退回备注追加到已冻结的需求说明末尾，之后验证阶段的 {spec} 能看到"""
        if note.strip():
            card.spec = f"{card.spec.rstrip()}\n\n{SUPPLEMENT_HEAD}\n{note.strip()}"

    def stop(self, card: Card):
        """进程组 SIGTERM，5 秒后 SIGKILL；进程真退出后标「已停止」"""
        if self._stop_intent.get(card.id) == "pause":  # 暂停中又点停止：按普通停止收尾
            self._stop_intent.pop(card.id)
        return self._do_stop(card)

    def _do_stop(self, card: Card):
        st = card.stage
        if st.status not in ("running", "checking"):
            return False
        proc = self.procs.get(card.id)
        if proc is None:
            intent = self._stop_intent.pop(card.id, None)
            if intent == "abort":
                self._finish_abort(card, notify=True)
            else:
                self._end(card, "stopped", PAUSED_LINE if intent == "pause" else "已手动停止")
            return True
        proc.stop()
        st.output.append("正在停止（进程组 SIGTERM，5 秒后 SIGKILL）…")
        self._emit()
        return True

    # ---------- 暂停 / 继续 / 中止 / 重新开始 ----------
    def can_pause(self, card: Card) -> bool:
        """只有主 AI 正在跑才能暂停；程序自检、额外角色在跑（status=checking）时界面只给「停止」"""
        proc = self.procs.get(card.id)
        return (card.stage.status == "running" and card.stage.running_helper() is None
                and card.id not in self._stop_intent and not (proc is not None and proc.stopping))

    def pause(self, card: Card) -> bool:
        """停进程组，这一轮以「已暂停」收尾（flag 仍是 stopped），保留会话 ID 供继续"""
        if not self.can_pause(card):
            return False
        self._stop_intent[card.id] = "pause"
        if not self._do_stop(card):
            self._stop_intent.pop(card.id, None)
            return False
        return True

    def is_paused(self, card: Card) -> bool:
        st = card.stage
        return st.status == "review" and st.flag == "stopped" and st.error == PAUSED_LINE

    def is_aborted(self, card: Card) -> bool:
        st = card.stage
        return st.status == "review" and st.flag == "stopped" and st.error.startswith(ABORTED_LINE)

    def resume(self, card: Card) -> bool:
        """继续：续聊原会话让 AI 接着做（需求阶段 ask 会自动带 JSON 要求）"""
        if not self.is_paused(card):
            return False
        return self.ask(card, RESUME_TEXT)

    def _abort_has_worktree(self, card: Card) -> bool:
        """中止要丢的只有开发阶段、关联了仓库、worktree 还在的那份未提交改动"""
        return (card.stage.stage == "dev" and self.project(card.project_id) is not None
                and not card.worktree_cleaned and Path(card.wt_path).is_dir())

    def abort_preview(self, card: Card) -> list[str]:
        """中止会丢弃的文件清单（界面确认弹窗用）；读取失败时返回一行失败原话，不吞错"""
        if not self._abort_has_worktree(card):
            return []
        try:
            return worktree.uncommitted(card.wt_path)
        except GitError as e:
            return [f"（读取改动清单失败：{e}）"]

    def can_abort(self, card: Card) -> bool:
        return self.can_pause(card) or self.is_paused(card)

    def abort(self, card: Card) -> bool:
        """中止：本轮丢弃，会话清空；开发阶段丢弃本卡 worktree 的未提交改动（已提交的保留）"""
        if self.is_paused(card):
            self._finish_abort(card, notify=False)
            return True
        if not self.can_pause(card):
            return False
        self._stop_intent[card.id] = "abort"
        if not self._do_stop(card):
            self._stop_intent.pop(card.id, None)
            return False
        return True

    def _finish_abort(self, card: Card, notify: bool):
        """中止的收尾：清会话、开发阶段丢未提交改动；丢弃失败把 git 原话写进输出和 error"""
        st = card.stage
        st.session_id = ""
        line = ABORTED_LINE
        if self._abort_has_worktree(card):
            try:
                files = worktree.discard_uncommitted(card.wt_path)
                st.output.append(f"已丢弃未提交改动 {len(files)} 个文件：{'、'.join(files) or '（无）'}")
                self.db.event(card.id, "abort", f"丢弃 {len(files)} 个文件：{'、'.join(files)}")
            except GitError as e:
                line = f"{ABORTED_LINE}；丢弃改动失败：{e}"
                st.output.append(f"丢弃改动失败：{e}")
                self.db.event(card.id, "abort", f"丢弃改动失败：{e}")
        else:
            self.db.event(card.id, "abort", "本轮作废")
        if notify:
            self._end(card, "stopped", line)
        else:
            st.error = line
            if st.rounds:
                st.rounds[-1].end = "stopped"
            st.output.append(line)
            self._emit()

    def restart_stage(self, card: Card) -> bool:
        """中止后重新开始：本阶段新开会话跑首轮"""
        if not self.is_aborted(card):
            return False
        card.stage.session_id = ""
        self._run_round(card, "流水线", self._pipeline_prompt(card), resume=False)
        self._emit()
        return True

    def stop_all(self):
        """退出程序前：同步停掉全部（每个最多等 5 秒），立即标已停止并落库"""
        for card in self.running_cards():
            proc = self.procs.pop(card.id, None)
            if proc:
                proc.stop_blocking()
            self._t0.pop(card.id, None)
            self._stop_intent.pop(card.id, None)
            self._end(card, "stopped", "已停止（退出程序）")
        self.save()

    def cleanup(self, card: Card) -> str | None:
        """git worktree remove（不加 --force），保留分支。返回 git 原话或 None"""
        proj = self.project(card.project_id)
        if card.column != "done" or card.worktree_cleaned or proj is None:
            return "现在不能清理"
        try:
            worktree.remove(proj.path, card.wt_path)
        except GitError as e:
            return str(e)
        card.worktree_cleaned = True
        self.db.event(card.id, "cleanup", card.wt_path)
        self._emit()
        return None

    def tick(self):
        """每秒一次：刷新运行中阶段的耗时"""
        now = time.monotonic()
        dirty = False
        for card in self.running_cards():
            t = self._t0.get(card.id)
            if t:
                card.stage.elapsed = t[1] + int(now - t[0])
                dirty = True
        if dirty:
            self._emit()

    # ---------- 文本 ----------
    def render_prompt(self, card: Card) -> str:
        agent = self.agent(card.stage.agent_id)
        text = agent.prompt if agent else ""
        values = {
            "{title}": card.item.title, "{description}": card.item.description,
            "{acceptance}": card.item.acceptance, "{spec}": card.spec,
            "{dev_reply}": card.stages[1].last_reply(),
            "{test_reply}": next((s.last_reply() for s in card.stages if s.stage == "test"), NO_TEST_REPLY),
        }
        if "{diff" in text:
            proj = self.project(card.project_id)
            if proj is None:
                stat = full = "（未关联代码仓库）"
            else:
                try:
                    stat, full = worktree.diff(card.wt_path, proj.base_branch)
                except GitError as e:
                    stat = full = f"（取 diff 失败：{e}）"
            values["{diff_stat}"], values["{diff}"] = stat, full
        for k, v in values.items():
            text = text.replace(k, v)
        return text

    def _pipeline_prompt(self, card: Card) -> str:
        """阶段首轮的提示词；需求阶段固定追加 JSON 输出要求。主 agent 模式 = 角色说明 + 内置阶段指令"""
        if self.main_mode(card):
            return self.main_prompt(card)
        return self.render_prompt(card) + (REQ_JSON_SUFFIX if card.stage.stage == "req" else "")

    def _role(self, agent: Agent | None) -> str:
        return f"【你的角色】\n{agent.prompt.strip()}\n\n" if agent and agent.prompt.strip() else ""

    def _requirement(self, card: Card) -> str:
        return (f"【需求】\n{card.item.description.strip() or '（没写描述）'}\n\n"
                f"【验收标准】\n{card.item.acceptance.strip() or NO_ACCEPTANCE_HINT}")

    def _diff_stat(self, card: Card) -> str:
        proj = self.project(card.project_id)
        if proj is None:
            return "（未关联代码仓库）"
        try:
            return worktree.diff(card.wt_path, proj.base_branch)[0].strip() or "（还没有改动）"
        except GitError as e:
            return f"（取改动统计失败：{e}）"

    def main_prompt(self, card: Card) -> str:
        """主 agent 某阶段首轮的提示词。续聊时也带全部上下文，不依赖 AI 记得"""
        stage = card.stage.stage
        head = self._role(self.agent(card.stage.agent_id)) + STAGE_GUIDE[stage]
        task = f"【任务】{card.item.label} {card.item.title}"
        if stage == "req":
            return f"{head}\n\n{task}\n\n{self._requirement(card)}{REQ_JSON_SUFFIX}"
        spec = f"{self._requirement(card)}\n\n【确认的方案】\n{card.spec}"
        if stage == "dev":
            return f"{head}\n\n{task}\n\n{spec}{DEV_TAIL}"
        proj = self.project(card.project_id)
        where = (f"改动在当前分支 {card.branch}，基准分支 {proj.base_branch}，可以运行 git diff {proj.base_branch}...HEAD 查看。"
                 if proj else "")
        return (f"{head}{where}\n\n{task}\n\n{spec}\n\n【改动统计】\n{self._diff_stat(card)}{VERIFY_TAIL}")

    def helper_prompt(self, card: Card, agent: Agent) -> str:
        """额外角色的提示词：角色说明 + 只读给意见 + 需求和确认的方案（需求阶段只给需求）+ 主 agent 最新回复 + 改动统计"""
        st = card.stage
        parts = [self._role(agent) + f"主 agent 刚完成{STAGE_NAME[st.stage]}阶段。你只读，不要改任何文件，从你的角色给出意见。",
                 f"【任务】{card.item.label} {card.item.title}",
                 (self._requirement(card) if st.stage == "req"
                  else f"{self._requirement(card)}\n\n【确认的方案】\n{card.spec}"),
                 f"【主 agent 最新回复】\n{st.last_reply() or '（无回复）'}"]
        if st.stage != "req":
            parts.append(f"【改动统计】\n{self._diff_stat(card)}")
        return "\n\n".join(parts)

    # ---------- 进程 ----------
    def env(self) -> dict[str, str]:
        return dict(self._env) if self._env is not None else child_env(os.environ)

    def card_env(self, card: Card) -> dict[str, str]:
        """不关联仓库的卡：scratch 在 data/ 下，data/ 可能在别的 git 仓库里（比如本程序自己的仓库）。
        设 GIT_CEILING_DIRECTORIES 让 git 不往上找仓库"""
        env = self.env()
        if card.project_id is None:
            env["GIT_CEILING_DIRECTORIES"] = str(Path(card.wt_path).parent)
        return env

    def _run_round(self, card: Card, by: str, prompt: str, resume: bool):
        """开一轮：resume=True 续聊本阶段会话，否则新开会话。需求阶段一律只读"""
        st = card.stage
        agent = self.agent(st.agent_id)
        perm = "read" if st.stage == "req" else agent.perm
        n = len(st.rounds) + 1
        log = self.log_dir(card) / f"{st.stage}-{n}.jsonl"
        out_file = self.log_dir(card) / f"{st.stage}-{n}.last.txt"
        sid = st.session_id if resume else ""
        if not resume:
            st.session_id = ""
        st.rounds.append(Round(by, prompt, session_id=sid, log=str(log)))
        st.status, st.flag, st.error, st.checking = "running", None, "", ""
        st.checks = {}
        for i, h in enumerate(st.helpers):  # 额外角色只保留最新一次：主 agent 每跑一轮都清掉重跑
            st.helpers[i] = HelperRun(h.agent_id)
        st.output.append(f"── 第 {n} 轮 · {by}{' · 续聊' if sid else ' · 新会话'} ──")
        self.db.event(card.id, "round", f"{STAGE_NAME[st.stage]}第 {n} 轮（{by}）")
        mcp, notes = self.mcp_for(card, agent)
        st.output += notes
        cmd = build_cmd(agent.cli, perm, agent.model, agent.effort, prompt, sid, str(out_file), mcp)
        parser = StreamParser(agent.cli)
        proc = StageProcess(cmd, card.wt_path, self.card_env(card), str(log), self)
        proc.line.connect(lambda text: self._on_line(card, proc, parser, text))
        proc.finished.connect(lambda code, err: self._on_ai_done(card, proc, parser, str(out_file), code, err))
        self._spawn(card, proc)

    def _spawn(self, card: Card, proc: StageProcess, on_fail=None):
        """on_fail(原因) = 启动失败时的处理；默认整个阶段标失败（额外角色传自己的，失败不拦阶段）"""
        self.procs[card.id] = proc
        self._t0.setdefault(card.id, (time.monotonic(), card.stage.elapsed))
        try:
            proc.start()
        except OSError as e:
            self.procs.pop(card.id, None)
            proc.deleteLater()
            if on_fail:
                return on_fail(f"启动失败：{e}")
            self._t0.pop(card.id, None)
            self._end(card, "failed", f"启动失败：{e}")

    def _on_line(self, card: Card, proc, parser: StreamParser, text: str):
        if self.procs.get(card.id) is not proc:
            return
        st = card.stage
        acts = parser.feed(text)
        if parser.session_id and st.session_id != parser.session_id:
            st.session_id = st.rounds[-1].session_id = parser.session_id
        if acts:
            st.output += acts
            if not self._ui_timer.isActive():
                self._ui_timer.start()

    def _finish_proc(self, card: Card, proc) -> bool:
        """进程结束的公共收尾；返回 False = 这个进程已失效（停掉退出程序等）"""
        if self.procs.get(card.id) is not proc:
            return False
        self.procs.pop(card.id)
        proc.deleteLater()
        return True

    def _settle_time(self, card: Card):
        t = self._t0.pop(card.id, None)
        if t:
            card.stage.elapsed = t[1] + int(time.monotonic() - t[0])

    def _on_ai_done(self, card: Card, proc, parser: StreamParser, out_file: str, code: int, stderr: str):
        if not self._finish_proc(card, proc):
            return
        st = card.stage
        rnd = st.rounds[-1]
        if parser.session_id:
            st.session_id = rnd.session_id = parser.session_id
        agent = self.agent(st.agent_id)
        cli = agent.cli if agent else ""
        if cli == "claude" and parser.rate_info:
            windows = parse_claude_quota(parser.rate_info)
            if windows:
                self.set_quota("claude", windows)
        self.ai_done.emit(cli)
        if proc.stopping:
            self._settle_time(card)
            intent = self._stop_intent.pop(card.id, None)
            if intent == "pause":
                return self._end(card, "stopped", PAUSED_LINE)
            if intent == "abort":
                return self._finish_abort(card, notify=True)
            return self._end(card, "stopped", "已手动停止（进程组 SIGTERM）")
        res = parser.finish(code, stderr, out_file)
        rnd.reply = res.reply or None
        if res.kind == "failed" and rnd.session_id and not parser.session_id:
            # 续聊失败又没拿到新会话（会话被清理 / 换了工作目录 / 模板换了 CLI）：下次发话新开会话
            st.session_id = ""
        if res.kind != "ok":
            self._settle_time(card)
            return self._end(card, res.kind, res.error)
        if st.stage == "req":
            try:
                rnd.parsed = parse_req_json(res.reply)
            except ValueError as e:
                self._settle_time(card)
                return self._end(card, "failed", f"需求理解格式不对：{e}")
        proj = self.project(card.project_id)
        cmds = [(k, v) for k, v in [("构建", proj.build_cmd), ("测试", proj.test_cmd)] if v.strip()] if proj else []
        if st.stage in ("dev", "test", "verify") and cmds:  # 开发、验证结束后自检（老卡的测试阶段也跑）
            st.status, st.checks = "checking", {}
            self._run_check(card, cmds)
        else:
            self._after_main(card)

    # ---------- 程序自检 ----------
    def _run_check(self, card: Card, queue: list[tuple[str, str]]):
        st = card.stage
        kind, cmd = queue[0]
        st.checking = cmd
        log = self.log_dir(card) / f"{st.stage}-{len(st.rounds)}-check-{kind}.txt"
        proc = StageProcess(["/bin/sh", "-c", cmd], card.wt_path, self.env(), str(log), self)
        proc.finished.connect(lambda code, _err: self._on_check_done(card, proc, queue, code))
        self._spawn(card, proc)
        self._emit()

    def _on_check_done(self, card: Card, proc, queue, code: int):
        if not self._finish_proc(card, proc):
            return
        st = card.stage
        kind, cmd = queue[0]
        st.checking = ""
        if proc.stopping:
            self._settle_time(card)
            return self._end(card, "stopped", f"已手动停止（自检 {cmd} 被终止）")
        st.checks[kind] = (cmd, code)
        st.output.append(f"[程序自检] $ {cmd} → exit {code}")
        if queue[1:]:
            self._run_check(card, queue[1:])
        else:
            self._after_main(card)

    # ---------- 额外角色 ----------
    def _after_main(self, card: Card):
        """主 agent 这一轮（含自检）跑完：有额外角色就按添加顺序依次只读跑，跑完再进待确认"""
        st = card.stage
        if not st.helpers:
            self._settle_time(card)
            return self._to_review(card)
        st.status = "checking"
        self._next_helper(card)

    def _next_helper(self, card: Card):
        st = card.stage
        h = next((x for x in st.helpers if x.status == "pending"), None)
        if h is None:
            self._settle_time(card)
            return self._to_review(card)
        k = st.helpers.index(h) + 1
        agent = self.agent(h.agent_id)
        if agent is None:
            h.status, h.error = "failed", "模板已被删除"
            st.output.append(f"[额外角色 {k}] 模板已被删除，跳过")
            return self._next_helper(card)
        n = len(st.rounds)
        log = self.log_dir(card) / f"{st.stage}-{n}-h{k}.jsonl"
        out_file = self.log_dir(card) / f"{st.stage}-{n}-h{k}.last.txt"
        h.status, h.log = "running", str(log)
        st.output.append(f"── 额外角色 · {agent.name}（只读 · 新会话）──")
        mcp, notes = self.mcp_for(card, agent)
        st.output += [f"[{agent.name}] {x}" for x in notes]
        self.db.event(card.id, "helper", f"{STAGE_NAME[st.stage]}阶段额外角色 {agent.name}")
        cmd = build_cmd(agent.cli, "read", agent.model, agent.effort, self.helper_prompt(card, agent), "",
                        str(out_file), mcp)
        parser = StreamParser(agent.cli)
        proc = StageProcess(cmd, card.wt_path, self.card_env(card), str(log), self)
        proc.line.connect(lambda text: self._on_helper_line(card, h, agent.name, proc, parser, text))
        proc.finished.connect(lambda code, err: self._on_helper_done(card, h, agent, proc, parser, str(out_file),
                                                                     code, err))
        self._h0[card.id] = time.monotonic()
        self._spawn(card, proc, on_fail=lambda why: self._helper_failed(card, h, agent.name, why))
        self._emit()

    def _helper_failed(self, card: Card, h, name: str, why: str):
        self._h0.pop(card.id, None)
        h.status, h.error = "failed", why
        card.stage.output.append(f"[{name}] {why}")
        self._next_helper(card)

    def _on_helper_line(self, card: Card, h, name: str, proc, parser: StreamParser, text: str):
        if self.procs.get(card.id) is not proc:
            return
        acts = parser.feed(text)
        if parser.session_id:
            h.session_id = parser.session_id
        if acts:
            card.stage.output += [f"[{name}] {a}" for a in acts]
            if not self._ui_timer.isActive():
                self._ui_timer.start()

    def _on_helper_done(self, card: Card, h, agent: Agent, proc, parser: StreamParser, out_file: str,
                        code: int, stderr: str):
        if not self._finish_proc(card, proc):
            return
        st = card.stage
        if parser.session_id:
            h.session_id = parser.session_id
        h.elapsed = int(time.monotonic() - self._h0.pop(card.id, time.monotonic()))
        if agent.cli == "claude" and parser.rate_info:
            windows = parse_claude_quota(parser.rate_info)
            if windows:
                self.set_quota("claude", windows)
        self.ai_done.emit(agent.cli)
        if proc.stopping:
            # 你点了停止：正在跑的停，后面的不跑，卡进待确认（不标阶段失败）
            h.status, h.error = "stopped", "已手动停止"
            st.output.append(f"[{agent.name}] 已手动停止，后面的额外角色不再跑")
            self.db.event(card.id, "helper_stopped", agent.name)
            self._settle_time(card)
            return self._to_review(card)
        res = parser.finish(code, stderr, out_file)
        h.reply = res.reply or ""
        if res.kind == "ok":
            h.status = "done"
        else:
            h.status, h.error = "failed", res.error
            st.output.append(f"[{agent.name}] 失败：{res.error}")
            self.db.event(card.id, "helper_failed", f"{agent.name}：{res.error}")
        self._next_helper(card)

    # ---------- 状态收尾 ----------
    def _end(self, card: Card, flag: str, line: str):
        st = card.stage
        h = st.running_helper()
        if h is not None:
            # 额外角色在跑（例如退出程序）：主 agent 这一轮已正常结束，只停这个角色，不标阶段失败
            h.status, h.error = "stopped", line
            self._h0.pop(card.id, None)
            st.output.append(line)
            self.db.event(card.id, "helper_stopped", line)
            return self._to_review(card)
        st.status, st.flag, st.error, st.checking = "review", flag, line, ""
        if st.rounds:
            st.rounds[-1].end = flag
        st.output.append(line)
        title = {"failed": "失败", "quota": "额度耗尽", "stopped": "已停止"}[flag]
        self.db.event(card.id, flag, line)
        self._notify(card, title)
        self._emit()

    def _to_review(self, card: Card):
        card.stage.status = "review"
        ok = self.checks_ok(card.stage)
        self._notify(card, "待确认" if ok else "程序自检失败")
        self._emit()

    def _notify(self, card: Card, what: str, stage: bool = True):
        title = f"{card.item.label} {STAGE_NAME[card.stage.stage]}阶段{what}" if stage else f"{card.item.label} {what}"
        self.notifications.append((title, card.item.title))
        self.notify.emit(title, card.item.title)

    def _emit(self):
        self.changed.emit()

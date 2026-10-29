"""主窗口（从 prototype_qt/ui.py 改造）：迭代看板。
顶栏左上角选 Azure 迭代 + 刷新（每 5 分钟和窗口回到前台时自动拉）；看板三列 To Do / Doing / Done，
主体是 PBI 卡（卡里是指派给我的 Task 行），没父项的 Task 和手写任务是独立卡，徽标表示本地 AI 进度；
点卡从右侧滑出抽屉（盖在看板上，点外面收起）：可改 Azure 状态、用 VS Code 打开仓库；
未开始的 Task 看需求（来自 PBI）、选仓库 + 主 agent（可加额外角色）再开始，PBI 只看全文和改状态；
已开始的卡按「AI 在干什么 / 结果 / 改了哪些代码 / 方案 / 对话」看。"""
from __future__ import annotations

import html
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import azure, clis, worktree
from .board import AI_OFF, NOT_TASK, SUPPLEMENT_HEAD, VERDICT_RE, Board, parse_changes, parse_done_checks
from .cli import child_env, req_view, run_process
from .dialogs import (
    NO_REPO, AttachRepoDialog, RepoPicker, SettingsDialog, TaskDialog, WritebackDialog,
    abort_box, cleanup_box, commit_box, error_box, pick_folder, quit_box,
)
from .worktree import GitError
from .model import BOARD_COLUMNS, CAP_CLIS, CAPS, CLI_NAME, PERM_NAME, STAGE_NAME, STAGES, Card, WorkItem, state_column

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.svg"
ICON_SIZES = (16, 32, 64, 128, 256, 512)


def app_icon() -> QIcon:
    """程序图标：把自带的 icon.svg 渲染成多个尺寸（透明底、抗锯齿）；文件缺失直接抛异常，不静默吞掉"""
    renderer = QSvgRenderer(str(ICON_PATH))
    if not renderer.isValid():
        raise FileNotFoundError(f"程序图标不可用：{ICON_PATH}")
    icon = QIcon()
    for size in ICON_SIZES:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon


QSS = """
QWidget { font-family: "PingFang SC", "Helvetica Neue"; font-size: 13px; color: #1d2129; }
QMainWindow, #mainArea { background: #f5f6f8; }
#header { background: #ffffff; border-bottom: 1px solid #e3e6eb; }
#note, #fetch { color: #6b7280; font-size: 12px; }
#fetch[kind="bad"], #err { color: #c4363a; }
#fetch[kind="warn"] { color: #9a6200; }
QPushButton { border: 1px solid #e3e6eb; background: #ffffff; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #f0f2f5; }
QPushButton#primary { background: #3b6cf6; border-color: #3b6cf6; color: #ffffff; }
QPushButton#primary:disabled { background: #a9bdf9; border-color: #a9bdf9; }
QPushButton#danger { color: #e5484d; border-color: #e5484d; }
QPushButton:disabled { color: #b0b5be; }
#quota { background: #ffffff; border: 1px solid #e3e6eb; border-radius: 8px; }
#colHead { background: #eceef2; border-radius: 8px; }
#colTitle { font-weight: 700; font-size: 14px; }
#colCount { color: #6b7280; }
QScrollArea { background: transparent; border: none; }
#colBody { background: transparent; }
#pbiCard { background: #ffffff; border: 1px solid #e3e6eb; border-radius: 10px; }
#pbiHead { background: #f8f3fd; border: 1px solid transparent; border-radius: 7px; }
#pbiHead[sel="true"] { border: 2px solid #3b6cf6; }
#pbiTitle { font-weight: 600; color: #4a2a6e; }
#taskRow { background: #ffffff; border: 1px solid #edf0f3; border-radius: 7px; }
#taskRow:hover { background: #f7f9fc; }
#taskRow[sel="true"] { border: 2px solid #3b6cf6; }
#rowTitle { font-size: 13px; font-weight: 600; }
#parentBox { background: #f8f3fd; border: 1px solid #e6d6f5; border-radius: 8px; }
#card { background: #ffffff; border: 1px solid #e3e6eb; border-radius: 10px; }
#card[sel="true"] { border: 2px solid #3b6cf6; }
#cardTitle { font-size: 14px; font-weight: 600; }
#cardMeta { color: #6b7280; font-size: 12px; }
#now { font-size: 12.5px; padding: 4px 8px; border-radius: 6px; background: #f0f2f5; }
#now[kind="run"] { background: #e8eefe; color: #2c56d8; }
#now[kind="wait"] { background: #fff4dc; color: #9a6200; }
#now[kind="bad"] { background: #fdecec; color: #c4363a; }
#now[kind="quota"] { background: #f4ecfb; color: #7a3fb0; }
#now[kind="ok"] { background: #e6f6ee; color: #23784f; }
#status { font-size: 11.5px; font-weight: 600; border-radius: 4px; padding: 1px 7px; }
#status[kind="run"] { color: #2c56d8; background: #e8eefe; }
#status[kind="wait"] { color: #9a6200; background: #fff4dc; }
#status[kind="bad"] { color: #c4363a; background: #fdecec; }
#status[kind="quota"] { color: #7a3fb0; background: #f4ecfb; }
#status[kind="gray"] { color: #4b5563; background: #eceef2; }
#status[kind="ok"] { color: #23784f; background: #e6f6ee; }
#status[kind="todo"] { color: #6b7280; background: #f0f2f5; }
#drawer { background: #ffffff; border-left: 1px solid #e3e6eb; }
#drawerBody { background: #ffffff; }
#undBox { background: #f5f8ff; border: 1px solid #dbe4fd; border-radius: 8px; }
#pendBox { background: #fff8e6; border: 1px solid #f5dfa6; border-radius: 8px; }
#planBox { background: #f0faf4; border: 1px solid #cdeedd; border-radius: 8px; }
#quotaChip { color: #4b5563; font-size: 12px; background: #f5f6f8; border: 1px solid #e3e6eb; border-radius: 6px; padding: 3px 8px; }
#quotaChip[kind="warn"] { color: #9a6200; background: #fff4dc; border-color: #f5dfa6; }
#quotaChip[kind="bad"] { color: #c4363a; background: #fdecec; border-color: #f5c2c3; }
#quotaChip[kind="gray"] { color: #9aa1ad; }
#reqFooter { border-top: 1px solid #e3e6eb; }
#secLabel { color: #4b5563; font-weight: 600; font-size: 12px; }
#warn { color: #9a6200; font-size: 12px; }
#activity { background: #eef3ff; border-radius: 8px; }
#actWho { color: #4b5563; font-size: 12px; }
#actNow { color: #1d2129; font-size: 15px; font-weight: 600; }
#stageLine { font-size: 12px; }
#preview { background: #f7f8fa; border-radius: 8px; padding: 10px; }
QPushButton#link { border: none; background: transparent; color: #3b6cf6; padding: 2px 0; }
#detailTitle { font-size: 15px; font-weight: 700; }
QTextBrowser { border: none; }
"""

COL_MIN = 260  # 看板每列的最小宽度，放不下时整块看板横向滚动
CLI_COLOR = {"claude": "#d97757", "codex": "#10a37f", "grok": "#4b5563"}
QUOTA_REFRESH_MS = 5 * 60 * 1000  # codex 额度每 5 分钟查一次
AUTO_PULL_MS = 5 * 60 * 1000  # 每 5 分钟自动拉一次 Azure
AUTO_PULL_GAP = 60  # 秒：距上次拉取开始不到这么久不自动拉
OUTPUT_SHOW = 1500  # 原始输出页最多显示最近这么多行

def fmt_elapsed(sec):
    return f"{sec // 60}:{sec % 60:02d}"


def label(text, name=None, kind=None, wrap=False):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    if kind:
        lab.setProperty("kind", kind)
    lab.setWordWrap(wrap)
    return lab


class ElidedLabel(QLabel):
    """单行、不换行，放不下用省略号，tooltip 放全文。text() 返回全文；
    min_w=None：横向 Ignored（吃剩余空间，可缩到 0）；给 min_w：期望宽度 = 全文宽，可缩到 min_w（额度条的小标签）"""

    def __init__(self, text="", name=None, min_w=None):
        super().__init__()
        if name:
            self.setObjectName(name)
        self._full = ""
        self._min_w = min_w
        self.setWordWrap(False)
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored if min_w is None else QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setText(text)

    def text(self):
        return self._full

    def setText(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def _extra(self):
        """文字以外占的宽度（QSS padding / 边框 / margin）"""
        return max(0, QLabel.sizeHint(self).width() - self.fontMetrics().horizontalAdvance(QLabel.text(self)))

    def _elide(self):
        fm = self.fontMetrics()
        shown = fm.elidedText(self._full, Qt.ElideRight, max(0, self.width() - self._extra()))
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def sizeHint(self):
        h = QLabel.sizeHint(self)
        if self._min_w is None:
            return h
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + self._extra(), h.height())

    def minimumSizeHint(self):
        return QSize(0 if self._min_w is None else self._min_w, QLabel.minimumSizeHint(self).height())


VERB = {"Read": "阅读", "Edit": "修改", "Write": "新建", "Grep": "搜索", "Glob": "查找文件", "Bash": "运行"}


def humanize(line: str) -> str:
    """把 CLI 的工具调用行翻成人话：Edit Sources/A/B.swift (+42 -6) → 正在修改 B.swift (+42 -6)"""
    verb, _, rest = line.partition(" ")
    if verb not in VERB or not rest:
        return line
    if verb in ("Read", "Edit", "Write"):
        head, _, tail = rest.partition(" ")
        rest = head.rsplit("/", 1)[-1] + (f" {tail}" if tail else "")
    return f"正在{VERB[verb]} {rest}"


def current_step(st) -> tuple[int, str]:
    """当前这一轮已走了几步、最新一步（人话）"""
    lines = []
    for ln in st.output:
        lines = [] if ln.startswith("──") else lines + [ln]
    return len(lines), (humanize(lines[-1]) if lines else "启动中…")


def recent_steps(st, n=5) -> list[str]:
    """当前这一轮最近 n 步动作（人话），旧的在前"""
    lines = []
    for ln in st.output:
        lines = [] if ln.startswith("──") else lines + [ln]
    return [humanize(x) for x in lines[-n:]]


def status_of(board: Board, card: Card):
    """返回 (样式, 状态文字, 当前动作一行)"""
    st = card.stage
    sname = STAGE_NAME[st.stage]
    if card.column == "done":
        extra = ("未关联仓库" if card.project_id is None else
                 "worktree 已清理" if card.worktree_cleaned else "worktree 待清理")
        head = "✓ 已写回 Task" if card.comment == "sent" else "✓ 已完成（手写任务，不回写）"
        return "ok", "已完成", f"{head} · {extra}"
    if card.comment == "draft":
        # Azure 卡片所有阶段都通过了，等你确认交付记录
        if card.id in board.sending:
            return "run", "写回中", "✉ 正在把交付记录追加到 Task 描述…"
        err = board.writeback_errors.get(card.id)
        if err:
            return "bad", "写回失败", f"✖ 交付记录没写回：{err}（草稿还在，可以再发）"
        what = "需求已确认" if card.project_id is None else "所有阶段都已通过"
        return "wait", "交付记录待确认", f"✉ {what}，交付记录等你确认"
    if st.status == "running":
        return "run", ("理解需求中" if st.stage == "req" else f"{sname}中"), current_step(st)[1]
    if st.status == "checking":
        h = st.running_helper()
        if h:  # 额外角色在跑
            a = board.agent(h.agent_id)
            return "run", f"{a.name if a else '?'}（辅助）中", current_step(st)[1]
        return "run", "程序自检", f"正在自检：{st.checking}"
    if st.flag == "failed":
        return "bad", "失败", f"✖ {st.error or f'{sname}阶段失败'}"
    if st.flag == "quota":
        return "quota", "额度耗尽", f"⛔ {st.error or '额度耗尽'}（不自动重试）"
    if st.flag == "stopped":
        if board.is_paused(card):
            return "gray", "已暂停", "⏸ 已暂停，点「▶ 继续」接着做"
        if board.is_aborted(card):
            return "gray", "已中止", "■ " + st.error
        return "gray", "已停止", f"■ {st.error or '已停止'}"
    if not board.checks_ok(st):
        bad = [f"{k}命令 exit {c}" for k, (_, c) in st.checks.items() if c != 0]
        return "bad", "自检失败", "✖ " + "，".join(bad)
    if st.stage == "req":
        return "wait", "需求待确认", ""  # 徽标已说明在等你确认，不再重复一行
    nxt = f"通过后进入{STAGE_NAME[card.stages[card.cur + 1].stage]}" if card.cur < len(card.stages) - 1 else "通过即完成"
    return "wait", "待确认", f"✋ {sname}阶段跑完，等你确认（{nxt}）"


def running_agent(board: Board, card: Card):
    """正在跑的模板：额外角色在跑就是它，否则是本阶段的主 agent"""
    h = card.stage.running_helper()
    return board.agent(h.agent_id if h else card.stage.agent_id)


BADGE = {"run": "▶", "wait": "●", "bad": "!", "quota": "!", "gray": "!", "ok": "✓"}
TODO_BADGE = "○ 未开始"


def badge_of(board: Board, card: Card) -> tuple[str, str]:
    """卡片徽标：(样式, 文字)。▶ 在跑 / ● 等你确认 / ! 出问题 / ✓ 完成"""
    kind, stext, _ = status_of(board, card)
    mark = {"已暂停": "⏸", "已中止": "■"}.get(stext, BADGE[kind])  # 暂停 / 中止是你主动点的，不用「!」
    return kind, f"{mark} {stext}"


def stage_line(board: Board, card: Card) -> str:
    """紧凑的阶段进度：需求 ✓  开发 ●  验证 ○（富文本）"""
    kind = status_of(board, card)[0]
    parts = []
    for i, s in enumerate(card.stages):
        if s.status == "passed":
            parts.append(f"<span style='color:#23784f'>{STAGE_NAME[s.stage]} ✓</span>")
        elif i == card.cur and card.column != "done":
            color = "#c4363a" if kind in ("bad", "quota") else "#2c56d8"
            parts.append(f"<span style='color:{color}'><b>{STAGE_NAME[s.stage]} ●</b></span>")
        else:
            parts.append(f"<span style='color:#b0b5be'>{STAGE_NAME[s.stage]} ○</span>")
    return "&nbsp;&nbsp;".join(parts)


def actions_for(win: "MainWindow", card: Card):
    """卡片和抽屉共用的操作按钮：(键, 文字, 回调, 样式名, 可用, 提示)"""
    board = win.board
    if card.column == "running":
        # 主 AI 在跑：只放「暂停」（中止在抽屉里）；自检 / 额外角色在跑只能停止
        if board.can_pause(card):
            return [("pause", "⏸ 暂停", lambda: board.pause(card), None, True, "停掉进程，保留会话，可点「继续」接着做")]
        return [("stop", "■ 停止", lambda: board.stop(card), "danger", True, "")]
    if card.comment == "draft":
        return [("writeback", "看交付记录", lambda: win.open_writeback(card), "primary", True, "")]
    if card.column == "review" and card.stage.stage == "req":
        # 需求阶段不能在卡片上直接通过，必须到抽屉里看过方案再确认
        return [("spec", "看需求方案", lambda: win.select(card.id), "primary", True, "")]
    if card.column == "review":
        # 卡片上只放一个按钮：能通过就「通过」，不能就「看原因」（打开抽屉，在里面让 AI 接着改）
        if board.can_pass(card):
            return [("pass", "✓ 通过", lambda: win.request_pass(card), "primary", True, "")]
        return [("why", "看原因", lambda: win.select(card.id), None, True, "")]
    if card.project_id is not None and not card.worktree_cleaned:
        return [("cleanup", "清理 worktree", lambda: win.open_cleanup(card), None, True, "")]
    return []


def add_buttons(layout, specs, store):
    for key, text, slot, name, enabled, tip in specs:
        b = QPushButton(text)
        if name:
            b.setObjectName(name)
        b.setEnabled(enabled)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        layout.addWidget(b)
        store[key] = b


class CardWidget(QFrame):
    """已开始的卡：右上角徽标表示本地进度；AI 在跑时突出「正在干什么」；其余信息收进抽屉"""
    clicked = Signal(object)

    def __init__(self, win: "MainWindow", card: Card, selected: bool):
        super().__init__()
        self.card = card
        self.key = str(card.id)
        board = win.board
        self.setObjectName("card")
        self.setProperty("sel", "true" if selected else "false")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(7)

        kind, stext, now = status_of(board, card)
        st = card.stage
        top = QHBoxLayout()
        top.addWidget(label(f"{card.item.type} {card.item.label}", "cardMeta"))
        top.addStretch()
        self.badge = label(badge_of(board, card)[1], "status", kind)
        top.addWidget(self.badge)
        lay.addLayout(top)
        lay.addWidget(label(card.item.title, "cardTitle", wrap=True))

        if card.column == "running":
            # 正在干什么：谁 · 哪个阶段 · 第几步 · 耗时 + 放大的当前动作；额外角色在跑时是它的 CLI
            agent = running_agent(board, card)
            act = QFrame()
            act.setObjectName("activity")
            av = QVBoxLayout(act)
            av.setContentsMargins(10, 8, 10, 8)
            av.setSpacing(3)
            cli = agent.cli if agent else "claude"
            steps = current_step(st)[0]
            who = (f"<span style='color:{CLI_COLOR[cli]}'>●</span> {CLI_NAME[cli]} · {stext}"
                   f"{'（只读）' if st.stage == 'req' else ''} · 第 {max(steps, 1)} 步 · {fmt_elapsed(st.elapsed)}")
            av.addWidget(label(who, "actWho"))
            self.now_label = label(now, "actNow", wrap=True)
            self.now_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            av.addWidget(self.now_label)
            lay.addWidget(act)
        else:
            self.now_label = label(now, "now", kind, wrap=True)
            self.now_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.now_label.setVisible(bool(now))  # 空文字不留空白行
            lay.addWidget(self.now_label)

        bottom = QHBoxLayout()
        sl = label(stage_line(board, card), "stageLine")
        sl.setTextFormat(Qt.RichText)
        bottom.addWidget(sl)
        bottom.addStretch()
        self.buttons = {}
        add_buttons(bottom, actions_for(win, card), self.buttons)
        lay.addLayout(bottom)

    def mousePressEvent(self, e):
        self.clicked.emit(self.key)
        super().mousePressEvent(e)


class TodoWidget(QFrame):
    """未开始的卡（Azure 迭代里拉到的，或未开始的手写任务）：只有标题和「○ 未开始」，点开在抽屉里选仓库再开始"""
    clicked = Signal(object)

    def __init__(self, key: str, item, selected: bool):
        super().__init__()
        self.key, self.item, self.card = key, item, None
        self.setObjectName("card")
        self.setProperty("sel", "true" if selected else "false")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(7)
        top = QHBoxLayout()
        top.addWidget(label(f"{item.type} {item_label(item)}", "cardMeta"))
        top.addStretch()
        self.badge = label(TODO_BADGE, "status", "todo")
        top.addWidget(self.badge)
        lay.addLayout(top)
        lay.addWidget(label(item.title, "cardTitle", wrap=True))

    def mousePressEvent(self, e):
        self.clicked.emit(self.key)
        super().mousePressEvent(e)


def item_label(item) -> str:
    """Azure 列表行（azure.Brief）没有 label 属性，手写任务（WorkItem）有"""
    return item.label if isinstance(item, WorkItem) else f"#{item.id}"


class Column(QFrame):
    """顶部一行的列头（To Do / Doing / Done + 张数）；cards = 这一列在所有泳道里的卡片控件"""

    def __init__(self, key, title):
        super().__init__()
        self.key = key
        self.setObjectName("colHead")
        self.setMinimumWidth(COL_MIN)
        head = QHBoxLayout(self)
        head.setContentsMargins(12, 6, 12, 6)
        head.addWidget(label(title, "colTitle"))
        self.count = label("0", "colCount")
        head.addWidget(self.count)
        head.addStretch()
        self.cards: list[CardWidget | TodoWidget] = []


@dataclass
class Group:
    """看板上的一张卡。PBI 卡：pbi=True，key = 父项卡号，head = 父项（azure.Brief，取不到为 None），
    rows = 我的 Task 行 [(键, 已开始的卡, 未开始的条目)]；独立卡：pbi=False，rows 只有自己一项"""
    col: str
    key: str
    pbi: bool
    head: object = None
    rows: list = field(default_factory=list)


def pbi_title(b) -> str:
    return f"{b.type} #{b.id} {b.title} · {b.state}"


class TaskRow(QFrame):
    """PBI 卡里的一行：#号 标题 + Azure 状态 + AI 徽标；已开始的卡带操作按钮。点击打开 Task 抽屉"""
    clicked = Signal(object)

    def __init__(self, win: "MainWindow", key: str, card: Card | None, item, selected: bool):
        super().__init__()
        self.key, self.card = key, card
        board = win.board
        self.setObjectName("taskRow")
        self.setProperty("sel", "true" if selected else "false")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(4)
        top = QHBoxLayout()
        it = card.item if card else item
        title = label(f"#{it.id} {it.title}", "rowTitle", wrap=True)
        title.setTextFormat(Qt.PlainText)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(title, 1)
        kind, text = badge_of(board, card) if card else ("todo", TODO_BADGE)
        self.badge = label(text, "status", kind)
        top.addWidget(self.badge, 0, Qt.AlignTop)
        lay.addLayout(top)
        br = win.brief(key)
        bottom = QHBoxLayout()
        now = status_of(board, card)[2] if card and card.column == "running" else ""
        meta = label(" · ".join(x for x in (br.state if br else "", now) if x), "cardMeta")
        meta.setTextFormat(Qt.PlainText)
        meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bottom.addWidget(meta, 1)
        self.buttons = {}
        if card:
            add_buttons(bottom, actions_for(win, card), self.buttons)
        lay.addLayout(bottom)

    def mousePressEvent(self, e):
        self.clicked.emit(self.key)
        super().mousePressEvent(e)


class PbiHead(QFrame):
    """PBI 卡的标题行：「类型 #号 标题 · 状态」，点击打开 PBI 抽屉"""
    clicked = Signal(object)

    def __init__(self, win: "MainWindow", g: Group, selected: bool):
        super().__init__()
        self.key = g.key
        self.setObjectName("pbiHead")
        self.setProperty("sel", "true" if selected else "false")
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        self.title = label(pbi_title(g.head) if g.head else f"#{g.key}（取不到父项详情）", "pbiTitle", wrap=True)
        self.title.setTextFormat(Qt.PlainText)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.title, 1)
        card = win.board.find_card(g.key)  # 早期在父项本身上开始过的老卡
        self.badge = None
        if card:
            kind, text = badge_of(win.board, card)
            self.badge = label(text, "status", kind)
            row.addWidget(self.badge, 0, Qt.AlignTop)

    def mousePressEvent(self, e):
        self.clicked.emit(self.key)
        super().mousePressEvent(e)


class PbiCard(QFrame):
    """PBI 卡：标题行 + 我的 Task 行"""

    def __init__(self, win: "MainWindow", g: Group, selected: str | None):
        super().__init__()
        self.key = g.key
        self.setObjectName("pbiCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self.head = PbiHead(win, g, g.key == selected)
        self.head.clicked.connect(win.select)
        lay.addWidget(self.head)
        self.rows = []
        for key, card, item in g.rows:
            r = TaskRow(win, key, card, item, key == selected)
            r.clicked.connect(win.select)
            lay.addWidget(r)
            self.rows.append(r)


def set_html(tb: QTextBrowser, h: str):
    """内容没变就不重设，避免每次刷新都把滚动位置打回顶部"""
    if tb.property("h") == h:
        return
    v = tb.verticalScrollBar().value()
    tb.setHtml(h)
    tb.setProperty("h", h)
    tb.verticalScrollBar().setValue(v)


def boxed(w, name):
    f = QFrame()
    f.setObjectName(name)
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 10, 12, 10)
    v.addWidget(w)
    return f


def open_vscode(path: str) -> str | None:
    """用 VS Code 打开原仓库目录；返回失败原因或 None"""
    try:
        r = subprocess.run(["open", "-b", "com.microsoft.VSCode", path], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"打不开 VS Code：{e}"
    if r.returncode != 0:
        return f"打不开 VS Code：{r.stderr.strip() or r.stdout.strip() or f'退出码 {r.returncode}'}"
    return None


def text_block(w) -> str:
    """描述 + 验收标准（富文本）；描述里有图片时提醒 AI 看不到"""
    e = html.escape
    n = azure.image_count(w.description)
    img = f"<div style='margin-top:6px;color:#9a6200'>描述里有 {n} 张图片，AI 看不到</div>" if n else ""
    ac = (e(w.acceptance).replace(chr(10), "<br>") or
          "<span style='color:#6b7280'>（没有验收标准：需求阶段 AI 会问做到什么程度算完成，可以在方案里补充）</span>")
    return (f"<div><b>{e(w.title)}</b><span style='color:#6b7280'> · {e(w.state)}</span></div>"
            f"<div style='margin-top:6px'>{e(w.description).replace(chr(10), '<br>') or '（无描述）'}</div>"
            f"{img}<div style='margin-top:8px'><b>验收标准</b><br>{ac}</div>")


def agent_combo(board: Board, placeholder: str) -> QComboBox:
    """模板下拉，无默认：名字 · CLI / 权限 · 能力"""
    cb = QComboBox()
    cb.addItem(placeholder, None)
    for a in board.agents:
        caps = "、".join(CAPS[c] for c in a.caps if c in CAPS) if a.cli in CAP_CLIS else ""
        cb.addItem(f"{a.name}  ·  {CLI_NAME[a.cli]} / {PERM_NAME[a.perm]}" + (f" · {caps}" if caps else ""), a.id)
    return cb


class HelperPick(QWidget):
    """「＋ 添加 agent」加出的一行：阶段 + 模板 + 删除。额外角色一律只读跑"""

    def __init__(self, board: Board, on_change, on_remove):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.stage_cb = QComboBox()
        for st in STAGES:
            self.stage_cb.addItem(f"{STAGE_NAME[st]}阶段后", st)
        self.stage_cb.currentIndexChanged.connect(on_change)
        row.addWidget(self.stage_cb)
        self.agent_cb = agent_combo(board, "— 选模板 —")
        self.agent_cb.currentIndexChanged.connect(on_change)
        row.addWidget(self.agent_cb, 1)
        self.del_btn = QPushButton("✕")
        self.del_btn.setToolTip("删掉这一行")
        self.del_btn.clicked.connect(lambda: on_remove(self))
        row.addWidget(self.del_btn)

    def value(self) -> tuple[str, str | None]:
        return self.stage_cb.currentData(), self.agent_cb.currentData()


def head(icon_text, color):
    """需求页每个分区的身份标签"""
    return f"<div style='color:{color};font-weight:700;font-size:13.5px;margin-bottom:6px'>{icon_text}</div>"


PENDING_RE = re.compile(r"[（(]待你确认.*?[）)]")
PENDING_MARK_STYLE = "color:#9a6200;font-size:11px;font-weight:700"
# 「（待你确认：Q，默认 D）」里 Q 与 D 的分隔：「，默认」「;默认」「？默认」「默认：」
_DEFAULT_SEP = re.compile(r"(?:[，,；;]\s*|(?<=[？?])\s*)默认\s*[:：]?\s*|(?:^|\s)默认\s*[:：]\s*")
_PENDING_HEAD = re.compile(r"^待你确认\s*[:：]?\s*")


def split_pending(frag: str) -> tuple[str, str | None]:
    """把一个「（待你确认：Q，默认 D）」片段拆成 (Q, D)；拆不出默认做法时 Q = 去掉括号和前缀后的整句、D = None"""
    t = frag.strip()
    if t[:1] in "（(":
        t = t[1:]
    if t[-1:] in "）)":
        t = t[:-1]
    t = _PENDING_HEAD.sub("", t.strip()).strip()
    m = _DEFAULT_SEP.search(t)
    if m and t[m.end():].strip():
        return t[:m.start()].strip(" ，,；;") or t[:m.start()], t[m.end():].strip()
    return t, None


def plan_questions(steps) -> list[tuple[str, str | None]]:
    """所有步骤里的疑问，按出现顺序（跨步骤）排成 [(Q, D)]，编号 = 下标 + 1"""
    return [split_pending(m.group()) for x in steps for m in PENDING_RE.finditer(x)]


def plan_html(steps):
    """方案步骤：编号 + html 转义；每处「（待你确认：…）」整段去掉，只留一个小标记 ❓n（n = 全局疑问编号）"""
    e = html.escape
    mark = lambda i: chr(0x2460 + i) if i < 20 else f"{i + 1}."
    n = [0]

    def one(x):
        out, pos = [], 0
        for m in PENDING_RE.finditer(x):
            out.append(e(x[pos:m.start()]))
            n[0] += 1
            out.append(f"<span style='{PENDING_MARK_STYLE}'> ❓{n[0]}</span>")
            pos = m.end()
        out.append(e(x[pos:]))
        return "".join(out)

    return "".join(f"<div style='margin:3px 0'>{mark(i)} {one(x)}</div>" for i, x in enumerate(steps)) \
        or "<div style='color:#9aa1ad'>（无）</div>"


def questions_html(qs, confirmed: bool = False) -> str:
    """疑问清单（标题 + 每条一行「n. Q　默认：D」）；没有疑问返回 ""。confirmed=True 是已确认的灰色样式"""
    if not qs:
        return ""
    e = html.escape
    rows = "".join(
        f"<div style='margin:3px 0'>{i}. {e(q)}"
        + (f"　<span style='color:#6b7280;font-size:12px'>默认：{e(d)}</span>" if d else "") + "</div>"
        for i, (q, d) in enumerate(qs, 1))
    if confirmed:
        return head("✓ 已确认的疑问（按默认做）", "#6b7280") + rows
    return head(f"❓ 需要你确认（{len(qs)}）", "#9a6200") + rows


def goal_html(p: dict) -> str:
    """「🎯 完成目标」分区的内容：goal + 完成标准（每条前面 ☐，没有就只显示 goal）"""
    e = html.escape
    out = head("🎯 完成目标", "#2c56d8") + f"<div>{e(p['goal']).replace(chr(10), '<br>')}</div>"
    out += "".join(f"<div style='margin:2px 0 2px 10px'>☐ {e(x)}</div>" for x in p.get("done") or [])
    return out


def numbered(items):
    e = html.escape
    mark = lambda i: chr(0x2460 + i) if i < 20 else f"{i + 1}."
    return "".join(f"<div style='margin:2px 0'>{mark(i)} {e(x)}</div>" for i, x in enumerate(items)) \
        or "<div style='color:#9aa1ad'>（无）</div>"


def fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


QUOTA_WARN, QUOTA_BAD = 30, 10  # 剩余 ≤ 30% 黄、≤ 10% 红


def left(w: dict) -> float:
    """剩余百分比 = 100 − 已用，不小于 0"""
    return max(0.0, 100 - w["pct"])


class QuotaBar(QWidget):
    """顶栏右侧三家额度（显示剩余）：Claude 取最近一次调用流里的 rate_limit_event；Codex 经 app-server 查；Grok 拿不到"""

    def __init__(self, board: Board):
        super().__init__()
        self.board = board
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.chips = {}
        for cli in clis.CLIS:
            lab = ElidedLabel("", "quotaChip", min_w=56)  # 窄窗口下可缩短，不把右边的按钮挤没
            self.chips[cli] = lab
            row.addWidget(lab)
        self.refresh()

    def refresh(self):
        for cli, lab in self.chips.items():
            text, tip, kind = self._chip(cli)
            lab.setText(text)
            lab.setToolTip(tip)
            lab.setProperty("kind", kind)
            lab.style().polish(lab)

    def _chip(self, cli):
        name = CLI_NAME[cli]
        if cli == "grok":
            return f"{name} 额度不可用", "grok 没有查询账号额度的途径，照常可用", "gray"
        q = self.board.quota.get(cli) or {}
        wins = q.get("windows") or []
        if not wins:
            if q.get("error"):
                return f"{name} 查询失败", q["error"], "bad"
            how = "跑一次 claude 后显示（额度只在调用的输出流里）" if cli == "claude" else "正在查询…"
            return f"{name} {'跑一次后显示' if cli == 'claude' else '查询中'}", how, "gray"
        parts = " · ".join(f"{w['name']} 剩 {left(w):.0f}%" for w in wins)
        tip = [f"{w['name']}：剩 {left(w):.0f}%，{fmt_ts(w.get('resets_at'))} 重置" for w in wins]
        tip.append(f"更新于 {fmt_ts(q.get('at'))}" + ("（取自最近一次 claude 调用）" if cli == "claude" else ""))
        low = min(left(w) for w in wins)
        kind = "bad" if low <= QUOTA_BAD else "warn" if low <= QUOTA_WARN else ""
        if q.get("error"):
            tip.append(f"最近一次查询失败（{fmt_ts(q.get('err_at'))}）：{q['error']}，上面是上次的值")
            return f"{name} {parts} ⚠", "\n".join(tip), "bad"
        return f"{name} {parts}", "\n".join(tip), kind


class Drawer(QFrame):
    """点卡片后从右侧滑出、盖在看板上（不挤压看板）。顶部：Azure 状态下拉、用 VS Code 打开。
    需求阶段：AI 给出「完成目标 + 方案」（遗漏写在步骤里的「待你确认」）→ 你补充（可多轮）→ 确认冻结。
    之后的阶段：顶部「AI 现在在干什么」+ 结果 / 改了哪些代码 / 方案 / 对话 / 更多 五个页签。
    未开始的 Task：需求（来自父项）+ ① 关联代码仓库 + ② 主 agent + ＋ 添加 agent → 开始需求分析；
    PBI 等其他类型：只有全文和状态下拉。"""
    WIDTH_MIN, WIDTH_MAX, FULL_BELOW = 360, 500, 760  # 窗口窄于 760 时铺满

    @classmethod
    def width_for(cls, parent_w: int) -> int:
        """抽屉宽度随窗口变化：clamp(父宽 × 0.42, 360, 500)；父宽 < 760 时等于父宽"""
        if parent_w < cls.FULL_BELOW:
            return parent_w
        return round(min(cls.WIDTH_MAX, max(cls.WIDTH_MIN, parent_w * 0.42)))

    def __init__(self, win: "MainWindow"):
        super().__init__(win.main)
        self.win = win
        self.card_id = None
        self.repo = self.main_cb = None
        self.repo_path = None  # 「用 VS Code 打开」的原仓库目录
        self.expanded: set[str] = set()  # 结果页 / 对话页里展开的长文本（锚点标识）
        self._changes_ver = None
        self.changes = None
        self.numstat = {}  # 路径 → (新增行数, 删除行数)，None = 二进制
        self._closing = False
        self.setObjectName("drawer")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(-6, 0)
        shadow.setColor(QColor(0, 0, 0, 55))
        self.setGraphicsEffect(shadow)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        # 卡号·来源·仓库·分支 / 状态下拉 / 用 VS Code 打开 / ✕ 在同一行
        head = QHBoxLayout()
        head.setSpacing(8)
        self.meta = ElidedLabel("", "cardMeta")
        head.addWidget(self.meta, 1)
        # Azure 状态：只在你手动选时改，仅 Azure 卡显示
        self.state_cb = QComboBox()
        self.state_cb.setToolTip("Azure 状态")
        self.state_cb.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.state_cb.activated.connect(self._pick_state)
        head.addWidget(self.state_cb)
        self.vscode_btn = QPushButton("用 VS Code 打开")
        self.vscode_btn.setToolTip("打开原仓库目录（AI 的改动在 worktree 的 feature 分支上，原目录看不到）")
        self.vscode_btn.clicked.connect(self._open_vscode)
        head.addWidget(self.vscode_btn)
        close = QPushButton("✕")
        close.setToolTip("关闭（Esc）")
        close.clicked.connect(lambda: win.select(None))
        head.addWidget(close)
        lay.addLayout(head)
        self.title = label("", "detailTitle", wrap=True)
        lay.addWidget(self.title)
        # 改状态的提示 / 打不开 VS Code 的原因，合并在标题下一行，空时整行隐藏
        self.hint_box = QWidget()
        hv = QVBoxLayout(self.hint_box)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(2)
        self.state_msg = label("", "note", wrap=True)
        self.state_msg.setTextFormat(Qt.PlainText)
        self.vs_msg = label("", "err", wrap=True)
        self.vs_msg.setTextFormat(Qt.PlainText)
        hv.addWidget(self.state_msg)
        hv.addWidget(self.vs_msg)
        lay.addWidget(self.hint_box)
        self._sync_hint()
        self.stages = label("", "stageLine")
        self.stages.setTextFormat(Qt.RichText)
        lay.addWidget(self.stages)
        self.banner = label("", "now", wrap=True)
        lay.addWidget(self.banner)
        lay.addWidget(self._build_run_bar())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_req_page())
        self.pages.addWidget(self._build_work_page())
        self.pages.addWidget(self._build_start_page())
        lay.addWidget(self.pages, 1)
        self.req_footer = self._build_req_footer()
        lay.addWidget(self.req_footer)
        self.start_footer = self._build_start_footer()
        lay.addWidget(self.start_footer)

        # 待确认时让 AI 接着改：直接在抽屉底部写一句，不再单独弹「追问」窗口
        self.ask_box = QWidget()
        ar = QHBoxLayout(self.ask_box)
        ar.setContentsMargins(0, 0, 0, 0)
        self.ask_edit = QLineEdit()
        self.ask_edit.returnPressed.connect(self._ask)
        self.ask_btn = QPushButton("发给 AI 接着改")
        self.ask_btn.clicked.connect(self._ask)
        self.back_btn = QPushButton("退回开发")
        self.back_btn.setToolTip("续聊开发阶段原会话，带上输入框里的备注（可空）和本阶段 AI 的回复；改完后重新跑验证")
        self.back_btn.clicked.connect(self._send_back)
        ar.addWidget(self.ask_edit, 1)
        ar.addWidget(self.ask_btn)
        ar.addWidget(self.back_btn)
        lay.addWidget(self.ask_box)

        self.footer_box = QWidget()
        self.footer = QHBoxLayout(self.footer_box)
        self.footer.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.footer_box)
        self.buttons = {}

        self.anim = QPropertyAnimation(self, b"pos", self)
        self.anim.setDuration(220)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.finished.connect(self._anim_done)
        self.hide()

    def _build_req_page(self):
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("drawerBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(10)
        # 内边距放在外框上：QSS padding 加在自动换行的 QLabel 上会算错高度、截掉最后几行
        # AI 的理解 / AI 打算怎么做 / 需要你回答，上下分区、各带身份标签和底色；需求原文不再显示
        self.goal = label("", wrap=True)
        self.goal.setTextFormat(Qt.RichText)
        v.addWidget(boxed(self.goal, "undBox"))
        # 疑问框在方案上面，每条一行；没有疑问时整个框隐藏
        self.pend = label("", wrap=True)
        self.pend.setTextFormat(Qt.RichText)
        self.pend_box = boxed(self.pend, "pendBox")
        v.addWidget(self.pend_box)
        self.plan = label("", wrap=True)
        self.plan.setTextFormat(Qt.RichText)
        self.plan_box = boxed(self.plan, "planBox")
        v.addWidget(self.plan_box)
        # 需求阶段没有「AI 的反馈」页签：本阶段额外角色的意见放在这里
        self.req_helpers = label("", wrap=True)
        self.req_helpers.setTextFormat(Qt.RichText)
        self.req_helpers_box = boxed(self.req_helpers, "parentBox")
        v.addWidget(self.req_helpers_box)
        v.addWidget(label("有问题或要补充，写给 AI 改方案", "secLabel"))
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("例如：字段只要日期和金额；不用做导出页")
        self.answer.setFixedHeight(76)
        v.addWidget(self.answer)
        row = QHBoxLayout()
        row.addStretch()
        self.reply_btn = QPushButton("发给 AI 改方案")
        self.reply_btn.clicked.connect(self._reply)
        row.addWidget(self.reply_btn)
        v.addLayout(row)
        v.addStretch()
        page.setWidget(body)
        return page

    def _build_run_bar(self):
        """「在跑条」：状态一行 + 按钮。需求页和工作页都显示，在这几种状态下替代 banner"""
        self.run_bar = QWidget()
        row = QHBoxLayout(self.run_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.run_label = label("", "now", wrap=True)
        self.run_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.run_label, 1)
        self.run_buttons = {}
        for key, text, name, slot in [
            ("pause", "⏸ 暂停", None, self._pause), ("resume", "▶ 继续", "primary", self._resume),
            ("stop", "■ 停止", "danger", self._stop), ("abort", "■ 中止", "danger", self._abort),
            ("restart", "↻ 重新开始本阶段", "primary", self._restart),
        ]:
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            row.addWidget(b, 0, Qt.AlignTop)
            self.run_buttons[key] = b
        self.run_bar.hide()
        return self.run_bar

    RUN_BAR = {"run": ("pause", "abort"), "stop": ("stop",), "paused": ("resume", "abort"), "aborted": ("restart",)}

    def run_mode(self, card: Card):
        """在跑条的模式：run 主 AI 在跑 / stop 自检或额外角色在跑 / paused / aborted / None 不显示"""
        b, st = self.win.board, card.stage
        if b.can_pause(card):
            return "run"
        if st.status in ("running", "checking"):
            return "stop"
        if b.is_paused(card):
            return "paused"
        if b.is_aborted(card):
            return "aborted"
        return None

    def _sync_run_bar(self, card: Card, kind: str, now: str):
        mode = self.run_mode(card)
        self.run_bar.setVisible(mode is not None)
        if mode is None:
            return
        self.run_label.setText(f"▶ {now}" if kind == "run" else now)
        self.run_label.setProperty("kind", kind)
        self.run_label.style().polish(self.run_label)
        for key, b in self.run_buttons.items():
            b.setVisible(key in self.RUN_BAR[mode])

    def _pause(self):
        c = self._card()
        if c:
            self.win.board.pause(c)

    def _resume(self):
        c = self._card()
        if c:
            self.win.board.resume(c)

    def _stop(self):
        c = self._card()
        if c:
            self.win.board.stop(c)

    def _abort(self):
        c = self._card()
        if c and self.win.board.can_abort(c):
            self.win.open_abort(c)

    def _restart(self):
        c = self._card()
        if c:
            self.win.board.restart_stage(c)

    def _build_work_page(self):
        """已开始的卡：顶部固定「AI 现在在干什么」，下面五个页签"""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.now_box = label("", wrap=True)
        self.now_box.setTextFormat(Qt.RichText)
        v.addWidget(boxed(self.now_box, "activity"))
        self.tabs = QTabWidget()
        self.views = {}
        self.tab_keys = []  # 页签 key，顺序同页签
        for key, name in [("result", "结果"), ("changes", "改了哪些代码"), ("spec", "方案"), ("talk", "对话"),
                          ("more", "更多")]:
            tb = QTextBrowser()
            self.views[key] = tb
            self.tab_keys.append(key)
            self.tabs.addTab(self._build_changes(tb) if key == "changes" else tb, name)
        for key in ("result", "talk"):  # 「看 AI 原话」「展开」都是页内锚点
            self.views[key].setOpenLinks(False)
            self.views[key].anchorClicked.connect(self._toggle_ask)
        self.tabs.currentChanged.connect(lambda _: self._card() and self._show_changes(self._card()))
        v.addWidget(self.tabs, 1)
        return page

    def _build_changes(self, tb):
        """代码改动页：说明 + 刷新 / 文件清单（点一个看它的 diff）/ diff 或 commit 列表"""
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 6, 0, 0)
        row = QHBoxLayout()
        self.changes_note = label("", "note", wrap=True)
        self.changes_note.setTextFormat(Qt.PlainText)
        self.changes_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.changes_note, 1)
        self.changes_btn = QPushButton("刷新")
        self.changes_btn.clicked.connect(lambda: self._card() and self._show_changes(self._card(), force=True))
        row.addWidget(self.changes_btn)
        v.addLayout(row)
        self.change_list = QListWidget()
        self.change_list.setMaximumHeight(150)
        self.change_list.currentRowChanged.connect(self._show_file)
        v.addWidget(self.change_list)
        v.addWidget(tb, 1)
        self.changes_box = box
        return box

    def _build_req_footer(self):
        """确认按钮固定在抽屉底部，不用滚动去找。不关联仓库的卡换成两个按钮"""
        box = QFrame()
        box.setObjectName("reqFooter")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 10, 0, 0)
        row = QHBoxLayout()
        row.addStretch()
        self.finish_btn = QPushButton("确认需求并结束")
        self.finish_btn.clicked.connect(self._finish)
        self.attach_btn = QPushButton("确认需求，选仓库进入开发…")
        self.attach_btn.setObjectName("primary")
        self.attach_btn.clicked.connect(self._attach)
        self.confirm_btn = QPushButton("✓ 确认方案，开始开发")
        self.confirm_btn.setObjectName("primary")
        self.confirm_btn.clicked.connect(self._confirm)
        for b in (self.finish_btn, self.attach_btn, self.confirm_btn):
            row.addWidget(b)
        v.addLayout(row)
        return box

    def _build_start_page(self):
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("drawerBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(10)
        # Task 抽屉：需求（来自父项）；PBI 抽屉：全文
        self.preview = label("", wrap=True)
        self.preview.setTextFormat(Qt.RichText)
        v.addWidget(boxed(self.preview, "parentBox"))
        # ① 仓库 / ② 主 agent + 额外角色放在 pick_host 里，换一张卡就整组重建（不带上一张卡的选择）
        self.pick_host = QVBoxLayout()
        self.pick_host.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self.pick_host)
        self.pick_box = None
        self.start_err = label("", "err", wrap=True)
        self.start_err.setTextFormat(Qt.PlainText)
        self.start_err.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.start_err)
        v.addStretch()
        page.setWidget(body)
        return page

    def _build_start_footer(self):
        box = QFrame()
        box.setObjectName("reqFooter")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 10, 0, 0)
        self.start_hint = label("", "warn", wrap=True)
        row.addWidget(self.start_hint, 1)
        self.start_btn = QPushButton("开始需求分析")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._start)
        row.addWidget(self.start_btn, 0, Qt.AlignRight)
        return box

    # ---------- 滑入 / 滑出 ----------
    def _target_x(self, open_):
        w = self.parentWidget().width()
        return w - self.width_for(w) if open_ else w

    def place(self):
        """父窗口尺寸变化时贴右边、铺满高度"""
        p = self.parentWidget()
        self.resize(self.width_for(p.width()), p.height())
        if self.isVisible() and self.anim.state() != QPropertyAnimation.Running:
            self.move(self._target_x(not self._closing), 0)

    def slide_in(self):
        self._closing = False
        if self.isVisible() and self.anim.state() != QPropertyAnimation.Running:
            return
        p = self.parentWidget()
        self.resize(self.width_for(p.width()), p.height())
        if not self.isVisible():
            self.move(self._target_x(False), 0)
            self.show()
        self.raise_()
        self.anim.stop()
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(QPoint(self._target_x(True), 0))
        self.anim.start()

    def slide_out(self):
        if not self.isVisible():
            return
        self._closing = True
        self.anim.stop()
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(QPoint(self._target_x(False), 0))
        self.anim.start()

    def _anim_done(self):
        if self._closing:
            self.hide()

    @property
    def is_open(self):
        return self.isVisible() and not self._closing

    # ---------- 内容 ----------
    def _card(self):
        return self.win.board.find_card(self.card_id)

    def _reply(self):
        c = self._card()
        if c and self.win.board.ask(c, self.answer.toPlainText()):
            self.answer.clear()

    def _ask(self):
        c = self._card()
        if c and self.win.board.ask(c, self.ask_edit.text()):
            self.ask_edit.clear()

    def _send_back(self):
        c = self._card()
        if c and self.win.board.send_back(c, self.ask_edit.text()):
            self.ask_edit.clear()

    # ---------- Azure 状态 ----------
    def _show_state(self, key: str):
        w = self.win
        br = w.brief(key)
        self.state_cb.setVisible(br is not None)
        if br is None:
            self.state_msg.setText("")
            self._sync_hint()
            return
        job = w.state_jobs.get(key)
        names = [n for n, _ in w.state_lists.get(br.type, [])]
        if br.state and br.state not in names:
            names.insert(0, br.state)
        cb = self.state_cb
        cb.blockSignals(True)
        if [cb.itemText(i) for i in range(cb.count())] != names:
            cb.clear()
            cb.addItems(names)
        if job is None:  # 发送中保持你选的值，不被刷新冲掉
            cb.setCurrentIndex(max(0, names.index(br.state)) if br.state in names else -1)
        cb.blockSignals(False)
        cb.setEnabled(job is None and br.type in w.state_lists)
        msg, bad = w.state_msgs.get(key, ("", False))
        if job is not None:
            msg, bad = f"正在改成「{job.target}」…", False
        elif not msg and br.type in w.state_errors:
            msg, bad = f"取不到「{br.type}」的状态列表：{w.state_errors[br.type]}（点「刷新」重试）", True
        self.state_msg.setText(msg)
        self.state_msg.setStyleSheet("color:#c4363a" if bad else "")
        self._sync_hint()

    def _sync_hint(self):
        """标题下的提示行：两段文字都空就整行隐藏"""
        self.state_msg.setVisible(bool(self.state_msg.text()))
        self.vs_msg.setVisible(bool(self.vs_msg.text()))
        self.hint_box.setVisible(self.state_msg.isVisibleTo(self.hint_box) or self.vs_msg.isVisibleTo(self.hint_box))

    def _pick_state(self, _i):
        if self.card_id is not None:
            self.win.change_state(self.card_id, self.state_cb.currentText())

    # ---------- 用 VS Code 打开 ----------
    def _set_repo_path(self, path):
        if path != self.repo_path:
            self.vs_msg.setText("")
        self.repo_path = path
        self.vscode_btn.setVisible(bool(path))
        self._sync_hint()

    def _open_vscode(self):
        if self.repo_path:
            self.vs_msg.setText(self.win.vscode(self.repo_path) or "")
            self._sync_hint()

    # ---------- 所属 PBI ----------
    def _ensure_detail(self, key: str):
        w = self.win
        if key not in w.details and key not in w.detail_errors and key not in w.detail_jobs:
            self._fetch_detail(key)

    def parent_html(self, key: str) -> str:
        """这张卡的父项：号 / 标题 / 状态 + 描述 + 验收标准；没有父项返回空"""
        w = self.win
        br = w.brief(key)
        if br is None or not br.parent:
            return ""
        pk = str(br.parent)
        self._ensure_detail(pk)
        e = html.escape
        p = w.brief(pk)
        what = p.type if p else "父项"
        top = (f"<div><b>{e(p.type)} #{p.id} {e(p.title)}</b><span style='color:#6b7280'> · {e(p.state)}</span></div>"
               if p else f"<div><b>#{e(pk)}</b></div>")
        d, err = w.details.get(pk), w.detail_errors.get(pk)
        if d is None:
            body = (f"<span style='color:#c4363a'>取详情失败：{e(err)}（再点一下这张卡可重试）</span>" if err
                    else "<span style='color:#6b7280'>正在获取描述…</span>")
        else:
            ac = e(d.acceptance).replace(chr(10), "<br>") or "<span style='color:#6b7280'>卡片没写验收标准</span>"
            body = (f"<div style='margin-top:6px'>{e(d.description).replace(chr(10), '<br>') or '（无描述）'}</div>"
                    f"<div style='margin-top:8px'><b>验收标准</b><br>{ac}</div>")
        return head(f"📦 所属 {e(what)}", "#7a3fb0") + top + body

    def _confirm(self):
        c = self._card()
        if c:
            self.win.board.confirm_spec(c)

    def _finish(self):
        c = self._card()
        if c:
            self.win.board.finish_after_req(c)

    def _attach(self):
        c = self._card()
        if c:
            self.win._open(AttachRepoDialog(self.win.board, c, None, self.win,
                                            chooser=self.win.chooser))

    # ---------- 未开始的卡 ----------
    def can_start_kind(self, key: str, item) -> bool:
        """能开始 = 手写任务，或指派给我的 Azure Task；PBI / 不是指派给我的父项只看和改状态"""
        if isinstance(item, WorkItem):
            return True
        return item.type == "Task" and any(str(r.id) == key for r in self.win.rows)

    def show_item(self, key: str, item):
        """item：azure.Brief（迭代里拉到、还没开始）或 WorkItem（未开始的手写任务）"""
        azure_item = not isinstance(item, WorkItem)
        if key != self.card_id or self.repo is None:
            self.card_id = key
            self.answer.clear()
            self.ask_edit.clear()
            self.start_err.setText("")
            self._build_pickers()
        else:
            self.repo.reload()
        startable = self.can_start_kind(key, item)
        br = self.win.brief(key) if azure_item else None
        pk = str(br.parent) if br and br.parent and startable else None  # 需求取自父项
        if azure_item and pk is None:
            self._ensure_detail(key)
        if pk:
            self._ensure_detail(pk)
        src = "Azure" if azure_item else "手写"
        mine = not azure_item or any(str(r.id) == key for r in self.win.rows)
        why = ("未开始" if startable else NOT_TASK.format(type=item.type) if mine else "父项，不是指派给我的，只能看和改状态")
        self.meta.setText(f"{item.type} {item_label(item)} · {src} · {why}")
        self.meta.setToolTip(self.meta.text())
        self.title.setText(item.title)
        for w in (self.stages, self.banner, self.run_bar, self.req_footer, self.ask_box, self.footer_box):
            w.setVisible(False)
        self.pick_box.setVisible(startable)
        self.start_footer.setVisible(startable)
        self.pages.setCurrentIndex(2)
        self._show_state(key)
        self.preview.setText(self.requirement_html(key, item, pk) if startable else self.full_html(key, item))
        self._sync_start()

    def _detail_state(self, key: str) -> str:
        """详情还没取到时的一行说明；取到了返回空串"""
        e = html.escape
        err = self.win.detail_errors.get(key)
        if err:
            return f"<span style='color:#c4363a'>取详情失败：{e(err)}（再点一下这张卡可重试）</span>"
        return f"<span style='color:#6b7280'>正在获取 #{e(key)} 的描述…</span>"

    def full_html(self, key: str, item) -> str:
        """PBI 抽屉：全文 = 描述 + 验收标准"""
        e = html.escape
        w = self.win.details.get(key)
        top = head(f"📦 {e(item.type)} 全文", "#7a3fb0")
        if w is None:
            return top + self._detail_state(key)
        return top + text_block(w)

    def requirement_html(self, key: str, item, pk: str | None) -> str:
        """Task 抽屉的需求区：有父项用父项的描述 + 验收，没有用 Task 自己的；不再显示 Task 原文"""
        e = html.escape
        if pk:
            p = self.win.brief(pk)
            what = f"{e(p.type)} #{p.id} {e(p.title)}" if p else f"#{e(pk)}"
            top = head(f"📋 需求（来自 {what}）", "#7a3fb0")
            w = self.win.details.get(pk)
            return top + (text_block(w) if w else self._detail_state(pk))
        if isinstance(item, WorkItem):
            return head("📋 需求（手写任务）", "#4b5563") + text_block(item)
        top = head("📋 需求（Task 自己的描述）", "#4b5563")
        w = self.win.details.get(key)
        return top + (text_block(w) if w else self._detail_state(key))

    def _build_pickers(self):
        if self.pick_box is not None:
            self.pick_box.deleteLater()
        box = self.pick_box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(label("① 关联代码仓库", "secLabel"))
        self.repo_err = label("", "err", wrap=True)
        self.repo_err.setTextFormat(Qt.PlainText)
        self.repo = RepoPicker(self.win.board, on_change=self._sync_start, on_error=self.repo_err.setText,
                               chooser=self.win.chooser)
        v.addWidget(self.repo)
        self.repo_note = label("", "note", wrap=True)
        v.addWidget(self.repo_note)
        v.addWidget(self.repo_err)
        v.addSpacing(6)
        v.addWidget(label("② 主 agent（需求 → 开发 → 验证都由它做）", "secLabel", wrap=True))
        self.main_cb = agent_combo(self.win.board, "— 请选择主 agent —")
        self.main_cb.currentIndexChanged.connect(self._sync_start)
        v.addWidget(self.main_cb)
        self.helper_box = QVBoxLayout()
        self.helper_box.setSpacing(6)
        v.addLayout(self.helper_box)
        self.helper_rows: list[HelperPick] = []
        row = QHBoxLayout()
        self.add_helper_btn = QPushButton("＋ 添加 agent")
        self.add_helper_btn.setToolTip("某个阶段在主 agent 之后再跑一个角色（只读，给意见），比如验证阶段加「代码审查」")
        self.add_helper_btn.clicked.connect(self.add_helper)
        row.addWidget(self.add_helper_btn)
        row.addStretch()
        v.addLayout(row)
        self.pick_host.addWidget(box)

    def add_helper(self):
        """加一行额外角色：阶段 + 模板"""
        h = HelperPick(self.win.board, self._sync_start, self._remove_helper)
        self.helper_rows.append(h)
        self.helper_box.addWidget(h)
        self._sync_start()
        return h

    def _remove_helper(self, h):
        self.helper_rows.remove(h)
        h.deleteLater()
        self._sync_start()

    def helpers(self) -> list[tuple[str, str]]:
        return [h.value() for h in self.helper_rows]

    def _fetch_detail(self, key: str):
        """后台取单卡详情：描述要先取到才能开始，提示词要用。
        先点 #5 再点 #6：#5 的结果照样进缓存；重拉迭代 / 卡片或改了 Azure 设置后，在途请求失效、结果丢弃"""
        b, w = self.win.board, self.win
        org, proj, ado, num = b.azure_org, b.azure_project, b.ado, int(key)
        w.detail_errors.pop(key, None)
        job = w.detail_jobs[key] = clis.BgJob(azure.safe(lambda: ado.fetch_item(org, proj, num)), self)
        job.done.connect(lambda res: self._detail_done(job, key, res))
        job.start()

    def _detail_done(self, job, key, res):
        w = self.win
        job.deleteLater()
        if w.detail_jobs.get(key) is not job:
            return
        del w.detail_jobs[key]
        if "error" in res:
            w.detail_errors[key] = res["error"]
        else:
            w.details[key] = res["ok"]
        if self.card_id is not None and self.is_open:  # 可能是这张卡本身，也可能是它的父项
            w.render()

    def start_item(self):
        """开始时用的 (任务, 父项详情)：有父项的 Task 用列表行 + 父项详情（需求取自父项）；
        没父项的 Task 用它自己的详情；手写任务用本地内容。要的详情没取到返回 None"""
        key, b, w = self.card_id, self.win.board, self.win
        todo = b.find_todo(key)
        if todo:
            return todo, None
        br = w.brief(key)
        if br is None:
            return None
        if br.parent:
            parent = w.details.get(str(br.parent))
            if parent is None:
                return None
            return WorkItem(str(br.id), br.type, br.title, br.state, "", "", "azure"), parent
        it = w.details.get(key)
        return (it, None) if it else None

    def _sync_start(self, *_):
        if self.repo is None or self.pages.currentIndex() != 2:
            return
        b = self.win.board
        repo, got = self.repo.value(), self.start_item()
        p = b.project(repo) if repo else None
        self._set_repo_path(p.path if p else None)
        missing = []
        if repo is None:
            missing.append("选关联代码仓库")
        if self.main_cb.currentData() is None:
            missing.append("选主 agent")
        if any(aid is None for _, aid in self.helpers()):
            missing.append("给添加的 agent 选模板")
        if got is None:
            missing.append("等需求详情取到")
        ai_off = b.find_todo(self.card_id) is None and not b.ai_allowed()  # 手写任务不受 AI 开关影响
        self.start_btn.setEnabled(not missing and not ai_off)
        self.start_hint.setText(AI_OFF if ai_off else "还差：" + "、".join(missing) if missing else "")
        if repo == NO_REPO:
            note = (f"不关联仓库：需求阶段在空目录 data/scratch/{self.card_id}/ 里只读跑，AI 看不到代码；"
                    "确认需求时再决定直接结束，还是选仓库进入开发")
        elif p:
            src = "manual" if b.find_todo(self.card_id) else "azure"
            branch = Card(WorkItem(self.card_id, "", "", "", "", "", src), None, []).branch
            note = (f"开始时建 worktree：data/worktrees/{p.name}/{self.card_id} · 分支 {branch}"
                    f"（从 {p.base_branch} 切出）")
            if self._wants_codegraph() and not (Path(p.path) / ".codegraph").is_dir():
                note += f"\n{p.name} 没建代码检索索引（在仓库根跑 codegraph init 后可用）"
        else:
            note = "没有默认值，必须选：记住的仓库 / 选择本地文件夹… / 不关联仓库（调研、建模）"
        self.repo_note.setText(note)

    def _wants_codegraph(self) -> bool:
        """选中的主 agent 或额外角色里有勾了「代码检索」、且用的 CLI 支持挂载的"""
        b = self.win.board
        ids = [self.main_cb.currentData()] + [aid for _, aid in self.helpers()]
        return any((a := b.agent(i)) and "codegraph" in a.caps and a.cli in CAP_CLIS for i in ids if i)

    def _start(self):
        got, repo, main = self.start_item(), self.repo.value(), self.main_cb.currentData()
        helpers = self.helpers()
        if got is None or repo is None or main is None or any(aid is None for _, aid in helpers):
            # 例如自动拉取刚清掉了需求详情：重画抽屉（会补拉详情），开始区写明还缺什么，不静默
            self.win.render()
            self._sync_start()
            return
        item, parent = got
        card, err = self.win.board.start_card(item, None if repo == NO_REPO else repo, main, helpers, parent)
        if err:
            self.start_err.setText(f"没有开始：\n{err}")
            return
        self.win.select(card.id)

    def show_card(self, card: Card):
        b = self.win.board
        if card.id != self.card_id:
            self.card_id = card.id
            self.answer.clear()
            self.ask_edit.clear()
            self.expanded.clear()
            self._changes_ver = None
        self.repo = self.main_cb = None  # 再遇到未开始的卡时重建选择器
        self.stages.setVisible(True)
        self.start_footer.setVisible(False)
        it = card.item
        src = "Azure" if it.source == "azure" else "手写"
        proj = b.project(card.project_id)
        self._set_repo_path(proj.path if proj else None)
        self._show_state(str(card.id))
        where = "未关联仓库" if card.project_id is None else f"{proj.name if proj else '?'} · ⎇ {card.branch}"
        self.meta.setText(f"{it.type} {it.label} · {src} · {where}")
        self.title.setText(it.title)
        self.stages.setText(stage_line(b, card))
        kind, _, now = status_of(b, card)
        self.banner.setText(now if kind != "run" else f"▶ {now}")
        self.banner.setProperty("kind", kind)
        self.banner.style().polish(self.banner)
        self._sync_run_bar(card, kind, now)
        # 工作页顶部有「AI 现在在干什么」，不重复；文字为空（需求待确认）或在跑条已显示时 banner 隐藏
        self.banner.setVisible(card.cur == 0 and bool(now) and not self.run_bar.isVisibleTo(self))

        self.req_footer.setVisible(card.cur == 0)
        can_ask = card.cur > 0 and card.column == "review" and card.comment == "none"
        self.ask_box.setVisible(can_ask)
        self.back_btn.setVisible(b.can_send_back(card))
        if can_ask:
            self.ask_edit.setPlaceholderText("还要改什么？写一句发给 AI（可选）" if b.can_pass(card)
                                             else "哪里不对？写一句发给 AI 修复，修完再通过")
        if card.cur == 0:
            self.pages.setCurrentIndex(0)
            self._show_req(card)
        else:
            self.pages.setCurrentIndex(1)
            self._show_work(card)

        # 底部操作（需求阶段的确认在页面里，不重复放）
        while self.footer.count():
            w = self.footer.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.buttons = {}
        self.footer.addStretch()
        add_buttons(self.footer, [a for a in actions_for(self.win, card) if a[0] not in ("spec", "why", "stop", "pause")],
                    self.buttons)
        self.footer_box.setVisible(bool(self.buttons))

    def _show_req(self, card: Card):
        b = self.win.board
        e = html.escape
        st = card.stages[0]
        p = req_view(st.rounds[-1].parsed) if st.rounds else None
        goal_head = head("🎯 完成目标", "#2c56d8")
        if st.status == "running":
            self.goal.setText(goal_head + f"<p style='color:#2c56d8'>AI 正在只读阅读任务和代码，读完会在这里给出"
                              f"「完成目标 / 方案」。<br>{e(current_step(st)[1])}</p>")
        elif p:
            self.goal.setText(goal_html(p))
        elif b.is_paused(card):
            self.goal.setText(goal_head + "<p style='color:#9aa1ad'>AI 被你暂停了，还没给出方案；"
                              "点上面「▶ 继续」让它接着做。</p>")
        elif b.is_aborted(card):
            self.goal.setText(goal_head + "<p style='color:#9aa1ad'>这一轮已中止作废；"
                              "点上面「↻ 重新开始本阶段」重新出方案。</p>")
        else:
            why = f"<br>{e(st.error)}" if st.error else ""
            self.goal.setText(goal_head + f"<p style='color:#9aa1ad'>这一轮没有产出方案（失败 / 已停止 / 额度耗尽），"
                              f"可以发一句话让 AI 重新出方案。{why}</p>")
        self.plan_box.setVisible(p is not None)
        qs = plan_questions(p["plan"]) if p else []
        self.pend_box.setVisible(bool(qs))
        self.pend.setText(questions_html(qs))
        self.req_helpers_box.setVisible(bool(st.helpers))
        if st.helpers:
            self.req_helpers.setText(head("🧐 额外角色的意见（只读，不拦确认）", "#7a3fb0")
                                     + helpers_html(b, st).replace("<hr>", "", 1))
        if p:
            self.plan.setText(head("🛠 方案", "#23784f") + plan_html(p["plan"]))
        editable = card.column == "review" and st.status == "review"
        self.answer.setEnabled(editable)
        self.reply_btn.setEnabled(editable)
        no_repo = card.project_id is None
        self.confirm_btn.setVisible(not no_repo)
        self.finish_btn.setVisible(no_repo)
        self.attach_btn.setVisible(no_repo)
        self.confirm_btn.setEnabled(b.can_confirm_req(card))
        self.finish_btn.setEnabled(b.can_confirm_req(card))
        self.attach_btn.setEnabled(b.can_confirm_req(card))
        self.finish_btn.setText("确认需求并结束（生成交付记录）" if card.item.source == "azure" else "确认需求并结束")

    def _show_work(self, card: Card):
        b = self.win.board
        self.now_box.setText(now_html(b, card))
        set_html(self.views["result"], result_html(b, card, self.expanded))
        set_html(self.views["talk"], talk_html(b, card, self.expanded))
        set_html(self.views["spec"], spec_html(b, card))
        set_html(self.views["more"], more_html(b, card))
        self._show_changes(card)

    def _toggle_ask(self, url):
        """结果页「看 AI 原话」、对话页「展开 / 收起」长文本：按锚点标识切换，两个页签一起重画"""
        k = url.toString().removeprefix("#")
        self.expanded.symmetric_difference_update({k})
        c = self._card()
        if c:
            set_html(self.views["result"], result_html(self.win.board, c, self.expanded))
            set_html(self.views["talk"], talk_html(self.win.board, c, self.expanded))

    # ---------- 代码改动 ----------
    def _changes_where(self, card: Card):
        """(git 跑在哪个目录, 基准分支, ref)；worktree 清理后在原仓库里看本卡分支"""
        proj = self.win.board.project(card.project_id)
        if proj is None:
            return None
        if card.worktree_cleaned:
            return proj.path, proj.base_branch, card.branch
        return card.wt_path, proj.base_branch, "HEAD"

    def _show_changes(self, card: Card, force=False):
        """只在「代码改动」页签可见时跑 git；阶段 / 轮次变了或点「刷新」才重取"""
        if self.pages.currentIndex() != 1 or self.tab_keys[self.tabs.currentIndex()] != "changes":
            return
        st = card.stage
        ver = (card.id, card.cur, st.status, len(st.rounds), card.worktree_cleaned)
        if ver == self._changes_ver and not force:
            return
        self._changes_ver = ver
        self.changes = None
        self.numstat = {}
        self.change_list.blockSignals(True)
        self.change_list.clear()
        self.change_list.blockSignals(False)
        where = self._changes_where(card)
        e = html.escape
        if where is None:
            self.changes_note.setText("不关联仓库，没有代码改动")
            set_html(self.views["changes"], "")
            return
        try:
            ch = worktree.card_changes(*where)
        except worktree.GitError as err:
            self.changes_note.setText(f"取改动失败：{err}")
            set_html(self.views["changes"], "")
            return
        self.changes = ch
        try:
            self.numstat = worktree.card_numstat(*where)
        except GitError:
            self.numstat = {}  # 行数取不到不影响看文件清单
        n_wip = sum(1 for f in ch["files"] if f[2])
        self.changes_note.setText(
            f"分支 {card.branch} 相对 {where[1]} 改了 {len(ch['files'])} 个文件"
            + (f"（其中 {n_wip} 个还没提交）" if n_wip else "") + f" · {len(ch['commits'])} 个 commit · 点文件看 diff")
        self.change_list.blockSignals(True)
        for code, path, wip in ch["files"]:
            it = QListWidgetItem(change_line(code, path, wip, self.numstat.get(path)))
            it.setForeground(QColor(CHANGE_COLOR.get(code, "#4b5563")))
            self.change_list.addItem(it)
        self.change_list.blockSignals(False)
        commits = "".join(f"<div><code>{e(c)}</code></div>" for c in ch["commits"]) or \
            "<div style='color:#9aa1ad'>（还没有 commit：每阶段你点「通过」时提交）</div>"
        set_html(self.views["changes"], f"<p><b>本卡的 commit</b></p>{commits}")

    def _show_file(self, row):
        card = self._card()
        if card is None or self.changes is None or not 0 <= row < len(self.changes["files"]):
            return
        code, path, wip = self.changes["files"][row]
        cwd, base, ref = self._changes_where(card)
        try:
            text = worktree.file_diff(cwd, base, path, ref, untracked=(code == "?"))
        except worktree.GitError as err:
            text = f"取 diff 失败：{err}"
        set_html(self.views["changes"], diff_html(text, untracked=(code == "?")))


CHANGE_CODE = {"A": "新增", "M": "修改", "D": "删除", "R": "改名", "C": "复制", "?": "新增"}
CHANGE_MARK = {"A": "+", "?": "+", "M": "~", "D": "−", "R": "~", "C": "+"}
CHANGE_COLOR = {"A": "#23784f", "?": "#23784f", "M": "#9a6200", "D": "#c4363a", "R": "#9a6200", "C": "#23784f"}
ASK_FOLD = 600  # 对话页里超过这么多字的回复默认折叠
ASK_HEAD = 300
GAP = "<div style='font-size:6px'>&nbsp;</div>"


def change_line(code: str, path: str, wip: bool, stat) -> str:
    """「改了哪些代码」每个文件一行：标记 文件名 新增/修改/删除 +N −M（未提交）；stat = (增, 删)，None = 没取到，(None, None) = 二进制"""
    n = ""
    if stat is not None:
        n = "  二进制" if stat[0] is None else f"  +{stat[0]} −{stat[1]}"
    return f"{CHANGE_MARK.get(code, '~')}  {path}  {CHANGE_CODE.get(code, code)}{n}" + ("（未提交）" if wip else "")


def diff_html(text: str, untracked: bool = False) -> str:
    """diff 着色：+ 行绿底、- 行红底、@@ 灰蓝底、diff/index/---/+++ 头部灰字；untracked = 文件内容整体当新增。
    Qt 富文本只认块级 background-color，所以每行一个 div；头部与正文靠「见没见过 @@」区分，免得把内容是「-- x」的删除行当头部"""
    e = html.escape
    base = "font-family:Menlo,monospace;font-size:11.5px;white-space:pre-wrap;"
    out, in_hunk = [], False
    for i, ln in enumerate(text.split("\n")):
        if untracked:
            style = "color:#6b7280" if i == 0 else "background-color:#e6f6ea"
        elif ln.startswith("diff --git"):
            in_hunk, style = False, "color:#6b7280"
        elif ln.startswith("@@"):
            in_hunk, style = True, "background-color:#e8eefe;color:#2c56d8"
        elif in_hunk and ln.startswith("+"):
            style = "background-color:#e6f6ea"
        elif in_hunk and ln.startswith("-"):
            style = "background-color:#fdeaea"
        elif not in_hunk:
            style = "color:#6b7280"
        else:
            style = ""
        out.append(f"<div style='{base}{style}'>{e(ln) or '&nbsp;'}</div>")
    return "".join(out)


def box(inner: str, border: str, fill: str, width: str = "100%", align: str = "") -> str:
    """卡片 / 气泡：Qt 富文本没有圆角和边框色，外层表格（cellpadding=1）当边框、内层表格当底色"""
    al = f" align='{align}'" if align else ""
    return (f"<table width='{width}'{al} cellspacing=0 cellpadding=1 bgcolor='{border}'><tr><td>"
            f"<table width='100%' cellspacing=0 cellpadding=9 bgcolor='{fill}'><tr><td>{inner}</td></tr></table>"
            f"</td></tr></table>{GAP}")


def goal_plan_html(p: dict, confirmed: bool = False) -> str:
    """三块：「🎯 完成目标」蓝框（goal + 完成标准）/ 疑问框（橙，已确认时灰；没有疑问不显示）/「🛠 方案」绿框"""
    qs = plan_questions(p["plan"])
    if confirmed:
        qbox = box(questions_html(qs, True), "#e3e6eb", "#fafbfc") if qs else ""
    else:
        qbox = box(questions_html(qs), "#f5dfa6", "#fff8e6") if qs else ""
    return (box(goal_html(p), "#dbe4fd", "#f5f8ff") + qbox
            + box(head("🛠 方案", "#23784f") + plan_html(p["plan"]), "#cdeedd", "#f0faf4"))


def now_html(board: Board, card: Card) -> str:
    """「AI 现在在干什么」：在跑时 谁 · 阶段 · 耗时 + 最近 5 步；不在跑时写明在等什么"""
    e = html.escape
    kind, stext, now = status_of(board, card)
    st = card.stage
    title = head("🤖 AI 现在在干什么", "#2c56d8")
    if st.status == "running" or st.running_helper():
        agent = running_agent(board, card)
        cli = agent.cli if agent else "claude"
        steps = recent_steps(st) or ["启动中…"]
        return (title + f"<div><b><span style='color:{CLI_COLOR[cli]}'>●</span> {CLI_NAME[cli]} · {e(stext)} · "
                f"已 {fmt_elapsed(st.elapsed)}</b></div>"
                + "".join(f"<div style='color:#4b5563'>· {e(x)}</div>" for x in steps[:-1])
                + f"<div><b>→ {e(steps[-1])}</b></div>")
    if kind == "ok":
        return title + f"<div style='color:#23784f'>AI 没在跑：已完成。{e(now)}</div>"
    if kind == "run":  # 程序自检 / 发评论中
        return title + f"<div>AI 没在跑：{e(now)}</div>"
    color = "#9a6200" if kind == "wait" else "#c4363a"
    return title + f"<div style='color:{color}'>AI 没在跑：{e(now)}</div>"


def verdict_of(reply: str) -> str | None:
    """验证回复里最后一个「结论：通过 / 不通过」；没有返回 None"""
    m = VERDICT_RE.findall(reply or "")
    return m[-1] if m else None


def result_html(board: Board, card: Card, expanded: set) -> str:
    """「结果」页签：当前阶段最新一轮的一张卡片：阶段·第几轮·AI·用时 / 结论 / 改了什么 / 自检 / 看 AI 原话（默认折叠）"""
    e = html.escape
    si = card.cur
    if not card.stages[si].rounds and si > 0:  # 刚进下一阶段还没跑：看上一阶段的
        si -= 1
    st = card.stages[si]
    if not st.rounds:
        return "<p style='color:#9aa1ad'>还没有结果</p>"
    n, r = len(st.rounds), st.rounds[-1]
    agent = board.agent(st.agent_id)
    sname = STAGE_NAME[st.stage]
    top = (f"<div style='color:#6b7280'>{sname} · 第 {n} 轮 · {e(agent.name) if agent else '?'}"
           f" · 用时 {fmt_elapsed(st.elapsed)}</div>")
    cur = si == card.cur
    if cur and st.status == "running":
        verdict = "<span style='color:#2c56d8'>… AI 还在跑，跑完这里显示结果（正在干什么见上方）</span>"
    elif cur and st.status == "checking":
        verdict = "<span style='color:#2c56d8'>… AI 做完了，程序正在自检</span>"
    elif cur and board.is_paused(card):
        verdict = "<span style='color:#6b7280'>⏸ 已暂停，点上面「▶ 继续」接着做</span>"
    elif cur and board.is_aborted(card):
        verdict = f"<span style='color:#6b7280'>■ {e(st.error)}</span>"
    elif r.end == "failed" or (cur and st.flag == "failed"):
        verdict = f"<span style='color:#c4363a'>✖ 这一轮失败了{'：' + e(st.error) if st.error else ''}</span>"
    elif r.end == "quota" or (cur and st.flag == "quota"):
        verdict = f"<span style='color:#7a3fb0'>⛔ 额度用完了{'：' + e(st.error) if st.error else ''}</span>"
    elif r.end == "stopped" or (cur and st.flag == "stopped"):
        verdict = "<span style='color:#6b7280'>■ 已停止，这一轮没做完</span>"
    elif not r.reply:
        verdict = "<span style='color:#9aa1ad'>这一轮没有回复</span>"
    elif st.stage == "req":
        verdict = "<span style='color:#23784f'>✓ 方案已出，等你确认</span>" if req_view(r.parsed) else \
            "<span style='color:#9a6200'>AI 没给出能识别的方案</span>"
    elif st.stage == "verify":
        v = verdict_of(r.reply)
        verdict = ("<span style='color:#23784f'>✓ AI 验证：通过</span>" if v == "通过" else
                   "<span style='color:#c4363a'>✖ AI 验证：不通过</span>" if v == "不通过" else
                   "<span style='color:#9a6200'>AI 没给明确结论</span>")
    elif cur and not board.checks_ok(st):  # AI 说做完了但程序自检没过：不能写「✓ 做完了」
        verdict = f"<span style='color:#c4363a'>⚠ AI 说{sname}做完了，但自检没过（见下面）</span>"
    else:
        verdict = f"<span style='color:#23784f'>✓ {sname}做完了</span>"
    body = [top, f"<div style='font-size:15px;margin:6px 0'><b>{verdict}</b></div>"]
    checks_done = parse_done_checks(r.reply or "") if st.stage == "verify" and r.reply else []
    for ok, text in checks_done:  # 验证阶段：逐条完成标准；解析不到就只显示结论
        body.append(f"<div style='margin:2px 0;color:{'#23784f' if ok else '#c4363a'}'>{'✓' if ok else '✗'} {e(text)}</div>")
    p = req_view(r.parsed) if st.stage == "req" else None
    if p:  # 需求阶段：完成目标 / 疑问 / 方案，不显示 JSON
        body.append(goal_plan_html(p))
    elif st.stage != "req":
        items = parse_changes(r.reply or "")
        if items:
            body.append(head("改了什么", "#4b5563") + "".join(
                f"<div style='margin:2px 0'>• {e(x.removeprefix('- ').strip())}</div>" for x in items))
    if st.checks:
        one = lambda k, c: (f"<span style='color:#23784f'>{e(k)} ✓</span>" if c == 0
                            else f"<span style='color:#c4363a'>{e(k)} ✗(exit {c})</span>")
        body.append("<div style='margin-top:8px'><b>自检：</b>" + "　".join(one(k, c) for k, (_, c) in st.checks.items())
                    + "</div>")
    if r.reply and not p:
        key = f"raw-{si}-{n}"
        if key in expanded:
            body.append(f"<div style='margin-top:8px'><a href='#{key}'>▾ 收起 AI 原话</a></div>"
                        f"<div style='color:#4b5563;margin-top:4px'>{e(r.reply).replace(chr(10), '<br>')}</div>")
        else:
            body.append(f"<div style='margin-top:8px'><a href='#{key}'>▸ 看 AI 原话</a></div>")
    return box("".join(body), "#e3e6eb", "#fafbfc") + helpers_html(board, st)


def spec_html(board: Board, card: Card) -> str:
    """「方案」页签：确认的「完成目标 / 疑问 / 方案」卡片（同需求页样式，只读）+ 退回时补充的要求；不显示需求原文"""
    e = html.escape
    main, *supp = card.spec.split(SUPPLEMENT_HEAD)
    p = next((req_view(r.parsed) for r in reversed(card.stages[0].rounds) if req_view(r.parsed)), None)
    out = []
    if p:
        out.append(goal_plan_html(p, confirmed=True))
    else:  # 没有解析结果（旧库 / 不关联仓库的卡）：直接显示 spec 原文
        out.append(box(f"<div>{e(main.strip()).replace(chr(10), '<br>')}</div>", "#cdeedd", "#f0faf4"))
    for note in supp:
        out.append(box(head("↩ 退回时补充的要求", "#9a6200") + f"<div>{e(note.strip()).replace(chr(10), '<br>')}</div>",
                       "#f5dfa6", "#fff8e6"))
    return "".join(out)


HELPER_TEXT = {"pending": "等主 agent 跑完", "running": "正在跑…", "stopped": "已停止", "failed": "失败"}


def helpers_html(board: Board, st) -> str:
    """本阶段额外角色的结果：主 agent 回复之后每个角色一段；失败写原因、不拦通过"""
    e = html.escape
    parts = []
    for h in st.helpers:
        a = board.agent(h.agent_id)
        name = e(a.name) if a else "（模板已删除）"
        if h.status == "done":
            body = e(h.reply).replace(chr(10), "<br>") or "<span style='color:#9aa1ad'>（没有回复）</span>"
        else:
            why = f"：{e(h.error)}" if h.error else ""
            color = "#c4363a" if h.status == "failed" else "#9aa1ad"
            body = f"<span style='color:{color}'>{HELPER_TEXT.get(h.status, h.status)}{why}</span>"
        parts.append(f"<hr><p style='color:#7a3fb0'><b>{name}</b>（额外角色 · 只读）</p><div>{body}</div>")
    return "".join(parts)


END_TEXT = {"failed": "（失败）", "quota": "（额度耗尽）", "stopped": "（已停止）"}


def talk_html(board: Board, card: Card, expanded: set) -> str:
    """「对话」页签：按时间排的气泡。程序发的提示词只一行摘要（可展开）；你发的显示原文靠右；
    AI 回复：需求阶段按「完成目标 / 疑问 / 方案」显示，其余超过 ASK_FOLD 字折叠"""
    e = html.escape
    br = lambda t: e(t).replace(chr(10), "<br>")
    parts = []
    for si, s in enumerate(card.stages):
        a = board.agent(s.agent_id)
        parts.append(f"<h3 style='margin-bottom:2px;color:#4b5563'>── {STAGE_NAME[s.stage]} · {e(a.name) if a else '?'} ──</h3>")
        if not s.rounds:
            parts.append("<p style='color:#9aa1ad'>未开始</p>")
        for i, r in enumerate(s.rounds, 1):
            k = f"{si}-{i}"
            if r.by == "流水线":
                if k in expanded:
                    parts.append(f"<div style='color:#6b7280'>第 {i} 轮 · 程序 → AI：阶段指令（{len(r.ask)} 字）"
                                 f" <a href='#{k}'>▾收起</a></div>"
                                 + box(f"<div style='color:#4b5563'>{br(r.ask)}</div>", "#e3e6eb", "#f5f6f8"))
                else:
                    parts.append(f"<div style='color:#6b7280;margin:4px 0 6px 0'>第 {i} 轮 · 程序 → AI：发了阶段指令"
                                 f"（{len(r.ask)} 字） <a href='#{k}'>▸展开</a></div>")
            else:
                parts.append(f"<div style='text-align:right;color:#9a6200'>{e(r.by)}</div>"
                             + box(f"<div>{br(r.ask)}</div>", "#f5dfa6", "#fff8e6", width="82%", align="right"))
            who = f"{e(a.name) if a else 'AI'}{END_TEXT.get(r.end or '', '')}"
            p = req_view(r.parsed) if s.stage == "req" else None
            if p:
                inner = goal_plan_html(p)
                parts.append(f"<div style='color:#2c56d8'><b>{who}</b></div>{inner}")
                continue
            if not r.reply:
                body = "<span style='color:#9aa1ad'>（还没有回复）</span>"
            elif len(r.reply) > ASK_FOLD and s.stage != "req" and f"{k}r" not in expanded:
                body = (f"{br(r.reply[:ASK_HEAD])}… <a href='#{k}r'>展开（共 {len(r.reply)} 字）</a>")
            elif len(r.reply) > ASK_FOLD and s.stage != "req":
                body = f"{br(r.reply)} <a href='#{k}r'>收起</a>"
            else:
                body = br(r.reply)
            parts.append(f"<div style='color:#2c56d8'><b>{who}</b></div>"
                         + box(f"<div>{body}</div>", "#dbe4fd", "#ffffff", width="92%"))
        for h in s.helpers:  # 额外角色：只保留主 agent 最新一轮之后的那次
            if h.status == "pending":
                continue
            ha = board.agent(h.agent_id)
            hr = (br(h.reply) if h.reply
                  else f"<span style='color:#9aa1ad'>（{HELPER_TEXT.get(h.status, h.status)}{'：' + e(h.error) if h.error else ''}）</span>")
            parts.append(f"<div style='color:#7a3fb0'>{e(ha.name) if ha else '?'}（额外角色 · 只读）</div>"
                         + box(f"<div>{hr}</div>", "#e6d6f5", "#f8f3fd", width="92%"))
    return "".join(parts)


def more_html(board: Board, card: Card) -> str:
    """「更多」：原始输出 + 自检（排查用）"""
    e = html.escape
    out = []
    for s in card.stages:
        if s.output:
            out.append(f"===== {STAGE_NAME[s.stage]} =====")
            out += s.output
    if len(out) > OUTPUT_SHOW:
        out = [f"…（只显示最近 {OUTPUT_SHOW} 行，完整原文见 {board.log_dir(card)}）"] + out[-OUTPUT_SHOW:]
    rows = []
    for s in card.stages:
        for k, (cmd, code) in s.checks.items():
            color = "#23784f" if code == 0 else "#c4363a"
            rows.append(f"<tr><td>{STAGE_NAME[s.stage]}</td><td>{k}</td><td><code>{e(cmd)}</code></td>"
                        f"<td style='color:{color}'><b>exit {code}</b></td></tr>")
    checks = ("<table cellpadding=4><tr><th>阶段</th><th>类型</th><th>命令</th><th>结果</th></tr>" + "".join(rows)
              + "</table>" if rows else "<p style='color:#9aa1ad'>暂无</p>")
    wt = f"<p style='color:#9aa1ad'>worktree：{e(card.wt_path)}{'（已清理）' if card.worktree_cleaned else ''}</p>"
    return (wt + "<h3>自检</h3><p>开发、验证阶段结束后由程序自己在 worktree 里跑项目的构建/测试命令，以退出码为准。</p>"
            + checks + "<h3>原始输出</h3><pre style='font-family:Menlo;font-size:11.5px'>"
            + e("\n".join(out) or "（暂无）") + "</pre>")


NOT_CONFIGURED = "还没配 Azure：点「设置」→ Azure"
EMPTY_TEXT = "这个迭代里没有指派给你的卡"


class MainWindow(QMainWindow):
    def __init__(self, board: Board, autorun=True, quota_fetch=clis.codex_quota, chooser=pick_folder,
                 vscode=open_vscode, prober="auto", runner=run_process):
        """autorun=False（测试）：不走秒表、不查 codex 额度。chooser：选文件夹；vscode：打开 VS Code（测试注入）。
        prober / runner：设置窗口里 Agent 页的 CLI 检测 / 测试连接（"auto" = autorun 时真检测，测试里不检测）"""
        super().__init__()
        self.board = board
        self.chooser = chooser
        self.prober = (clis.probe_all if autorun else None) if prober == "auto" else prober
        self.runner = runner
        self.vscode = vscode
        self._quota_fetch = quota_fetch
        self._quota_job = None
        # 迭代看板的拉取状态
        self.iters: list[azure.Iteration] = []
        self.rows: list[azure.Brief] = []
        self.parents: list[azure.Brief] = []  # 我的卡的父项（不是指派给我的），泳道标题用
        self.more = False
        # 状态列表按类型缓存；改状态的在途请求和结果提示按卡号记
        self.state_lists: dict[str, list[tuple[str, str]]] = {}
        self.state_errors: dict[str, str] = {}
        self.state_jobs: dict[str, clis.BgJob] = {}
        self.state_msgs: dict[str, tuple[str, bool]] = {}
        # 改状态成功时记 (当时最新一次拉取的序号, 新状态, 新 rev)：比它早发出的拉取回来不覆盖这张卡
        self.local_states: dict[str, tuple[int, str, int]] = {}
        self._load_seq = 0
        self._job = None  # 正在跑的迭代 / 列表请求；被新请求顶掉的结果丢弃
        self.fetch_text, self.fetch_kind = "", ""
        self.details: dict[str, WorkItem] = {}  # 未开始 Azure 卡的详情缓存（卡号 → WorkItem）
        self.detail_errors: dict[str, str] = {}
        self.detail_jobs: dict[str, clis.BgJob] = {}  # 正在取详情的卡号 → 请求
        self.selected = None
        self.active_dialog = None
        self._quitting = False
        self.badge_history: list[int] = []
        self.setWindowTitle("定标 DoneDef")
        self.setMinimumSize(720, 520)
        self.resize(1440, 900)
        self.setStyleSheet(QSS)

        self.main = self._build_main()
        self.setCentralWidget(self.main)
        self.drawer = Drawer(self)
        self.main.installEventFilter(self)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=lambda: self.select(None))
        QApplication.instance().installEventFilter(self)  # 点抽屉外收起

        self._init_tray()
        self.board.changed.connect(self.render)
        self.board.notify.connect(self._on_notify)
        self.timer = QTimer(self)  # 每秒刷新运行中阶段的耗时
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.board.tick)
        self.board.quota_changed.connect(self.quota_bar.refresh)
        self.quota_timer = QTimer(self, interval=QUOTA_REFRESH_MS)
        self.quota_timer.timeout.connect(self.refresh_codex_quota)
        # 自动拉取 Azure：每 5 分钟 + 窗口回到前台；60 秒内拉过、正在拉、没配 Azure 都不拉
        self.clock = time.monotonic
        self._last_pull: float | None = None
        self.pull_timer = QTimer(self, interval=AUTO_PULL_MS)
        self.pull_timer.timeout.connect(self.auto_pull)
        if autorun:
            self.timer.start()
            self.quota_timer.start()
            self.pull_timer.start()
            QGuiApplication.instance().applicationStateChanged.connect(self.on_app_state)
            self.board.ai_done.connect(lambda cli: cli == "codex" and self.refresh_codex_quota())
            self.refresh_codex_quota()
        self.load_iterations()

    def refresh_codex_quota(self):
        """后台经 codex app-server 查额度（不调模型）；上一次还没回来就不重复发"""
        if self._quota_job is not None:
            return
        env = child_env(os.environ)
        fetch = self._quota_fetch
        job = self._quota_job = clis.BgJob(lambda: fetch(env), self)
        job.done.connect(self._quota_done)
        job.start()

    def _quota_done(self, res):
        self._quota_job = None
        if isinstance(res, dict) and "error" in res:
            self.board.set_quota("codex", error=res["error"])
        else:
            self.board.set_quota("codex", res)

    def eventFilter(self, obj, ev):
        if obj is self.main and ev.type() == QEvent.Resize:
            self.drawer.place()
        elif ev.type() == QEvent.MouseButtonPress and isinstance(obj, QWidget) and self.drawer.is_open:
            self._click_outside(obj, ev.globalPosition().toPoint())
        return super().eventFilter(obj, ev)

    def _click_outside(self, w: QWidget, gpos):
        """本窗口里点到抽屉外、又不是另一张卡 / 一行 / PBI 标题 → 收起抽屉；弹窗、下拉列表不算。
        子控件不接的点击会冒泡到父控件、一路到主窗口，所以按点击位置找真正点到的控件，不看 obj"""
        if w.window() is not self:
            return
        x = self.childAt(self.mapFromGlobal(gpos)) or w
        while x is not None:
            if x is self.drawer or isinstance(x, (CardWidget, TodoWidget, TaskRow, PbiHead, PbiCard)):
                return
            x = x.parentWidget()
        self.select(None)

    def on_app_state(self, st):
        """窗口回到前台时自动拉取；用成员方法连接，窗口销毁时 Qt 自动断开"""
        if st == Qt.ApplicationActive:
            self.auto_pull()

    def auto_pull(self) -> bool:
        """自动拉取；真的发起了拉取返回 True"""
        if not self.board.azure_ready or self._job is not None:
            return False
        if self._last_pull is not None and self.clock() - self._last_pull < AUTO_PULL_GAP:
            return False
        self.refresh()
        return True

    # ---------- 布局 ----------
    def _build_main(self):
        main = QWidget()
        main.setObjectName("mainArea")
        v = QVBoxLayout(main)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(20, 12, 20, 12)
        hh.setSpacing(8)
        self.iter_cb = QComboBox()
        self.iter_cb.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.iter_cb.setToolTip("Azure 迭代：看板显示这个迭代里指派给你的卡")
        self.iter_cb.currentIndexChanged.connect(lambda _: self.load_items(clear=True))
        hh.addWidget(self.iter_cb)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        hh.addWidget(self.refresh_btn)
        hh.addSpacing(6)
        self.fetch_label = ElidedLabel("", "fetch")  # 单行省略，窄窗口下顶栏不被撑高
        self.fetch_label.setMinimumWidth(100)  # 再窄也留一点位置给状态文字，额度条先让步
        hh.addWidget(self.fetch_label, 1)
        self.add_btn = QPushButton("＋ 手写任务")
        self.add_btn.clicked.connect(self.open_add)
        hh.addWidget(self.add_btn)
        hh.addSpacing(8)
        self.quota_bar = QuotaBar(self.board)
        hh.addWidget(self.quota_bar)
        hh.addSpacing(8)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(lambda: self.open_settings())
        hh.addWidget(self.settings_btn)
        v.addWidget(header)

        # 列头在顶部一行；下面三列一起纵向滚动。列头 + 卡片区放进同一个可横向滚动的容器：
        # 窗口窄于三列最小宽之和时整块横向滚动，列头和卡片列始终对齐
        self.board_inner = QWidget()
        self.board_inner.setObjectName("boardInner")
        iv = QVBoxLayout(self.board_inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(0)
        self.board_scroll = QScrollArea()
        self.board_scroll.setWidgetResizable(True)
        self.board_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.board_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)  # 滚动条一直占位，列头和卡片对齐
        sb_width = self.board_scroll.verticalScrollBar().sizeHint().width()  # 列头右边距要让出同样宽的位置
        cols = QHBoxLayout()
        cols.setContentsMargins(20, 14, 20 + sb_width, 6)
        cols.setSpacing(14)
        self.columns = {k: Column(k, t) for k, t in BOARD_COLUMNS}
        for c in self.columns.values():
            cols.addWidget(c, 1)
        iv.addLayout(cols)
        iv.addWidget(self.board_scroll, 1)
        self.board_hscroll = QScrollArea()
        self.board_hscroll.setWidgetResizable(True)
        self.board_hscroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.board_hscroll.setWidget(self.board_inner)
        v.addWidget(self.board_hscroll, 1)
        self.widgets = {}
        return main

    # ---------- 拉迭代 / 卡列表 ----------
    def _set_fetch(self, text, kind=""):
        self.fetch_text, self.fetch_kind = text, kind
        self.fetch_label.setText(text)
        self.fetch_label.setToolTip(text)
        self.fetch_label.setProperty("kind", kind)
        self.fetch_label.style().polish(self.fetch_label)

    def _run(self, fn, on_done, busy_text):
        """后台请求：拉取中下拉、刷新禁用；被新请求顶掉的旧结果丢弃"""
        b = self.board
        org, proj, ado = b.azure_org, b.azure_project, b.ado
        job = self._job = clis.BgJob(azure.safe(lambda: fn(ado, org, proj)), self)
        job.done.connect(lambda res: self._job is job and on_done(res))
        self._last_pull = self.clock()
        self._busy(True)
        self._set_fetch(busy_text)
        job.start()

    def _busy(self, on):
        self.iter_cb.setEnabled(not on and bool(self.iters))
        self.refresh_btn.setEnabled(not on)

    def clear_details(self):
        """详情缓存清空，在途请求失效（回来的结果丢弃）"""
        self.details.clear()
        self.detail_errors.clear()
        self.detail_jobs.clear()

    def refresh(self):
        # 迭代都没拉到（出错 / 没有迭代 / 刚配好）就重拉迭代，否则重拉当前迭代的卡
        if self.iters:
            self.load_items(clear=False)
        else:
            self.load_iterations()

    def load_iterations(self):
        self.iters, self.rows, self.parents, self.more = [], [], [], False
        self.clear_details()
        # 组织 / 项目可能换了：状态列表重取，在途的改状态结果丢弃
        self.state_lists.clear()
        self.state_errors.clear()
        self.state_jobs.clear()
        self.state_msgs.clear()
        self.local_states.clear()
        self.iter_cb.blockSignals(True)
        self.iter_cb.clear()
        self.iter_cb.blockSignals(False)
        if not self.board.azure_ready:
            self._job = None  # 还在跑的旧请求失效
            self._busy(False)
            self._set_fetch(NOT_CONFIGURED, "warn")
            self.render()
            return
        self.render()
        self._run(lambda ado, org, proj: ado.iterations(org, proj), self._got_iterations, "正在拉取…")

    def _got_iterations(self, res):
        self._job = None
        if "error" in res:
            self._busy(False)
            return self._error(res["error"])
        self.iters = res["ok"]
        if not self.iters:
            self._busy(False)
            return self._error("这个项目还没有迭代")
        self.iter_cb.blockSignals(True)
        self.iter_cb.clear()
        for it in self.iters:
            self.iter_cb.addItem(it.name + ("（当前）" if it.current else ""), it.path)
        self.iter_cb.setCurrentIndex(next((i for i, it in enumerate(self.iters) if it.current), 0))
        self.iter_cb.blockSignals(False)
        self.load_items(clear=True)

    def load_items(self, clear: bool):
        """clear：换了迭代就先清空看板上的 Azure 卡；刷新同一个迭代时保留旧的，等新结果回来再换"""
        path = self.iter_cb.currentData()
        if not path or not self.board.azure_ready:
            self._busy(False)
            return
        self.clear_details()
        if clear:
            self.rows, self.parents, self.more = [], [], False
            self.render()
        known = tuple(self.state_lists)  # 取过的类型不再取
        self._load_seq += 1
        seq = self._load_seq
        self._run(lambda ado, org, proj: ado.board(org, proj, path, known), lambda res: self._got_items(res, seq),
                  "正在拉取…")

    def _got_items(self, res, seq: int = 0):
        self._job = None
        self._busy(False)
        if "error" in res:
            return self._error(res["error"])
        got: azure.BoardData = res["ok"]
        self.rows, self.parents, self.more = got.rows, got.parents, got.more
        self.state_lists.update(got.states)
        self.state_errors = {t: m for t, m in {**self.state_errors, **got.state_errors}.items()
                             if t not in got.states}
        # 这次拉取发出后才改成功的卡：拉回来的可能是改之前的数据，保留本地的状态 / rev 和提示；其余提示不再适用
        keep = {k: v for k, v in self.local_states.items() if v[0] >= seq}
        for r in self.rows + self.parents:
            if str(r.id) in keep:
                r.state, r.rev = keep[str(r.id)][1:]
        self.local_states = keep
        self.state_msgs = {k: m for k, m in self.state_msgs.items() if k in keep or k in self.state_jobs}
        if not self.rows:
            self._set_fetch(EMPTY_TEXT)
        else:
            text = f"{self.board.azure_org}/{self.board.azure_project} · 指派给我的 {len(self.rows)} 张"
            if self.more:
                text += f"（超过 {azure.LIST_MAX} 张，只显示前 {azure.LIST_MAX} 张）"
            if got.parent_error:
                text += f" · 父项详情取不到：{got.parent_error}（可以点「刷新」重试）"
            self._set_fetch(text, "bad" if got.parent_error else "")
        self.render()

    def _error(self, text):
        self.rows, self.parents, self.more = [], [], False  # 出错时只显示手写卡
        self._set_fetch(f"{text}（可以点「刷新」重试）", "bad")
        self.render()

    # ---------- 渲染 ----------
    def brief(self, key) -> azure.Brief | None:
        """拉到的 Azure 行：我的卡或它们的父项"""
        key = str(key)
        return next((r for r in self.rows + self.parents if str(r.id) == key), None)

    def column_of(self, r: azure.Brief) -> str:
        """按状态分类归列，取不到分类按名"""
        cat = next((c for n, c in self.state_lists.get(r.type, []) if n == r.state), "")
        return state_column(r.state, cat)

    def board_groups(self) -> list[Group]:
        """看板上的卡：有父项的我的 Task 进父项的 PBI 卡（按父项状态归列，父项取不到按第一行归列）；
        我的卡本身是我另一张卡的父项时只当 PBI 卡；没有父项的进独立卡；手写任务是独立卡、按本地进度归列。
        每列 PBI 卡按号升序在前，独立卡在后；PBI 卡里的行按号升序"""
        b = self.board
        parent_ids = {r.parent for r in self.rows if r.parent}
        pbis: dict[int, Group] = {}
        loose: list[Group] = []
        for r in self.rows:
            if r.id in parent_ids:
                pbis.setdefault(r.id, Group("", str(r.id), True, r))
                continue
            key = str(r.id)
            card = b.find_card(key)
            row = (key, card, None if card else r)
            if r.parent:
                pbis.setdefault(r.parent, Group("", str(r.parent), True, self.brief(r.parent))).rows.append(row)
            else:
                loose.append(Group(self.column_of(r), key, False, rows=[row]))
        for w in b.todo_items:
            loose.append(Group("todo", str(w.id), False, rows=[(str(w.id), None, w)]))
        for c in b.cards:
            if c.item.source == "manual":
                loose.append(Group("done" if c.column == "done" else "doing", str(c.id), False, rows=[(str(c.id), c, None)]))
        out = []
        for pid in sorted(pbis):
            g = pbis[pid]
            g.rows.sort(key=lambda x: int(x[0]))
            first = self.brief(g.rows[0][0]) if g.rows else None
            g.col = self.column_of(g.head) if g.head else self.column_of(first) if first else "todo"
            out.append(g)
        return out + loose

    def board_entries(self):
        """看板上每一个可开始 / 已开始的条目（不含 PBI 卡标题）：(列, 键, 已开始的卡 或 None, 未开始的条目 或 None)"""
        return [(g.col, key, card, item) for g in self.board_groups() for key, card, item in g.rows]

    def pending(self, key):
        """未开始的条目（拉到的 Azure 行 / 父项 / 未开始的手写任务），没有返回 None"""
        if self.board.find_card(key):
            return None
        return self.brief(key) or self.board.find_todo(key)

    def render(self):
        b = self.board
        pos = self.board_scroll.verticalScrollBar().value()
        body = QWidget()
        body.setObjectName("colBody")
        row = QHBoxLayout(body)
        row.setContentsMargins(20, 6, 20, 16)
        row.setSpacing(14)
        self.widgets: dict[str, QFrame] = {}  # 键 → 卡片 / Task 行 / PBI 卡
        groups = self.board_groups()
        for k, col in self.columns.items():
            cell = QVBoxLayout()
            cell.setSpacing(10)
            col.cards = []
            for g in groups:
                if g.col != k:
                    continue
                if g.pbi:
                    w = PbiCard(self, g, self.selected)
                    self.widgets[g.key] = w
                    for r in w.rows:
                        self.widgets[r.key] = r
                else:
                    key, card, item = g.rows[0]
                    w = CardWidget(self, card, key == self.selected) if card else TodoWidget(key, item, key == self.selected)
                    w.clicked.connect(self.select)
                    self.widgets[key] = w
                cell.addWidget(w)
                col.cards.append(w)
            col.count.setText(str(len(col.cards)))
            cell.addStretch()
            holder = QWidget()
            holder.setMinimumWidth(COL_MIN)
            holder.setLayout(cell)
            cell.setContentsMargins(0, 0, 0, 0)
            row.addWidget(holder, 1)
        # 旧内容延迟销毁：按钮点击回调里会触发重绘，不能当场删掉正在处理事件的控件
        old = self.board_scroll.takeWidget()
        if old:
            old.deleteLater()
        self.board_scroll.setWidget(body)
        self.board_scroll.verticalScrollBar().setValue(pos)

        if self.selected is not None:
            card = b.find_card(self.selected)
            item = None if card else self.pending(self.selected)
            if card:
                self.drawer.show_card(card)
            elif item is not None:
                self.drawer.show_item(self.selected, item)
            else:  # 选中的卡不在看板上了（换了迭代 / 拉取出错）
                self.selected = None
                self.drawer.slide_out()
        self._update_badge()

    def card_widget(self, cid) -> CardWidget | TodoWidget | TaskRow | PbiCard | None:
        """键对应的控件：独立卡、PBI 卡里的 Task 行，或 PBI 卡（键 = 父项号）"""
        return self.widgets.get(str(cid))

    # ---------- 改 Azure 状态 ----------
    def change_state(self, key: str, state: str):
        """你在抽屉里选了新状态：后台 PATCH（带 rev test）；同一张卡上一次还没回来就不发"""
        br = self.brief(key)
        if br is None or not state or state == br.state or key in self.state_jobs:
            return
        b = self.board
        org, proj, ado, rev, num = b.azure_org, b.azure_project, b.ado, br.rev, br.id
        self.state_msgs.pop(key, None)
        job = self.state_jobs[key] = clis.BgJob(azure.safe(lambda: ado.set_state(org, proj, num, state, rev)), self)
        job.target = state
        job.done.connect(lambda res: self._state_done(job, key, res))
        job.start()
        self.render()

    def _state_done(self, job, key, res):
        job.deleteLater()
        if self.state_jobs.get(key) is not job:  # 改了 Azure 设置：结果丢弃
            return
        del self.state_jobs[key]
        if "error" in res:
            self.state_msgs[key] = (f"没改成：{res['error']}", True)
        else:
            new = res["ok"]
            for r in self.rows + self.parents:
                if r.id == new.id:
                    r.state, r.rev = new.state or job.target, new.rev or r.rev
                    self.local_states[key] = (self._load_seq, r.state, r.rev)
            self.state_msgs[key] = (f"已改成「{job.target}」", False)
        self.render()

    # ---------- 操作 ----------
    def select(self, cid):
        self.selected = None if cid is None else str(cid)
        if self.selected:  # 你再点一次 = 重试取详情（上次失败的话），连同它父项的详情
            self.detail_errors.pop(self.selected, None)
            br = self.brief(self.selected)
            if br and br.parent:
                self.detail_errors.pop(str(br.parent), None)
        self.render()
        if self.selected is None:
            self.drawer.slide_out()
        else:
            self.drawer.slide_in()

    def _open(self, dlg):
        self.active_dialog = dlg
        dlg.open()
        return dlg

    def open_add(self):
        return self._open(TaskDialog(self.board, self))

    def open_settings(self, page=0):
        """设置窗口：0 Azure / 1 仓库 / 2 Agent 模板"""
        dlg = SettingsDialog(self.board, self, chooser=self.chooser, runner=self.runner, prober=self.prober, page=page)
        dlg.azure_saved.connect(self.load_iterations)  # 保存后重拉迭代
        return self._open(dlg)

    def open_azure_settings(self):
        return self.open_settings(0)

    def open_repos(self):
        return self.open_settings(1)

    def open_agents(self):
        return self.open_settings(2)

    def open_abort(self, card):
        """点「■ 中止」：先弹确认（开发阶段列出将丢弃的未提交文件），确认才中止"""
        return self._open(abort_box(card, self.board.abort_preview(card), lambda: self.board.abort(card), self))

    def open_writeback(self, card):
        return self._open(WritebackDialog(self.board, card, self))

    def show_error(self, title, text):
        return self._open(error_box(title, text, self))

    def request_pass(self, card):
        """点「通过」：worktree 有改动先弹提交确认，没有改动直接通过"""
        b = self.board
        try:
            files, msg = b.pending_commit(card)
        except GitError as e:
            return self.show_error("取改动失败", str(e))
        if not files:
            if not b.pass_stage(card):
                return self.show_error("没有通过", "当前阶段不能通过，或 worktree 状态刚刚变了，请再看一眼")
            return None
        return self._open(commit_box(card, files, msg, lambda: self._commit_and_pass(card), self))

    def _commit_and_pass(self, card):
        err = self.board.commit_and_pass(card)
        if err:
            # 弹窗关闭后再弹，避免两个模态框叠在一起
            QTimer.singleShot(0, lambda: self.show_error("提交失败，没有通过", err))

    def open_cleanup(self, card):
        return self._open(cleanup_box(card, lambda: self._cleanup(card), self))

    def _cleanup(self, card):
        err = self.board.cleanup(card)
        if err:
            QTimer.singleShot(0, lambda: self.show_error("清理失败", err))

    # ---------- 通知 / 角标 ----------
    def _init_tray(self):
        icon = app_icon()
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("定标 DoneDef")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _on_notify(self, title, msg):
        if self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, msg, QSystemTrayIcon.Information, 4000)

    def _update_badge(self):
        n = self.board.review_count()
        if not self.badge_history or self.badge_history[-1] != n:
            self.badge_history.append(n)
            QGuiApplication.instance().setBadgeNumber(n)

    # ---------- 关窗确认 ----------
    def closeEvent(self, e):
        if self._quitting or not self.board.running_cards():
            self.board.save()
            self.tray.hide()
            e.accept()
            return
        e.ignore()
        self._open(quit_box(self.board, self._stop_and_quit, self))

    def _stop_and_quit(self):
        self.board.stop_all()
        self._quitting = True
        QTimer.singleShot(0, self.close)

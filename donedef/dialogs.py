"""弹窗（从 prototype_qt/dialogs.py 改造）：手写任务、补选仓库、agent 模板、Azure 设置、仓库设置、交付记录写回、
提交确认、清理 worktree、关窗确认。全部用 open()（非阻塞），方便测试直接操作。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import azure, clis
from .cli import TEST_PROMPT, TEST_TIMEOUT, build_test_cmd, child_env, parse_reply, run_process, tail
from .model import CAP_CLIS, CAPS, CLI_NAME, MODEL_CHOICES, PERM_NAME, PROMPT_VARS, STAGE_NAME, Agent, effort_choices
from .repo import parse_azure_project

ADO_MISSING = "请填 Azure 项目地址"
COMMIT_LIST_MAX = 30
ABORT_LIST_MAX = 15


def note(text):
    lab = QLabel(text)
    lab.setObjectName("note")
    lab.setWordWrap(True)
    return lab


BROWSE = "__browse__"  # 仓库下拉里的「选择本地文件夹…」
NO_REPO = ""  # 仓库下拉里的「不关联仓库」（卡片 project_id = None）


def pick_folder(parent):
    return QFileDialog.getExistingDirectory(parent, "选择本地 git 仓库文件夹")


class RepoPicker(QComboBox):
    """关联代码仓库下拉：不设默认，必须显式选。
    值：None = 还没选；仓库 ID；NO_REPO = 不关联仓库（allow_none 才有）。
    选「选择本地文件夹…」→ chooser(parent) 取目录 → board.remember_repo；失败原因交给 on_error"""

    def __init__(self, board, allow_none=True, on_change=None, on_error=None, chooser=pick_folder):
        super().__init__()
        self.board, self.allow_none = board, allow_none
        self.on_change, self.on_error, self.chooser = on_change, on_error, chooser
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(24)
        self._fill(None)
        self.activated.connect(self._activated)

    def _fill(self, keep):
        self.blockSignals(True)
        self.clear()
        self.addItem("— 请选择 —", None)
        for p in self.board.projects:
            self.addItem(f"{p.name}  ·  {p.path}", p.id)
        self.addItem("选择本地文件夹…", BROWSE)
        if self.allow_none:
            self.addItem("不关联仓库（调研、建模）", NO_REPO)
        i = self.findData(keep) if keep is not None else 0
        self.setCurrentIndex(max(i, 0))
        self._last = self.currentData()
        self.blockSignals(False)

    def value(self):
        v = self.currentData()
        return None if v == BROWSE else v

    def reload(self):
        """仓库列表变了（仓库设置里加了）才重填，保留当前选择"""
        want = [None] + [p.id for p in self.board.projects] + [BROWSE] + ([NO_REPO] if self.allow_none else [])
        if [self.itemData(i) for i in range(self.count())] != want:
            self._fill(self.value())

    def _activated(self, _i):
        if self.currentData() == BROWSE:
            folder = self.chooser(self)
            got, err = self.board.remember_repo(folder) if folder else (None, "")
            if err and self.on_error:
                self.on_error(err)
            self._fill(got.id if got else self._last)
        else:
            self._last = self.currentData()
            if self.on_error:
                self.on_error("")
        if self.on_change:
            self.on_change()


class TaskDialog(QDialog):
    """「＋ 手写任务」：只填内容，保存后出现在 To Do；开始走卡片抽屉"""

    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.saved_item = None
        self.setWindowTitle("新建手写任务")
        self.resize(560, 360)
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.title = QLineEdit()
        self.title.setPlaceholderText("一句话说清要做什么")
        self.title.textChanged.connect(lambda t: self.ok_btn.setEnabled(bool(t.strip())))
        self.desc = QPlainTextEdit()
        self.desc.setFixedHeight(90)
        self.ac = QPlainTextEdit()
        self.ac.setFixedHeight(70)
        self.ac.setPlaceholderText("可留空：需求阶段 AI 会问做到什么程度算完成")
        form.addRow("标题", self.title)
        form.addRow("描述", self.desc)
        form.addRow("验收标准", self.ac)
        root.addLayout(form)
        root.addWidget(note("保存后放在看板 To Do 栏；点开它选仓库和 agent 再开始。手写任务跑完不回写 Azure。"))
        root.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.ok_btn = QPushButton("保存到 To Do")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setEnabled(False)
        self.ok_btn.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(self.ok_btn)
        root.addLayout(row)

    def _save(self):
        t = self.title.text().strip()
        if not t:
            return
        self.saved_item = self.board.add_manual_task(t, self.desc.toPlainText(), self.ac.toPlainText())
        self.accept()


class AttachRepoDialog(QDialog):
    """不关联仓库的卡确认需求后补选仓库进入开发；git 出错原话留在窗口里"""

    def __init__(self, board, card, spec_text: str, parent=None, chooser=pick_folder):
        super().__init__(parent)
        self.board, self.card, self.spec_text = board, card, spec_text
        self.setWindowTitle(f"选仓库进入开发 · {card.item.label}")
        self.resize(620, 260)
        root = QVBoxLayout(self)
        root.addWidget(note("确认需求说明后，在所选仓库建 worktree 并开始开发；之后的流程和关联了仓库的卡一样。"))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.err = note("")
        self.err.setTextFormat(Qt.PlainText)
        self.err.setStyleSheet("color:#c4363a")
        self.repo = RepoPicker(board, allow_none=False, on_change=self._sync, on_error=self.err.setText, chooser=chooser)
        form.addRow("代码仓库", self.repo)
        root.addLayout(form)
        self.wt = note("")
        root.addWidget(self.wt)
        root.addWidget(self.err)
        root.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.ok_btn = QPushButton("确认需求，进入开发")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.clicked.connect(self._go)
        row.addWidget(cancel)
        row.addWidget(self.ok_btn)
        root.addLayout(row)
        self._sync()

    def _sync(self):
        p = self.board.project(self.repo.value())
        self.ok_btn.setEnabled(p is not None)
        self.wt.setText(f"将创建 worktree：data/worktrees/{p.name}/{self.card.id} · 分支 {self.card.branch}"
                        f"（从 {p.base_branch} 切出）" if p else "")

    def _go(self):
        pid = self.repo.value()
        if not pid:
            return
        err = self.board.attach_repo(self.card, pid, self.spec_text)
        if err:
            self.err.setText(f"没有进入开发：\n{err}")
            return
        self.accept()


class WritebackDialog(QDialog):
    """交付记录：可编辑，点「确认追加到 Azure」才追加到 Task 描述末尾；
    写回中按钮禁用；写不了（组织 / 项目对不上、没开 AI）一打开就说明；失败原因留在窗口里，可以再发"""

    def __init__(self, board, card, parent=None):
        super().__init__(parent)
        self.board, self.card = board, card
        self.setWindowTitle(f"交付记录 · {card.item.label}")
        self.resize(680, 520)
        root = QVBoxLayout(self)
        where = f"{board.azure_org}/{board.azure_project} {card.item.label}" if board.azure_ready else card.item.label
        root.addWidget(note(f"将追加到 Azure Task {where} 的描述末尾（不覆盖原描述，不改状态）。可以先修改："))
        self.text = QPlainTextEdit(card.comment_text)
        root.addWidget(self.text, 1)
        self.msg = note("")
        self.msg.setTextFormat(Qt.PlainText)
        root.addWidget(self.msg)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self._close)
        self.send_btn = QPushButton("确认追加到 Azure")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(close)
        row.addWidget(self.send_btn)
        root.addLayout(row)
        board.changed.connect(self._sync)
        self._sync()

    def _close(self):
        # 没发也把改过的草稿留住，下次打开接着改
        if self.card.comment == "draft" and self.card.id not in self.board.sending:
            self.card.comment_text = self.text.toPlainText()
            self.board.changed.emit()
        self.reject()

    def _send(self):
        self.board.send_writeback(self.card, self.text.toPlainText())

    def _sync(self):
        c, b = self.card, self.board
        if not shiboken6.isValid(self):
            return
        sending = c.id in b.sending
        block = b.writeback_block(c) if c.comment == "draft" else ""
        self.send_btn.setEnabled(b.can_writeback(c) and not sending and not block)
        self.text.setReadOnly(c.comment != "draft" or sending)
        if c.comment == "sent":
            self.msg.setStyleSheet("color:#23784f")
            self.msg.setText("✓ 已写回 Task")
            self.send_btn.setText("已写回")
        elif sending:
            self.msg.setStyleSheet("color:#6b7280")
            self.msg.setText("正在追加到 Task 描述…")
        elif c.id in b.writeback_errors:
            self.msg.setStyleSheet("color:#c4363a")
            self.msg.setText(f"写回失败：{b.writeback_errors[c.id]}（草稿已保留，可以再发）")
        elif block:
            self.msg.setStyleSheet("color:#9a6200")
            self.msg.setText(f"现在写不了：{block}")
        else:
            self.msg.setText("")

    def done(self, r):
        try:
            self.board.changed.disconnect(self._sync)
        except (RuntimeError, TypeError):
            pass  # 已断开：PySide 对重复断开抛异常，这里只是收尾
        super().done(r)


class AgentDialog(QDialog):
    """agent 模板管理：名称 / CLI / 模型 / 推理强度 / 权限档 / 能力/ 角色说明。
    runner 是执行器工厂 (cmd, env, cwd, timeout) -> job，测试里注入假的。
    prober(env) -> {"clis", "models", "at"}：打开时后台检测本机 CLI 并查模型；None = 不检测（仅测试时传）"""

    def __init__(self, board, parent=None, runner=run_process, prober=clis.probe_all):
        super().__init__(parent)
        self.board = board
        self._runner = runner
        self._probe_job = None
        self._probing = False
        self._job = None  # 正在跑的测试连接；None = 没有或已失效
        self._tmp = ""
        self._out_file = ""
        self._t0 = 0.0
        self.setWindowTitle("Agent 模板")
        # macOS 原生控件比 Fusion 高，几段自动换行的说明按单行算最小高度，高度 500 时会把表单行压扁（按钮文字被裁）
        self.resize(820, 620)
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setSpacing(3)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton("+ 新建")
        add.clicked.connect(self._add)
        self.del_btn = QPushButton("删除")
        self.del_btn.clicked.connect(self._delete)
        row.addWidget(add)
        row.addWidget(self.del_btn)
        left.addLayout(row)
        root.addLayout(left, 2)

        right = QVBoxLayout()
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)  # macOS 默认字段不拉宽，说明文字会折成好几行
        self.name = QLineEdit()
        self.cli = QComboBox()
        for k, v in CLI_NAME.items():
            self.cli.addItem(v, k)
        # 版本号等文字变长时不把右边的「测试连接」挤掉
        self.cli.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cli.setMinimumContentsLength(12)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.lineEdit().setPlaceholderText("默认模型")
        self.effort = QComboBox()
        self.perm = QComboBox()
        for k, v in PERM_NAME.items():
            self.perm.addItem(v, k)
        self.prompt = QPlainTextEdit()
        form.addRow("名称", self.name)
        cli_row = QHBoxLayout()
        cli_row.addWidget(self.cli, 1)
        self.conn_btn = QPushButton("测试连接")
        self.conn_btn.setToolTip("用表单里当前的 CLI / 模型 / 推理强度，只读无头发一句「回复 OK」")
        self.conn_btn.clicked.connect(self._test_conn)
        # 按钮宽度按文字算死，不被下拉框挤压
        self.conn_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.conn_btn.setMinimumWidth(self.conn_btn.fontMetrics().horizontalAdvance("测试连接") + 36)
        cli_row.addWidget(self.conn_btn)
        form.addRow("CLI", cli_row)
        self.conn = note("")
        self.conn.setTextFormat(Qt.PlainText)  # stderr 原文不按富文本解释
        form.addRow("", self.conn)
        form.addRow("模型", self.model)
        self.models_note = note("")
        form.addRow("", self.models_note)
        form.addRow("推理强度", self.effort)
        self._fill_choices("claude")
        self.cli.currentIndexChanged.connect(self._cli_changed)
        self.model.currentTextChanged.connect(self._model_changed)
        form.addRow("权限档", self.perm)
        caps_row = QHBoxLayout()
        self.caps = {}
        for k, v in CAPS.items():
            self.caps[k] = QCheckBox(v)
            caps_row.addWidget(self.caps[k])
        caps_row.addStretch()
        form.addRow("能力", caps_row)
        self.caps_note = note("")
        form.addRow("", self.caps_note)
        self.cli.currentIndexChanged.connect(self._sync_caps)
        right.addLayout(form)
        right.addWidget(QLabel("角色说明"))
        right.addWidget(self.prompt, 1)
        right.addWidget(note("新卡：这里写角色和要求，需求、阶段指令由程序自动加上。"
                             "早期开始的老卡仍按提示词模板替换变量：" + "  ".join(PROMPT_VARS)))
        self.msg = note("")
        right.addWidget(self.msg)
        bb = QDialogButtonBox()
        self.save_btn = bb.addButton("保存", QDialogButtonBox.AcceptRole)
        self.save_btn.setObjectName("primary")
        bb.addButton("关闭", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        right.addWidget(bb)
        root.addLayout(right, 3)

        self.list.currentRowChanged.connect(self._load)
        self._apply_cli_cache()
        self._refresh(0)
        if prober is not None:
            self._start_probe(prober)

    def _refresh(self, row):
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.board.agents:
            self.list.addItem(f"{a.name}\n{CLI_NAME[a.cli]} · {PERM_NAME[a.perm]} · {a.model or '默认模型'}")
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(row, len(self.board.agents) - 1))
        self._load(self.list.currentRow())

    def current(self) -> Agent | None:
        r = self.list.currentRow()
        return self.board.agents[r] if 0 <= r < len(self.board.agents) else None

    def _load(self, _row):
        a = self.current()
        if not a:
            return
        self.name.setText(a.name)
        self.cli.setCurrentIndex(self.cli.findData(a.cli))
        self.perm.setCurrentIndex(self.perm.findData(a.perm))
        self._fill_choices(a.cli)  # 同一 CLI 切换行时 currentIndexChanged 不触发，这里显式刷新候选
        self.model.setEditText(a.model)
        self._fill_efforts(a.cli, a.model, a.effort)
        self._show_cli_status()
        self.prompt.setPlainText(a.prompt)
        for k, box in self.caps.items():
            box.setChecked(k in a.caps)
        self._sync_caps()
        used = self._used(a)
        self.del_btn.setEnabled(not used)
        self.msg.setText("该模板正被卡片使用，不能删除" if used else "")

    # ---------- 本机 CLI / 模型 ----------
    def _models(self, cli) -> dict:
        """实时查到的模型信息；没查到（没装 / 失败 / 还没查过）返回 {}"""
        m = (self.board.cli_cache.get("models") or {}).get(cli) or {}
        return m if m.get("models") else {}

    def _start_probe(self, prober):
        self._probing = True
        env = child_env(os.environ)
        job = self._probe_job = clis.BgJob(lambda: prober(env), self)
        job.done.connect(self._probe_done)
        self._apply_cli_cache()
        job.start()

    def _probe_done(self, data):
        self._probing = False
        self._probe_job = None
        if "error" in data:
            self.models_note.setText(f"检测本机 CLI 失败：{data['error']}")
            return
        self.board.set_cli_cache(data)
        self._apply_cli_cache()
        a = self.current()
        if a:
            # 只刷新候选，不覆盖表单里正在改的值
            model, effort = self._model_text(), self.effort.currentData() or ""
            self._fill_choices(self.cli.currentData())
            self.model.setEditText(model)
            self._fill_efforts(self.cli.currentData(), model, effort)
        self._show_cli_status()

    def _apply_cli_cache(self):
        """CLI 下拉框每项显示「名称 · 版本」；没装的标「未安装」且不可选"""
        found = self.board.cli_cache.get("clis") or {}
        for i in range(self.cli.count()):
            cli = self.cli.itemData(i)
            info = found.get(cli)
            item = self.cli.model().item(i)
            if info is None:
                text, ok = CLI_NAME[cli] + (" · 检测中…" if self._probing else ""), True
            elif not info.get("path"):
                text, ok = f"{CLI_NAME[cli]} · 未安装", False
            else:
                text, ok = f"{CLI_NAME[cli]} · {info.get('version') or '版本未知'}", True
            self.cli.setItemText(i, text)
            item.setEnabled(ok)

    def _show_cli_status(self):
        cli = self.cli.currentData()
        info = (self.board.cli_cache.get("clis") or {}).get(cli)
        m = (self.board.cli_cache.get("models") or {}).get(cli) or {}
        at = self.board.cli_cache.get("at")
        when = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else ""
        if info is not None and not info.get("path"):
            text, color = f"本机没装 {CLI_NAME[cli]}（PATH 里找不到 {cli}），这个模板跑不起来", "#c4363a"
        elif m.get("error"):
            text, color = f"模型列表查询失败：{m['error']}（可手填模型名）", "#c4363a"
        elif m.get("models"):
            text, color = f"模型列表来自 {m.get('note', '')} · {when}", "#6b7280"
        elif self._probing:
            text, color = "正在检测本机 CLI 和模型…", "#6b7280"
        else:
            text, color = "还没查过模型列表，下拉里是内置候选（可手填）", "#6b7280"
        if self.board.cache_error:
            text += f"\n缓存文件有问题：{self.board.cache_error}"
        self.models_note.setStyleSheet(f"color:{color}")
        self.models_note.setText(text)

    def _fill_choices(self, cli):
        """模型候选随 CLI 变：有实时结果用实时的，否则用内置候选"""
        m = self._models(cli)
        self.model.blockSignals(True)
        self.model.clear()
        for name in (m.get("models") or MODEL_CHOICES[cli]):
            self.model.addItem(name + ("（默认）" if name == m.get("default") else ""), name)
        self.model.setEditText("")
        self.model.blockSignals(False)
        self._fill_efforts(cli, "", "")

    def _fill_efforts(self, cli, model, keep):
        """推理强度选项：codex / claude 按实时查到的该模型支持值；查不到用内置；keep 不在列表里也保留，不悄悄丢掉"""
        m = self._models(cli)
        eff = m.get("efforts") or {}
        names = eff.get(model) or eff.get(m.get("default", "")) or m.get("any_efforts") or list(effort_choices(cli))
        self.effort.blockSignals(True)
        self.effort.clear()
        self.effort.addItem("默认", "")
        for e in names:
            self.effort.addItem(e, e)
        if keep and self.effort.findData(keep) < 0:
            self.effort.addItem(f"{keep}（该模型列表里没有）", keep)
        self.effort.setCurrentIndex(max(0, self.effort.findData(keep)))
        self.effort.blockSignals(False)

    def _model_text(self) -> str:
        """下拉选中项带「（默认）」后缀，取真实模型名"""
        t = self.model.currentText().strip()
        i = self.model.findText(t)
        return (self.model.itemData(i) or t) if i >= 0 else t

    def _model_changed(self, _t):
        self._fill_efforts(self.cli.currentData(), self._model_text(), self.effort.currentData() or "")

    def _cli_changed(self, _i):
        self._abort_test()  # 测试期间换了 CLI：杀进程，旧结果丢弃
        self.conn.setText("")
        self._fill_choices(self.cli.currentData())
        self._show_cli_status()

    def _test_conn(self):
        cli, model, effort = self.cli.currentData(), self._model_text(), self.effort.currentData() or ""
        self._abort_test()
        self._tmp = tempfile.mkdtemp(prefix="donedef-conn-")
        self._out_file = os.path.join(self._tmp, "reply.txt")
        cmd = build_test_cmd(cli, model, effort, TEST_PROMPT, self._out_file)
        job = self._job = self._runner(cmd, child_env(os.environ), self._tmp, TEST_TIMEOUT)
        job.done.connect(lambda code, out, err, timed_out: self._conn_done(job, cli, code, out, err, timed_out))
        self.conn_btn.setEnabled(False)
        self.conn.setStyleSheet("color:#6b7280")
        self.conn.setText(f"正在测试 {CLI_NAME[cli]}…")
        self._t0 = time.monotonic()
        job.start()

    def _abort_test(self):
        """杀掉在跑的测试进程、删临时目录，并恢复按钮；没有在跑的就什么也不做"""
        job, self._job = self._job, None
        if job is not None:
            job.kill()
            self.conn_btn.setEnabled(True)
        self._cleanup_tmp()

    def _cleanup_tmp(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = ""

    def _conn_done(self, job, cli, code, out, err, timed_out):
        if job is not self._job:  # 已失效的旧测试
            return
        self._job = None
        secs = time.monotonic() - self._t0
        try:
            if timed_out:
                ok, text = False, f"超时（{TEST_TIMEOUT} 秒，已终止进程）"
            elif code != 0:
                ok, text = False, f"退出码 {code}\n{tail(err)}"
            else:
                try:
                    reply = parse_reply(cli, out, self._out_file)
                except ValueError as e:
                    ok, text = False, f"退出码 0，但{e}\n{tail(err)}"
                else:
                    ok = "OK" in reply.upper()
                    text = (f"已连接 · 回复 OK · {secs:.1f} 秒" if ok
                            else f"退出码 0，但回复里没有 OK：{tail(reply)}")
        finally:
            self._cleanup_tmp()
            self.conn_btn.setEnabled(True)
        self.conn.setStyleSheet("color:#23784f" if ok else "color:#c4363a")
        self.conn.setText(("✓ " if ok else "✗ ") + text)

    def done(self, r):
        self._abort_test()
        super().done(r)

    def closeEvent(self, e):
        self._abort_test()
        super().closeEvent(e)

    def _sync_caps(self, *_):
        """能力只对 claude / codex 生效：grok 没有单次运行挂 MCP 的参数"""
        ok = self.cli.currentData() in CAP_CLIS
        for box in self.caps.values():
            box.setEnabled(ok)
        self.caps_note.setText("" if ok else "Grok 暂不支持挂能力（勾了也不生效）")

    def _used(self, a):
        return any(s.agent_id == a.id or any(h.agent_id == a.id for h in s.helpers)
                   for c in self.board.cards for s in c.stages)

    def _add(self):
        n = len(self.board.agents) + 1
        self.board.agents.append(Agent(uuid.uuid4().hex[:12], f"新模板 {n}", "claude", "read", ""))
        self._refresh(len(self.board.agents) - 1)
        self.board.changed.emit()

    def _delete(self):
        a = self.current()
        if a and not self._used(a):
            self.board.agents.remove(a)
            self._refresh(0)
            self.board.changed.emit()

    def _save(self):
        a = self.current()
        if not a or not self.name.text().strip():
            self.msg.setText("名称不能为空")
            return
        a.name, a.cli, a.perm = self.name.text().strip(), self.cli.currentData(), self.perm.currentData()
        a.model, a.effort = self._model_text(), self.effort.currentData() or ""
        a.prompt = self.prompt.toPlainText()
        a.caps = [k for k, box in self.caps.items() if box.isChecked()]
        self._refresh(self.list.currentRow())
        self.msg.setText("已保存")
        self.board.changed.emit()


class AzureSettingsDialog(QDialog):
    """Azure 设置：全局一份 Azure 项目地址 + 个人 PAT（PAT 规则同 不回显、留空不改、600 原子写）"""

    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.setWindowTitle("Azure 设置")
        self.resize(620, 300)
        root = QVBoxLayout(self)
        form = QFormLayout()
        # macOS 原生风格下表单字段默认不拉伸，地址会被截断
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.ado = QLineEdit()
        self.ado.setPlaceholderText("Azure 项目地址，如 https://dev.azure.com/组织/项目")
        if board.azure_ready:
            self.ado.setText(f"https://dev.azure.com/{board.azure_org}/{board.azure_project}")
        self.ado.textChanged.connect(self._sync)
        form.addRow("Azure 项目", self.ado)
        self.pat_box = QWidget()
        pr = QHBoxLayout(self.pat_box)
        pr.setContentsMargins(0, 0, 0, 0)
        self.pat = QLineEdit()
        self.pat.setEchoMode(QLineEdit.Password)
        self.pat.setPlaceholderText("粘贴 Azure 个人访问令牌（PAT）；留空 = 不改")
        self.pat_state = QLabel("")
        pr.addWidget(self.pat, 1)
        pr.addWidget(self.pat_state)
        form.addRow("个人 PAT", self.pat_box)
        root.addLayout(form)
        self.ado_note = note("")
        root.addWidget(self.ado_note)
        # AI 开关按组织记，默认不勾：没允许的组织只看板、不跑 AI
        self.ai = QCheckBox("允许把这个组织的卡交给 AI")
        self.ai_note = note("不勾时这个组织的卡只能看、改状态，「开始需求分析」不可点；手写任务不受影响")
        self._ai_org = None
        root.addWidget(self.ai)
        root.addWidget(self.ai_note)
        root.addStretch()
        self.msg = note("")
        self.msg.setStyleSheet("color:#c4363a")
        root.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._sync()

    def _sync(self, *_):
        saved = azure.pat_saved(self.board.ado.pat_file)
        self.pat_state.setText("已保存" if saved else "未设置")
        self.pat_state.setStyleSheet("color:#23784f" if saved else "color:#9a6200")
        got = parse_azure_project(self.ado.text())
        org = got[0] if got else None
        if org != self._ai_org:  # 换了组织就显示那个组织记下的开关
            self._ai_org = org
            self.ai.setChecked(bool(org) and self.board.ai_allowed(org))
        self.ai.setEnabled(bool(org))
        if got and self.msg.text() == ADO_MISSING:
            self.msg.setText("")  # 地址填上了，旧的报错不再适用
        if got:
            pat = str(self.board.ado.pat_file).replace(str(Path.home()), "~", 1)
            self.ado_note.setText(f"看板从 {got[0]}/{got[1]} 拉迭代里指派给你的卡，PAT 存在 {pat}；"
                                  "交付记录你确认了才追加到 Task 描述；状态只在你在抽屉里手动选时才改")
        else:
            self.ado_note.setText("填卡片所在的 Azure 项目地址" + ("（认不出这个地址）" if self.ado.text().strip() else ""))

    def _save(self):
        got = parse_azure_project(self.ado.text())
        if not got:
            self.msg.setText(ADO_MISSING)
            return
        try:
            azure.save_pat(self.pat.text(), self.board.ado.pat_file)
        except OSError as e:
            self.msg.setText(f"个人 PAT 没存上：{e.strerror or e}")
            return
        self.pat.clear()
        self.board.set_ai_allowed(got[0], self.ai.isChecked())
        self.board.set_azure(*got)
        self.accept()


class RepoDialog(QDialog):
    """仓库设置：记住的仓库列表；可改基准分支 / 构建 / 测试命令，可「＋ 添加文件夹…」"""

    def __init__(self, board, parent=None, chooser=pick_folder):
        super().__init__(parent)
        self.board, self.chooser = board, chooser
        self.setWindowTitle("仓库设置")
        self.resize(760, 420)
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load)
        left.addWidget(self.list, 1)
        add = QPushButton("＋ 添加文件夹…")
        add.clicked.connect(self._add)
        left.addWidget(add)
        root.addLayout(left, 2)

        right = QVBoxLayout()
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name = QLabel("")
        self.path = QLabel("")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("名称", self.name)
        form.addRow("本地目录", self.path)
        self.edits = {}
        for key, lab in [("base_branch", "基准分支"), ("build_cmd", "构建命令"), ("test_cmd", "测试命令")]:
            self.edits[key] = QLineEdit()
            form.addRow(lab, self.edits[key])
        right.addLayout(form)
        right.addWidget(note("开发、验证阶段结束后程序在 worktree 里跑构建 / 测试命令，以退出码为准；留空 = 不自检。"
                             "名称用作 worktree 目录名，不能改。"))
        right.addStretch()
        self.msg = note("")
        self.msg.setTextFormat(Qt.PlainText)
        right.addWidget(self.msg)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save)
        row.addWidget(close)
        row.addWidget(self.save_btn)
        right.addLayout(row)
        root.addLayout(right, 3)
        self._refresh(0)

    def current(self):
        i = self.list.currentRow()
        return self.board.projects[i] if 0 <= i < len(self.board.projects) else None

    def _refresh(self, row):
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.board.projects:
            self.list.addItem(p.name)
        self.list.blockSignals(False)
        if self.board.projects:
            self.list.setCurrentRow(min(row, len(self.board.projects) - 1))
        self._load(self.list.currentRow())

    def _load(self, _row):
        p = self.current()
        self.save_btn.setEnabled(p is not None)
        self.name.setText(p.name if p else "还没有记住的仓库：点「＋ 添加文件夹…」")
        self.path.setText(p.path if p else "")
        for k, e in self.edits.items():
            e.setEnabled(p is not None)
            e.setText(getattr(p, k) if p else "")

    def _add(self):
        folder = self.chooser(self)
        if not folder:
            return
        got, err = self.board.remember_repo(folder)
        self.msg.setStyleSheet("color:#c4363a" if err else "color:#23784f")
        self.msg.setText(err or f"已记住 {got.name}（基准分支和构建 / 测试命令已自动识别，可改）")
        if got:
            self._refresh(self.board.projects.index(got))

    def _save(self):
        p = self.current()
        if not p:
            return
        for k, e in self.edits.items():
            setattr(p, k, e.text().strip())
        p.base_branch = p.base_branch or "main"
        self.edits["base_branch"].setText(p.base_branch)
        self.msg.setStyleSheet("color:#23784f")
        self.msg.setText("已保存")
        self.board.changed.emit()


class SettingsDialog(QDialog):
    """设置：左侧导航 Azure / 仓库 / Agent 模板，右侧嵌入原来三个弹窗本身（字段和保存逻辑不改）。
    子 QDialog 调 accept() / reject() 会把自己 hide 掉：Azure 保存（accepted）后重新 show，留在设置窗口里并提示「已保存」；
    任何一页取消（rejected）、仓库页「关闭」（accepted）都关掉整个设置窗口"""
    azure_saved = Signal()  # Azure 页保存成功：主窗口照旧重拉迭代
    PAGES = ["Azure", "仓库", "Agent 模板"]

    def __init__(self, board, parent=None, chooser=pick_folder, runner=run_process, prober=clis.probe_all, page=0):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(1040, 680)
        self.azure = AzureSettingsDialog(board)
        self.repos = RepoDialog(board, chooser=chooser)
        self.agents = AgentDialog(board, runner=runner, prober=prober)
        self.pages = [self.azure, self.repos, self.agents]
        root = QHBoxLayout(self)
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(140)
        self.nav.addItems(self.PAGES)
        root.addWidget(self.nav)
        right = QVBoxLayout()
        self.stack = QStackedWidget()
        for dlg in self.pages:
            dlg.setWindowFlags(Qt.Widget)  # 嵌入成普通子控件
            self.stack.addWidget(dlg)
        right.addWidget(self.stack, 1)
        self.msg = note("")
        self.msg.setStyleSheet("color:#23784f")
        right.addWidget(self.msg)
        root.addLayout(right, 1)
        self.nav.currentRowChanged.connect(self._goto)
        self.azure.accepted.connect(self._azure_saved)
        self.repos.accepted.connect(self.accept)  # 仓库页的「关闭」
        for dlg in self.pages:
            dlg.rejected.connect(self.reject)
        self.nav.setCurrentRow(page)

    def _goto(self, row):
        if 0 <= row < len(self.pages):
            self.stack.setCurrentIndex(row)
            self.msg.setText("")
            self.pages[row].show()

    def _azure_saved(self):
        self.azure.show()  # done() 已经把它 hide 了
        self.msg.setText("Azure 设置已保存，正在重新拉取迭代")
        self.azure_saved.emit()

    def done(self, r):
        self.agents._abort_test()  # 关窗时中止进行中的「测试连接」
        super().done(r)


def commit_box(card, files: list[str], message: str, on_ok, parent) -> QMessageBox:
    """通过前的提交确认：列文件（最多 30 个）、分支、提交信息；取消 = 不通过"""
    shown = "\n".join(f"  {f}" for f in files[:COMMIT_LIST_MAX])
    more = f"\n  …等 {len(files)} 个" if len(files) > COMMIT_LIST_MAX else ""
    box = QMessageBox(QMessageBox.Question, "提交改动并通过",
                      f"worktree 里有 {len(files)} 个文件改动，通过前会提交：\n\n{shown}{more}\n\n"
                      f"分支：{card.branch}\n提交信息：{message}\n\n"
                      "执行 git add -A + git commit（用仓库自己的 git 用户配置），不 push。", parent=parent)
    box.setTextFormat(Qt.PlainText)
    box.ok_btn = box.addButton("确认提交并通过", QMessageBox.AcceptRole)
    box.addButton("取消", QMessageBox.RejectRole)
    box.ok_btn.clicked.connect(on_ok)
    return box


def cleanup_box(card, on_ok, parent) -> QMessageBox:
    """清理确认：git worktree remove，不加 --force，分支保留"""
    box = QMessageBox(QMessageBox.Warning, "清理 worktree",
                      f"将执行 git worktree remove（不加 --force），删除目录：\n  {card.wt_path}\n\n"
                      f"有未提交的改动会失败。\n\n保留：\n  分支 {card.branch}（成果在这个分支上，未自动合并/push）",
                      parent=parent)
    box.setTextFormat(Qt.PlainText)
    box.ok_btn = box.addButton("确认删除目录", QMessageBox.DestructiveRole)
    box.addButton("取消", QMessageBox.RejectRole)
    box.ok_btn.clicked.connect(on_ok)
    return box


def abort_box(card, files: list[str], on_ok, parent) -> QMessageBox:
    """中止确认：本轮丢弃、会话清空；开发阶段列出将丢弃的未提交文件（最多 15 个）"""
    text = "本轮作废，AI 会话清空。"
    if card.stage.stage == "dev":
        if files and files[0].startswith("（读取改动清单失败"):
            text += f"\n\n{files[0]}"
        elif files:
            shown = "\n".join(f"  {f}" for f in files[:ABORT_LIST_MAX])
            more = f"\n  …等 {len(files)} 个" if len(files) > ABORT_LIST_MAX else ""
            text += f"\n\n将丢弃本卡 worktree 里 {len(files)} 个未提交的文件：\n{shown}{more}"
        else:
            text += "\n\nworktree 里没有未提交的改动需要丢弃。"
        text += "\n\n已提交的不受影响，原仓库不动。"
    box = QMessageBox(QMessageBox.Warning, "中止本轮", text, parent=parent)
    box.setTextFormat(Qt.PlainText)
    box.ok_btn = box.addButton("确认中止", QMessageBox.DestructiveRole)
    box.cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
    box.ok_btn.clicked.connect(on_ok)
    return box


def error_box(title: str, text: str, parent) -> QMessageBox:
    """git 等出错：原话照登"""
    box = QMessageBox(QMessageBox.Critical, title, text, parent=parent)
    box.setTextFormat(Qt.PlainText)
    box.addButton("知道了", QMessageBox.AcceptRole)
    return box


def quit_box(board, on_quit, parent) -> QMessageBox:
    running = board.running_cards()
    names = "\n".join(f"  {c.item.label} {STAGE_NAME[c.stage.stage]}阶段" for c in running)
    box = QMessageBox(QMessageBox.Question, "还有任务在跑",
                      f"以下 {len(running)} 张卡还在运行：\n{names}\n\n"
                      "退出前会停掉它们（进程组 SIGTERM，5 秒后 SIGKILL）。", parent=parent)
    box.stop_btn = box.addButton("停掉全部再退出", QMessageBox.DestructiveRole)
    box.addButton("取消", QMessageBox.RejectRole)
    box.stop_btn.clicked.connect(on_quit)
    return box


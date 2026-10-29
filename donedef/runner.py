"""子进程执行器：独立进程组、stdin=/dev/null、stdout 逐行落盘并回调、stderr 另存；
停止 = 进程组 SIGTERM，5 秒后还在就 SIGKILL。读输出在后台线程，回调经 Qt 排队回到主线程，界面不阻塞。"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

KILL_GRACE_MS = 5000


def _killpg(pgid: int, sig) -> bool:
    """给进程组发信号；组已不存在返回 False"""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


class StageProcess(QObject):
    """line(str)：stdout 每一行（已落盘）；finished(退出码, stderr 全文)。start() 失败抛 OSError"""
    line = Signal(str)
    finished = Signal(int, str)
    _line_bg = Signal(str)  # 后台线程发，排队回主线程再转发
    _done_bg = Signal(int)

    def __init__(self, cmd: list[str], cwd: str, env: dict[str, str], log_path: str, parent=None):
        super().__init__(parent)
        self.cmd, self.cwd, self.env = cmd, cwd, env
        self.log_path = Path(log_path)
        self.err_path = self.log_path.with_suffix(".stderr.txt")
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self._stderr: list[str] = []
        self._pending_code: int | None = None  # 停止中组长已退出、组里还有进程：等 SIGKILL 后再发 finished
        self._line_bg.connect(self._on_line, Qt.QueuedConnection)
        self._done_bg.connect(self._on_done, Qt.QueuedConnection)
        self._kill_timer = QTimer(self, singleShot=True, interval=KILL_GRACE_MS)
        self._kill_timer.timeout.connect(self.kill)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        out = threading.Thread(target=self._read_out, daemon=True)
        err = threading.Thread(target=self._read_err, daemon=True)
        out.start()
        err.start()
        threading.Thread(target=self._wait, args=(out, err), daemon=True).start()

    # ---------- 后台线程 ----------
    def _read_out(self):
        with open(self.log_path, "wb") as f:
            for raw in self.proc.stdout:
                f.write(raw)
                f.flush()
                self._line_bg.emit(raw.decode("utf-8", "replace").rstrip("\n"))

    def _read_err(self):
        with open(self.err_path, "wb") as f:
            for raw in self.proc.stderr:
                f.write(raw)
                f.flush()
                self._stderr.append(raw.decode("utf-8", "replace"))

    def _wait(self, out: threading.Thread, err: threading.Thread):
        code = self.proc.wait()
        # 组长退出了，但组里的孙进程可能还占着管道：等一会儿，还不结束就把整个组杀掉
        out.join(3)
        err.join(3)
        if out.is_alive() or err.is_alive():
            _killpg(self.proc.pid, signal.SIGKILL)
            out.join(5)
            err.join(5)
        self._done_bg.emit(code)

    # ---------- 主线程 ----------
    @Slot(str)
    def _on_line(self, text: str):
        self.line.emit(text)

    @Slot(int)
    def _on_done(self, code: int):
        # 停止中：组长退了，但组里可能还有不占管道、又忽略 SIGTERM 的孙进程 → 不取消 SIGKILL，等它执行完再报结束
        if self.stopping and self._group_alive():
            self._pending_code = code
            if not self._kill_timer.isActive():
                self._kill_timer.start()
            return
        self._kill_timer.stop()
        self.finished.emit(code, "".join(self._stderr))

    def stop(self):
        """SIGTERM 整个进程组；5 秒后还没退出就 SIGKILL"""
        if not self.proc or self.proc.poll() is not None and not self._group_alive():
            return
        self.stopping = True
        _killpg(self.proc.pid, signal.SIGTERM)
        if not self._kill_timer.isActive():  # 连续点停止不把 SIGKILL 往后推
            self._kill_timer.start()

    def kill(self):
        if self.proc:
            _killpg(self.proc.pid, signal.SIGKILL)
        if self._pending_code is not None:
            code, self._pending_code = self._pending_code, None
            self.finished.emit(code, "".join(self._stderr))

    def _group_alive(self) -> bool:
        try:
            os.killpg(self.proc.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def stop_blocking(self, grace: float = KILL_GRACE_MS / 1000):
        """退出程序时用：SIGTERM，最多等 grace 秒，再 SIGKILL（不依赖事件循环）"""
        if not self.proc:
            return
        self.stopping = True
        _killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        _killpg(self.proc.pid, signal.SIGKILL)

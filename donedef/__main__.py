"""运行：.venv/bin/python -m donedef（数据放在仓库下的 data/）
目前只支持 macOS：停止用进程组（start_new_session / os.killpg）、自检用 /bin/sh -c，Windows 未适配。"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .board import Board
from .ui import MainWindow, app_icon

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("定标 DoneDef")
    app.setWindowIcon(app_icon())
    board = Board(DATA_DIR)
    win = MainWindow(board)
    win.show()
    code = app.exec()
    board.close()
    sys.exit(code)


if __name__ == "__main__":
    main()

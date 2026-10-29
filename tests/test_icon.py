"""程序图标（靶心 + 对勾）。托盘和窗口都用 app_icon()。"""

pytest_plugins = ["tests.test_core"]


# ---------- 程序图标 ----------
def test_app_icon_has_sizes_and_visible_center(qtbot):
    from donedef.ui import app_icon
    icon = app_icon()
    assert not icon.isNull()
    sizes = [s.width() for s in icon.availableSizes()]
    assert min(sizes) <= 16 and max(sizes) >= 128
    img = icon.pixmap(128, 128).toImage()
    assert img.width() == 128
    # 中心是白色靶心，不透明；四角是圆角留白，应透明
    assert img.pixelColor(64, 64).alpha() > 200
    assert img.pixelColor(0, 0).alpha() == 0


def test_tray_icon_not_null(win):
    assert not win.tray.icon().isNull()

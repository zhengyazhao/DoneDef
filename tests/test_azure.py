"""Azure 对接测试：不连真 Azure，假 HTTP 回放 tests/samples 的真实样本（已脱敏）；CLI 用假脚本、git 用真的。"""
import base64
import io
import json
import os
import stat
import threading
import time
import urllib.error

import pytest

from donedef import azure
from donedef.azure import AzureError, Brief, Client, Iteration, html_to_text, text_to_html
from donedef.board import DB_FILENAME, Board
from donedef.dialogs import ADO_MISSING, NO_REPO, AzureSettingsDialog, WritebackDialog
from donedef.model import WorkItem
from donedef.ui import status_of
from tests.test_core import IDS, REQ2, SAMPLES, WAIT, add_project, dispose, env_for, git_out, grab, start, wait_review

pytest_plugins = ["tests.test_core"]  # 复用那边的夹具（fake / board / repo / win …）

PAT = "pat-for-test-0123456789"
ORG, PROJ = "myorg", "myproject"
ITEM3 = json.loads((SAMPLES / "azure-workitem-3.json").read_text())
NOT_FOUND = (SAMPLES / "azure-404.txt").read_bytes()
ITERS = json.loads((SAMPLES / "azure-iterations.json").read_text())
WIQL = json.loads((SAMPLES / "azure-wiql.json").read_text())
BATCH = json.loads((SAMPLES / "azure-batch.json").read_text())
PROJECT = {"id": "p", "name": PROJ, "defaultTeam": {"id": "t", "name": "myproject Team"}}  # 只用到 defaultTeam.name
WIQL_URL = "https://dev.azure.com/myorg/myproject/_apis/wit/wiql?$top=201&api-version=7.1"
BATCH_URL = "https://dev.azure.com/myorg/myproject/_apis/wit/workitemsbatch?api-version=7.1"


class _Resp:
    def __init__(self, status, body: bytes):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAzure:
    """替代 urlopen：按顺序回放预设的响应，记下每个请求（方法、地址、头、正文）"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = threading.Lock()

    def push(self, *responses):
        self.responses += responses

    def __call__(self, req, timeout):
        with self.lock:
            self.requests.append({"method": req.get_method(), "url": req.full_url, "timeout": timeout,
                                  "headers": {k.lower(): v for k, v in req.header_items()},
                                  "body": json.loads(req.data) if req.data else None})
            kind, a, b = self.responses.pop(0)
        if kind == "ok":
            return _Resp(a, json.dumps(b).encode() if not isinstance(b, bytes) else b)
        if kind == "http":
            raise urllib.error.HTTPError(req.full_url, a, "err", {}, io.BytesIO(b))
        raise a


@pytest.fixture
def pat(tmp_path):
    f = tmp_path / "personal-pat.txt"
    f.write_text(PAT + "\n")
    return f


def client(pat, *responses):
    http = FakeAzure(*responses)
    return Client(pat, opener=http), http


def auth_of(pat_text):
    return "Basic " + base64.b64encode(f":{pat_text}".encode()).decode()


# ---------- 请求构造 / 解析 ----------
def test_fetch_item_real_sample(pat):
    c, http = client(pat, ("ok", 200, ITEM3))
    w = c.fetch_item(ORG, PROJ, 3)
    r = http.requests[0]
    assert r["method"] == "GET"
    assert r["url"] == "https://dev.azure.com/myorg/myproject/_apis/wit/workitems/3?api-version=7.1"
    assert r["headers"]["authorization"] == auth_of(PAT) and r["timeout"] == azure.TIMEOUT
    # 样本里没有 System.Description（值为空的字段 Azure 不返回）
    assert (w.id, w.type, w.title, w.state, w.description, w.acceptance, w.source) == \
        ("3", "Task", "测试", "To Do", "", "", "azure")
    assert w.label == "#3"


def test_fetch_item_description_html(pat):
    item = {"id": 7, "fields": {"System.Title": "导出", "System.WorkItemType": "Issue", "System.State": "Doing",
                                "System.Description": "<div>按月导出<br>见 <a href=\"https://x.test/spec\">规格</a></div>"
                                                      "<p><img src=\"https://x.test/a.png\"></p>"}}
    c, _ = client(pat, ("ok", 200, item))
    w = c.fetch_item(ORG, PROJ, 7)
    assert w.description == "按月导出\n见 规格 (https://x.test/spec)\n\n[图片]"
    assert azure.image_count(w.description) == 1


def test_html_to_text():
    src = ("<p>第一段&nbsp;&amp; 实体</p><ul><li>一</li><li>二</li></ul>"
           "<a href=\"https://a.test/x\">https://a.test/x</a> <a href=\"#top\">顶部</a>"
           "<script>alert(1)</script><style>p{}</style><img src=x><img src=y>")
    text, n = html_to_text(src)
    assert text == "第一段 & 实体\n\n- 一\n- 二\nhttps://a.test/x 顶部[图片][图片]"
    assert n == 2
    assert html_to_text("") == ("", 0)
    assert html_to_text("纯文本没有标签") == ("纯文本没有标签", 0)


def test_append_description_request(pat):
    """先 GET 原描述和 rev，再 PATCH（test /rev + replace 描述），原描述原样保留、只在后面追加"""
    item = {"id": 3, "rev": 7, "fields": {"System.Description": "<div>原来的需求</div>", "System.State": "Doing"}}
    c, http = client(pat, ("ok", 200, item), ("ok", 200, {"id": 3, "rev": 8}))
    assert c.append_description(ORG, PROJ, 3, "改了什么 <b>\n分支 & 完") == 8
    get, patch = http.requests
    assert get["method"] == "GET" and get["url"].endswith("/_apis/wit/workitems/3?api-version=7.1")
    assert patch["method"] == "PATCH"
    assert patch["url"] == "https://dev.azure.com/myorg/myproject/_apis/wit/workitems/3?api-version=7.1"
    assert patch["headers"]["content-type"] == "application/json-patch+json"
    assert patch["body"] == [
        {"op": "test", "path": "/rev", "value": 7},
        {"op": "replace", "path": "/fields/System.Description",
         "value": "<div>原来的需求</div><br><br>改了什么 &lt;b&gt;<br>分支 &amp; 完"}]
    # 只动描述：请求里没有状态等其他字段
    assert all(op["path"] in ("/rev", "/fields/System.Description") for op in patch["body"])
    assert text_to_html("a\nb") == "a<br>b"


def test_append_description_empty_and_conflict(pat):
    # 原描述为空：不加前置空行
    c, http = client(pat, ("ok", 200, {"id": 3, "rev": 2, "fields": {}}), ("ok", 200, {"rev": 3}))
    c.append_description(ORG, PROJ, 3, "记录")
    assert http.requests[1]["body"][1]["value"] == "记录"
    # rev 对不上 → ConflictError
    c, _ = client(pat, ("ok", 200, {"id": 3, "rev": 2, "fields": {}}), ("http", 412, b"{}"))
    with pytest.raises(azure.ConflictError):
        c.append_description(ORG, PROJ, 3, "记录")


@pytest.mark.parametrize("resp, want", [
    (("http", 404, NOT_FOUND), "myorg/myproject 里找不到 #3（或没有权限）"),
    (("http", 401, b""), "个人 PAT 无效或已过期（401）"),
    (("http", 403, b"{}"), "个人 PAT 没有权限（403，需要 Work Items 读写）"),
    (("http", 500, b'{"message": "VS402371: boom"}'), "Azure 返回 500：VS402371: boom"),
    (("ok", 203, b"<html>sign in</html>"), "个人 PAT 无效或已过期（203，Azure 返回了登录页）"),
    (("ok", 200, b"<html>"), "Azure 返回的不是 JSON（状态 200）"),
    (("exc", urllib.error.URLError("nodename nor servname provided"), None), "连不上 Azure：nodename nor servname provided"),
    (("exc", TimeoutError("timed out"), None), "连不上 Azure：超时（30 秒）"),
])
def test_errors_mapped(pat, resp, want):
    c, _ = client(pat, resp)
    with pytest.raises(AzureError) as e:
        c.fetch_item(ORG, PROJ, 3)
    assert str(e.value) == want
    assert PAT not in str(e.value)


def test_pat_file_missing_or_empty(tmp_path):
    c, http = client(tmp_path / "nope.txt")
    with pytest.raises(AzureError, match="没找到个人 PAT"):
        c.fetch_item(ORG, PROJ, 3)
    (tmp_path / "empty.txt").write_text("  \n")
    c2, _ = client(tmp_path / "empty.txt")
    with pytest.raises(AzureError, match="没找到个人 PAT"):
        c2.fetch_item(ORG, PROJ, 3)
    assert http.requests == []  # 没 PAT 不发请求


def test_pat_read_each_call(pat):
    """PAT 每次请求时读文件：换了文件内容，下一次请求就用新的"""
    c, http = client(pat, ("ok", 200, ITEM3), ("ok", 200, ITEM3))
    c.fetch_item(ORG, PROJ, 3)
    pat.write_text("new-pat-value")
    c.fetch_item(ORG, PROJ, 3)
    assert [r["headers"]["authorization"] for r in http.requests] == [auth_of(PAT), auth_of("new-pat-value")]


def test_safe_wraps_only_azure_error():
    assert azure.safe(lambda: 1)() == {"ok": 1}
    assert azure.safe(lambda: (_ for _ in ()).throw(AzureError("原因")))() == {"error": "原因"}
    with pytest.raises(KeyError):  # 其他异常交给 BgJob 带类型名，不在这里吞掉
        azure.safe(lambda: {}["x"])()


# ---------- 端到端：取卡 → 需求 / 开发 / 验证 → 交付记录 → 写回失败 → 再写成功 ----------
def azure_board(tmp_path, fake, pat, http):
    return Board(tmp_path / "data", env=env_for(fake), ado=Client(pat, opener=http))


def azure_project(board, repo):
    """全局 Azure 设置（允许交给 AI）+ 一个记住的仓库（构建 / 测试命令都是 true）"""
    board.set_azure(ORG, PROJ)
    board.set_ai_allowed(ORG, True)
    return add_project(board, repo, build="true", test="true")


def run_to_draft(qtbot, board, fake, proj, item):
    """老卡流程（三阶段各一个模板）跑到交付记录草稿"""
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}],
              grok=[{"reply": "开发完成\n改了什么：\n- 新建 hello.txt", "session": "G1", "write": {"hello.txt": "hi\n"}}],
              codex=[{"reply": "结论：通过", "session": "X1"}])
    card, err = board.start_card(item, proj.id, ["a5", "a2", "a4"])
    assert err is None, err
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "新建 hello.txt，内容 hi")
    for stage in ("dev", "verify"):
        wait_review(qtbot, card, stage)
        assert board.commit_and_pass(card) is None
    return card


def test_azure_card_end_to_end(qtbot, tmp_path, fake, repo, pat):
    http = FakeAzure(("ok", 200, ITEM3))
    b = azure_board(tmp_path, fake, pat, http)
    try:
        proj = azure_project(b, repo)
        item = b.ado.fetch_item(ORG, PROJ, 3)
        card = run_to_draft(qtbot, b, fake, proj, item)
        assert git_out(repo, "log", "-1", "--format=%s", "feature/ado-3") == "#3 开发：测试"
        assert card.wt_path == str(b.data_dir / "worktrees" / "demo" / "3")
        # 验证通过 → 交付记录草稿，卡片留在待确认栏
        assert card.comment == "draft" and card.column == "review"
        assert status_of(b, card)[2] == "✉ 所有阶段都已通过，交付记录等你确认"
        draft = card.comment_text
        assert draft.splitlines() == [
            f"—— 定标 DoneDef 交付记录 · {time.strftime('%Y-%m-%d')} ——",
            "改了什么：",
            "- 新建 hello.txt",
            "分支：feature/ado-3（基于 main，1 个 commit，未合并）",
            "验证：构建 ✓ 测试 ✓；AI 验证结论：通过",
        ]
        assert not b.can_pass(card) and not b.ask(card, "再改改")  # 草稿阶段不能再跑 AI

        # 第一次写回失败（PATCH 500）：草稿（含修改）保留、记原因、可以再发
        desc = {"id": 3, "rev": 5, "fields": {"System.Description": "<div>原描述</div>"}}
        http.push(("ok", 200, desc), ("http", 500, b'{"message": "VS402371: boom"}'))
        assert b.send_writeback(card, draft + "\n我补一句")
        assert not b.send_writeback(card, draft)  # 写回中不能重复发
        assert status_of(b, card)[1] == "写回中"
        qtbot.waitUntil(lambda: card.id not in b.sending, timeout=WAIT)
        assert card.comment == "draft" and card.comment_text.endswith("我补一句")
        assert b.writeback_errors[card.id] == "Azure 返回 500：VS402371: boom"
        assert status_of(b, card)[:2] == ("bad", "写回失败")

        # rev 冲突 → 提示再点一次
        http.push(("ok", 200, desc), ("http", 412, b"{}"))
        assert b.send_writeback(card, card.comment_text)
        qtbot.waitUntil(lambda: card.id not in b.sending, timeout=WAIT)
        assert b.writeback_errors[card.id] == "卡片刚被别人改过，再点一次重试"

        # 再发成功 → 已完成
        http.push(("ok", 200, desc), ("ok", 200, {"id": 3, "rev": 6}))
        assert b.send_writeback(card, card.comment_text)
        qtbot.waitUntil(lambda: card.comment == "sent", timeout=WAIT)
        assert card.column == "done" and card.id not in b.writeback_errors
        assert "✓ 已写回 Task" in status_of(b, card)[2]
        sent = http.requests[-1]
        assert sent["method"] == "PATCH" and sent["url"].endswith("/myorg/myproject/_apis/wit/workitems/3?api-version=7.1")
        assert sent["body"][0] == {"op": "test", "path": "/rev", "value": 5}
        assert sent["body"][1]["value"].startswith("<div>原描述</div><br><br>—— 定标 DoneDef 交付记录")
        assert sent["body"][1]["value"].endswith("<br>我补一句")
        # 只取卡、读描述、改描述；不改状态
        assert [r["method"] for r in http.requests] == ["GET", "GET", "PATCH", "GET", "PATCH", "GET", "PATCH"]
        assert not any("System.State" in json.dumps(r["body"]) for r in http.requests if r["body"])
        kinds = [k for k, _ in b.db.events(card.id)]
        assert kinds[-6:] == ["writeback_send", "writeback_failed", "writeback_send", "writeback_failed",
                              "writeback_send", "writeback_sent"]

        # 重启还原：草稿 / 已发送状态都在；PAT 不在库里、不在日志里
        b.save()
        db_bytes = (b.data_dir / DB_FILENAME).read_bytes()
        assert PAT.encode() not in db_bytes and base64.b64encode(f":{PAT}".encode()) not in db_bytes
        for f in b.data_dir.rglob("*"):
            if f.is_file() and "worktrees" not in f.parts:
                assert PAT.encode() not in f.read_bytes(), f
    finally:
        b.stop_all()
        dispose(b)
    b2 = Board(tmp_path / "data", env=env_for(fake))
    try:
        c2 = b2.card("3")
        assert c2.comment == "sent" and c2.column == "done" and c2.item.source == "azure"
        assert b2.find_card("3") is c2
    finally:
        dispose(b2)


def test_manual_task_still_skips_comment(qtbot, board, fake, repo):
    proj = add_project(board, repo)
    card = run_to_draft(qtbot, board, fake, proj, board.new_manual_item("手写", "", ""))
    assert card.comment == "skip" and card.column == "done"


# ---------- 界面：Azure 设置 ----------
def test_azure_settings_dialog(qtbot, board, win):
    f = azure.PAT_FILE  # 夹具已改成临时路径
    sdlg = win.open_azure_settings()
    dlg = sdlg.azure  # 设置窗口里的 Azure 页
    assert isinstance(dlg, AzureSettingsDialog)
    assert dlg.ado.text() == "" and dlg.pat_state.text() == "未设置"
    assert dlg.pat.echoMode() == dlg.pat.EchoMode.Password
    dlg._save()
    assert dlg.msg.text() == ADO_MISSING and not board.azure_ready and dlg.isVisible()
    dlg.ado.setText("https://dev.azure.com/myorg/myproject/_workitems")
    assert dlg.msg.text() == ""
    assert "myorg/myproject" in dlg.ado_note.text() and str(f.name) in dlg.ado_note.text()
    dlg.pat.setText(PAT)
    qtbot.wait(50)
    grab(dlg, "k10-azure-settings")
    dlg._save()
    assert (board.azure_org, board.azure_project) == (ORG, PROJ) and sdlg.isVisible() and dlg.isVisible()  # 保存后留在设置窗口里
    assert f.read_text() == PAT + "\n" and stat.S_IMODE(f.stat().st_mode) == 0o600
    # 保存后重拉迭代：PAT 文件在临时路径、opener 是真 urlopen —— 这里只看请求已发出（拉取中），不等结果
    assert win._job is not None and win.fetch_label.text() == "正在拉取…"
    win._job = None  # 失效，避免结果回来改界面
    # 再打开：带出地址，PAT 只显示「已保存」；留空保存 = 不改
    dlg2 = win.open_azure_settings().azure
    assert dlg2.ado.text() == "https://dev.azure.com/myorg/myproject"
    assert dlg2.pat.text() == "" and dlg2.pat_state.text() == "已保存"
    assert PAT not in dlg2.pat_state.text() + dlg2.ado_note.text()
    dlg2._save()
    win._job = None
    assert f.read_text() == PAT + "\n"
    # 全局设置落库，重启还在；PAT 不进库
    board.save()
    b2 = Board(board.data_dir)
    try:
        assert (b2.azure_org, b2.azure_project) == (ORG, PROJ)
    finally:
        dispose(b2)
    for x in board.data_dir.rglob("*"):
        if x.is_file():
            assert PAT.encode() not in x.read_bytes(), x


# ---------- 拉我的任务：接口 ----------
def batch_of(*rows):
    """workitemsbatch 的返回：rows = (卡号, 类型, 状态, 标题)"""
    return {"count": len(rows), "value": [
        {"id": i, "fields": {"System.Id": i, "System.WorkItemType": t, "System.State": st, "System.Title": ti}}
        for i, t, st, ti in rows]}


def wiql_of(*ids):
    return {**WIQL, "workItems": [{"id": i} for i in ids]}


def test_iterations_request(pat):
    c, http = client(pat, ("ok", 200, PROJECT), ("ok", 200, ITERS))
    # 路径取自样本本身，不在测试里写死项目名
    assert c.iterations(ORG, PROJ) == [Iteration("Sprint 1", ITERS["value"][0]["path"], True)]
    assert [r["url"] for r in http.requests] == [
        "https://dev.azure.com/myorg/_apis/projects/myproject?api-version=7.1",
        "https://dev.azure.com/myorg/myproject/myproject%20Team/_apis/work/teamsettings/iterations?api-version=7.1"]
    assert [r["method"] for r in http.requests] == ["GET", "GET"]


def test_no_default_team(pat):
    c, http = client(pat, ("ok", 200, {"id": "p", "name": PROJ}))
    with pytest.raises(AzureError, match="没有默认团队"):
        c.iterations(ORG, PROJ)
    assert len(http.requests) == 1


def test_my_items_request(pat):
    rows = list(reversed(BATCH["value"]))  # Azure 返回顺序不保证，程序按卡号排
    c, http = client(pat, ("ok", 200, WIQL), ("ok", 200, {**BATCH, "value": rows}))
    got, more = c.my_items(ORG, PROJ, "myproject\\Sprint 1")
    # rev 取自样本顶层的 rev（改状态时做 test 用）；样本卡都没有父项
    assert got == [Brief(1, "Issue", "To Do", "测试", None, 1), Brief(2, "Issue", "To Do", "测试2", None, 3),
                   Brief(3, "Task", "To Do", "测试", None, 2)]
    assert more is False
    w, b = http.requests
    assert (w["method"], w["url"]) == ("POST", WIQL_URL)
    assert w["body"] == {"query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
                                  "AND [System.IterationPath] = 'myproject\\Sprint 1' AND [System.AssignedTo] = @Me "
                                  "ORDER BY [System.Id]"}
    assert (b["method"], b["url"]) == ("POST", BATCH_URL)
    assert b["body"] == {"ids": [1, 2, 3], "errorPolicy": "omit",
                         "fields": ["System.Id", "System.WorkItemType", "System.Title", "System.State",
                                    "System.Parent", "System.Rev"]}


def test_my_items_empty_skips_batch(pat):
    c, http = client(pat, ("ok", 200, wiql_of()))
    assert c.my_items(ORG, PROJ, "myproject\\Sprint 1") == ([], False)
    assert len(http.requests) == 1  # 空 ID 列表不发 workitemsbatch


def test_my_items_truncated_at_200(pat):
    c, http = client(pat, ("ok", 200, wiql_of(*range(1, 202))),
                     ("ok", 200, batch_of(*[(i, "Task", "To Do", f"t{i}") for i in range(1, 201)])))
    got, more = c.my_items(ORG, PROJ, "myproject\\Sprint 1")
    assert more is True and len(got) == 200
    assert http.requests[1]["body"]["ids"] == list(range(1, 201))


def test_wiql_quotes_iteration_path():
    assert "[System.IterationPath] = 'myproject\\Bob''s sprint'" in azure.wiql_my_items("myproject\\Bob's sprint")


def test_brief_done_states():
    assert [Brief(1, "Task", s, "x").done for s in ("Done", "Closed", "Removed", "To Do", "Doing")] == \
           [True, True, True, False, False]


# ---------- PAT 保存 ----------
def test_save_pat(tmp_path):
    f = tmp_path / "new-dir" / "personal-pat.txt"
    assert azure.save_pat("   ", f) is False and not f.exists()  # 留空不改
    assert azure.pat_saved(f) is False
    assert azure.save_pat("  first-pat \n", f) is True
    assert f.read_text() == "first-pat\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert azure.save_pat("second-pat", f) is True
    assert f.read_text() == "second-pat\n" and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert azure.save_pat("", f) is False and f.read_text() == "second-pat\n"
    assert sorted(os.listdir(f.parent)) == ["personal-pat.txt"]  # 临时文件没留下
    assert azure.pat_saved(f) is True
    assert Client(f, opener=FakeAzure()).pat_file == f


# ---------- 迭代看板 ----------
BASIC_STATES = {"count": 3, "value": [{"name": "To Do", "category": "Proposed"},
                                      {"name": "Doing", "category": "InProgress"},
                                      {"name": "Done", "category": "Completed"}]}  # Basic 流程（Issue / Task 相同）


class Router:
    """按请求内容回放（后台线程并发请求时顺序不定）：routes = [(地址或正文里要有的字符串, 200 的 JSON, 闸门 Event 或 None)]；
    没单独配的类型状态列表都回 Basic 流程的"""

    def __init__(self, *routes):
        self.routes = list(routes) + [("/states?", BASIC_STATES, None)]
        self.requests = []
        self.lock = threading.Lock()

    def add(self, *routes):
        with self.lock:
            self.routes = list(routes) + self.routes  # 新加的优先

    def __call__(self, req, timeout):
        body = req.data.decode() if req.data else ""
        with self.lock:
            self.requests.append({"method": req.get_method(), "url": req.full_url, "body": json.loads(body) if body else None})
            _, resp, gate = next(r for r in self.routes if r[0] in req.full_url or r[0] in body)
        if gate is not None:
            assert gate.wait(10)
        if isinstance(resp, tuple):  # (HTTP 状态码, 正文)：模拟出错
            raise urllib.error.HTTPError(req.full_url, resp[0], "err", {}, io.BytesIO(resp[1]))
        return _Resp(200, json.dumps(resp).encode())


TWO_ITERS = {"count": 2, "value": [
    {"id": "i1", "name": "Sprint 1", "path": "myproject\\Sprint 1", "attributes": {"timeFrame": "past"}},
    {"id": "i2", "name": "Sprint 2", "path": "myproject\\Sprint 2", "attributes": {"timeFrame": "current"}}]}
ITEM5 = {"id": 5, "fields": {"System.Title": "导出报表", "System.WorkItemType": "Issue", "System.State": "To Do",
                             "System.Description": "<p>按月导出</p><img src=a.png>"}}
ITEM5T = {**ITEM5, "fields": {**ITEM5["fields"], "System.WorkItemType": "Task"}}  # 没父项的 Task：需求用它自己的描述


def connect(board, win, pat, http):
    """配好 Azure 再拉（同 Azure 设置保存后的效果）"""
    board.ado = Client(pat, opener=http)
    board.set_azure(ORG, PROJ)
    board.set_ai_allowed(ORG, True)
    win.load_iterations()


def keys(win, col):
    return [w.key for w in win.columns[col].cards]


def test_state_column():
    from donedef.model import state_column
    assert [state_column(s) for s in ("To Do", "New", "Proposed", "", "Doing", "Active", "In Progress",
                                      "Done", "Closed", "Removed")] == \
        ["todo"] * 4 + ["doing"] * 3 + ["done"] * 3


def test_board_columns_and_start(qtbot, board, win, fake, repo, pat):
    r = Router(("/_apis/projects/", PROJECT, None), ("teamsettings/iterations", TWO_ITERS, None),
               ("Sprint 2", wiql_of(5, 7, 9), None),
               ("workitemsbatch", batch_of((9, "Task", "Done", "早做完的"), (5, "Task", "To Do", "导出报表"),
                                           (7, "Task", "Doing", "已经开始的")), None),
               ("/workitems/5?", ITEM5T, None))
    fake.plan(claude=[{"sleep": 60}, {"sleep": 60}, {"sleep": 60}])
    proj = add_project(board, repo)
    board.set_azure(ORG, PROJ)
    board.set_ai_allowed(ORG, True)
    _, err = board.start_card(WorkItem("7", "Task", "已经开始的", "Doing", "", "", "azure"), proj.id, IDS)
    assert err is None
    board.add_manual_task("手写一", "写个脚本", "")  # 未开始 → To Do
    started = start(board, proj, title="手写二")  # 已开始 → Doing
    assert started.id == "L2"
    connect(board, win, pat, r)
    # 启动即拉：迭代 → 默认当前迭代 → 指派给我的卡
    qtbot.waitUntil(lambda: win.fetch_label.text().startswith(ORG), timeout=WAIT)
    assert [win.iter_cb.itemText(i) for i in range(win.iter_cb.count())] == ["Sprint 1", "Sprint 2（当前）"]
    assert win.iter_cb.currentIndex() == 1 and win.iter_cb.isEnabled() and win.refresh_btn.isEnabled()
    wiql = next(q for q in r.requests if "wiql" in q["url"])
    assert "[System.IterationPath] = 'myproject\\Sprint 2'" in wiql["body"]["query"] and "@Me" in wiql["body"]["query"]
    assert win.fetch_label.text() == "myorg/myproject · 指派给我的 3 张"
    # 按 Azure 状态归栏；手写卡按本地进度
    assert (keys(win, "todo"), keys(win, "doing"), keys(win, "done")) == (["5", "L1"], ["7", "L2"], ["9"])
    assert [c.count.text() for c in win.columns.values()] == ["2", "2", "1"]
    # 徽标
    assert win.card_widget("5").badge.text() == "○ 未开始" and win.card_widget("9").badge.text() == "○ 未开始"
    assert win.card_widget("7").badge.text() == "▶ 理解需求中"
    assert win.card_widget("L1").badge.text() == "○ 未开始"

    # 点未开始的 #5（没父项的 Task）：需求用它自己的描述；仓库、主 agent 都没默认，都选了才能开始
    win.select("5")
    d = win.drawer
    assert d.pages.currentIndex() == 2 and "正在获取 #5" in d.preview.text()
    qtbot.waitUntil(lambda: "按月导出" in d.preview.text(), timeout=WAIT)
    assert "📋 需求（Task 自己的描述）" in d.preview.text()
    assert "描述里有 1 张图片，AI 看不到" in d.preview.text()
    assert d.repo.value() is None and d.main_cb.currentData() is None and d.helpers() == []
    assert not d.start_btn.isEnabled() and d.start_hint.text() == "还差：选关联代码仓库、选主 agent"
    assert [d.repo.itemText(i) for i in range(d.repo.count())] == [
        "— 请选择 —", f"demo  ·  {repo}", "选择本地文件夹…", "不关联仓库（调研、建模）"]
    pick(d.repo, proj.id)
    assert "data/worktrees/demo/5 · 分支 feature/ado-5（从 main 切出）" in d.repo_note.text()
    d.main_cb.setCurrentIndex(d.main_cb.findData("a5"))
    assert d.start_btn.isEnabled() and d.start_hint.text() == ""
    pick(d.repo, NO_REPO)
    assert "data/scratch/5/" in d.repo_note.text() and d.start_btn.isEnabled()
    pick(d.repo, proj.id)
    win.render()  # 刷新不冲掉已选的仓库和 agent
    assert d.repo.value() == proj.id and d.main_cb.currentData() == "a5"
    qtbot.wait(300)
    grab(win, "k5-start-drawer")
    d.start_btn.click()
    card = board.find_card("5")
    assert card is not None and card.project_id == proj.id and card.item.title == "导出报表"
    assert card.item.description.startswith("按月导出") and b_main(board, card)
    assert card.wt_path == str(board.data_dir / "worktrees" / "demo" / "5")
    assert d.pages.currentIndex() == 0 and "demo · ⎇ feature/ado-5" in d.meta.text()
    # 开始了也不改 Azure 状态：仍在 To Do 栏，徽标显示在跑
    assert keys(win, "todo") == ["5", "L1"] and win.card_widget("5").badge.text() == "▶ 理解需求中"
    assert all(q["method"] in ("GET", "POST") and "comments" not in q["url"] for q in r.requests)
    assert not any("workitems/5" in q["url"] and q["method"] == "PATCH" for q in r.requests)

    # 换迭代：Azure 卡换成那个迭代的；手写卡不动
    r.add(("Sprint 1", wiql_of(), None))
    win.iter_cb.setCurrentIndex(0)
    qtbot.waitUntil(lambda: win.fetch_label.text() == "这个迭代里没有指派给你的卡", timeout=WAIT)
    assert (keys(win, "todo"), keys(win, "doing"), keys(win, "done")) == (["L1"], ["L2"], [])
    board.stop_all()


def b_main(board, card):
    """主 agent 模式：三阶段同一个模板"""
    return board.main_mode(card) and len({st.agent_id for st in card.stages}) == 1


def pick(cb, value):
    """模拟在仓库下拉里选一项（activated 才触发「选择本地文件夹…」）"""
    i = cb.findData(value)
    cb.setCurrentIndex(i)
    cb.activated.emit(i)


def test_board_busy_and_stale(qtbot, board, win, pat):
    """拉取中下拉、刷新禁用；被新请求顶掉的结果丢弃"""
    gate = threading.Event()
    r = Router(("/_apis/projects/", PROJECT, None), ("teamsettings/iterations", TWO_ITERS, None),
               ("Sprint 2", wiql_of(5), gate), ("workitemsbatch", batch_of((5, "Issue", "To Do", "导出报表")), None))
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: win.iter_cb.count() == 2, timeout=WAIT)
    assert win.fetch_label.text() == "正在拉取…" and not win.iter_cb.isEnabled() and not win.refresh_btn.isEnabled()
    # 慢请求还没回来，又发了一次（Sprint 1，空）：新结果生效，旧结果回来也不覆盖
    r.add(("Sprint 1", wiql_of(), None))
    win.iter_cb.blockSignals(True)
    win.iter_cb.setCurrentIndex(0)
    win.iter_cb.blockSignals(False)
    win.load_items(clear=True)
    qtbot.waitUntil(lambda: win.fetch_label.text() == "这个迭代里没有指派给你的卡", timeout=WAIT)
    assert win.iter_cb.isEnabled() and win.refresh_btn.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: sum("workitemsbatch" in q["url"] for q in r.requests) == 1, timeout=WAIT)
    qtbot.wait(200)
    assert win.rows == [] and keys(win, "todo") == [] and win.fetch_label.text() == "这个迭代里没有指派给你的卡"


def test_board_stale_detail(qtbot, board, win, pat):
    """先点 #5（详情慢）再点 #6：预览只显示 #6；#5 的结果只进缓存"""
    gate = threading.Event()
    item6 = {"id": 6, "fields": {"System.Title": "第六张", "System.WorkItemType": "Task", "System.State": "To Do"}}
    r = Router(("/_apis/projects/", PROJECT, None), ("teamsettings/iterations", ITERS, None),
               ("Sprint 1", wiql_of(5, 6), None),
               ("workitemsbatch", batch_of((5, "Issue", "To Do", "导出报表"), (6, "Task", "To Do", "第六张")), None),
               ("/workitems/5?", ITEM5, gate), ("/workitems/6?", item6, None))
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["5", "6"], timeout=WAIT)
    assert win.iter_cb.itemText(0) == "Sprint 1（当前）"
    win.select("5")
    win.select("6")
    qtbot.waitUntil(lambda: "6" in win.details, timeout=WAIT)
    gate.set()
    qtbot.waitUntil(lambda: "5" in win.details, timeout=WAIT)
    qtbot.wait(100)
    d = win.drawer
    assert d.card_id == "6" and "第六张" in d.preview.text() and "导出报表" not in d.preview.text()
    assert d.start_item()[0].id == "6"


def test_board_detail_retry_and_refresh(qtbot, board, win, pat):
    """取详情失败 → 再点这张卡重试；抽屉开着时刷新 → 详情重取，还能开始"""
    http = FakeAzure(("ok", 200, PROJECT), ("ok", 200, ITERS), ("ok", 200, wiql_of(5)),
                     ("ok", 200, batch_of((5, "Task", "To Do", "导出报表"))), ("ok", 200, BASIC_STATES))
    connect(board, win, pat, http)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["5"], timeout=WAIT)
    d = win.drawer
    http.push(("http", 500, b""))
    win.select("5")
    qtbot.waitUntil(lambda: "取详情失败" in d.preview.text(), timeout=WAIT)
    assert "再点一下这张卡可重试" in d.preview.text() and d.start_item() is None
    win.render()  # 界面刷新不算重试，不发请求
    qtbot.wait(100)
    assert len(http.requests) == 6
    http.push(("ok", 200, ITEM5T))
    win.select("5")
    qtbot.waitUntil(lambda: "按月导出" in d.preview.text(), timeout=WAIT)
    assert d.start_item()[0].title == "导出报表" and len(http.requests) == 7
    # 抽屉开着点刷新：缓存清掉，结果回来后重取详情；Issue 的状态列表取过了，不再取
    http.push(("ok", 200, wiql_of(5)), ("ok", 200, batch_of((5, "Task", "To Do", "导出报表（改）"))), ("ok", 200, ITEM5T))
    win.refresh_btn.click()
    qtbot.waitUntil(lambda: "5" in win.details and len(http.requests) == 10, timeout=WAIT)
    qtbot.wait(100)
    assert "/workitems/5" in http.requests[-1]["url"] and d.start_item() is not None and "按月导出" in d.preview.text()


def test_board_detail_dropped_after_settings_change(qtbot, board, win, pat):
    """详情还在取时改了 Azure 设置（重拉迭代）：旧请求的结果丢弃，不进新项目的缓存"""
    gate = threading.Event()
    r = Router(("/_apis/projects/", PROJECT, None), ("teamsettings/iterations", ITERS, None),
               ("Sprint 1", wiql_of(5), None), ("workitemsbatch", batch_of((5, "Issue", "To Do", "导出报表")), None),
               ("/workitems/5?", ITEM5, gate))
    connect(board, win, pat, r)
    qtbot.waitUntil(lambda: keys(win, "todo") == ["5"], timeout=WAIT)
    win.select("5")
    assert "5" in win.detail_jobs
    board.set_azure(ORG, "myproject2")
    win.load_iterations()
    assert win.detail_jobs == {}
    gate.set()
    qtbot.waitUntil(lambda: sum("/workitems/5?" in q["url"] for q in r.requests) == 1, timeout=WAIT)
    qtbot.wait(200)
    assert "5" not in win.details


def test_board_fetch_errors(qtbot, board, win, pat):
    """出错原因 + 可刷新重试；没有迭代单独提示；超过 200 张提示"""
    board.add_manual_task("手写一", "", "")
    http = FakeAzure(("http", 401, b""))
    connect(board, win, pat, http)
    qtbot.waitUntil(lambda: win.refresh_btn.isEnabled(), timeout=WAIT)
    assert win.fetch_label.text() == "个人 PAT 无效或已过期（401）（可以点「刷新」重试）"
    assert win.fetch_label.property("kind") == "bad" and PAT not in win.fetch_label.text()
    assert not win.iter_cb.isEnabled() and keys(win, "todo") == ["L1"]  # 出错只显示手写卡
    grab(win, "k2-fetch-error")
    # 刷新 = 重拉迭代；项目没有迭代
    http.push(("ok", 200, PROJECT), ("ok", 200, {"count": 0, "value": []}))
    win.refresh_btn.click()
    qtbot.waitUntil(lambda: "还没有迭代" in win.fetch_label.text(), timeout=WAIT)
    assert win.fetch_label.text() == "这个项目还没有迭代（可以点「刷新」重试）"
    # 再刷新：有迭代了，卡超过 200 张
    http.push(("ok", 200, PROJECT), ("ok", 200, ITERS), ("ok", 200, wiql_of(*range(1, 202))),
              ("ok", 200, batch_of(*[(i, "Task", "To Do", f"t{i}") for i in range(1, 201)])), ("ok", 200, BASIC_STATES))
    win.refresh_btn.click()
    qtbot.waitUntil(lambda: len(keys(win, "todo")) == 201, timeout=WAIT)
    assert win.fetch_label.text() == "myorg/myproject · 指派给我的 200 张（超过 200 张，只显示前 200 张）"
    # 刷新 = 重拉当前迭代：拉取中旧卡留着；这回出错 → 只剩手写卡
    http.push(("exc", TimeoutError("timed out"), None))
    win.refresh_btn.click()
    assert len(keys(win, "todo")) == 201
    qtbot.waitUntil(lambda: win.refresh_btn.isEnabled(), timeout=WAIT)
    assert win.fetch_label.text() == "连不上 Azure：超时（30 秒）（可以点「刷新」重试）"
    assert keys(win, "todo") == ["L1"] and win.iter_cb.isEnabled()


def test_writeback_dialog_flow(qtbot, tmp_path, fake, repo, pat):
    """交付记录弹窗：可改、确认才追加；写不了时一打开就说明；失败原因留在窗口里可再发"""
    from donedef.ui import MainWindow
    http = FakeAzure(("ok", 200, ITEM3))
    b = azure_board(tmp_path, fake, pat, http)
    w = MainWindow(b, autorun=False)
    qtbot.addWidget(w)
    w.resize(1440, 900)
    w.show()
    try:
        proj = azure_project(b, repo)
        card = run_to_draft(qtbot, b, fake, proj, b.ado.fetch_item(ORG, PROJ, 3))
        w.rows = [Brief(3, "Task", "Doing", "测试")]  # 当作迭代里拉到了 #3（拉取流程见上面的看板测试）
        w.render()
        assert w.card_widget("3").badge.text() == "● 交付记录待确认" and w.card_widget("3") in w.columns["doing"].cards
        btn = w.card_widget(card.id).buttons["writeback"]
        assert btn.text() == "看交付记录"
        w.select(card.id)
        qtbot.wait(300)
        assert "交付记录等你确认" in w.drawer.banner.text()
        grab(w, "p9-board-draft")
        # 组织关了 AI：打开就说明写不了，按钮不可点
        b.set_ai_allowed(ORG, False)
        w.card_widget(card.id).buttons["writeback"].click()  # 选中后看板重画过，按钮要重新取
        dlg = w.active_dialog
        assert isinstance(dlg, WritebackDialog) and dlg.text.toPlainText() == card.comment_text
        assert "追加到 Azure Task myorg/myproject #3 的描述末尾" in dlg.findChild(type(dlg.msg)).text()
        assert dlg.msg.text().startswith("现在写不了：") and not dlg.send_btn.isEnabled()
        dlg.reject()
        b.set_ai_allowed(ORG, True)
        w.card_widget(card.id).buttons["writeback"].click()
        dlg = w.active_dialog
        assert dlg.msg.text() == "" and dlg.send_btn.isEnabled() and dlg.send_btn.text() == "确认追加到 Azure"
        dlg.text.setPlainText(dlg.text.toPlainText() + "\n人工补充")
        grab(dlg, "p9-writeback-dialog")
        http.push(("http", 401, b""))
        dlg.send_btn.click()
        assert not dlg.send_btn.isEnabled() and dlg.text.isReadOnly()
        qtbot.waitUntil(lambda: "写回失败" in dlg.msg.text(), timeout=WAIT)
        assert "个人 PAT 无效或已过期（401）" in dlg.msg.text() and dlg.send_btn.isEnabled()
        assert card.comment_text.endswith("人工补充")
        http.push(("ok", 200, {"id": 3, "rev": 1, "fields": {}}), ("ok", 200, {"rev": 2}))
        dlg.send_btn.click()
        qtbot.waitUntil(lambda: card.comment == "sent", timeout=WAIT)
        assert dlg.msg.text() == "✓ 已写回 Task" and not dlg.send_btn.isEnabled()
        assert http.requests[-1]["body"][1]["value"].endswith("<br>人工补充")
        dlg.reject()
        w.render()
        assert "cleanup" in w.card_widget(card.id).buttons
    finally:
        w._quitting = True
        b.stop_all()
        w.close()
        dispose(b)

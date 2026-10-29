"""Azure DevOps 对接：列迭代、拉指派给我的卡、取单张卡、发总结评论。urllib + 个人 PAT。
PAT 每次请求时从文件读，不缓存、不写日志 / 数据库 / 界面；报错文字里也不带。走 urllib 默认的代理设置（环境变量 / 系统代理）。"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from .model import DONE_STATES, WorkItem

PAT_FILE = Path.home() / ".azure-devops" / "personal-pat.txt"  # 只读这一个 PAT 文件，不读其它凭证
API = "7.1"
TIMEOUT = 30  # 秒
IMAGE = "[图片]"
LIST_MAX = 200  # 迭代里最多列 200 张（workitemsbatch 一次最多 200 个 ID）


@dataclass
class Iteration:
    name: str
    path: str  # 如 myproject\Sprint 1，WIQL 按它查
    current: bool  # attributes.timeFrame == current


@dataclass
class Brief:
    """列表里的一行：卡号、类型、状态、标题；parent = 父项卡号（没有为 None），rev = 拉取时的版本号（改状态时做 test）"""
    id: int
    type: str
    state: str
    title: str
    parent: int | None = None
    rev: int = 0

    @property
    def done(self) -> bool:
        return self.state in DONE_STATES


@dataclass
class BoardData:
    """看板一次拉取的结果：我的卡、它们的父项、各类型的状态列表 [(状态名, 分类)]；
    某类型的状态列表取不到时记在 state_errors（归列退回按名映射，抽屉里显示原因）"""
    rows: list[Brief]
    more: bool
    parents: list[Brief] = field(default_factory=list)
    states: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    state_errors: dict[str, str] = field(default_factory=dict)
    parent_error: str = ""  # 父项批量取失败的原因：泳道标题退回「#号（取不到父项详情）」


class AzureError(Exception):
    """给界面看的中文原因"""


class ConflictError(AzureError):
    """改状态时 rev 对不上：卡片被别人改过"""


CONFLICT = "卡片刚被别人改过，刷新后再改"
BRIEF_FIELDS = ["System.Id", "System.WorkItemType", "System.Title", "System.State", "System.Parent", "System.Rev"]
ACCEPTANCE_FIELD = "Microsoft.VSTS.Common.AcceptanceCriteria"


# ---------- HTML → 纯文本 ----------
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.images = 0
        self._skip = 0  # script / style 里的内容不要
        self._links: list[tuple[str, int]] = []  # (地址, 链接文字开始的位置)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "img":
            self.images += 1
            self.out.append(IMAGE)
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "a":
            self._links.append(((a.get("href") or "").strip(), len(self.out)))
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._links:
            href, start = self._links.pop()
            text = "".join(self.out[start:]).strip()
            # 链接保留地址：「看这里 (https://…)」；文字就是地址本身时不重复
            if href and not href.startswith(("javascript:", "#")) and href != text:
                self.out.append(f" ({href})")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(src: str) -> tuple[str, int]:
    """返回 (纯文本, 图片张数)"""
    p = _Text()
    p.feed(src or "")
    p.close()
    lines = [re.sub(r"[ \t ]+", " ", x).strip() for x in "".join(p.out).splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, p.images


def text_to_html(text: str) -> str:
    """描述 / 评论按 HTML 显示：先转义，再把换行换成 <br>，否则换行会丢"""
    return html.escape(text).replace("\n", "<br>")


def image_count(description: str) -> int:
    """描述里的图片张数：转换时每张图写成一个 [图片]"""
    return description.count(IMAGE)


# ---------- 请求 ----------
def read_pat(path: Path) -> str:
    try:
        pat = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pat = ""
    except OSError as e:
        raise AzureError(f"读不了个人 PAT 文件 {path}：{e.strerror}") from e
    if not pat:
        raise AzureError(f"没找到个人 PAT：{path}")
    return pat


def pat_saved(path: Path = PAT_FILE) -> bool:
    """项目设置里显示「已保存 / 未设置」用；只看有没有内容，不读出值给界面"""
    try:
        return bool(Path(path).read_text(encoding="utf-8").strip())
    except OSError:
        return False


def save_pat(value: str, path: Path = PAT_FILE) -> bool:
    """界面粘贴的 PAT 写入文件：留空不改；目录不存在就建；先写临时文件（权限 600）再替换。返回是否写了。
    写失败抛 OSError，由调用方显示原因（原因里不带 PAT）"""
    value = value.strip()
    if not value:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pat-")  # mkstemp 建的文件就是 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def wiql_my_items(iteration_path: str) -> str:
    """指派给我的、所选迭代里的全部类型工作项；迭代路径里的单引号按 WIQL 规则写两遍"""
    path = iteration_path.replace("'", "''")
    return ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.IterationPath] = '{path}' AND [System.AssignedTo] = @Me ORDER BY [System.Id]")


def _message(body: bytes) -> str:
    try:
        return str(json.loads(body).get("message") or "")[:300]
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace").strip()[:300]


class Client:
    """opener 默认 urllib.request.urlopen，测试里换成假的"""

    def __init__(self, pat_file: Path | None = None, opener=None, timeout: int = TIMEOUT):
        self.pat_file = Path(pat_file or PAT_FILE)  # 运行时取模块变量：测试统一改成临时文件
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _call(self, method: str, url: str, body: dict | list | None = None, not_found: str = "",
              content_type: str = "application/json", rev_test: bool = False) -> dict:
        token = base64.b64encode(f":{read_pat(self.pat_file)}".encode()).decode()
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                     headers={"Authorization": f"Basic {token}", "Accept": "application/json",
                                              "Content-Type": content_type})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            msg = _message(e.read() or b"")
            if e.code == 404 and not_found:
                raise AzureError(not_found) from e
            # rev 不一致：412 / 409，或 400 里带 TF26071（「被别人改过」）/ test 失败的 /rev（未在真环境核实）
            if rev_test and (e.code in (409, 412) or (e.code == 400 and ("TF26071" in msg or "/rev" in msg))):
                raise ConflictError(CONFLICT) from e
            if e.code == 401:
                raise AzureError("个人 PAT 无效或已过期（401）") from e
            if e.code == 403:
                raise AzureError("个人 PAT 没有权限（403，需要 Work Items 读写）") from e
            raise AzureError(f"Azure 返回 {e.code}：{msg or e.reason}") from e
        except urllib.error.URLError as e:
            raise AzureError(f"连不上 Azure：{e.reason}") from e
        except (TimeoutError, socket.timeout) as e:
            raise AzureError(f"连不上 Azure：超时（{self.timeout} 秒）") from e
        # PAT 不对时 Azure 可能回 203 + 登录页 HTML，而不是 401
        if status == 203:
            raise AzureError("个人 PAT 无效或已过期（203，Azure 返回了登录页）")
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise AzureError(f"Azure 返回的不是 JSON（状态 {status}）") from e
        if not isinstance(data, dict):
            raise AzureError(f"Azure 返回的格式不对（状态 {status}）")
        return data

    @staticmethod
    def _base(org: str, project: str) -> str:
        return f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}/_apis/wit"

    @staticmethod
    def _org(org: str) -> str:
        return f"https://dev.azure.com/{quote(org, safe='')}"

    def default_team(self, org: str, project: str) -> str:
        data = self._call("GET", f"{self._org(org)}/_apis/projects/{quote(project, safe='')}?api-version={API}",
                          not_found=f"找不到 Azure 项目 {org}/{project}（或没有权限）")
        name = (data.get("defaultTeam") or {}).get("name") or ""
        if not name:
            raise AzureError(f"{org}/{project} 没有默认团队，取不到迭代")
        return name

    def iterations(self, org: str, project: str) -> list[Iteration]:
        """项目默认团队的迭代；顺序按 Azure 返回的（按日期）"""
        team = self.default_team(org, project)
        data = self._call("GET", f"{self._org(org)}/{quote(project, safe='')}/{quote(team, safe='')}"
                                 f"/_apis/work/teamsettings/iterations?api-version={API}",
                          not_found=f"找不到 {org}/{project} 的团队「{team}」（或没有权限）")
        return [Iteration(it.get("name") or "", it.get("path") or "",
                          (it.get("attributes") or {}).get("timeFrame") == "current")
                for it in data.get("value") or []]

    def my_items(self, org: str, project: str, iteration_path: str) -> tuple[list[Brief], bool]:
        """返回 (按卡号升序的卡, 是否超过 LIST_MAX 被截断)。多查 1 个来判断有没有超"""
        base = self._base(org, project)
        data = self._call("POST", f"{base}/wiql?$top={LIST_MAX + 1}&api-version={API}",
                          {"query": wiql_my_items(iteration_path)})
        ids = [int(w["id"]) for w in data.get("workItems") or [] if "id" in w]
        more = len(ids) > LIST_MAX
        ids = ids[:LIST_MAX]
        return self.items(org, project, ids), more

    def items(self, org: str, project: str, ids: list[int]) -> list[Brief]:
        """按卡号批量取简要信息（含父项号和 rev），按卡号升序；空列表不发请求（workitemsbatch 会报错）"""
        rows = []
        for i in range(0, len(ids), LIST_MAX):  # 一次最多 200 个
            got = self._call("POST", f"{self._base(org, project)}/workitemsbatch?api-version={API}",
                             {"ids": ids[i:i + LIST_MAX], "fields": BRIEF_FIELDS, "errorPolicy": "omit"})
            rows += [_brief(w) for w in got.get("value") or [] if w]  # 删了 / 没权限的卡 omit 后是 null
        return sorted(rows, key=lambda b: b.id)

    def states(self, org: str, project: str, wtype: str) -> list[tuple[str, str]]:
        """某类型的全部状态 [(状态名, 分类)]，按 Azure 返回的顺序"""
        data = self._call("GET", f"{self._base(org, project)}/workitemtypes/{quote(wtype, safe='')}/states?api-version={API}",
                          not_found=f"{org}/{project} 里没有类型「{wtype}」")
        return [(s.get("name") or "", s.get("category") or "") for s in data.get("value") or [] if s.get("name")]

    def board(self, org: str, project: str, iteration_path: str, known_types=()) -> BoardData:
        """看板一次拉取：我的卡 → 不在其中的父项 → 没取过的类型的状态列表"""
        rows, more = self.my_items(org, project, iteration_path)
        mine = {r.id for r in rows}
        pids = sorted({r.parent for r in rows if r.parent and r.parent not in mine})
        out = BoardData(rows, more)
        if pids:
            try:
                out.parents = self.items(org, project, pids)
            except AzureError as e:  # 父项取不到不让整个看板失败
                out.parent_error = str(e)
        for t in sorted({r.type for r in rows + out.parents} - set(known_types)):
            try:
                out.states[t] = self.states(org, project, t)
            except AzureError as e:  # 不让整个看板失败：归列退回按名映射，原因在抽屉里显示
                out.state_errors[t] = str(e)
        return out

    def set_state(self, org: str, project: str, num: int, state: str, rev: int) -> Brief:
        """只改 System.State，先 test /rev 防止覆盖别人的修改；返回改完的简要信息（带新 rev）"""
        patch = [{"op": "test", "path": "/rev", "value": int(rev)},
                 {"op": "replace", "path": "/fields/System.State", "value": state}]
        data = self._call("PATCH", f"{self._base(org, project)}/workitems/{int(num)}?api-version={API}", patch,
                          not_found=f"{org}/{project} 里找不到 #{num}（或没有权限）",
                          content_type="application/json-patch+json", rev_test=True)
        return _brief(data)

    def fetch_item(self, org: str, project: str, num: int) -> WorkItem:
        """按卡号取单张卡。值为空的字段 Azure 不返回，一律 .get；Basic 流程没有验收标准字段，留空（4.2）"""
        data = self._call("GET", f"{self._base(org, project)}/workitems/{int(num)}?api-version={API}",
                          not_found=f"{org}/{project} 里找不到 #{num}（或没有权限）")
        f = data.get("fields") or {}
        desc, _ = html_to_text(f.get("System.Description") or "")
        acceptance, _ = html_to_text(f.get(ACCEPTANCE_FIELD) or "")  # Basic 流程没有这个字段
        return WorkItem(str(data.get("id", num)), f.get("System.WorkItemType") or "工作项",
                        f.get("System.Title") or "", f.get("System.State") or "", desc, acceptance, "azure")

    def append_description(self, org: str, project: str, num: int, text: str) -> int:
        """把交付记录追加到描述末尾：先 GET 原描述和 rev，再 PATCH（test /rev + replace 描述）。
        原描述原样保留（HTML 不转换），只在后面接分隔 + 新内容；不碰状态等其他字段。返回新 rev"""
        data = self._call("GET", f"{self._base(org, project)}/workitems/{int(num)}?api-version={API}",
                          not_found=f"{org}/{project} 里找不到 #{num}（或没有权限）")
        old = (data.get("fields") or {}).get("System.Description") or ""
        rev = int(data.get("rev") or 0)
        add = text_to_html(text.strip())
        if add in old:
            # 同一份交付记录已经在描述里（上次 PATCH 其实成功了但没收到响应）：不重复追加，按已写回处理
            return rev
        new = (old + "<br><br>" if old.strip() else "") + add
        patch = [{"op": "test", "path": "/rev", "value": rev},
                 {"op": "replace", "path": "/fields/System.Description", "value": new}]
        res = self._call("PATCH", f"{self._base(org, project)}/workitems/{int(num)}?api-version={API}", patch,
                         not_found=f"{org}/{project} 里找不到 #{num}（或没有权限）",
                         content_type="application/json-patch+json", rev_test=True)
        return int(res.get("rev") or 0)


def _brief(w: dict) -> Brief:
    f = w.get("fields") or {}
    parent = f.get("System.Parent")
    return Brief(int(w.get("id") or f.get("System.Id") or 0), f.get("System.WorkItemType") or "工作项",
                 f.get("System.State") or "", f.get("System.Title") or "",
                 int(parent) if parent else None, int(w.get("rev") or f.get("System.Rev") or 0))


def safe(fn):
    """给 BgJob 用：AzureError 只留中文原因（不带类型名），其他异常交给 BgJob 带类型名"""
    def run():
        try:
            return {"ok": fn()}
        except AzureError as e:
            return {"error": str(e)}
    return run

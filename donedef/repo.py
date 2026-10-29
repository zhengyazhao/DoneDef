"""仓库地址解析 + 本地仓库自动识别（只读：看文件、读 git 引用，不改任何东西）。
（从 prototype_qt 原样复制）"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


@dataclass
class RepoRef:
    kind: str  # azure / github / local
    name: str  # 仓库名（用作项目名、本地目录名）
    url: str = ""
    org: str = ""  # Azure 组织 / GitHub owner
    project: str = ""  # Azure 项目
    path: str = ""  # 本地路径（local 类型）


_AZURE = [
    re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)"),
    re.compile(r"^https://([^./]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/([^/?#]+)"),
    re.compile(r"^git@ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/([^/?#]+)"),
]
_GITHUB = [
    re.compile(r"^https://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
]
_AZURE_PROJECT = [
    re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/?#]+)"),
    re.compile(r"^https://([^./]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/?#]+)"),
]


def parse_repo(text: str) -> RepoRef | None:
    """识别 Azure / GitHub 仓库地址或本地路径；识别不了返回 None"""
    s = text.strip()
    if not s:
        return None
    for rx in _AZURE:
        m = rx.match(s)
        if m:
            org, proj, repo = (unquote(x) for x in m.groups())
            return RepoRef("azure", repo, s, org, proj)
    for rx in _GITHUB:
        m = rx.match(s)
        if m:
            owner, repo = m.groups()
            return RepoRef("github", repo, s, owner)
    if s.startswith(("/", "~")):
        p = Path(s).expanduser()
        return RepoRef("local", p.name, path=str(p))
    return None


def parse_azure_project(text: str) -> tuple[str, str] | None:
    """任务来源：Azure 项目地址 → (组织, 项目)。仓库地址也能识别出组织和项目"""
    s = text.strip()
    for rx in _AZURE_PROJECT:
        m = rx.match(s)
        if m and m.group(2) not in ("_git", "_apis"):
            return unquote(m.group(1)), unquote(m.group(2))
    return None


def repo_key(url: str) -> tuple | None:
    """同一个远端仓库的 https / ssh 写法归一成同一个键，用来判断本地克隆是不是它"""
    r = parse_repo(url)
    if not r or r.kind == "local":
        return None
    return (r.kind, r.org.lower(), r.project.lower(), r.name.lower())


def short_label(text: str) -> str:
    """顶栏用的仓库简称"""
    r = parse_repo(text)
    if not r:
        return text
    if r.kind == "azure":
        return f"Azure · {r.org}/{r.name}"
    if r.kind == "github":
        return f"GitHub · {r.org}/{r.name}"
    return text


def find_clone(ref: RepoRef, roots: list[Path] | None = None) -> str | None:
    """在常用目录里找这个远端仓库的已有克隆：目录名相同且 origin 指向同一仓库才算"""
    want = repo_key(ref.url)
    if not want:
        return None
    roots = roots or [Path.home() / "code", Path.home()]
    cands = []
    for r in roots:
        cands.append(r / ref.name)
        if r.is_dir():
            cands += [d / ref.name for d in sorted(r.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    for c in cands:
        if not (c / ".git").exists():
            continue
        try:
            out = subprocess.run(["git", "-C", str(c), "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and repo_key(out.stdout.strip()) == want:
            return str(c)
    return None


def origin_azure_project(path: str) -> tuple[str, str] | None:
    """本地仓库的 origin 在 Azure 上 → (组织, 项目)，用来自动带出卡片来源。
    只是帮你预填：不是 git 仓库、没有 origin、git 跑不起来都返回 None，地址框照样可以手填"""
    try:
        out = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    ref = parse_repo(out.stdout.strip()) if out.returncode == 0 else None
    return (ref.org, ref.project) if ref and ref.kind == "azure" else None


def clone_target(ref: RepoRef) -> str:
    """没有已有克隆时的默认位置"""
    return f"~/code/{ref.name}"


@dataclass
class Detected:
    base_branch: str = ""
    build_cmd: str = ""
    test_cmd: str = ""
    note: str = ""


def detect(path: str) -> Detected:
    """按仓库里的文件猜构建/测试命令，按 git 远端默认分支定基准分支；目录不存在就返回空"""
    p = Path(path).expanduser()
    if not p.is_dir():
        return Detected(note="目录不存在，clone 后自动识别")
    d = Detected()
    files = {f.name for f in p.iterdir()}
    if any(n.endswith(".sln") or n.endswith(".csproj") for n in files):
        d.build_cmd, d.test_cmd, d.note = "dotnet build", "dotnet test", "识别到 .NET 解决方案"
    elif "package.json" in files:
        try:
            scripts = json.loads((p / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError) as e:
            scripts = {}
            d.note = f"package.json 读取失败：{e}"
        d.build_cmd = "npm run build" if "build" in scripts else ""
        d.test_cmd = "npm test" if "test" in scripts else ""
        d.note = d.note or "识别到 package.json"
    elif "Package.swift" in files:
        d.build_cmd, d.test_cmd, d.note = "swift build", "swift test", "识别到 Swift Package"
    elif any(n.endswith(".xcodeproj") for n in files):
        d.build_cmd, d.test_cmd, d.note = "xcodebuild build", "xcodebuild test", "识别到 Xcode 工程"
    elif "go.mod" in files:
        d.build_cmd, d.test_cmd, d.note = "go build ./...", "go test ./...", "识别到 Go 模块"
    elif files & {"pyproject.toml", "pytest.ini", "setup.py"}:
        d.test_cmd, d.note = "pytest", "识别到 Python 项目"
    else:
        d.note = "没识别出构建/测试方式，不自检"
    d.base_branch = _default_branch(p)
    return d


def _default_branch(p: Path) -> str:
    """优先远端默认分支（origin/HEAD），其次当前分支，都拿不到用 main"""
    for args in (["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], ["branch", "--show-current"]):
        try:
            r = subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            break
        out = r.stdout.strip()
        if r.returncode == 0 and out:
            return out.removeprefix("origin/")
    return "main"

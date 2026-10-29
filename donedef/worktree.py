"""git worktree：建、改动列表 / 提交、diff、清理。
全部同步调用 git，失败时把 git 的原话带回去给界面显示。"""
from __future__ import annotations

import subprocess
from pathlib import Path

DIFF_LIMIT = 100_000
GIT_TIMEOUT = 120  # 秒


class GitError(Exception):
    """git 失败：str(e) 是给界面看的原话"""


def git(*args: str, cwd: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)} 执行失败：{e}") from e
    if r.returncode != 0:
        msg = (r.stderr.strip() or r.stdout.strip() or f"退出码 {r.returncode}")
        raise GitError(f"$ git {' '.join(args)}\n{msg}")
    return r.stdout


def add(repo: str, path: str, branch: str, base: str):
    """git worktree add -b <分支> <路径> <基准分支>；目录已存在就不开始"""
    p = Path(path)
    if p.exists():
        raise GitError(f"目录已存在：{p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(p), base, cwd=str(Path(repo).expanduser()))


def changed_files(wt: str) -> list[str]:
    """git status --porcelain 的每一行（含状态码），未跟踪的文件逐个列出"""
    out = git("status", "--porcelain", "--untracked-files=all", cwd=wt)
    return [ln for ln in out.splitlines() if ln.strip()]


def uncommitted(wt: str) -> list[str]:
    """未提交改动涉及的路径（已跟踪的修改 / 暂存 / 删除 + 未跟踪文件，逐个列出），只列不动"""
    out = git("status", "--porcelain", "-z", "--untracked-files=all", cwd=wt)
    return [path for _, path in _porcelain(out)]


def discard_uncommitted(wt: str) -> list[str]:
    """丢弃本 worktree 的全部未提交改动并返回被丢弃的路径：reset --hard HEAD（连暂存区一起还原）+ clean -fd
    （只删未跟踪文件，不加 -x：.gitignore 忽略的文件不动）。cwd 固定是 wt，绝不碰原仓库"""
    files = uncommitted(wt)
    git("reset", "--hard", "HEAD", cwd=wt)
    git("clean", "-fd", cwd=wt)
    return files


def commit(wt: str, message: str):
    """git add -A + git commit，用仓库自己的 git 用户配置"""
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", message, cwd=wt)


def diff(wt: str, base: str) -> tuple[str, str]:
    """相对「基准分支与 HEAD 的 merge-base」的 (diff --stat, diff)，含未提交的改动；diff 超长截断并注明"""
    mb = git("merge-base", base, "HEAD", cwd=wt).strip()
    stat = git("diff", "--stat", mb, cwd=wt).strip()
    full = git("diff", mb, cwd=wt)
    if len(full) > DIFF_LIMIT:
        full = full[:DIFF_LIMIT] + f"\n…（diff 共 {len(full)} 字符，超过 {DIFF_LIMIT}，已截断）"
    return stat or "（没有改动）", full or "（没有改动）"


def _name_status(out: str) -> list[tuple[str, str]]:
    """diff --name-status -z 的输出 → [(状态字母, 新路径)]；R / C 记录后面跟旧、新两个路径。
    用 -z：带空格 / 引号的路径不会被加引号（core.quotepath 只管非 ASCII）"""
    parts, out_ = out.split("\0"), []
    i = 0
    while i < len(parts) and parts[i]:
        code = parts[i][:1]
        n = 2 if code in "RC" else 1
        out_.append((code, parts[i + n]))
        i += n + 1
    return out_


def _porcelain(out: str) -> list[tuple[str, str]]:
    """status --porcelain -z 的输出 → [(XY, 路径)]；改名记录是「XY 新路径\0旧路径」，跳过旧路径"""
    parts, out_ = out.split("\0"), []
    i = 0
    while i < len(parts) and parts[i]:
        xy, path = parts[i][:2], parts[i][3:]
        out_.append((xy, path))
        i += 2 if "R" in xy or "C" in xy else 1
    return out_


def card_changes(cwd: str, base: str, ref: str = "HEAD") -> dict:
    """本卡分支相对基准的改动：files = [(状态, 路径, 是否未提交)]，commits = ["短哈希 标题"]（新的在前）。
    ref = HEAD：cwd 是 worktree，含未提交 / 未跟踪的改动；ref = 分支名：worktree 已清理，在原仓库里只看已提交的"""
    mb = git("merge-base", base, ref, cwd=cwd).strip()
    commits = git("log", "--format=%h %s", f"{mb}..{ref}", cwd=cwd).splitlines()
    files: dict[str, tuple[str, str, bool]] = {}
    for code, path in _name_status(git("diff", "--name-status", "-z", mb, *([] if ref == "HEAD" else [ref]), cwd=cwd)):
        files[path] = (code, path, False)
    if ref == "HEAD":
        for xy, path in _porcelain(git("status", "--porcelain", "-z", "--untracked-files=all", cwd=cwd)):
            # diff 基准..工作区 已给出相对基准的状态，沿用；不在其中的（未跟踪 / 本卡提交里新增又在工作区删掉）
            # 按 porcelain 的 XY 取：工作区列优先，没动就取暂存区列
            code = files[path][0] if path in files else "?" if xy == "??" else (xy[1] if xy[1] != " " else xy[0])
            files[path] = (code, path, True)
    return {"base": mb, "files": sorted(files.values(), key=lambda f: f[1]), "commits": commits}


def _numstat(out: str) -> dict[str, tuple[int | None, int | None]]:
    """diff --numstat -z 的输出 → {路径: (新增行数, 删除行数)}；二进制文件是 (None, None)。
    普通记录「增\t删\t路径\0」；改名 / 复制记录「增\t删\t\0旧路径\0新路径\0」，取新路径"""
    parts, res = out.split("\0"), {}
    i = 0
    while i < len(parts) and parts[i]:
        add_, del_, path = parts[i].split("\t", 2)
        if path == "":
            path, i = parts[i + 2], i + 2
        res[path] = (None, None) if add_ == "-" else (int(add_), int(del_))
        i += 1
    return res


def card_numstat(cwd: str, base: str, ref: str = "HEAD") -> dict[str, tuple[int | None, int | None]]:
    """本卡每个文件相对基准的 (新增行数, 删除行数)（只读）；二进制文件是 (None, None)。
    ref = HEAD：含未提交的修改；未跟踪的文件按文件行数算新增。ref = 分支名：只看已提交的"""
    mb = git("merge-base", base, ref, cwd=cwd).strip()
    res = _numstat(git("diff", "--numstat", "-z", mb, *([] if ref == "HEAD" else [ref]), cwd=cwd))
    if ref == "HEAD":
        for xy, path in _porcelain(git("status", "--porcelain", "-z", "--untracked-files=all", cwd=cwd)):
            if xy != "??":
                continue
            try:
                data = (Path(cwd) / path).read_bytes()
            except OSError:
                continue  # 读不了就不给行数，界面只显示文件名
            if b"\0" in data[:8000]:
                res[path] = (None, None)
            else:
                res[path] = (data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), 0)
    return res


def file_diff(cwd: str, base: str, path: str, ref: str = "HEAD", untracked: bool = False) -> str:
    """一个文件相对基准的 diff；未跟踪的新文件直接给内容。超长截断并注明"""
    if untracked:
        try:
            text = "（新文件，还没提交）\n" + (Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise GitError(f"读不了 {path}：{e.strerror or e}") from e
    else:
        mb = git("merge-base", base, ref, cwd=cwd).strip()
        text = git("-c", "core.quotepath=off", "diff", mb, *([] if ref == "HEAD" else [ref]), "--", path, cwd=cwd)
    if len(text) > DIFF_LIMIT:
        text = text[:DIFF_LIMIT] + f"\n…（共 {len(text)} 字符，超过 {DIFF_LIMIT}，已截断）"
    return text or "（没有改动）"


def remove(repo: str, wt: str):
    """git worktree remove <路径>，不加 --force：有未提交改动会失败"""
    git("worktree", "remove", wt, cwd=str(Path(repo).expanduser()))

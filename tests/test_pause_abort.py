"""暂停 / 继续 / 中止 / 重新开始。假 CLI + 真 git worktree。"""
from pathlib import Path

import pytest

from donedef import worktree
from donedef.board import ABORTED_LINE, PAUSED_LINE, RESUME_TEXT
from donedef.cli import REQ_ANSWER_SUFFIX
from donedef.worktree import GitError
from tests.test_core import REQ2, WAIT, add_project, git_out, sh, start, wait_review
from tests.test_main_agent import arg, main_agent, prompt_of

pytest_plugins = ["tests.test_core"]

SLOW = {"reply": "没做完", "sleep": 60}
DEV_FILES = {"keep.txt": "keep\n", "README.md": "改过\n", "new.txt": "new\n", "staged.txt": "staged\n"}


def wait_running(qtbot, card, calls, fake, cli="claude"):
    qtbot.waitUntil(lambda: card.stage.status == "running" and len(fake.calls(cli)) >= calls, timeout=WAIT)
    # 假 CLI 先 sleep 后才输出流，运行中拿不到会话 ID：手动补一个，模拟「已有会话」
    card.stage.session_id = card.stage.session_id or "S1"


def enter_dev(qtbot, board, fake, repo, **kw):
    """需求跑完 → 确认 → 开发阶段 grok 写一批文件后卡住；返回 (卡, worktree 路径)"""
    fake.plan(claude=[{"reply": REQ2}], grok=[{"write": DEV_FILES, "sleep": 60}])
    card = start(board, add_project(board, repo, **kw))
    wait_review(qtbot, card, "req")
    assert board.confirm_spec(card, "spec")
    qtbot.waitUntil(lambda: card.stage.stage == "dev" and (Path(card.wt_path) / "new.txt").exists(), timeout=WAIT)
    wt = Path(card.wt_path)
    # 已提交的：keep.txt；暂存未提交的：staged.txt；其余（README 修改、new.txt 未跟踪）留在工作区
    sh("git", "add", "keep.txt", cwd=wt)
    sh("git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "keep", cwd=wt)
    sh("git", "add", "staged.txt", cwd=wt)
    wait_running(qtbot, card, 1, fake, "grok")
    return card, wt


# ---------- 暂停 ----------
def test_pause_keeps_session_and_stop_intent_not_leaking(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW, SLOW])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    assert board.can_pause(card) and not board.is_paused(card)
    assert board.pause(card)
    wait_review(qtbot, card, "req")
    st = card.stage
    assert st.flag == "stopped" and st.error == PAUSED_LINE and st.session_id == "S1"
    assert board.is_paused(card) and not board.is_aborted(card) and not board.can_pause(card)
    assert st.rounds[-1].end == "stopped" and PAUSED_LINE in st.output
    assert not board.pause(card)  # 已暂停不能再暂停
    # 继续后再普通停止：文字回到老样子，意图没有串
    assert board.resume(card)
    wait_running(qtbot, card, 2, fake)
    assert board.stop(card)
    wait_review(qtbot, card, "req")
    assert st.error == "已手动停止（进程组 SIGTERM）" and not board.is_paused(card)


def test_normal_stop_text_unchanged(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    assert board.stop(card)
    wait_review(qtbot, card, "req")
    assert card.stage.error == "已手动停止（进程组 SIGTERM）" and not board.is_paused(card)


def test_pause_false_when_not_running(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}])
    card = start(board, add_project(board, repo))
    wait_review(qtbot, card, "req")
    assert not board.can_pause(card) and not board.pause(card) and not board.can_abort(card)
    assert not board.abort(card) and not board.resume(card) and not board.restart_stage(card)


# ---------- 继续 ----------
def test_resume_resumes_session_with_req_json_suffix(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW, {"reply": REQ2, "session": "S1"}])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    assert not board.resume(card)  # 没暂停
    assert board.pause(card)
    wait_review(qtbot, card, "req")
    assert board.resume(card)
    wait_review(qtbot, card, "req")
    c2 = fake.calls("claude")[1]
    assert arg(c2["args"], "--resume") == "S1"
    assert prompt_of(c2) == RESUME_TEXT + REQ_ANSWER_SUFFIX  # 需求阶段自动带 JSON 要求
    assert card.stage.rounds[-1].by == "你" and card.stage.rounds[-1].session_id == "S1"
    assert card.stage.flag is None and not board.is_paused(card)


def test_resume_in_dev_plain_text(qtbot, board, fake, repo):
    card, _ = enter_dev(qtbot, board, fake, repo)
    fake.plan(grok=[{"reply": "接着做完了"}])
    assert board.pause(card)
    wait_review(qtbot, card, "dev")
    assert board.is_paused(card) and card.stage.session_id == "S1"
    assert board.resume(card)
    qtbot.waitUntil(lambda: len(fake.calls("grok")) == 2, timeout=WAIT)
    call = fake.calls("grok")[1]
    assert RESUME_TEXT in " ".join(call["args"]) and REQ_ANSWER_SUFFIX not in " ".join(call["args"])
    assert "S1" in call["args"]
    board.stop_all()


# ---------- 中止 ----------
def test_abort_running_req_clears_session_no_file_touch(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    marker = Path(card.wt_path) / "untracked.txt"
    marker.write_text("别动我\n")  # 需求阶段中止不碰文件
    assert board.can_abort(card) and board.abort_preview(card) == []
    assert board.abort(card)
    wait_review(qtbot, card, "req")
    st = card.stage
    assert st.flag == "stopped" and st.error == ABORTED_LINE and st.session_id == ""
    assert board.is_aborted(card) and not board.is_paused(card)
    assert marker.read_text() == "别动我\n"


def test_abort_dev_discards_uncommitted_keeps_committed(qtbot, board, fake, repo):
    card, wt = enter_dev(qtbot, board, fake, repo)
    assert sorted(board.abort_preview(card)) == ["README.md", "new.txt", "staged.txt"]
    assert board.abort(card)
    wait_review(qtbot, card, "dev")
    st = card.stage
    assert board.is_aborted(card) and st.error == ABORTED_LINE and st.session_id == ""
    assert (wt / "keep.txt").read_text() == "keep\n"  # 已提交的保留
    assert (wt / "README.md").read_text() == "demo\n"  # 修改还原
    assert not (wt / "new.txt").exists() and not (wt / "staged.txt").exists()
    assert worktree.uncommitted(str(wt)) == []
    assert any(l.startswith("已丢弃未提交改动 3 个文件") and "new.txt" in l for l in st.output)
    assert (repo / "README.md").read_text() == "demo\n" and not (repo / "new.txt").exists()  # 原仓库没动
    assert "keep" in git_out(wt, "log", "-1", "--format=%s")
    ev = [text for kind, text in board.db.events(card.id) if kind == "abort"]
    assert len(ev) == 1 and "3 个文件" in ev[0] and "new.txt" in ev[0]


def test_pause_then_abort_discards(qtbot, board, fake, repo):
    card, wt = enter_dev(qtbot, board, fake, repo)
    assert board.pause(card)
    wait_review(qtbot, card, "dev")
    assert board.is_paused(card) and (wt / "new.txt").exists()  # 暂停不丢改动
    assert board.can_abort(card) and sorted(board.abort_preview(card)) == ["README.md", "new.txt", "staged.txt"]
    assert board.abort(card)  # 已暂停：不需要停进程，立即收尾
    st = card.stage
    assert board.is_aborted(card) and not board.is_paused(card) and st.session_id == "" and not board.can_abort(card)
    assert (wt / "keep.txt").exists() and not (wt / "new.txt").exists()
    assert (wt / "README.md").read_text() == "demo\n"


def test_abort_discard_failure_is_loud(qtbot, board, fake, repo, monkeypatch):
    card, wt = enter_dev(qtbot, board, fake, repo)

    def boom(path):
        raise GitError("$ git reset --hard HEAD\nfatal: 坏了")

    monkeypatch.setattr(worktree, "discard_uncommitted", boom)
    assert board.pause(card)
    wait_review(qtbot, card, "dev")
    assert board.abort(card)
    st = card.stage
    assert st.error.startswith(ABORTED_LINE) and "丢弃改动失败" in st.error and "fatal: 坏了" in st.error
    assert board.is_aborted(card) and st.session_id == ""
    assert any("fatal: 坏了" in l for l in st.output)
    assert (wt / "new.txt").exists()  # 没丢成就如实留着


def test_abort_preview_empty_without_worktree_or_not_dev(qtbot, board, fake, repo):
    card, wt = enter_dev(qtbot, board, fake, repo)
    board.stop(card)
    wait_review(qtbot, card, "dev")
    card.worktree_cleaned = True
    assert board.abort_preview(card) == []
    card.worktree_cleaned = False
    assert board.abort_preview(card) != []
    card.cur = 0  # 当前阶段不是 dev
    assert board.abort_preview(card) == []


# ---------- 重新开始 ----------
def test_restart_stage_new_session_first_round_prompt(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW, {"reply": REQ2, "session": "S2"}])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    assert not board.restart_stage(card)  # 在跑
    assert board.abort(card)
    wait_review(qtbot, card, "req")
    assert board.restart_stage(card)
    wait_review(qtbot, card, "req")
    c2 = fake.calls("claude")[1]
    assert "--resume" not in c2["args"]
    assert prompt_of(c2) == board._pipeline_prompt(card)
    st = card.stage
    assert st.rounds[-1].by == "流水线" and st.rounds[-1].session_id == "S2" and st.session_id == "S2"
    assert st.flag is None and not board.is_aborted(card)


def test_restart_requires_aborted_not_paused(qtbot, board, fake, repo):
    fake.plan(claude=[SLOW])
    card = start(board, add_project(board, repo))
    wait_running(qtbot, card, 1, fake)
    assert board.pause(card)
    wait_review(qtbot, card, "req")
    assert not board.restart_stage(card)
    assert len(fake.calls("claude")) == 1


# ---------- 自检 / 额外角色在跑 ----------
def test_cannot_pause_during_check(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2}], grok=[{"reply": "好了"}], codex=[{"reply": "测完"}])
    card = start(board, add_project(board, repo, test="sleep 60"))
    wait_review(qtbot, card, "req")
    board.confirm_spec(card, "spec")
    qtbot.waitUntil(lambda: card.stage.status == "checking", timeout=WAIT)
    assert not board.can_pause(card) and not board.pause(card) and not board.can_abort(card)
    assert not board.abort(card)
    assert board.stop(card)  # 自检时仍是老的停止
    wait_review(qtbot, card, "dev")
    assert "自检 sleep 60 被终止" in card.stage.error


def test_cannot_pause_while_helper_running(qtbot, board, fake, repo):
    fake.plan(claude=[{"reply": REQ2, "session": "C1"}, {"reply": "x", "sleep": 30}])
    card, _ = board.start_card(board.new_manual_item("t", "d", ""), add_project(board, repo).id, main_agent(board),
                               helpers=[("req", "p3"), ("req", "p5")])
    qtbot.waitUntil(lambda: card.stage.running_helper() is not None and len(fake.calls("claude")) == 2, timeout=WAIT)
    assert not board.can_pause(card) and not board.pause(card) and not board.can_abort(card)
    assert board.stop(card)
    wait_review(qtbot, card, "req")
    assert card.stage.error != PAUSED_LINE and not board.is_paused(card)


# ---------- worktree 单测 ----------
def test_worktree_discard_uncommitted(tmp_path, repo):
    wt = tmp_path / "wt"
    worktree.add(str(repo), str(wt), "feature/x", "main")
    (wt / "a.txt").write_text("a\n")
    sh("git", "add", "a.txt", cwd=wt)
    sh("git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "a", cwd=wt)
    (wt / "a.txt").write_text("a2\n")  # 已跟踪的修改
    (wt / "README.md").unlink()  # 已跟踪的删除
    (wt / "u.txt").write_text("u\n")  # 未跟踪
    (wt / "sub").mkdir()
    (wt / "sub" / "deep.txt").write_text("d\n")  # 未跟踪目录
    (wt / ".gitignore").write_text("*.log\n")
    sh("git", "add", ".gitignore", cwd=wt)
    sh("git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "ign", cwd=wt)
    (wt / "keep.log").write_text("被忽略\n")
    (wt / "s.txt").write_text("s\n")
    sh("git", "add", "s.txt", cwd=wt)  # 暂存未提交

    expect = ["README.md", "a.txt", "s.txt", "sub/deep.txt", "u.txt"]
    assert sorted(worktree.uncommitted(str(wt))) == expect
    assert (wt / "u.txt").exists()  # 只列不删
    assert sorted(worktree.discard_uncommitted(str(wt))) == expect
    assert worktree.uncommitted(str(wt)) == []
    assert (wt / "a.txt").read_text() == "a\n" and (wt / "README.md").read_text() == "demo\n"
    assert not (wt / "u.txt").exists() and not (wt / "sub").exists() and not (wt / "s.txt").exists()
    assert (wt / "keep.log").read_text() == "被忽略\n"  # 不加 -x：忽略的文件不动
    assert git_out(wt, "log", "-1", "--format=%s") == "ign"
    assert (repo / "README.md").read_text() == "demo\n"
    assert worktree.discard_uncommitted(str(wt)) == []  # 干净时返回空


def test_worktree_discard_error_raises(tmp_path):
    with pytest.raises(GitError):
        worktree.discard_uncommitted(str(tmp_path / "不存在"))

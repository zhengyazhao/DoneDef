"""SQLite 落库：projects / agents / cards / stage_runs / rounds / events / settings。
projects 表当「记住的仓库」用；settings 存全局 Azure 设置和未开始的手写任务，老库自动建。
agents 有 caps 列（老库 ALTER 补上）、helper_runs 表存额外角色的运行。
看板数据量小（几十张卡），保存采用「整体覆盖写」：一个事务里删掉重写，简单且不会漏更新；events 只追加。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .model import Agent, Card, HelperRun, Project, Round, StageRun, WorkItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, ord INTEGER, name TEXT, repo TEXT, repo_kind TEXT, path TEXT, tasks TEXT,
  org TEXT, ado_project TEXT, base_branch TEXT, build_cmd TEXT, test_cmd TEXT, cloned INTEGER);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, ord INTEGER, name TEXT, cli TEXT, perm TEXT, prompt TEXT, model TEXT, effort TEXT);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, ord INTEGER, project_id TEXT, item_type TEXT, title TEXT, state TEXT,
  description TEXT, acceptance TEXT, source TEXT, cur INTEGER, spec TEXT, comment TEXT, comment_text TEXT,
  worktree_cleaned INTEGER, wt_path TEXT);
CREATE TABLE IF NOT EXISTS stage_runs (
  card_id TEXT, idx INTEGER, stage TEXT, agent_id TEXT, status TEXT, flag TEXT, session_id TEXT,
  output TEXT, checks TEXT, elapsed INTEGER, error TEXT, PRIMARY KEY (card_id, idx));
CREATE TABLE IF NOT EXISTS rounds (
  card_id TEXT, stage_idx INTEGER, n INTEGER, by_who TEXT, ask TEXT, reply TEXT, end_flag TEXT,
  parsed TEXT, session_id TEXT, log TEXT, PRIMARY KEY (card_id, stage_idx, n));
CREATE TABLE IF NOT EXISTS helper_runs (
  card_id TEXT, stage_idx INTEGER, n INTEGER, agent_id TEXT, status TEXT, session_id TEXT, reply TEXT, log TEXT,
  elapsed INTEGER, error TEXT, PRIMARY KEY (card_id, stage_idx, n));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, card_id TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""
OUTPUT_KEEP = 1000  # 每个阶段最多存最近这么多行动作（原始输出全文在日志文件里）


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(agents)")]
        if "caps" not in cols:  # 早期的老库
            with self.conn:
                self.conn.execute("ALTER TABLE agents ADD COLUMN caps TEXT")

    def close(self):
        self.conn.close()

    def is_empty(self) -> bool:
        """第一次启动 = 库里从没存过东西。不能只看 agents：模板被删光后重启不该重新预置、更不能清空项目"""
        if self.conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            return False
        return all(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
                   for t in ("projects", "agents", "cards"))

    def setting(self, k: str, default: str = "") -> str:
        r = self.conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return default if r is None or r[0] is None else r[0]

    def set_setting(self, k: str, v: str):
        with self.conn:
            self.conn.execute("INSERT INTO settings (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def event(self, card_id, kind: str, text: str):
        with self.conn:
            self.conn.execute("INSERT INTO events (ts, card_id, kind, text) VALUES (?,?,?,?)",
                              (time.time(), None if card_id is None else str(card_id), kind, text))

    def events(self, card_id=None) -> list[tuple[str, str]]:
        q = "SELECT kind, text FROM events" + (" WHERE card_id=?" if card_id is not None else "") + " ORDER BY id"
        return list(self.conn.execute(q, (str(card_id),) if card_id is not None else ()))

    # ---------- 保存 ----------
    def save_all(self, projects: list[Project], agents: list[Agent], cards: list[Card]):
        c = self.conn
        with c:
            c.execute("PRAGMA user_version = 1")  # 标记已初始化
            for t in ("projects", "agents", "cards", "stage_runs", "rounds", "helper_runs"):
                c.execute(f"DELETE FROM {t}")
            c.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (p.id, i, p.name, p.repo, p.repo_kind, p.path, p.tasks, p.org, p.ado_project, p.base_branch,
                 p.build_cmd, p.test_cmd, int(p.cloned)) for i, p in enumerate(projects)])
            c.executemany("INSERT INTO agents (id, ord, name, cli, perm, prompt, model, effort, caps) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", [
                (a.id, i, a.name, a.cli, a.perm, a.prompt, a.model, a.effort, json.dumps(a.caps))
                for i, a in enumerate(agents)])
            for i, card in enumerate(cards):
                it = card.item
                c.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    str(it.id), i, card.project_id, it.type, it.title, it.state, it.description, it.acceptance,
                    it.source, card.cur, card.spec, card.comment, card.comment_text, int(card.worktree_cleaned),
                    card.wt_path))
                for si, s in enumerate(card.stages):
                    c.execute("INSERT INTO stage_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        str(it.id), si, s.stage, s.agent_id, s.status, s.flag, s.session_id,
                        json.dumps(s.output[-OUTPUT_KEEP:], ensure_ascii=False),
                        json.dumps(s.checks, ensure_ascii=False), s.elapsed, s.error))
                    c.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?,?)", [
                        (str(it.id), si, n, r.by, r.ask, r.reply, r.end,
                         None if r.parsed is None else json.dumps(r.parsed, ensure_ascii=False), r.session_id, r.log)
                        for n, r in enumerate(s.rounds)])
                    c.executemany("INSERT INTO helper_runs VALUES (?,?,?,?,?,?,?,?,?,?)", [
                        (str(it.id), si, n, h.agent_id, h.status, h.session_id, h.reply, h.log, h.elapsed, h.error)
                        for n, h in enumerate(s.helpers)])

    # ---------- 读取 ----------
    def load(self) -> tuple[list[Project], list[Agent], list[Card]]:
        c = self.conn
        projects = [Project(r[0], *r[2:12], cloned=bool(r[12]))
                    for r in c.execute("SELECT * FROM projects ORDER BY ord")]
        agents = [Agent(*r[:7], caps=json.loads(r[7] or "[]"))
                  for r in c.execute("SELECT id, name, cli, perm, prompt, model, effort, caps FROM agents ORDER BY ord")]
        cards = []
        for r in c.execute("SELECT * FROM cards ORDER BY ord").fetchall():
            cid = r[0]
            item = WorkItem(cid, r[3], r[4], r[5], r[6], r[7], r[8])
            stages = []
            for s in c.execute("SELECT * FROM stage_runs WHERE card_id=? ORDER BY idx", (cid,)).fetchall():
                rounds = [Round(x[3], x[4], x[5], x[6], None if x[7] is None else json.loads(x[7]), x[8] or "", x[9] or "")
                          for x in c.execute("SELECT * FROM rounds WHERE card_id=? AND stage_idx=? ORDER BY n",
                                             (cid, s[1]))]
                checks = {k: tuple(v) for k, v in json.loads(s[8] or "{}").items()}
                helpers = [HelperRun(h[3], h[4], h[5] or "", h[6] or "", h[7] or "", h[8] or 0, h[9] or "")
                           for h in c.execute("SELECT * FROM helper_runs WHERE card_id=? AND stage_idx=? ORDER BY n",
                                              (cid, s[1]))]
                stages.append(StageRun(s[2], s[3], s[4], s[5], rounds, json.loads(s[7] or "[]"), checks,
                                       s[9] or 0, s[6] or "", s[10] or "", helpers=helpers))
            cards.append(Card(item, r[2], stages, r[9], r[10], r[11], r[12], bool(r[13]), r[14]))
        return projects, agents, cards

#!/usr/bin/env python3
"""
session_discovery.py — 会话发现器（R2：评测入口重构）。

统一会话模型（多 agent × 两类会话）：
  - agent:  "claude" | "dsh" | "airlab" | "eval"
  - state:  "live"（正在发送）| "history"（已发生）
  - source: "projects"（Claude Code 历史 JSONL）/ "session_root"（本评测落盘）/
            "eval_run"（eval_server 发起的运行中）/ ...
  - path / query（首条真实用户指令摘要）/ model / started_at / updated_at / size

发现来源：
  1. Claude Code 历史：~/.claude/projects/<slug>/<sessionId>.jsonl（可配置目录）
  2. 本评测落盘：session_root/*/{session.jsonl,raw.jsonl}（claude_backend/dsh_backend 产出）
  3. eval 发起：eval_server 内存中的运行中任务（由 EvalServer 传入，本模块不感知）

用法：
    from session_discovery import discover_all, SessionInfo
    sessions = discover_all(projects_dir=None, session_root="results/board")
"""

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class SessionInfo:
    session_id: str
    agent: str = "claude"          # claude / dsh / airlab / eval
    state: str = "history"         # live / history
    source: str = ""               # projects / session_root / eval_run / ...
    path: str | None = None
    query: str | None = None       # 首条真实用户指令摘要
    model: str | None = None
    started_at: int | None = None  # 毫秒 epoch
    updated_at: int | None = None
    size: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 工具 ----------

def _first_user_query(jsonl_path: Path, limit: int = 160) -> str | None:
    """读 Claude Code JSONL 前若干行，取第一条真实用户指令摘要（排除 / 命令与系统注入）。"""
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(400):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                text = text.strip()
                if text and not text.startswith("/") and not text.startswith("<"):
                    return text[:limit]
    except OSError:
        return None
    return None


# ---------- 来源 1：Claude Code projects ----------

def discover_claude_projects(projects_dir: str | Path | None = None,
                             limit: int = 300) -> list[SessionInfo]:
    """扫描 Claude Code 历史会话目录 ~/.claude/projects/*/<sessionId>.jsonl。

    排除子目录（subagents/、tool-results/ 等）中的文件——只取顶层会话日志。
    """
    base = Path(projects_dir) if projects_dir else Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return []
    out: list[SessionInfo] = []
    for slug in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not slug.is_dir():
            continue
        for log in slug.glob("*.jsonl"):
            try:
                mtime = int(log.stat().st_mtime * 1000)
                size = log.stat().st_size
            except OSError:
                continue
            out.append(SessionInfo(
                session_id=log.stem,
                agent="claude",
                state="history",
                source="projects",
                path=str(log),
                query=_first_user_query(log),
                updated_at=mtime,
                size=size,
                extra={"slug": slug.name},
            ))
            if len(out) >= limit:
                return out
    return out


# ---------- 来源 2：本评测落盘 session_root ----------

def discover_session_root(root: str | Path | None) -> list[SessionInfo]:
    """扫描本评测落盘目录 root/*/{session.jsonl,raw.jsonl}。

    - 有 raw.jsonl（stream-json 原始行）→ claude 来源
    - 仅有 session.jsonl（DSH 格式统一事件）→ dsh 来源
    """
    if not root:
        return []
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[SessionInfo] = []
    for sess_dir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not sess_dir.is_dir():
            continue
        sid = sess_dir.name
        raw = sess_dir / "raw.jsonl"
        log = sess_dir / "session.jsonl"
        # 优先 session.jsonl（统一事件流，replay 用 DSH 口径直接解析）；
        # raw.jsonl 是 stream-json 原始行，与 claude_jsonl_to_events（Claude Code JSONL）口径不同，仅兜底。
        if log.is_file():
            out.append(SessionInfo(
                session_id=sid, agent="claude", state="history", source="session_root",
                path=str(log),
                query=_session_query_from_log(log),
                updated_at=_mtime_ms(log), size=log.stat().st_size,
                extra={"kind": "dsh_unified"},
            ))
        elif raw.is_file():
            out.append(SessionInfo(
                session_id=sid, agent="claude", state="history", source="session_root",
                path=str(raw),
                query=_session_query_from_log(log) or _first_user_query(raw),
                updated_at=_mtime_ms(raw), size=raw.stat().st_size,
                extra={"kind": "claude_stream_json"},
            ))
    return out


def _session_query_from_log(log: Path) -> str | None:
    """从 DSH 格式 session.jsonl 里取第一条 user/message 的文本（query 摘要）。"""
    if not log.is_file():
        return None
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(400):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user/message":
                    continue
                content = (obj.get("data") or {}).get("content") or []
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                full = "\n".join(texts).strip()
                if full:
                    return full[:160]
    except OSError:
        return None
    return None


def _mtime_ms(p: Path) -> int:
    try:
        return int(p.stat().st_mtime * 1000)
    except OSError:
        return 0


# ---------- 来源 3：受限样本目录（用户配置，多类型混合） ----------

def _detect_agent(path: Path) -> str | None:
    """读首行判断会话类型：dsh（DSH JSONL 首行 type=session）/ claude（Claude Code JSONL）/ airlab（文本日志）/ codemaker（SQLite 会话库）。"""
    if str(path).lower().endswith(".db"):
        from codemaker_backend import is_codemaker_db
        return "codemaker" if is_codemaker_db(path) else None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first:
        return None
    try:
        obj = json.loads(first)
    except json.JSONDecodeError:
        return "airlab"
    if isinstance(obj, dict) and obj.get("type") == "session":
        return "dsh"
    return "claude"


def _first_text_line(path: Path, limit: int = 160) -> str | None:
    """airlab 文本日志：取第一条非空、非纯符号行作为 query 摘要。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not set(line) <= set("-=#*_| \t"):
                    return line[:limit]
    except OSError:
        return None
    return None


# ---------- 来源 4：Codemaker 会话库（opencode.db，单库多会话） ----------

def discover_codemaker_db(db_path: str | Path, limit: int = 100) -> list[SessionInfo]:
    """从 Codemaker 会话库（opencode.db）发现全部会话，每会话一个 SessionInfo。"""
    from codemaker_backend import CodemakerDB
    db = CodemakerDB(db_path)
    out: list[SessionInfo] = []
    for s in db.list_sessions():
        out.append(SessionInfo(
            session_id=s["session_id"],
            agent="codemaker",
            state="history",
            source="codemaker",
            path=str(db.db_path),
            query=(s.get("title") or "")[:160],
            model=s.get("model"),
            started_at=s.get("started_at"),
            updated_at=s.get("updated_at"),
            size=_size_of(db.db_path),
            extra={"directory": s.get("directory"), "title": s.get("title"),
                   "cost_usd": s.get("cost_usd"), "archived_at": s.get("archived_at")},
        ))
        if len(out) >= limit:
            break
    return out


def _size_of(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def discover_samples_dir(samples_dir: str | Path | None, limit: int = 50) -> list[SessionInfo]:
    """扫描受限样本目录：目录下每个会话文件自动识别 agent（claude/dsh/airlab/codemaker）。

    支持的扩展名：.jsonl / .json（claude、dsh）、.log / .txt（airlab）、
    .db（codemaker 会话库，一个库展开为多个会话）。
    """
    if not samples_dir:
        return []
    base = Path(samples_dir)
    if not base.is_dir():
        return []
    out: list[SessionInfo] = []
    files = [p for p in base.iterdir() if p.is_file()
             and p.suffix.lower() in (".jsonl", ".json", ".log", ".txt", ".db")]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for p in files:
        agent = _detect_agent(p)
        if agent is None:
            continue
        if agent == "codemaker":
            out.extend(discover_codemaker_db(p, limit=limit))
        else:
            if agent == "claude":
                query = _first_user_query(p)
            elif agent == "dsh":
                query = _session_query_from_log(p)
            else:
                query = _first_text_line(p)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append(SessionInfo(
                session_id=p.stem, agent=agent, state="history", source="samples",
                path=str(p), query=query, updated_at=_mtime_ms(p), size=size,
            ))
        if len(out) >= limit:
            break
    return out


def discover_single_path(path: str | Path) -> SessionInfo | None:
    """解析单个会话文件路径（手动指定），自动识别 agent 类型。"""
    p = Path(path)
    if not p.is_file():
        return None
    agent = _detect_agent(p)
    if agent is None:
        return None
    if agent == "codemaker":
        # 会话库含多会话：取最新一个作为代表（完整列表走 discover_codemaker_db）
        infos = discover_codemaker_db(p, limit=1)
        return infos[0] if infos else None
    if agent == "claude":
        query = _first_user_query(p)
    elif agent == "dsh":
        query = _session_query_from_log(p)
    else:
        query = _first_text_line(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return SessionInfo(
        session_id=p.stem, agent=agent, state="history", source="manual",
        path=str(p), query=query, updated_at=_mtime_ms(p), size=size,
    )


# ---------- 合并 ----------

def _dedupe(sessions: list[SessionInfo]) -> list[SessionInfo]:
    """按 (agent, session_id) 去重，保留第一个（来源优先级由调用方排序决定）。"""
    seen = set()
    out = []
    for s in sessions:
        key = (s.agent, s.session_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def discover_all(projects_dir: str | Path | None = None,
                 session_root: str | Path | None = None,
                 eval_runs: dict | None = None,
                 samples_dir: str | Path | None = None,
                 codemaker_db: str | Path | None = None,
                 batch_root: str | Path | None = None,
                 limit: int = 300) -> list[SessionInfo]:
    """合并全部会话来源：受限样本目录（若配置，则只用它）+ Claude projects +
    本评测落盘 + Codemaker 会话库（若指定）+ eval 发起（运行中）。

    samples_dir 配置时：只从该目录发现（用户指定的多类型会话集合，如 10 个样例），
    不再全扫 projects/session_root。eval_runs（运行中）始终并入。

    codemaker_db: Codemaker opencode.db 路径；None 时自动探测默认位置
    （~/.local/share/codemaker/opencode.db），存在才并入。

    eval_runs: {"<session_id>": {"agent","task_id","query","started_at"}}（eval_server 传入）。
    """
    sessions: list[SessionInfo] = []

    # 运行中（eval 发起）优先
    for sid, info in (eval_runs or {}).items():
        sessions.append(SessionInfo(
            session_id=sid,
            agent=info.get("agent", "eval"),
            state="live",
            source="eval_run",
            query=info.get("query") or info.get("task_id"),
            model=info.get("model"),
            started_at=info.get("started_at"),
            updated_at=info.get("updated_at"),
            extra={"task_id": info.get("task_id")},
        ))

    if samples_dir:
        sessions += discover_samples_dir(samples_dir, limit=limit)
    else:
        sessions += discover_session_root(session_root)

    # 批量评测落盘（results/batch）也并入会话评测列表（独立于 samples_dir 限制）
    sessions += discover_session_root(batch_root)

    # 真实 Claude Code projects 始终并入（独立于 samples_dir 限制——samples 只是示例，
    # 用户本机全部真实会话日志都要进评测系统）
    sessions += discover_claude_projects(projects_dir, limit=limit)

    # Codemaker 会话库：显式指定或默认位置存在时并入（独立于 samples_dir 限制）
    cm_db = codemaker_db
    if cm_db is None:
        from codemaker_backend import _default_db_path
        cand = _default_db_path()
        if cand.is_file():
            cm_db = cand
    if cm_db:
        from codemaker_backend import is_codemaker_db
        if is_codemaker_db(cm_db):
            sessions += discover_codemaker_db(cm_db, limit=limit)
    # 排序：live 在前，其余按 updated_at 倒序
    sessions = _dedupe(sessions)
    sessions.sort(key=lambda s: (0 if s.state == "live" else 1, -(s.updated_at or 0)))
    return sessions


# ---------- 独立运行 ----------

if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="发现可评测会话")
    parser.add_argument("--projects", default=None, help="Claude Code projects 目录（缺省 ~/.claude/projects）")
    parser.add_argument("--session-root", default="results/board", help="本评测落盘根目录")
    args = parser.parse_args()
    for s in discover_all(args.projects, args.session_root):
        line = (f"[{s.state:7}] {s.agent:6} {s.session_id[:40]:40} {s.source:14} "
                f"{(s.query or '')[:40]}")
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))

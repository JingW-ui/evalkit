#!/usr/bin/env python3
"""
eval_store.py — SQLite 评测记录存储（M1：替换 results/eval_records.json）。

背景：原 EvalRecords 用 JSON 文件，多进程读写靠「写前重读 + 读前重载」缓解竞态，
但读-改-写非原子，并发写会丢记录/损坏文件。本模块换成 SQLite（WAL 模式），
解决并发安全，并支持后续 M2（批次）与 M3（统计/人工复核）在 SQL 层扩展。

表：executions（session_id 主键 = 同 session 覆盖语义；run_id/task_id/run_idx
供 M2 批量评测关联，当前可空；level_auto/success_auto/review_* 供 M3 人工复核）。
"""

import json
import sqlite3
import threading
from pathlib import Path

_DEFAULT_DB = Path(__file__).parent / "results" / "eval_records.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT,
    task_id TEXT,
    run_idx INTEGER DEFAULT 1,
    agent TEXT,
    skill_expected TEXT,
    level TEXT,
    level_source TEXT,
    level_reason TEXT,
    success INTEGER,
    success_by TEXT,
    tool_calls_total INTEGER,
    input_tokens INTEGER,
    cost_cny REAL,
    human_interventions INTEGER,
    turn_end_reason TEXT,
    query TEXT,
    level_auto TEXT,
    success_auto INTEGER,
    review_status TEXT DEFAULT 'unreviewed',
    review_note TEXT,
    reviewed_at INTEGER,
    _at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_exec_agent_level ON executions(agent, level);
"""

_FIELDS = [
    "session_id", "run_id", "task_id", "run_idx", "agent", "skill_expected",
    "level", "level_source", "level_reason", "success", "success_by",
    "tool_calls_total", "input_tokens", "cost_cny", "human_interventions",
    "turn_end_reason", "query", "level_auto", "success_auto",
    "review_status", "review_note", "reviewed_at", "_at",
]


class EvalStore:
    """SQLite 评测记录存储（线程安全，WAL 模式）。"""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(self, record: dict) -> None:
        """写入/覆盖一条记录（session_id 相同则覆盖）。"""
        row = [record.get(f) for f in _FIELDS]
        cols = ", ".join(_FIELDS)
        placeholders = ", ".join("?" for _ in _FIELDS)
        sql = f"INSERT OR REPLACE INTO executions ({cols}) VALUES ({placeholders})"
        with self._lock:
            self._conn.execute(sql, row)
            self._conn.commit()

    def all(self) -> list:
        """全部记录，按 _at 倒序（最新在前），success 还原为 bool。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions ORDER BY _at DESC").fetchall()
        recs = []
        for r in rows:
            d = dict(r)
            if d.get("success") is not None:
                d["success"] = bool(d["success"])
            recs.append(d)
        return recs

    def matrix(self) -> dict:
        """能力画像（agent×L1-L4 SR + 平均指标）+ 明细列表（口径与旧 JSON 版一致）。"""
        recs = self.all()
        cells = {}
        for r in recs:
            key = (r.get("agent"), r.get("level"))
            c = cells.setdefault(key, {"count": 0, "success": 0,
                                       "tool_calls": 0, "input_tokens": 0,
                                       "cost_cny": 0.0, "human_interventions": 0,
                                       "end_reasons": {}})
            c["count"] += 1
            c["success"] += 1 if r.get("success") else 0
            c["tool_calls"] += r.get("tool_calls_total") or 0
            c["input_tokens"] += r.get("input_tokens") or 0
            c["cost_cny"] += r.get("cost_cny") or 0
            c["human_interventions"] += r.get("human_interventions") or 0
            end = r.get("turn_end_reason")
            c["end_reasons"][end] = c["end_reasons"].get(end, 0) + 1
        portrait = []
        for (agent, level), c in sorted(cells.items()):
            portrait.append({
                "agent": agent, "level": level, "count": c["count"],
                "success": c["success"],
                "sr": round(c["success"] / c["count"], 3) if c["count"] else None,
                "avg_tools": round(c["tool_calls"] / c["count"], 1) if c["count"] else 0,
                "avg_tokens_in": round(c["input_tokens"] / c["count"]) if c["count"] else 0,
                "avg_cost_cny": round(c["cost_cny"] / c["count"], 4) if c["count"] else 0,
                "avg_interventions": round(c["human_interventions"] / c["count"], 2) if c["count"] else 0,
            })
        return {"portrait": portrait, "records": recs}


def migrate_json_to_db(json_path=None, db_path=None) -> int:
    """把 results/eval_records.json 的历史记录一次性导入 SQLite，返回导入条数。

    幂等：同 session_id 覆盖，可重复执行。
    """
    json_path = Path(json_path) if json_path else _DEFAULT_DB.parent / "eval_records.json"
    if not json_path.is_file():
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        recs = json.load(f)
    if not isinstance(recs, list):
        return 0
    store = EvalStore(db_path)
    try:
        for r in recs:
            if isinstance(r, dict):
                store.upsert(r)
    finally:
        store.close()
    return len(recs)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = migrate_json_to_db()
    print(f"迁移完成：{n} 条记录 → {_DEFAULT_DB}")

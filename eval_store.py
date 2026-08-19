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
import time
from pathlib import Path

_DEFAULT_DB = Path(__file__).parent / "results" / "eval_records.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT,
    task_id TEXT,
    run_idx INTEGER DEFAULT 1,
    agent TEXT,
    model TEXT,
    skill_expected TEXT,
    level TEXT,
    level_source TEXT,
    level_reason TEXT,
    success INTEGER,
    success_by TEXT,
    tool_calls_total INTEGER,
    tool_success INTEGER,
    tool_fail INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cost_cny REAL,
    duration_ms INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_exec_task ON executions(task_id, run_idx);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT,
    level TEXT,
    skill_expected TEXT,
    query TEXT,
    device_var TEXT,
    expected_answer TEXT,
    tools_required TEXT,
    accept_criteria TEXT,
    success_condition TEXT,
    prep TEXT,
    version TEXT,
    repeat INTEGER DEFAULT 1,
    note TEXT,
    enabled INTEGER DEFAULT 1,
    created_at INTEGER,
    updated_at INTEGER
);
"""

_FIELDS = [
    "session_id", "run_id", "task_id", "run_idx", "agent", "model", "skill_expected",
    "level", "level_source", "level_reason", "success", "success_by",
    "tool_calls_total", "tool_success", "tool_fail", "input_tokens",
    "output_tokens", "cache_read_tokens", "cost_cny", "duration_ms",
    "human_interventions", "turn_end_reason", "query", "level_auto",
    "success_auto", "review_status", "review_note", "reviewed_at", "_at",
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
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """老库补列（幂等）：executions / tasks 缺的列 ALTER TABLE ADD COLUMN。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(executions)")}
        for col, typ in {
            "tool_success": "INTEGER", "tool_fail": "INTEGER",
            "output_tokens": "INTEGER", "cache_read_tokens": "INTEGER",
            "duration_ms": "INTEGER", "model": "TEXT",
        }.items():
            if col not in cols:
                self._conn.execute(f"ALTER TABLE executions ADD COLUMN {col} {typ}")
        # tasks 表新增字段（题库试卷化）
        tcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for col, typ in {
            "title": "TEXT", "device_var": "TEXT",
            "expected_answer": "TEXT", "tools_required": "TEXT",
            "accept_criteria": "TEXT", "prep": "TEXT", "version": "TEXT",
            "enabled": "INTEGER",
        }.items():
            if col not in tcols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        self._conn.commit()

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

    def get(self, session_id: str) -> dict | None:
        """单条记录（success 还原 bool）；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("success") is not None:
            d["success"] = bool(d["success"])
        return d

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

    @staticmethod
    def _mean_sd(values) -> tuple:
        """均值 + 样本标准差（n<2 时 sd=None；忽略 None）。"""
        xs = [v for v in values if v is not None]
        n = len(xs)
        if n == 0:
            return None, None
        mean = sum(xs) / n
        if n < 2:
            return mean, None
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        return mean, var ** 0.5

    @staticmethod
    def _wilson_lower(success: int, n: int, z: float = 1.96) -> float | None:
        """二项成功率的 95% Wilson 置信区间下界（n=0 返回 None）。"""
        if n <= 0:
            return None
        p = success / n
        z2 = z * z
        denom = 1 + z2 / n
        center = (p + z2 / (2 * n)) / denom
        half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
        return max(0.0, center - half)

    def stats(self) -> dict:
        """统计总览：按 task 聚合 n 次执行，SR + 均值±σ（样本标准差）。

        返回 {"rows": [...]}，每行含 task_id/skill_expected/level/agent + n/success/sr +
        duration_ms/duration_sd + cost_cny/cost_sd/cost_sum + tool_sr/tool_sr_sd +
        tool_calls/tool_calls_sd + input_tokens/input_sd + human_interventions。
        """
        recs = [r for r in self.all()
                if r.get("task_id") and r.get("review_status") != "invalid"]
        task_map = {t["task_id"]: t for t in self.list_tasks()}
        groups = {}
        for r in recs:
            key = (r.get("task_id"), r.get("skill_expected"), r.get("level"), r.get("agent"), r.get("model"))
            groups.setdefault(key, []).append(r)
        rows = []
        for key, rs in sorted(groups.items(), key=lambda kv: tuple(str(x or "") for x in kv[0])):
            task_id, skill, level, agent, model = key
            n = len(rs)
            success = sum(1 for r in rs if r.get("success"))
            tool_srs = []
            for r in rs:
                s = r.get("tool_success") or 0
                f = r.get("tool_fail") or 0
                if s + f > 0:
                    tool_srs.append(s / (s + f))
            tool_sr, tool_sr_sd = self._mean_sd(tool_srs)
            dur, dur_sd = self._mean_sd([r.get("duration_ms") for r in rs])
            cost, cost_sd = self._mean_sd([r.get("cost_cny") for r in rs])
            tools, tools_sd = self._mean_sd([r.get("tool_calls_total") for r in rs])
            tin, tin_sd = self._mean_sd([r.get("input_tokens") for r in rs])
            inter, _ = self._mean_sd([r.get("human_interventions") for r in rs])
            ci_lower = self._wilson_lower(success, n)
            ac = (task_map.get(task_id) or {}).get("accept_criteria") or {}
            veto = bool(ac.get("veto")) if isinstance(ac, dict) else (level == "L4")
            veto_hit = bool(veto and success < n)
            rows.append({
                "task_id": task_id, "skill_expected": skill, "level": level, "agent": agent, "model": model,
                "n": n, "success": success,
                "sr": round(success / n, 3) if n else None,
                "duration_ms": round(dur, 1) if dur is not None else None,
                "duration_sd": round(dur_sd, 1) if dur_sd is not None else None,
                "cost_cny": round(cost, 4) if cost is not None else None,
                "cost_sd": round(cost_sd, 4) if cost_sd is not None else None,
                "cost_sum": round(sum(r.get("cost_cny") or 0 for r in rs), 4),
                "tool_sr": round(tool_sr, 3) if tool_sr is not None else None,
                "tool_sr_sd": round(tool_sr_sd, 3) if tool_sr_sd is not None else None,
                "tool_calls": round(tools, 1) if tools is not None else None,
                "tool_calls_sd": round(tools_sd, 1) if tools_sd is not None else None,
                "input_tokens": round(tin) if tin is not None else None,
                "input_sd": round(tin_sd) if tin_sd is not None else None,
                "human_interventions": round(inter, 2) if inter is not None else None,
                "ci_lower": round(ci_lower, 3) if ci_lower is not None else None,
                "veto": veto, "veto_hit": veto_hit,
            })
        return {"rows": rows}

    def review(self, session_id: str, level=None, success=None, note=None, reset=False,
               defense=None) -> dict | None:
        """人工复核/答辩：修正 level/success（保留自动值，留痕）。

        defense 归因结论：'fail'（计入失败，不改值）、'pass'（机器误判，纠正 success=1）、
        'invalid'（题目硬伤，排除统计）。reset=True 重置为自动值。
        返回更新后的记录；session 不存在返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            if reset:
                d["level"] = d.get("level_auto")
                d["success"] = d.get("success_auto")
                d["review_status"] = "unreviewed"
            elif defense == "invalid":
                d["review_status"] = "invalid"
            elif defense == "pass":
                if d.get("success_auto") is None:
                    d["success_auto"] = d.get("success")
                d["success"] = 1
                d["review_status"] = "corrected"
            elif defense == "fail":
                d["review_status"] = "reviewed"
            else:
                changed = False
                if level is not None:
                    if d.get("level_auto") is None:
                        d["level_auto"] = d.get("level")
                    d["level"] = level
                    changed = True
                if success is not None:
                    if d.get("success_auto") is None:
                        d["success_auto"] = d.get("success")
                    d["success"] = 1 if success else 0
                    changed = True
                d["review_status"] = "corrected" if changed else "reviewed"
            if note is not None:
                d["review_note"] = note
            d["reviewed_at"] = int(time.time() * 1000)
            # 直接写入（不调 self.upsert，避免同线程嵌套 acquire 非重入锁死锁）
            cols = ", ".join(_FIELDS)
            placeholders = ", ".join("?" for _ in _FIELDS)
            sql = f"INSERT OR REPLACE INTO executions ({cols}) VALUES ({placeholders})"
            self._conn.execute(sql, [d.get(f) for f in _FIELDS])
            self._conn.commit()
        out = {k: d.get(k) for k in _FIELDS}
        if out.get("success") is not None:
            out["success"] = bool(out["success"])
        return out

    # ---- 任务定义（tasks 表） ----

    _TASK_FIELDS = ["task_id", "title", "level", "skill_expected", "query",
                    "device_var", "expected_answer", "tools_required",
                    "accept_criteria", "success_condition", "prep", "version",
                    "repeat", "note", "enabled", "created_at", "updated_at"]

    @staticmethod
    def _parse_task_json(d: dict) -> dict:
        """tasks 的 JSON 串字段还原为 dict/list（容错；缺省给空结构）。"""
        for k, empty in (("success_condition", {}), ("expected_answer", {}),
                         ("accept_criteria", {}), ("tools_required", [])):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    d[k] = empty
            elif v is None:
                d[k] = empty
        return d

    def list_tasks(self) -> list:
        """列任务定义（JSON 串字段还原为 dict/list）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        out = []
        for r in rows:
            out.append(self._parse_task_json(dict(r)))
        return out

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return self._parse_task_json(dict(row))

    def upsert_task(self, task: dict) -> dict:
        """创建/更新任务（task_id 主键）；dict/list 字段序列化为 JSON 串。"""
        t = dict(task)
        for k in ("success_condition", "expected_answer", "accept_criteria"):
            if isinstance(t.get(k), (dict, list)):
                t[k] = json.dumps(t[k], ensure_ascii=False)
        if isinstance(t.get("tools_required"), list):
            t["tools_required"] = json.dumps(t["tools_required"], ensure_ascii=False)
        now = int(time.time() * 1000)
        t["updated_at"] = now
        if t.get("created_at") is None:
            t["created_at"] = now
        if t.get("enabled") is None:
            t["enabled"] = 1
        cols = ", ".join(self._TASK_FIELDS)
        ph = ", ".join("?" for _ in self._TASK_FIELDS)
        sql = f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({ph})"
        with self._lock:
            self._conn.execute(sql, [t.get(f) for f in self._TASK_FIELDS])
            self._conn.commit()
        return t

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def generate_tasks(self, domains: list, params: dict = None, count: int = 1) -> list:
        """按 skill(域) 生成 L1-L4 任务并写入 tasks 表，返回生成的任务列表。"""
        from task_gen import build_tasks
        tasks = build_tasks(domains, params or {}, count)
        for t in tasks:
            self.upsert_task(t)
        return tasks

    def import_papers(self, papers_dir=None) -> list:
        """装载 papers/*.yaml（题库权威源）并 upsert 到 tasks 表，返回导入的任务列表。"""
        from task_gen import load_papers
        tasks = load_papers(papers_dir)
        for t in tasks:
            self.upsert_task(t)
        return tasks


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

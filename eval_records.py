#!/usr/bin/env python3
"""
eval_records.py — 评测记录与 L1-L4 判级（R4：评测矩阵）。

判级策略（评测完成时自动判定）：
  1. task 匹配优先：query 命中 tasks/*.json（task.query 关键词 / task.note 引用 session_id）
     → level 取 task.level，成功判定走 task 校验器（evidence_anchor / negative_honesty / file_exists）
  2. 未匹配 → 自动推断：L4 启发式（query 含「不存在/无法/404」类词）→ classify_level 规则兜底
  3. 成功判定：有 task 用校验器；无 task 用结束原因兜底（completed→成功，error/interrupted/max-tokens→失败）

评测记录持久化到 results/eval_records.json；矩阵聚合 = 能力画像（agent×L1-L4 SR+平均指标）+ 明细。
"""

import json
import time
from pathlib import Path

# ---------- task 加载与匹配 ----------

_TASKS_CACHE = None
_TASKS_MTIME = 0


def load_tasks(tasks_dir: str | Path = None) -> list:
    """加载 evalkit 任务（tasks/*.json + tasks/gen/*.json 生成物；带 mtime 缓存）。"""
    global _TASKS_CACHE, _TASKS_MTIME
    base = Path(tasks_dir) if tasks_dir else Path(__file__).parent / "tasks"
    if not base.is_dir():
        return []
    json_files = list(base.glob("*.json"))
    # 生成物子目录（task_gen / eval_batch gen 的产物）也纳入任务库
    gen_dir = base / "gen"
    if gen_dir.is_dir():
        json_files += list(gen_dir.glob("*.json"))
    mtime = max((p.stat().st_mtime for p in json_files), default=0)
    if _TASKS_CACHE is not None and mtime == _TASKS_MTIME:
        return _TASKS_CACHE
    tasks = []
    for tf in sorted(json_files):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                t = json.load(f)
            t["_file"] = str(tf)
            tasks.append(t)
        except Exception:
            pass
    _TASKS_CACHE, _TASKS_MTIME = tasks, mtime
    return tasks


def match_task(query: str, session_id: str, tasks: list = None) -> dict | None:
    """query 包含 task.query 关键词，或 task.note 引用该 session_id（要求 session_id 足够长防误匹配）。"""
    if not query and not session_id:
        return None
    for t in (tasks if tasks is not None else load_tasks()):
        tq = t.get("query", "")
        note = t.get("note", "")
        if tq and query and tq in query:
            return t
        if note and session_id and len(session_id) >= 8 and session_id in note:
            return t
    return None


# ---------- L4 启发式 ----------

_L4_MARKERS = ["不存在", "无法", "不可能", "做不到", "404", "不存在的设备", "不存在的文件",
               "not found", "does not exist", "无此", "查无", "无效设备", "无效的"]


def _l4_hint(query: str) -> bool:
    q = (query or "").lower()
    return any(m in q for m in _L4_MARKERS)


# ---------- 成功判定 ----------

def _validate_evidence_anchor(task: dict, text: str) -> tuple:
    cond = task.get("success_condition", {})
    anchors = cond.get("anchors", [])
    threshold = cond.get("threshold", max(1, len(anchors) // 2 + 1))
    hits = [a for a in anchors if a in text]
    return len(hits) >= threshold, f"evidence_anchor {len(hits)}/{threshold}"


def _validate_negative_honesty(task: dict, text: str) -> tuple:
    cond = task.get("success_condition", {})
    neg = cond.get("negation_markers", [])
    fake = cond.get("fake_success_markers", [])
    honest = any(m in text for m in neg)
    faked = any(m in text for m in fake)
    return honest and not faked, "negative_honesty"


def _validate_file(task: dict, metrics: dict) -> tuple:
    # 简化：文件校验看工具序列有无 Write，或结束原因兜底
    writes = [k for k in (metrics.get("tool_calls_by_name") or {}) if "write" in k.lower()]
    ok = bool(writes) or (metrics.get("turn_end_reason") == "completed")
    return ok, "file_exists(近似)"


# ---------- 主判定 ----------

def judge_eval(query: str, session_id: str, metrics: dict, assistant_text: str = "",
               tasks: list = None) -> dict:
    """
    评测完成时判定：level + 成功。

    Args:
        query: 会话首条用户指令（判级与 task 匹配用）。
        session_id: 会话 id。
        metrics: EventMetrics 快照。
        assistant_text: assistant 文本拼接（锚点匹配用）。
        tasks: 显式 task 列表（批量评测时传入本次执行的任务，避免跨任务误匹配）。

    Returns:
        {level, level_source, level_reason, success, success_by}
    """
    end = metrics.get("turn_end_reason")
    task = match_task(query, session_id, tasks)

    if task is not None:
        level = task.get("level", "L?")
        ctype = (task.get("success_condition") or {}).get("type", "evidence_anchor")
        if ctype == "negative_honesty":
            success, by = _validate_negative_honesty(task, assistant_text)
        elif ctype == "file_exists":
            success, by = _validate_file(task, metrics)
        else:
            success, by = _validate_evidence_anchor(task, assistant_text)
        return {
            "level": level, "level_source": "task",
            "level_reason": f"task {task.get('task_id')} · {task.get('_file', '')}",
            "success": success, "success_by": by,
        }

    # 自动推断：L4 启发式 → 规则
    if _l4_hint(query):
        level, src, reason = "L4", "auto", "query 含不可能任务特征词"
    else:
        try:
            from classify_level import classify_level
            cls = classify_level({
                "total_tokens": (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0),
                "tool_calls_total": metrics.get("tool_calls_total") or 0,
                "tool_dist": metrics.get("tool_calls_by_name") or {},
                "user_turns": metrics.get("user_turns") or 0,
                "human_interventions": metrics.get("human_interventions") or 0,
                "skill_loaded": metrics.get("skill_loaded"),
            }, use_llm=False)
            level = cls.get("level", "L?")
            reason = cls.get("reason", "")
        except Exception:
            level, reason = "L?", ""
        src = "auto"
    success = end == "completed"
    return {
        "level": level, "level_source": src, "level_reason": reason,
        "success": success, "success_by": f"end_reason={end}",
    }


# ---------- 评测记录存储 ----------

class EvalRecords:
    """评测记录：内存 + 落盘 results/eval_records.json。"""

    def __init__(self, path: str | Path = None):
        self.path = Path(path) if path else Path(__file__).parent / "results" / "eval_records.json"
        self._records: list = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                with open(self.path, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
        except Exception:
            self._records = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, record: dict) -> None:
        record["_at"] = int(time.time() * 1000)
        # 多进程共享文件（eval_server + eval_batch 同时写）：写前重新加载，
        # 避免各自内存中的完整列表互相覆盖、复活对方已删除的旧记录。
        self._load()
        # 同 session 覆盖旧记录
        self._records = [r for r in self._records if r.get("session_id") != record.get("session_id")]
        self._records.append(record)
        self.save()

    def all(self, reload: bool = True) -> list:
        """全部记录；reload=True 时先重读文件（多进程共享，eval_batch 写入立即可见）。"""
        if reload:
            self._load()
        return list(self._records)

    def matrix(self) -> dict:
        """能力画像（agent×L1-L4 SR + 平均指标）+ 明细列表。读取前重载文件。"""
        self._load()
        recs = self._records
        # 画像
        cells = {}
        for r in recs:
            key = (r.get("agent"), r.get("level"))
            c = cells.setdefault(key, {"count": 0, "success": 0,
                                       "tool_calls": 0, "input_tokens": 0, "cost_cny": 0.0,
                                       "human_interventions": 0, "end_reasons": {}})
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
        return {"portrait": portrait, "records": list(reversed(recs))}

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
import operator
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


# ---------- 组合规则树评估器（success_condition = all/any/not + 原子条件） ----------

def _strip_mcp(name: str) -> str:
    """剥 MCP 前缀：mcp__<server>__xxx → xxx；本地工具（Bash/Grep/Read/...）保留原名。"""
    if not name:
        return ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _collect_tools(metrics: dict) -> dict:
    """从 metrics.tasks[].tools[] 提取工具链证据：归一化名 → [{args,result,ok}, ...]。"""
    tools: dict = {}
    for t in (metrics.get("tasks") or []):
        for tool in (t.get("tools") or []):
            name = _strip_mcp(tool.get("name", ""))
            if name:
                tools.setdefault(name, []).append(tool)
    return tools


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _evaluate_atomic(rule: dict, ctx: dict) -> bool:
    """评估原子条件。ctx = {metrics, text, tools}。"""
    metrics = ctx["metrics"]
    text = ctx["text"]
    tools = ctx["tools"]
    rtype = rule.get("type")

    if rtype == "tool_called":
        name = _strip_mcp(rule.get("tool") or "")
        if not name:
            return False
        calls = tools.get(name, [])
        if not calls:
            return False
        if rule.get("args_contains") and not any(
                rule["args_contains"] in (c.get("args") or "") for c in calls):
            return False
        if rule.get("min_calls") is not None and len(calls) < rule["min_calls"]:
            return False
        return True

    if rtype == "tool_result":
        name = _strip_mcp(rule.get("tool") or "")
        keys = _as_list(rule.get("contains") or rule.get("any_of"))
        if not name or not keys:
            return False
        min_hits = rule.get("min_hits", 1)
        hits = sum(1 for c in tools.get(name, [])
                   if any(k in (c.get("result") or "") for k in keys))
        return hits >= min_hits

    if rtype == "tool_ok":
        if rule.get("tool"):
            calls = tools.get(_strip_mcp(rule["tool"]), [])
        else:
            calls = [c for lst in tools.values() for c in lst]
        return sum(1 for c in calls if c.get("ok")) >= (rule.get("min", 1) or 1)

    if rtype == "tool_success_rate":
        total = metrics.get("tool_calls_total") or 0
        success = metrics.get("tool_success") or 0
        if total <= 0:
            return False
        return (success / total) >= (rule.get("min", 0) or 0)

    if rtype == "tool_fail_zero":
        name = _strip_mcp(rule.get("tool") or "")
        if not name:
            return False
        norm: dict = {}
        for k, v in (metrics.get("tool_fail_by_name") or {}).items():
            kk = _strip_mcp(k)
            norm[kk] = norm.get(kk, 0) + v
        return norm.get(name, 0) == 0

    if rtype == "text_contains":
        if _as_list(rule.get("any_of")) and not any(
                k in text for k in _as_list(rule.get("any_of"))):
            return False
        if any(k in text for k in _as_list(rule.get("not_contains"))):
            return False
        return True

    if rtype == "metric":
        name = rule.get("name")
        value = rule.get("value")
        if name is None or value is None:
            return False
        actual = metrics.get(name)
        if actual is None:
            return False
        fn = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
              "<": operator.lt, "=": operator.eq, "==": operator.eq}.get(rule.get("op", ">="))
        if fn is None:
            return False
        try:
            return fn(actual, value)
        except TypeError:
            return False

    if rtype == "file_exists":
        path = rule.get("path")
        if not path:
            return False
        p = Path(path)
        if not p.is_file():
            return False
        if rule.get("min_size") is not None and p.stat().st_size < rule["min_size"]:
            return False
        if rule.get("contains"):
            try:
                if rule["contains"] not in p.read_text(encoding="utf-8", errors="replace"):
                    return False
            except Exception:
                return False
        return True

    return False


def _evaluate_rule(rule: dict, ctx: dict) -> bool:
    """评估规则树（all/any/not 组合 + 原子条件）。"""
    if not isinstance(rule, dict):
        return False
    rtype = rule.get("type")
    if rtype == "all":
        return all(_evaluate_rule(sub, ctx) for sub in (rule.get("rules") or []))
    if rtype == "any":
        return any(_evaluate_rule(sub, ctx) for sub in (rule.get("rules") or []))
    if rtype == "not":
        return not _evaluate_rule(rule.get("rule") or {}, ctx)
    return _evaluate_atomic(rule, ctx)


_ATOMIC_TYPES = {"tool_called", "tool_result", "tool_ok", "tool_success_rate",
                 "tool_fail_zero", "text_contains", "metric", "file_exists"}


def _evaluate_success_condition(sc, metrics, text) -> tuple:
    """评估 success_condition（新规则树/原子条件 + 旧三类型兼容）。返回 (success, by)。"""
    if not isinstance(sc, dict) or not sc.get("type"):
        return True, "no_condition"
    stype = sc.get("type")
    ctx = {"metrics": metrics or {}, "text": text or ""}
    ctx["tools"] = _collect_tools(ctx["metrics"])
    if stype in ("all", "any", "not"):
        return _evaluate_rule(sc, ctx), f"rule_tree:{stype}"
    if stype in _ATOMIC_TYPES:
        return _evaluate_atomic(sc, ctx), f"atomic:{stype}"
    # 旧三类型兼容
    if stype == "negative_honesty":
        return _validate_negative_honesty({"success_condition": sc}, text)
    return _validate_evidence_anchor({"success_condition": sc}, text)


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
        success, by = _evaluate_success_condition(
            task.get("success_condition") or {}, metrics, assistant_text)
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
    """评测记录：SQLite 存储（results/eval_records.db），替代原 JSON 文件。

    接口不变（add/all/matrix），存储层换成 SQLite（WAL 模式，eval_store.EvalStore），
    解决原 JSON 多进程读写竞态（写前重读 + 读前重载只是缓解，非原子）。
    """

    def __init__(self, path: str | Path = None):
        from eval_store import EvalStore
        self.path = Path(path) if path else Path(__file__).parent / "results" / "eval_records.db"
        self._store = EvalStore(self.path)

    def save(self) -> None:
        """兼容旧接口：SQLite 每次 upsert 已即时落盘，此方法为空操作。"""

    def add(self, record: dict) -> None:
        """写入/覆盖一条记录（同 session_id 覆盖），即时落盘。"""
        record["_at"] = int(time.time() * 1000)
        self._store.upsert(record)

    def all(self, reload: bool = True) -> list:
        """全部记录（最新在前）。reload 参数保留以兼容旧接口，SQLite 每次实时查询。"""
        return self._store.all()

    def get(self, session_id):
        """单条记录；不存在返回 None。"""
        return self._store.get(session_id)

    def matrix(self) -> dict:
        """能力画像（agent×L1-L4 SR + 平均指标）+ 明细列表。"""
        return self._store.matrix()

    # ---- 转发到 EvalStore（M3 stats/review + M4 任务定义） ----

    def stats(self) -> dict:
        return self._store.stats()

    def review(self, session_id, level=None, success=None, note=None, reset=False, defense=None):
        return self._store.review(session_id, level, success, note, reset, defense)

    def list_tasks(self) -> list:
        return self._store.list_tasks()

    def get_task(self, task_id):
        return self._store.get_task(task_id)

    def upsert_task(self, task) -> dict:
        return self._store.upsert_task(task)

    def delete_task(self, task_id) -> bool:
        return self._store.delete_task(task_id)

    def import_papers(self, papers_dir=None) -> list:
        return self._store.import_papers(papers_dir)

    def cleanup_invalid(self) -> dict:
        return self._store.cleanup_invalid()

    def close(self) -> None:
        """关闭底层 SQLite 连接（释放文件句柄，便于临时目录/服务退出清理）。"""
        self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

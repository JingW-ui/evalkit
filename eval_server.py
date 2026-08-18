#!/usr/bin/env python3
"""
eval_server.py — evalkit 实时评测看板服务（R2：会话观察器）。

R1 是"评测执行器"（手动开始 → 服务端发起任务）；R2 升级为"会话观察器"：
  - 看板打开即发现可评测会话（/api/sessions），**入口默认开启，选中即评测**；
  - 已发生的会话（history）→ 离线重放：读日志 → 统一事件批量广播 + 一次性指标；
  - 正在发送的会话（live）→ 实时挂接：
      * eval 发起（run_task）事件流（R1 保留）；
      * 外部 Claude 会话 JSONL 尾随（tail_attach.JsonlTails）；
  - 多 agent：claude / dsh / airlab / codemaker / eval，会话列表分组展示，每会话独立评测面板。

端点：
  GET  /                   → web/evalboard.html
  GET  /events             → SSE 流（所有帧带 session_id，前端按面板路由）
  GET  /api/status         → 当前 eval 发起任务状态
  GET  /api/sessions       → 发现的可评测会话列表
  POST /api/start          → 新建评测（eval 发起，次级入口）
  POST /api/stop           → 取消当前 eval 发起任务
  POST /api/attach         → 挂接会话 {session_id, agent, path, mode: live|replay}
  POST /api/detach         → 解除挂接 {session_id}

SSE 帧（data: {json}\n\n，均含 session_id）：
  run/start | event | metrics | warning | run/end | run/cancel | error
  session/added（发现轮询发现新会话时推送）

用法：
    python eval_server.py --port 8090 --web web --session-root results/board
"""

import json
import os
import time
import queue
import argparse
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dsh_backend import EventMetrics
from session_discovery import discover_all, discover_single_path, discover_samples_dir, discover_session_root, _detect_agent, SessionInfo
from eval_records import EvalRecords, judge_eval
from tail_attach import JsonlTails, claude_jsonl_to_events
from codemaker_backend import CodemakerDB, CodemakerEventAdapter, CodemakerTails, is_codemaker_db
from agent_status import AgentStatus


# ---------- SSE hub ----------

class SseHub:
    """内存 SSE 广播：订阅者队列 + 最近帧历史（新客户端重放）。"""

    def __init__(self, history: int = 500):
        self._queues: set = set()
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=history)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            for frame in self._history:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    break
            self._queues.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, frame: dict) -> None:
        with self._lock:
            self._history.append(frame)
            for q in list(self._queues):
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass  # 慢客户端丢帧，不阻塞


# ---------- 评测/观察服务 ----------

def _airlab_started_at(d: dict, path) -> int | None:
    """airlab 文本日志无完整时间戳，用文件 mtime - 耗时 近似会话开始（毫秒 epoch）。"""
    try:
        mtime_ms = int(Path(path).stat().st_mtime * 1000)
        dur_ms = int((d.get("duration_s") or 0) * 1000)
        return mtime_ms - dur_ms
    except Exception:
        return None


def airlab_to_events(d: dict, path=None) -> list:
    """scan_airlab_log 同构 dict → 统一事件流（user/message + tool/call+result + assistant/message + turn/end）。

    与 claude/dsh/codemaker 通道同构，供 TrajectoryView / TasksPanel 消费：
      - user/message：唯一任务 query（prompt）
      - tool/call + tool/result：tool_seq 序列（优先用 tool_times 真实 HH:MM:SS；缺失时 1s 步进）
      - assistant/message：最终文本（has_final_text）
      - turn/end：completion → completed / interrupted
    时间基准：文件 mtime - duration 近似会话开始（与 _airlab_started_at 一致）。
    """
    tasks = d.get("tasks") or []
    tool_seq = d.get("tool_seq") or []
    tool_times = d.get("tool_times") or []   # [(HH:MM:SS, name), ...]
    t0 = _airlab_started_at(d, path)
    if t0 is None:
        t0 = int(time.time() * 1000)

    def _hm_to_ms(ts):
        try:
            h, mi, s = ts.split(":")
            day_s = int(h) * 3600 + int(mi) * 60 + int(s)
            return t0 + day_s * 1000
        except (ValueError, AttributeError):
            return None

    evs = []
    if tasks:
        evs.append({"type": "user/message", "seq": -1, "time": t0,
                    "data": {"content": [{"type": "text", "text": tasks[0].get("query", "")}]}})
    for i, item in enumerate(tool_seq):
        s = str(item)
        name = s.split("(", 1)[0].strip() if "(" in s else s.strip()
        args = s[s.find("(") + 1:s.rfind(")")] if "(" in s else ""
        # 优先真实行时间（tool_times 与 tool_seq 同序）
        t = _hm_to_ms(tool_times[i][0]) if i < len(tool_times) and tool_times[i] else None
        if t is None:
            t = t0 + (i + 1) * 1000
        evs.append({"type": "tool/call", "seq": -1, "time": t,
                    "data": {"name": name, "callId": f"airlab_{i}",
                             "arguments": args[:120]}})
        # tool/result：下一条 tool 行或结尾；用 0.5s 间隔（轨迹展示用，耗时统计不依赖此）
        t_end = _hm_to_ms(tool_times[i + 1][0]) if i + 1 < len(tool_times) and tool_times[i + 1] else None
        if t_end is None:
            t_end = t + 500
        evs.append({"type": "tool/result", "seq": -1, "time": t_end,
                    "data": {"callId": f"airlab_{i}",
                             "message": {"role": "user", "content": [{
                                 "type": "tool-result", "content": "", "isError": False}]}}})
    if tasks:
        completed = tasks[0].get("completion") == "completed"
        t_end = _hm_to_ms(tool_times[-1][0]) if tool_times else None
        t_msg = (t_end or t0 + (len(tool_seq) + 1) * 1000) + 1000
        evs.append({"type": "assistant/message", "seq": -1, "time": t_msg,
                    "data": {"message": {
                        "model": next(iter(d.get("model_usage", {}) or {}), ""),
                        "content": [{"type": "text", "text": tasks[0].get("completion", "")}],
                    }}})
        evs.append({"type": "turn/end", "seq": -1, "time": t_msg + 500,
                    "data": {"reason": {"kind": "completed" if completed else "interrupted"}}})
    return evs


def _airlab_to_metrics(d: dict, path=None) -> dict:
    """session_report.scan_airlab 同构 dict → EventMetrics 快照风格（无逐事件流）。"""
    tok = d.get("tokens", {}) or {}
    tool_dist = d.get("tool_dist", {}) or {}
    tasks = d.get("tasks") or []
    completion = d.get("completion") or (tasks[0].get("completion") if tasks else None)
    # 附加子任务（scan_airlab 的 task_subitems：按 belongs_to 匹配任务 query）
    subitems = d.get("task_subitems") or []
    if subitems and tasks:
        for t in tasks:
            t["subitems"] = [s for s in subitems if s.get("belongs_to") == t.get("query")]
    # 附加工具链（scan 的 tool_seq：名称+参数摘要；无耗时/状态）
    tool_seq = d.get("tool_seq") or []
    if tool_seq and tasks:
        tasks[0].setdefault("tools", [
            {"name": str(item).split("(", 1)[0], "args": str(item)[:120],
             "call_ms": None, "dur_ms": None, "ok": None}
            for item in tool_seq
        ])
    if isinstance(d.get("cost_analysis"), dict) and d["cost_analysis"].get("cost_usd") is not None:
        cost_usd = d["cost_analysis"]["cost_usd"]
    else:
        cost_usd = d.get("cost_usd")   # airlab 顶层直接给 cost 字段
    # 任务列表（TasksPanel 消费：含 query / subitems / tools 工具链）
    task_list = []
    for t in tasks:
        task_list.append({
            "query": t.get("query", ""),
            "tool_calls": t.get("tool_calls") or len(t.get("tools") or []),
            "tokens": t.get("tokens") or {},
            "start_ms": None, "end_ms": None,
            "subitems": t.get("subitems") or [],
            "tools": t.get("tools") or [],
        })
    return {
        "input_tokens": tok.get("input", 0),
        "cache_read_tokens": tok.get("cache_read", 0),
        "cache_write_tokens": tok.get("cache_write", 0),
        "output_tokens": tok.get("output", 0),
        "tool_calls_total": d.get("total_tool_calls") or sum(tool_dist.values()),
        "tool_calls_by_name": tool_dist,
        "user_turns": len(tasks),
        "tasks": task_list,
        "turn_end_reason": completion or None,
        "model": next(iter(d.get("model_usage", {}) or {}), None),
        "skill_loaded": tasks[0].get("skill_loaded") if tasks else None,
        "skill_count": tasks[0].get("skill_count", 0) if tasks else 0,
        "human_interventions": d.get("total_human_interventions", 0),
        "duration_ms": int((d.get("duration_s") or 0) * 1000),
        # 真实耗时（airlab 通道：由日志行时间直接提取，非虚构事件流）
        "llm_ms": d.get("llm_ms", 0.0),
        "tool_ms": d.get("tool_ms", 0.0),
        "human_wait_ms": d.get("human_wait_ms", 0.0),
        "started_at": _airlab_started_at(d, path),   # 由 _replay_airlab 注入 path 近似
        "cost_usd": cost_usd,
        "tool_success": None, "tool_fail": None, "tool_success_rate": None,
    }


class EvalServer:
    def __init__(self, web_dir: str, session_root: str = None,
                 projects_dir: str | None = None, samples_dir: str | None = None,
                 extra_paths: list | None = None, codemaker_db: str | None = None,
                 batch_root: str | None = None):
        self.hub = SseHub()
        self.web_dir = Path(web_dir)
        self.session_root = session_root
        self.projects_dir = projects_dir
        self.samples_dir = samples_dir
        self.extra_paths: list = list(extra_paths or [])
        self.codemaker_db = codemaker_db
        self.batch_root = batch_root
        self._path_map: dict = {}         # session_id -> 文件路径（raw 浏览用）
        self._aliases: dict = {}          # session_id -> 显示名（重命名）
        self._hidden: set = set()         # session_id 隐藏集（删除 = 列表移除，不删文件）
        self._load_meta()
        self._records = EvalRecords()     # 评测记录（L1-L4 判级 + 矩阵）
        self._lock = threading.Lock()
        self._job: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._current: dict = {"state": "idle"}
        self._batch_cancel: threading.Event | None = None
        self._batch_state: dict = {"state": "idle"}
        self._eval_runs: dict = {}          # session_id -> info（运行中 eval 发起）
        self._tails = JsonlTails(on_events=self._on_tail_events)
        self._cm_tails = CodemakerTails(on_events=self._on_tail_events)
        self._tail_metrics: dict = {}     # session_id -> EventMetrics（外部实时会话）
        self._replays: dict = {}            # session_id -> thread
        self._terminals: dict = {}          # pid -> info（打开的外部 claude 对话终端）
        self._agent_status = AgentStatus()

    # ---- 统一发布（帧自动带 session_id） ----

    def _meta_file(self) -> Path:
        return Path(__file__).parent / "conf.json"

    def _load_meta(self) -> None:
        """读会话元数据（重命名别名 / 隐藏集）——conf.json 的 session_aliases/session_hidden。"""
        try:
            with open(self._meta_file(), "r", encoding="utf-8") as f:
                conf = json.load(f)
            self._aliases = conf.get("session_aliases", {}) or {}
            self._hidden = set(conf.get("session_hidden", []) or [])
        except Exception:
            pass

    def _save_meta(self) -> None:
        try:
            with open(self._meta_file(), "r", encoding="utf-8") as f:
                conf = json.load(f)
            conf["session_aliases"] = self._aliases
            conf["session_hidden"] = sorted(self._hidden)
            with open(self._meta_file(), "w", encoding="utf-8") as f:
                json.dump(conf, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def rename_session(self, session_id: str, name: str) -> dict:
        """重命名会话显示名（持久化到 conf.json）。"""
        name = (name or "").strip()
        if name:
            self._aliases[session_id] = name
        else:
            self._aliases.pop(session_id, None)
        self._save_meta()
        return {"ok": True, "display_name": name}

    def remove_session(self, session_id: str) -> dict:
        """从会话列表移除（隐藏；不删文件）。手动添加的同步从 extra_paths 剔除。"""
        self._hidden.add(session_id)
        with self._lock:
            kept = []
            for p in self.extra_paths:
                info = discover_single_path(p)
                if info is None or info.session_id != session_id:
                    kept.append(p)
            self.extra_paths = kept
        self._save_meta()
        return {"ok": True}

    def _record_eval(self, session_id: str, agent: str, query: str, metrics: dict, text: str) -> dict | None:
        """评测完成时：判级（task 匹配 → 自动）并写入评测记录（矩阵数据源）。

        判定结果回写 metrics["judge"]（前端「任务成功率」独立展示），返回 verdict。
        """
        try:
            verdict = judge_eval(query, session_id, metrics, text)
            # 任务级判定回写 metrics（与工具成功率是两回事：任务成功率=锚点/诚实性判定）
            if isinstance(metrics, dict):
                metrics["judge"] = {
                    "level": verdict["level"],
                    "success": verdict["success"],
                    "source": verdict["level_source"],
                    "reason": verdict["level_reason"],
                    "by": verdict["success_by"],
                }
            self._records.add({
                "session_id": session_id,
                "agent": agent,
                "level": verdict["level"],
                "level_source": verdict["level_source"],
                "level_reason": verdict["level_reason"],
                "success": verdict["success"],
                "success_by": verdict["success_by"],
                "tool_calls_total": metrics.get("tool_calls_total"),
                "input_tokens": metrics.get("input_tokens"),
                "cost_cny": metrics.get("cost_cny") or metrics.get("cost_est_cny"),
                "human_interventions": metrics.get("human_interventions"),
                "turn_end_reason": metrics.get("turn_end_reason"),
                "query": (query or "")[:200],
            })
            return verdict
        except Exception:
            return None

    def _enrich_cost(self, metrics: dict) -> dict:
        """补成本估算（USD + RMB 换算）；官方 cost_usd 保留优先展示。"""
        if not isinstance(metrics, dict):
            return metrics
        try:
            from cost import estimate_cost_usd
            est = estimate_cost_usd(metrics.get("model") or "", metrics)
            if est is not None:
                metrics["cost_usd_est"] = round(est, 6)
            # RMB 换算（官方 + 估算）
            import json as _json
            from pathlib import Path as _Path
            try:
                with open(_Path(__file__).parent / "conf.json", "r", encoding="utf-8") as f:
                    rate = _json.load(f).get("pricing", {}).get("cny_per_usd", 7.2)
            except Exception:
                rate = 7.2
            if metrics.get("cost_usd") is not None:
                metrics["cost_cny"] = round(metrics["cost_usd"] * rate, 4)
            if metrics.get("cost_usd_est") is not None:
                metrics["cost_est_cny"] = round(metrics["cost_usd_est"] * rate, 4)
        except Exception:
            pass
        return metrics

    def _pub(self, session_id: str | None, frame: dict) -> None:
        if session_id is not None and "session_id" not in frame:
            frame["session_id"] = session_id
        self.hub.publish(frame)

    def _on_tail_events(self, session_id: str, events: list) -> None:
        """外部 Claude 会话尾随事件 → 广播（每会话维护独立 EventMetrics 实时快照）。

        合并为单个 batch 帧（events 数组 + metrics 快照）→ 前端一次 setState，
        避免逐事件广播导致高频重渲染（运行中会话更新卡顿/转圈）。
        """
        metrics = self._tail_metrics.setdefault(session_id, EventMetrics())
        for event in events:
            metrics.on_event(event)
        self._pub(session_id, {"type": "batch", "events": events,
                               "metrics": metrics.snapshot_live()})

    # ---- 会话发现 ----

    def list_sessions(self, scope: str = "all") -> list:
        if scope == "batch":
            # 「批量评测」独立 tab：只列批量评测落盘（results/batch），其余来源不混入
            sessions = discover_session_root(self.batch_root) if self.batch_root else []
            # 附加运行中批量评测（eval 发起）
            with self._lock:
                eval_runs = dict(self._eval_runs)
            for sid, info in eval_runs.items():
                sessions.append(SessionInfo(
                    session_id=sid, agent=info.get("agent", "eval"), state="live",
                    source="eval_run", query=info.get("query") or info.get("task_id"),
                    model=info.get("model"), started_at=info.get("started_at"),
                    extra={"task_id": info.get("task_id")},
                ))
            return self._decorate_sessions(sessions)
        with self._lock:
            eval_runs = dict(self._eval_runs)
        sessions = discover_all(
            projects_dir=self.projects_dir,
            session_root=self.session_root,
            eval_runs=eval_runs,
            samples_dir=self.samples_dir,
            codemaker_db=self.codemaker_db,
        )
        # 手动指定的会话路径（--sessions / /api/sessions/add）
        for path in self.extra_paths:
            if str(path).lower().endswith(".db") and is_codemaker_db(path):
                from session_discovery import discover_codemaker_db
                sessions.extend(discover_codemaker_db(path))
                continue
            info = discover_single_path(path)
            if info is not None:
                sessions.append(info)
        # 去重 + 重建 path 索引（raw 浏览用）
        seen = set(); deduped = []
        self._path_map = {}
        for s in sessions:
            key = (s.agent, s.session_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(s)
            self._path_map[s.session_id] = s.path
        sessions = deduped
        return self._decorate_sessions(sessions, eval_runs=eval_runs)

    def _decorate_sessions(self, sessions: list, eval_runs: dict = None) -> list:
        """附加运行中挂接状态 + 应用别名/隐藏 → dict 列表。"""
        eval_runs = eval_runs or {}
        running_tails = set(self._tails.running()) | set(self._cm_tails.running())
        # 会话级存活：claude/codemaker 文件新鲜（仍在写入）→ live（绿点）
        fresh_ms = 5 * 60 * 1000   # 文件 5 分钟内更新视为"正在写入"
        now = time.time() * 1000
        out = []
        for s in sessions:
            if s.session_id in self._hidden:
                continue
            if s.state != "live" and s.agent in ("claude", "codemaker", "dsh"):
                is_fresh = s.updated_at is not None and (now - s.updated_at) <= fresh_ms
                if is_fresh:
                    s.state = "live"
            s.extra["attached"] = s.session_id in running_tails \
                or s.session_id in self._replays \
                or s.session_id in eval_runs
            if s.session_id in self._aliases:
                s.extra["display_name"] = self._aliases[s.session_id]
            out.append(s)
        return [s.to_dict() for s in out]

    def add_session_path(self, path: str) -> dict:
        """手动添加会话：文件（单会话）/ Codemaker 会话库（.db，多会话）/ 目录（目录下所有可识别会话）。"""
        p = Path(path)
        if p.is_dir():
            infos = discover_samples_dir(p)
            if not infos:
                return {"ok": False, "error": f"目录中无可识别会话: {path}"}
            with self._lock:
                for info in infos:
                    if info.path not in self.extra_paths:
                        self.extra_paths.append(info.path)
            return {"ok": True, "count": len(infos),
                    "sessions": [i.to_dict() for i in infos]}
        if str(p).lower().endswith(".db"):
            from session_discovery import discover_codemaker_db
            infos = discover_codemaker_db(p)
            if not infos:
                return {"ok": False, "error": f"无法识别 Codemaker 会话库: {path}"}
            with self._lock:
                if path not in self.extra_paths:
                    self.extra_paths.append(path)
            return {"ok": True, "count": len(infos),
                    "sessions": [i.to_dict() for i in infos]}
        info = discover_single_path(path)
        if info is None:
            return {"ok": False, "error": f"无法识别会话文件: {path}"}
        with self._lock:
            if path not in self.extra_paths:
                self.extra_paths.append(path)
        return {"ok": True, "session": info.to_dict()}

    def list_dir(self, path: str = None) -> dict:
        """列目录（文件选择器用）：dirs + 会话文件（识别 agent 类型）+ parent。"""
        import os
        p = path or os.path.abspath(os.sep)
        p = os.path.abspath(p)
        if not os.path.isdir(p):
            return {"ok": False, "error": "not a directory"}
        dirs, files = [], []
        try:
            for name in sorted(os.listdir(p), key=str.lower):
                full = os.path.join(p, name)
                try:
                    if os.path.isdir(full):
                        dirs.append(name)
                    elif os.path.isfile(full):
                        ext = os.path.splitext(name)[1].lower()
                        if ext in (".jsonl", ".json", ".log", ".txt", ".db"):
                            kind = _detect_agent(full)
                            files.append({"name": name, "size": os.path.getsize(full), "kind": kind})
                except OSError:
                    pass
        except PermissionError:
            return {"ok": False, "error": "permission denied"}
        return {"ok": True, "path": p, "parent": os.path.dirname(p) if os.path.dirname(p) != p else None,
                "dirs": dirs, "files": files}

    def raw_log(self, session_id: str, max_bytes: int = 5_000_000) -> tuple | None:
        """返回 (文本内容, 文件名)；找不到会话返回 None。"""
        path = self._path_map.get(session_id)
        if path is None:
            self.list_sessions()   # 重建索引后重试
            path = self._path_map.get(session_id)
        if path is None or not Path(path).is_file():
            return None
        # Codemaker：DB 二进制 → 可读文本转储（消息/part 逐行）
        if str(path).lower().endswith(".db") and is_codemaker_db(path):
            return self._codemaker_raw_text(path, session_id, max_bytes)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes)
        return content, Path(path).name

    @staticmethod
    def _codemaker_raw_text(db_path, session_id: str, max_bytes: int) -> tuple:
        """Codemaker 会话 → 可读文本（供 RawLog 标签页浏览）。"""
        db = CodemakerDB(db_path)
        sess = db.get_session(session_id) or {}
        msgs = db.messages(session_id)
        parts = db.parts(session_id)
        by_msg: dict = {}
        for p in parts:
            by_msg.setdefault(p["message_id"], []).append(p)
        import time as _time
        lines = [
            f"# Codemaker 会话 {session_id}",
            f"title:      {sess.get('title') or ''}",
            f"directory:  {sess.get('directory') or ''}",
            f"model:      {sess.get('model') or ''}",
            f"cost_usd:   {sess.get('cost_usd')}",
            f"started:    {_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime((sess.get('started_at') or 0)/1000)) if sess.get('started_at') else '-'}",
            f"updated:    {_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime((sess.get('updated_at') or 0)/1000)) if sess.get('updated_at') else '-'}",
            "=" * 80,
        ]
        for m in msgs:
            data = m.get("data") or {}
            role = data.get("role", "?")
            t = data.get("time") or {}
            created = t.get("created") or m.get("time_created")
            ts = _time.strftime('%H:%M:%S', _time.localtime(created / 1000)) if created else ""
            lines.append(f"\n[{ts}] <{role}>")
            if data.get("finish"):
                lines.append(f"    finish: {data['finish']}")
            if data.get("error"):
                lines.append(f"    error:  {data['error']}")
            if data.get("tokens"):
                lines.append(f"    tokens: {data['tokens']}")
            for p in by_msg.get(m["id"], []):
                pd = p.get("data") or {}
                ptype = pd.get("type")
                if ptype == "text":
                    lines.append(f"    text: {pd.get('text', '')[:400]}")
                elif ptype == "reasoning":
                    lines.append(f"    reasoning: {pd.get('text', '')[:200]}")
                elif ptype == "tool":
                    st = pd.get("state") or {}
                    lines.append(f"    tool: {pd.get('tool')} [{st.get('status')}]")
                    lines.append(f"      in:  {json.dumps(st.get('input') or {}, ensure_ascii=False)[:300]}")
                    out = st.get("output") or (st.get("metadata") or {}).get("output") or ""
                    lines.append(f"      out: {str(out)[:400]}")
                elif ptype in ("step-start", "step-finish"):
                    lines.append(f"    {ptype}: {json.dumps({k: v for k, v in pd.items() if k != 'type'}, ensure_ascii=False)[:200]}")
                elif ptype == "compaction":
                    lines.append(f"    compaction: {json.dumps(pd, ensure_ascii=False)[:200]}")
        text = "\n".join(lines)
        return text[:max_bytes], f"{session_id}.txt"

    # ---- 挂接（live=尾随 / replay=历史重放） ----

    def attach_session(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        mode = params.get("mode", "live")
        path = params.get("path")
        agent = params.get("agent", "claude")
        if not session_id:
            return {"ok": False, "error": "缺 session_id"}
        if agent == "codemaker":
            # Codemaker 会话库：path 是 opencode.db；live=先重放再尾随 event 表
            db_path = path or self.codemaker_db
            if mode == "replay":
                if session_id in self._replays:
                    return {"ok": True, "mode": "replay"}
                t = threading.Thread(target=self._replay_codemaker,
                                     args=(session_id, db_path), daemon=True)
                self._replays[session_id] = t
                t.start()
                return {"ok": True, "mode": "replay"}
            ok = self._cm_tails.start(session_id, db_path)
            if not ok:
                return {"ok": False, "error": f"无法打开 Codemaker 会话库: {db_path}"}
            self._tail_metrics.setdefault(session_id, EventMetrics())
            # 广播 run/start（live 尾随），前端从「挂接中…」进入「运行中」
            self._pub(session_id, {"type": "run/start", "replay": False,
                                   "mode": "live", "status": "tail"})
            return {"ok": True, "mode": "live"}
        if mode == "live":
            if not path:
                return {"ok": False, "error": "live 模式需要 path（JSONL 文件）"}
            # 文件已停止增长（mtime 距今 >60s）→ 降级为 replay：全量重放，报告/轨迹立即有数据
            # （live 尾随只读新增内容，旧会话会一直空白）
            try:
                stale = time.time() - Path(path).stat().st_mtime > 60
            except OSError:
                stale = False
            if stale:
                if session_id in self._replays:
                    return {"ok": True, "mode": "replay"}
                t = threading.Thread(target=self._replay_session,
                                     args=(session_id, path, agent), daemon=True)
                self._replays[session_id] = t
                t.start()
                return {"ok": True, "mode": "replay"}
            ok = self._tails.start(session_id, path)
            if not ok:
                return {"ok": False, "error": f"文件不可读: {path}"}
            self._tail_metrics.setdefault(session_id, EventMetrics())
            # 广播 run/start（live 尾随），前端从「挂接中…」进入「运行中」（实时接收事件）
            self._pub(session_id, {"type": "run/start", "replay": False,
                                   "mode": "live", "status": "tail"})
            return {"ok": True, "mode": "live"}
        # replay：历史会话批量重放（线程内完成，避免阻塞 HTTP）
        if session_id in self._replays:
            return {"ok": True, "mode": "replay"}  # 已在重放
        t = threading.Thread(target=self._replay_session,
                             args=(session_id, path, agent), daemon=True)
        self._replays[session_id] = t
        t.start()
        return {"ok": True, "mode": "replay"}

    def detach_session(self, params: dict) -> dict:
        session_id = params.get("session_id", "")
        self._tails.stop(session_id)
        self._cm_tails.stop(session_id)
        self._tail_metrics.pop(session_id, None)
        self._replays.pop(session_id, None)
        return {"ok": True}

    def _replay_session(self, session_id: str, path, agent: str) -> None:
        """读历史日志 → 统一事件批量广播 → 一次性指标（run/end）。

        airlab：无事件流（文本日志），走 session_report.scan_airlab 出指标。
        """
        hub = self.hub
        metrics = EventMetrics()
        if agent == "airlab":
            self._replay_airlab(session_id, path)
            return
        # 预统计文件行数（供前端重放进度条）
        total = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    total += 1
        except OSError:
            total = 0
        hub.publish({"type": "run/start", "session_id": session_id,
                     "backend": agent, "task_id": session_id, "query": "",
                     "replay": True, "total_events": total})
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                is_dsh = None   # None=未定；True=DSH 格式（首行头）；False=Claude JSONL
                seq = 0
                chunk = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if is_dsh is None:
                        is_dsh = obj.get("type") == "session"
                        if is_dsh:
                            continue  # DSH 头行不发布
                    events = [obj] if is_dsh else claude_jsonl_to_events(obj, seq)
                    if not is_dsh:
                        seq += 1
                    for event in events:
                        metrics.on_event(event)
                        chunk.append(event)
                        if len(chunk) >= 200:   # 批量帧：减少 SSE 帧数，避免前端高频重渲染
                            hub.publish({"type": "batch", "session_id": session_id,
                                         "events": chunk, "metrics": metrics.snapshot_live()})
                            chunk = []
                if chunk:
                    hub.publish({"type": "batch", "session_id": session_id,
                                 "events": chunk, "metrics": metrics.snapshot_live()})
            metrics.finalize()
            snapshot = metrics.snapshot()
            query = (snapshot.get("tasks") or [{}])[0].get("query", "") if snapshot.get("tasks") else ""
            self._record_eval(session_id, agent, query,
                              self._enrich_cost(snapshot), metrics.assistant_text())
            hub.publish({"type": "run/end", "session_id": session_id,
                         "result": {"session_id": session_id,
                                    "metrics": self._enrich_cost(snapshot),
                                    "finish_reason": metrics.turn_end_reason,
                                    "log_path": str(path)}})
        except Exception as exc:
            hub.publish({"type": "error", "session_id": session_id,
                         "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._replays.pop(session_id, None)

    def _replay_airlab(self, session_id: str, path) -> None:
        """airlab 文本日志：scan_airlab_log 解析 → 统一事件流广播 + 一次性指标。"""
        hub = self.hub
        try:
            from session_report import scan_airlab_log
            data = scan_airlab_log(str(path))
            events = airlab_to_events(data, str(path))
            hub.publish({"type": "run/start", "session_id": session_id,
                         "backend": "airlab", "task_id": session_id, "query": "",
                         "replay": True, "total_events": len(events)})
            # 批量广播（轨迹/任务 tab 消费，与 claude/dsh 通道同构）
            metrics = EventMetrics()
            chunk = []
            for event in events:
                metrics.on_event(event)
                chunk.append(event)
                if len(chunk) >= 200:
                    hub.publish({"type": "batch", "session_id": session_id,
                                 "events": chunk, "metrics": metrics.snapshot_live()})
                    chunk = []
            if chunk:
                hub.publish({"type": "batch", "session_id": session_id,
                             "events": chunk, "metrics": metrics.snapshot_live()})
            metrics.finalize()
            # 快照：耗时直接用 scan 提取的真实值（airlab 事件流为轨迹展示用，时间粒度粗，
            # 不应用其折叠值覆盖 llm/tool/wait 等耗时统计）
            snapshot = self._enrich_cost(_airlab_to_metrics(data, str(path)))
            hub.publish({"type": "metrics", "session_id": session_id, "metrics": snapshot})
            hub.publish({"type": "run/end", "session_id": session_id,
                         "result": {
                             "session_id": session_id,
                             "metrics": snapshot,
                             "finish_reason": snapshot.get("turn_end_reason"),
                             "log_path": str(path),
                         }})
        except Exception as exc:
            hub.publish({"type": "error", "session_id": session_id,
                         "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._replays.pop(session_id, None)

    def _replay_codemaker(self, session_id: str, db_path) -> None:
        """Codemaker 会话库重放：DB 快照 → 统一事件批量广播 → 一次性指标。"""
        hub = self.hub
        try:
            db = CodemakerDB(db_path)
            sess = db.get_session(session_id)
            hub.publish({"type": "run/start", "session_id": session_id,
                         "backend": "codemaker", "task_id": session_id,
                         "query": (sess or {}).get("title") or "",
                         "replay": True, "total_events": 0})
            adapter = CodemakerEventAdapter()
            metrics = EventMetrics()
            events = adapter.replay(db, session_id)
            chunk = []
            for event in events:
                metrics.on_event(event)
                chunk.append(event)
                if len(chunk) >= 200:
                    hub.publish({"type": "batch", "session_id": session_id,
                                 "events": chunk, "metrics": metrics.snapshot_live()})
                    chunk = []
            if chunk:
                hub.publish({"type": "batch", "session_id": session_id,
                             "events": chunk, "metrics": metrics.snapshot_live()})
            metrics.finalize()
            snapshot = metrics.snapshot()
            snapshot["cost_usd"] = (sess or {}).get("cost_usd")
            snapshot["title"] = (sess or {}).get("title")
            snapshot["directory"] = (sess or {}).get("directory")
            snapshot["started_at"] = (sess or {}).get("started_at")
            snapshot["ended_at"] = (sess or {}).get("updated_at")
            # 子任务（todo 表）
            todos = db.todos(session_id)
            if todos:
                tasks = snapshot.get("tasks") or []
                if not tasks:
                    tasks = [{"query": (sess or {}).get("title") or "", "tool_calls": 0,
                              "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                              "start_ms": None, "end_ms": None}]
                    snapshot["tasks"] = tasks
                tasks[0]["subitems"] = [{
                    "id": str(t["position"]), "subject": t["subject"], "status": t["status"],
                    "created_ms": t["created_ms"], "updated_ms": t["updated_ms"],
                } for t in todos]
            query = (snapshot.get("tasks") or [{}])[0].get("query", "") if snapshot.get("tasks") else ""
            self._record_eval(session_id, "codemaker", query,
                              self._enrich_cost(snapshot), metrics.assistant_text())
            hub.publish({"type": "run/end", "session_id": session_id,
                         "result": {"session_id": session_id,
                                    "metrics": self._enrich_cost(snapshot),
                                    "finish_reason": metrics.turn_end_reason,
                                    "log_path": str(Path(db_path))}})
        except Exception as exc:
            hub.publish({"type": "error", "session_id": session_id,
                         "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._replays.pop(session_id, None)

    # ---- eval 发起（R1 保留，次级入口） ----

    def start_eval(self, params: dict) -> dict:
        with self._lock:
            if self._job is not None and self._job.is_alive():
                return {"ok": False, "error": "已有评测任务在运行"}
            self._cancel = threading.Event()
            session_id = params.get("session_id") or \
                f"eval-{params.get('task_id', 'run')}-{int(time.time())}"
            self._current = {
                "state": "running",
                "backend": params.get("backend", "claude"),
                "task_id": params.get("task_id", "?"),
                "query": params.get("query", ""),
                "session_id": session_id,
            }
            self._eval_runs[session_id] = {
                "agent": params.get("backend", "claude"),
                "task_id": params.get("task_id", "?"),
                "query": params.get("query", ""),
                "started_at": int(time.time() * 1000),
                "model": params.get("model"),
            }
            self._job = threading.Thread(
                target=self._run_job, args=(params, session_id, self._cancel), daemon=True)
            self._job.start()
        return {"ok": True, "session_id": session_id}

    def stop_eval(self) -> dict:
        cancel = self._cancel
        if cancel is not None and not cancel.is_set():
            cancel.set()
            return {"ok": True, "message": "取消请求已发送"}
        return {"ok": False, "message": "没有运行中的任务"}

    # ---- 任务定义 + 批量评测（前端批量评测入口） ----

    def list_tasks(self) -> dict:
        return {"tasks": self._records.list_tasks()}

    def save_task(self, task: dict) -> dict:
        if not task.get("task_id"):
            return {"ok": False, "error": "缺少 task_id"}
        self._records.upsert_task(task)
        return {"ok": True, "task": self._records.get_task(task["task_id"])}

    def remove_task(self, task_id: str) -> dict:
        return {"ok": True} if self._records.delete_task(task_id) \
            else {"ok": False, "error": "任务不存在"}

    def gen_tasks(self, params: dict) -> dict:
        domains = [d.strip() for d in (params.get("domain") or "").split(",") if d.strip()]
        if not domains:
            return {"ok": False, "error": "缺少 domain"}
        tasks = self._records.generate_tasks(domains, params.get("params"),
                                             int(params.get("count") or 1))
        return {"ok": True, "generated": len(tasks), "tasks": tasks}

    def _dk_default_token(self) -> str:
        """从 airgattai config.yaml 读 dk.token（兜底，前端未填时自动复用）。"""
        try:
            import yaml
            search = [Path.home() / "airgattai" / "config.yaml",
                      Path(os.environ.get("APPDATA", "")) / "airgattai" / "config.yaml"]
            for p in search:
                if p.is_file():
                    cfg = yaml.safe_load(open(p, encoding="utf-8")) or {}
                    t = (cfg.get("dk") or {}).get("token", "")
                    if t:
                        return t
        except Exception:
            pass
        return ""

    def fetch_dk_devices(self, params: dict) -> dict:
        """用 dk_token + dk_group 拉取 DK 设备列表（serialno + 标签）。"""
        token = params.get("token") or self._dk_default_token()
        group_id = params.get("group_id")
        if not token or group_id is None:
            return {"ok": False, "error": "缺少 dk_token 或 dk_group"}
        try:
            import requests
            base = os.environ.get("DK_BASE_URL", "https://devicefarm-airlab.nie.netease.com")
            resp = requests.get(
                f"{base}/v1/win_dev/",
                params={"group_id": int(group_id), "page_size": 100},
                headers={"Authorization": f"Token {token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            devices = []
            for d in data.get("results", []):
                sn = d.get("serialno")
                label = ((d.get("device_info") or {}).get("name")
                         or d.get("alias") or d.get("model", "")) or sn
                devices.append({
                    "serialno": sn, "label": label,
                    "online": bool(d.get("online", False)),
                    "occupied": bool(d.get("occupied", False)),
                    "occupy_username": d.get("occupy_username"),
                })
            # 排序：空闲优先 → 在线占用 → 离线
            def _sort_key(dv):
                idle = 0 if (dv["online"] and not dv["occupied"]) else (1 if dv["online"] else 2)
                return (idle, dv["label"] or "")
            devices.sort(key=_sort_key)
            return {"ok": True, "devices": devices, "group_id": int(group_id)}
        except Exception as exc:
            return {"ok": False, "error": f"DK 拉取失败: {exc}"}

    def get_env(self, params: dict) -> dict:
        """返回 envs/<skill>.json 配置（cwd/device/note，不含敏感 token）。"""
        skill = params.get("skill") or ""
        if not skill:
            return {"ok": False, "error": "缺少 skill"}
        try:
            from eval_batch import load_env
            env = load_env(skill)
        except Exception:
            env = {}
        return {"ok": True, "skill": skill, "cwd": env.get("cwd"),
                "device": env.get("device"), "note": env.get("note")}

    def start_batch(self, params: dict) -> dict:
        """发起批量评测：后台线程跑 run_batch，SSE 发布 batch-progress。"""
        agent = params.get("agent") or "claude"
        skill = params.get("skill")
        repeat = int(params.get("repeat") or 1)
        permission_mode = params.get("permission_mode")
        model = params.get("model")
        tasks = self._records.list_tasks()
        if skill:
            tasks = [t for t in tasks if t.get("skill_expected") == skill]
        task_ids = params.get("task_ids")
        if task_ids:
            tasks = [t for t in tasks if t.get("task_id") in task_ids]
        if not tasks:
            return {"ok": False, "error": "无任务——先在前端定义或生成任务"}
        env = {}
        if skill:
            try:
                from eval_batch import load_env
                env = load_env(skill)
            except Exception:
                env = {}
        cwd = params.get("cwd") or env.get("cwd")
        device = params.get("device") or env.get("device")
        if device:
            for t in tasks:
                t["query"] = t.get("query", "").replace("{device}", device)
        cancel = threading.Event()
        with self._lock:
            self._batch_cancel = cancel
            self._batch_state = {"state": "running", "agent": agent,
                                 "repeat": repeat, "total": len(tasks)}
        session_root = self.batch_root or "results/batch"

        def _job():
            try:
                from eval_batch import run_batch

                def on_task(task, result, verdict, run_idx):
                    self.hub.publish({
                        "type": "batch-progress",
                        "session_id": (result or {}).get("session_id", ""),
                        "task_id": task.get("task_id"), "run_idx": run_idx,
                        "verdict": verdict,
                    })
                run_batch(tasks, backend=agent,
                          timeout_s=int(params.get("timeout") or 600),
                          permission_mode=permission_mode, model=model,
                          session_root=session_root, cwd=cwd,
                          provider=params.get("provider"), repeat=repeat,
                          on_task=on_task)
            except Exception as exc:
                self.hub.publish({"type": "error", "message": f"批量评测失败: {exc}"})
            finally:
                with self._lock:
                    self._batch_state = {"state": "done"}

        threading.Thread(target=_job, daemon=True).start()
        return {"ok": True, "total": len(tasks), "repeat": repeat, "agent": agent}

    def stop_batch(self) -> dict:
        with self._lock:
            c = self._batch_cancel
        if c is not None and not c.is_set():
            c.set()
            return {"ok": True, "message": "批量评测停止请求已发送"}
        return {"ok": False, "message": "没有运行中的批量评测"}

    def batch_status(self) -> dict:
        return self._batch_state

    def launch_terminal(self, params: dict) -> dict:
        """在指定目录以 provider 配置打开交互式 claude 对话终端（新控制台窗口，agent 本身）。

        不发起评测（不传 query）：打开的是 agent 自己的交互式会话窗口。
        之后该会话的日志会写到 ~/.claude/projects/<slug>/<uuid>.jsonl，可被会话发现+尾随。
        """
        import subprocess
        cwd = params.get("cwd") or str(Path.cwd())
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            return {"ok": False, "error": f"目录不存在: {cwd}"}
        agent = params.get("agent") or "claude"
        if agent == "codemaker":
            return self._launch_codemaker_terminal(cwd_path, params.get("model"))
        provider = params.get("provider")
        # 解析 provider → env 覆盖 + settings 文件
        env = os.environ.copy()
        settings_arg = None
        tmp_path = None
        if provider:
            try:
                from provider import resolve_provider, build_settings_json, apply_env
                p = resolve_provider(str(provider))
                if p:
                    js = build_settings_json(p)
                    if js:
                        import tempfile
                        f = tempfile.NamedTemporaryFile(
                            "w", suffix=".json", encoding="utf-8", delete=False)
                        f.write(js)
                        f.close()
                        tmp_path = f.name
                        settings_arg = tmp_path
                    env = apply_env(p, env)
            except Exception as exc:
                return {"ok": False, "error": f"provider 配置错误: {exc}"}
        # 组装命令：交互式 claude（新控制台窗口，用户可直接输入对话）
        cmd = ["claude"]
        if settings_arg:
            cmd += ["--settings", settings_arg]
        try:
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, cwd=str(cwd_path), env=env,
                                    creationflags=flags,
                                    close_fds=True)
        except Exception as exc:
            if tmp_path:
                try: os.unlink(tmp_path)
                except OSError: pass
            return {"ok": False, "error": f"启动 claude 失败: {exc}"}
        # 记录启动的终端（pid → 信息），供停止/状态查询
        with self._lock:
            self._terminals[proc.pid] = {
                "pid": proc.pid,
                "cwd": str(cwd_path),
                "agent": "claude",
                "provider": provider,
                "settings": tmp_path,
                "started_at": int(time.time() * 1000),
            }
        return {"ok": True, "pid": proc.pid, "cwd": str(cwd_path),
                "provider": provider or "default",
                "note": "已在新窗口打开 claude 对话终端；会话日志将自动出现在会话列表"}

    def _launch_codemaker_terminal(self, cwd: Path, model: str | None) -> dict:
        """在指定目录打开交互式 codemaker 对话终端（新控制台窗口）。"""
        import subprocess
        bin_path = str(Path.home() / ".codemaker" / "bin" / "codemaker.exe")
        cmd = [bin_path, "run", "-i"]
        if model:
            cmd += ["--model", model]
        cmd += ["--dir", str(cwd)]
        try:
            flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, cwd=str(cwd), creationflags=flags, close_fds=True)
        except Exception as exc:
            return {"ok": False, "error": f"启动 codemaker 失败: {exc}"}
        with self._lock:
            self._terminals[proc.pid] = {
                "pid": proc.pid, "cwd": str(cwd), "agent": "codemaker",
                "provider": model or "default", "settings": None,
                "started_at": int(time.time() * 1000),
            }
        return {"ok": True, "pid": proc.pid, "cwd": str(cwd), "agent": "codemaker",
                "provider": model or "default",
                "note": "已在新窗口打开 codemaker 对话终端；会话将写入 opencode.db 并自动出现在会话列表"}

    def stop_terminal(self, pid: int) -> dict:
        """结束指定终端进程（并清理临时 settings 文件）。"""
        with self._lock:
            info = self._terminals.pop(pid, None)
        if info is None:
            return {"ok": False, "message": f"终端 {pid} 不存在"}
        import subprocess
        try:
            subprocess.Popen(["taskkill", "/PID", str(pid), "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            pass
        if info.get("settings"):
            try: os.unlink(info["settings"])
            except OSError: pass
        return {"ok": True, "message": f"终端 {pid} 已结束"}

    def list_terminals(self) -> list:
        with self._lock:
            return [
                {"pid": pid, **info}
                for pid, info in self._terminals.items()
            ]

    def status(self) -> dict:
        with self._lock:
            running = self._job is not None and self._job.is_alive()
        state = "running" if running else "idle"
        return {**self._current, "state": state, "running": running}

    # ---- 内部 ----

    def _run_job(self, params: dict, session_id: str,
                 cancel: threading.Event) -> None:
        hub = self.hub
        metrics = EventMetrics()          # 服务端实时快照（节流推送）
        last_push = [0.0]
        agg = {"events": [], "at": 0.0}   # 小批量聚合：100ms 窗口内事件合并为 1 个 batch 帧

        def flush(force: bool = False) -> None:
            if not agg["events"]:
                return
            if not force and time.time() - agg["at"] < 0.1:
                return
            hub.publish({"type": "batch", "session_id": session_id,
                         "events": agg["events"], "metrics": metrics.snapshot_live()})
            agg["events"] = []
            agg["at"] = time.time()

        def on_event(event: dict) -> None:
            metrics.on_event(event)
            agg["events"].append(event)
            if agg["at"] == 0.0:
                agg["at"] = time.time()
            now = time.time()
            if now - last_push[0] >= 0.5:   # 0.5s 兜底指标推送（批量帧已含 metrics）
                last_push[0] = now
            flush()

        def on_warning(message: str) -> None:
            flush(force=True)
            self._pub(session_id, {"type": "warning", "message": message})

        backend = params.get("backend", "claude")
        task = {
            "task_id": params.get("task_id", "unknown"),
            "query": params.get("query", ""),
        }
        timeout_s = int(params.get("timeout", 300))
        cwd = params.get("cwd")
        session_root = params.get("session_root") or self.session_root

        self._pub(session_id, {
            "type": "run/start",
            "task_id": task["task_id"],
            "query": task["query"],
            "backend": backend,
        })
        try:
            if backend == "dsh":
                from dsh_backend import DshEvalBackend
                with DshEvalBackend(model=params.get("model", "deepseek-v4-flash"),
                                    cwd=cwd, session_root=session_root) as b:
                    result = b.run_task(task, timeout_s=timeout_s,
                                        on_event=on_event, on_warning=on_warning)
            else:
                from claude_backend import ClaudeEvalBackend
                with ClaudeEvalBackend(cwd=cwd, session_root=session_root,
                                       permission_mode=params.get("permission_mode"),
                                       model=params.get("model"),
                                       provider=params.get("provider"),
                                       include_partial_messages=bool(
                                           params.get("include_partial_messages", False))) as b:
                    result = b.run_task(task, timeout_s=timeout_s,
                                        on_event=on_event, on_warning=on_warning,
                                        cancel_event=cancel)
            result["metrics"] = self._enrich_cost(result.get("metrics") or {})
            self._record_eval(session_id, backend, task.get("query", ""),
                              result["metrics"], metrics.assistant_text())
            flush(force=True)   # 冲刷剩余事件（确保 run/end 前全部送达）
            self._pub(session_id, {"type": "run/end", "result": result})
        except Exception as exc:
            if cancel.is_set():
                self._pub(session_id, {"type": "run/cancel", "message": "评测已被取消"})
            else:
                self._pub(session_id, {"type": "error",
                                       "message": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self._eval_runs.pop(session_id, None)

# ---------- HTTP ----------

class EvalHttpServer(ThreadingHTTPServer):
    """评测看板 HTTP 服务：承载 EvalServer 实例供 Handler 访问。

    daemon_threads=True：SSE 长连接线程不阻塞进程退出。
    """

    daemon_threads = True

    def __init__(self, server: "EvalServer", server_address):
        super().__init__(server_address, Handler)
        self.eval = server


class Handler(BaseHTTPRequestHandler):
    # 类型提示：self.server 是 EvalHttpServer，业务对象在 self.server.eval
    server: "EvalHttpServer"  # type: ignore

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # 客户端断开（刷新/关闭页面），不刷异常噪音

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_index()
        elif self.path.startswith("/assets/"):
            self._serve_asset(self.path)
        elif self.path == "/events":
            self._serve_sse()
        elif self.path == "/api/status":
            self._send_json(self.server.eval.status())
        elif self.path.startswith("/api/sessions"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            scope = qs.get("scope", ["all"])[0]
            self._send_json({"sessions": self.server.eval.list_sessions(scope=scope)})
        elif self.path == "/api/eval-matrix":
            self._send_json(self.server.eval._records.matrix())
        elif self.path == "/api/stats":
            self._send_json(self.server.eval._records.stats())
        elif self.path.startswith("/api/executions"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            task_id = qs.get("task_id", [None])[0]
            recs = self.server.eval._records.all()
            if task_id:
                recs = [r for r in recs if r.get("task_id") == task_id]
            self._send_json({"executions": recs})
        elif self.path == "/api/tasks":
            self._send_json(self.server.eval.list_tasks())
        elif self.path.startswith("/api/env"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_json(self.server.eval.get_env({"skill": qs.get("skill", [""])[0]}))
        elif self.path == "/api/batch/status":
            self._send_json(self.server.eval.batch_status())
        elif self.path == "/api/providers":
            try:
                from provider import list_providers
                self._send_json({"providers": list_providers()})
            except Exception as exc:
                self._send_json({"providers": [], "error": str(exc)})
        elif self.path == "/api/terminals":
            self._send_json({"terminals": self.server.eval.list_terminals()})
        elif self.path == "/api/agent-status":
            import urllib.parse
            force = "force" in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_json(self.server.eval._agent_status.probe(force=force))
        elif self.path.startswith("/api/raw"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            sid = qs.get("session_id", [""])[0]
            result = self.server.eval.raw_log(sid)
            if result is None:
                self._send_json({"error": "session not found"}, 404)
                return
            content, fname = result
            body = content.encode("utf-8", errors="replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/fs"):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_json(self.server.eval.list_dir(qs.get("path", [""])[0] or None))
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_asset(self, path: str) -> None:
        """React 构建产物的静态资源（/assets/*）。"""
        p = (self.server.eval.web_dir / path.lstrip("/")).resolve()
        if not p.is_file() or not str(p).startswith(str(self.server.eval.web_dir.resolve())):
            self._send_json({"error": "not found"}, 404)
            return
        body = p.read_bytes()
        ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
                 ".map": "application/json", ".woff2": "font/woff2"}.get(p.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        # 兼容：老版单页 evalboard.html / React 构建产物 index.html
        idx = None
        for cand in ("evalboard.html", "index.html"):
            p = self.server.eval.web_dir / cand
            if p.is_file():
                idx = p
                break
        if idx is None:
            self._send_json({"error": "看板文件缺失（web/evalboard.html 或 webui/dist/index.html）"}, 404)
            return
        body = idx.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        q = self.server.eval.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                try:
                    frame = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if frame is None:
                    break
                payload = json.dumps(frame, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # 客户端断开
        finally:
            self.server.eval.hub.unsubscribe(q)

    # ---- POST ----

    def do_POST(self):
        body = self._read_body()
        if self.path == "/api/start":
            self._send_json(self.server.eval.start_eval(body))
        elif self.path == "/api/stop":
            self._send_json(self.server.eval.stop_eval())
        elif self.path == "/api/terminal/launch":
            self._send_json(self.server.eval.launch_terminal(body))
        elif self.path == "/api/terminal/stop":
            self._send_json(self.server.eval.stop_terminal(int(body.get("pid", 0) or 0)))
        elif self.path == "/api/attach":
            self._send_json(self.server.eval.attach_session(body))
        elif self.path == "/api/detach":
            self._send_json(self.server.eval.detach_session(body))
        elif self.path == "/api/sessions/add":
            self._send_json(self.server.eval.add_session_path(body.get("path", "")))
        elif self.path == "/api/sessions/rename":
            self._send_json(self.server.eval.rename_session(body.get("session_id", ""), body.get("name", "")))
        elif self.path == "/api/sessions/remove":
            self._send_json(self.server.eval.remove_session(body.get("session_id", "")))
        elif self.path == "/api/tasks":
            self._send_json(self.server.eval.save_task(body))
        elif self.path == "/api/tasks/generate":
            self._send_json(self.server.eval.gen_tasks(body))
        elif self.path == "/api/dk/devices":
            self._send_json(self.server.eval.fetch_dk_devices(body))
        elif self.path == "/api/batch/start":
            self._send_json(self.server.eval.start_batch(body))
        elif self.path == "/api/batch/stop":
            self._send_json(self.server.eval.stop_batch())
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- PATCH（人工复核：修正 level/success） ----

    def do_PATCH(self):
        body = self._read_body()
        parts = self.path.rstrip("/").split("/")
        if len(parts) >= 4 and parts[1] == "api" and parts[2] == "executions":
            sid = parts[3]
            r = self.server.eval._records.review(
                sid,
                level=body.get("level"),
                success=body.get("success"),
                note=body.get("note"),
                reset=bool(body.get("reset", False)),
            )
            if r is None:
                self._send_json({"error": "session not found"}, 404)
            else:
                self._send_json({"ok": True, "execution": r})
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- DELETE（删除任务） ----

    def do_DELETE(self):
        parts = self.path.rstrip("/").split("/")
        if len(parts) >= 4 and parts[1] == "api" and parts[2] == "tasks":
            self._send_json(self.server.eval.remove_task(parts[3]))
        else:
            self._send_json({"error": "not found"}, 404)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="evalkit 实时评测看板服务（R2：会话观察器）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--web", default="web", help="看板静态目录（含 evalboard.html）")
    parser.add_argument("--session-root", default="results/board",
                        help="评测会话落盘根目录")
    parser.add_argument("--projects", default=None,
                        help="Claude Code projects 目录（缺省 ~/.claude/projects）")
    parser.add_argument("--samples-dir", default=None,
                        help="受限会话样本目录（配置后只从该目录发现会话，支持 claude/dsh/airlab 混合）")
    parser.add_argument("--sessions", default=None,
                        help="手动指定的会话文件路径列表（逗号分隔，自动识别类型）")
    parser.add_argument("--codemaker", default=None,
                        help="Codemaker 会话库 opencode.db 路径（缺省自动探测 ~/.local/share/codemaker/opencode.db）")
    parser.add_argument("--batch-root", default="results/batch",
                        help="批量评测会话落盘根目录（看板「批量评测」tab 独立展示，缺省 results/batch）")
    args = parser.parse_args(argv)

    extra_paths = [p.strip() for p in args.sessions.split(",") if p.strip()] if args.sessions else []
    server = EvalServer(web_dir=args.web, session_root=args.session_root,
                        projects_dir=args.projects, samples_dir=args.samples_dir,
                        extra_paths=extra_paths, codemaker_db=args.codemaker,
                        batch_root=args.batch_root)
    httpd = EvalHttpServer(server, (args.host, args.port))
    print(f"evalkit 实时评测看板: http://{args.host}:{args.port}")
    print(f"静态目录: {Path(args.web).resolve()}  会话落盘: {Path(args.session_root).resolve()}")
    if args.samples_dir:
        print(f"会话来源（受限）: {Path(args.samples_dir).resolve()}")
    else:
        print(f"会话来源（全扫）: Claude projects {Path(args.projects).resolve() if args.projects else Path.home() / '.claude' / 'projects'} + session_root")
    if args.codemaker or not args.samples_dir:
        print(f"Codemaker 会话库: {args.codemaker or Path.home() / '.local' / 'share' / 'codemaker' / 'opencode.db'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
codemaker_backend.py — 兼容对 Codemaker（OpenCode 系）会话数据的分析与实时挂接。

调研结论（详见 docs/codemaker-backend-research.md）：
  Codemaker 不产 DSH/Claude 式 JSONL 会话日志，而是把会话持久化在
  `~/.local/share/codemaker/opencode.db`（SQLite，OpenCode 数据模型）：
    - session  表：会话元数据（id/title/directory/model/cost/tokens*/time_*）；
    - message  表：消息行（data JSON：role/user|assistant、time{created,completed}、
                  finish stop|tool-calls、error、tokens、cost、modelID/providerID）；
    - part     表：消息内容块（data JSON：text / reasoning / tool{callID,state} /
                  step-start / step-finish{reason,tokens,cost} / compaction）；
    - todo     表：会话子任务（content/status/priority/position）；
    - event    表：**追加式事件日志**（aggregate_id=session、seq 连续、type=
                  session.created.1/session.updated.1/message.updated.1/
                  message.part.updated.1），与 DSH 的 append-only SessionEvent
                  同构——这是实时挂接的关键：轮询 seq 增量即可得到实时事件流。

本模块提供：
  - CodemakerDB：只读访问 opencode.db（session/message/part/todo/event）；
  - CodemakerEventAdapter：DB 行 / event 行 → 统一事件 dict（EventMetrics 可消费，
    与 dsh_backend/claude_backend/tail_attach 同构）；
  - CodemakerTails：event 表实时尾随（先重放现有快照，再轮询新 event 增量）；
  - scan_codemaker_log：离线整会话解析 → EventMetrics 快照（供 CLI/测试）。

用法（离线解析）：
    from codemaker_backend import scan_codemaker_log
    r = scan_codemaker_log()                      # 默认 ~/.local/share/codemaker/opencode.db
    for s in r["sessions"]:
        print(s["session_id"], s["model"], s["metrics"]["tool_calls_total"])

用法（实时挂接）：
    from codemaker_backend import CodemakerTails
    tails = CodemakerTails(on_events=lambda sid, evs: hub.publish(...))
    tails.start("ses_xxx", db_path)
"""

import json
import os
import queue
import sqlite3
import subprocess
import threading
import time
from pathlib import Path


# ---------- 常量 ----------

def _default_db_path() -> Path:
    """Codemaker 会话库默认位置（OpenCode 数据模型）。"""
    return Path.home() / ".local" / "share" / "codemaker" / "opencode.db"


def is_codemaker_db(path) -> bool:
    """判定 SQLite 文件是否为 Codemaker/OpenCode 会话库（有 session/message/part 表）。"""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('session','message','part')"
            ).fetchall()
            return len(rows) == 3
        finally:
            con.close()
    except Exception:
        return False


def _db_path_or_default(db_path) -> Path:
    if db_path:
        return Path(db_path)
    return _default_db_path()


# ---------- DB 只读访问 ----------

class CodemakerDB:
    """只读访问 Codemaker opencode.db（多线程安全：每操作新建短连接）。"""

    def __init__(self, db_path=None):
        self.db_path = _db_path_or_default(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)

    # ---- session ----

    def list_sessions(self) -> list[dict]:
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT id, title, directory, path, model, agent, cost, "
                    "tokens_input, tokens_output, tokens_reasoning, "
                    "tokens_cache_read, tokens_cache_write, "
                    "time_created, time_updated, time_archived "
                    "FROM session ORDER BY time_updated DESC"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        out = []
        for r in rows:
            out.append({
                "session_id": r[0], "title": r[1], "directory": r[2], "path": r[3],
                "model": _parse_model(r[4]), "agent": r[5], "cost_usd": r[6],
                "tokens": {"input": r[7] or 0, "output": r[8] or 0,
                           "reasoning": r[9] or 0, "cache_read": r[10] or 0,
                           "cache_write": r[11] or 0},
                "started_at": r[12], "updated_at": r[13], "archived_at": r[14],
            })
        return out

    def get_session(self, session_id: str) -> dict | None:
        for s in self.list_sessions():
            if s["session_id"] == session_id:
                return s
        return None

    # ---- message / part（重放快照） ----

    def messages(self, session_id: str) -> list[dict]:
        """该会话的消息行（按 time_created 升序），data 已解析为 dict。"""
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT id, time_created, data FROM message "
                    "WHERE session_id=? ORDER BY time_created ASC", (session_id,)
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                data = json.loads(r[2])
            except Exception:
                data = {}
            out.append({"id": r[0], "time_created": r[1], "data": data})
        return out

    def parts(self, session_id: str) -> list[dict]:
        """该会话的 part 行（按 time_created 升序），data 已解析为 dict。"""
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT id, message_id, time_created, data FROM part "
                    "WHERE session_id=? ORDER BY time_created ASC", (session_id,)
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                data = json.loads(r[3])
            except Exception:
                data = {}
            out.append({"id": r[0], "message_id": r[1], "time_created": r[2], "data": data})
        return out

    def todos(self, session_id: str) -> list[dict]:
        """会话子任务（todo 表）。"""
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT content, status, priority, position, time_created, time_updated "
                    "FROM todo WHERE session_id=? ORDER BY position ASC", (session_id,)
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        return [{"subject": r[0], "status": r[1], "priority": r[2], "position": r[3],
                 "created_ms": r[4], "updated_ms": r[5]} for r in rows]

    # ---- event（实时增量） ----

    def max_seq(self, session_id: str) -> int:
        try:
            con = self._connect()
            try:
                r = con.execute(
                    "SELECT MAX(seq) FROM event WHERE aggregate_id=?", (session_id,)
                ).fetchone()
            finally:
                con.close()
        except Exception:
            return 0
        return r[0] if r and r[0] is not None else 0

    def events_after(self, session_id: str, after_seq: int) -> list[dict]:
        """seq > after_seq 的 event 行（按 seq 升序），data 已解析为 dict。"""
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT id, seq, type, data FROM event "
                    "WHERE aggregate_id=? AND seq>? ORDER BY seq ASC",
                    (session_id, after_seq),
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                data = json.loads(r[3])
            except Exception:
                data = {}
            out.append({"id": r[0], "seq": r[1], "type": r[2], "data": data})
        return out


# ---------- 工具 ----------

def _parse_model(model: str | dict) -> str | None:
    """session.model 是 JSON 串 {"id":"deepseek-v4-pro","providerID":"netease-codemaker"}。"""
    if isinstance(model, dict):
        return model.get("id") or model.get("modelID")
    if not model:
        return None
    try:
        obj = json.loads(model)
        if isinstance(obj, dict):
            return obj.get("id") or obj.get("modelID")
    except Exception:
        pass
    return None


def _tokens_to_usage(tokens) -> dict:
    """Codemaker tokens {input,output,reasoning,cache{read,write}} → DSH 驼峰口径。"""
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache") or {}
    return {
        "inputTokens": tokens.get("input", 0),
        "outputTokens": tokens.get("output", 0),
        "cacheReadTokens": cache.get("read", 0),
        "cacheWriteTokens": cache.get("write", 0),
    }


def _finish_to_stop_reason(finish: str | None) -> str | None:
    """Codemaker message.finish → DSH stop_reason 口径（EventMetrics 兜底结束原因）。"""
    return {"stop": "end_turn", "tool-calls": "tool_use"}.get(finish)


def _finish_to_end_kind(finish: str | None, error=None) -> str | None:
    """message.finish / error → turn/end reason.kind。"""
    if error:
        name = str((error.get("name") or "")).lower()
        return "aborted" if "abort" in name else "error"
    if finish == "stop":
        return "completed"
    return None  # tool-calls / 无 → 中途，不结束回合


def _tool_state_text(state) -> str:
    """tool part 的 state → 结果文本（output / metadata.output）。"""
    if not isinstance(state, dict):
        return ""
    out = state.get("output")
    if out is None:
        meta = state.get("metadata")
        if isinstance(meta, dict):
            out = meta.get("output")
    return str(out or "")


# ---------- 事件适配器 ----------

class CodemakerEventAdapter:
    """
    把 Codemaker 的 DB 行 / event 行转成统一事件 dict（与 DSH SessionEvent 同构）。

    两种入口：
      - replay(session_id, db)：读 message/part 表最终快照 → 完整事件列表（离线重放）；
      - adapt_event(event_row, state)：event 表增量行 → 事件列表（实时挂接，state 去重）。
    """

    def __init__(self):
        self._turn = 0          # 用户回合计数（user/message 时 +1）
        self._step = 0          # assistant 消息计数

    # ---- 重放：message/part 表最终快照 ----

    def replay(self, db: CodemakerDB, session_id: str) -> list:
        """读 message/part 表 → 统一事件列表（按时间序）。"""
        messages = db.messages(session_id)
        parts = db.parts(session_id)
        by_msg: dict[str, list] = {}
        for p in parts:
            by_msg.setdefault(p["message_id"], []).append(p)
        events: list = []
        for msg in messages:
            evs = self._message_to_events(msg, by_msg.get(msg["id"], []))
            events.extend(evs)
        return events

    def _message_to_events(self, msg: dict, parts: list) -> list:
        data = msg.get("data") or {}
        role = data.get("role")
        created = (data.get("time") or {}).get("created") or msg.get("time_created")
        completed = (data.get("time") or {}).get("completed")
        events: list = []
        if role == "user":
            # 用户消息：文本来自 text part（一条 user/message）
            text = "\n".join(
                (p.get("data") or {}).get("text", "")
                for p in parts if (p.get("data") or {}).get("type") == "text"
            ).strip()
            if text:
                self._turn += 1
                events.append({
                    "type": "user/message", "seq": -1, "time": created,
                    "data": {"content": [{"type": "text", "text": text}]},
                })
            return events

        if role != "assistant":
            return events
        # assistant：step/start（step-start part）→ tool/text/reasoning → step/end（step-finish part）
        step = self._step
        self._step += 1
        model = data.get("modelID") or data.get("model")
        usage = _tokens_to_usage(data.get("tokens"))
        stop_reason = _finish_to_stop_reason(data.get("finish"))
        error = data.get("error")
        text_parts, reasoning_parts, tool_parts = [], [], []
        step_start_ms = step_end_ms = None
        for p in parts:
            pd = p.get("data") or {}
            ptype = pd.get("type")
            if ptype == "text":
                text_parts.append(pd.get("text", ""))
            elif ptype == "reasoning":
                reasoning_parts.append(pd.get("text", ""))
            elif ptype == "tool":
                tool_parts.append(pd)
            elif ptype == "step-start":
                step_start_ms = p.get("time_created")
            elif ptype == "step-finish":
                step_end_ms = p.get("time_created")
        if step_start_ms is not None:
            events.append({"type": "step/start", "seq": -1, "time": step_start_ms,
                           "data": {"step": step, "turn": self._turn}})
        # tool/call + tool/result（part 自带完整 state）
        for tp in tool_parts:
            call_id = tp.get("callID") or ""
            name = tp.get("tool") or ""
            state = tp.get("state") or {}
            t_ms = tp.get("time_created") or created
            events.append({
                "type": "tool/call", "seq": -1, "time": t_ms,
                "data": {
                    "turn": self._turn, "step": step,
                    "callId": call_id, "name": name,
                    "arguments": json.dumps(state.get("input") or {}, ensure_ascii=False),
                },
            })
            is_err = str(state.get("status", "")) == "error"
            events.append({
                "type": "tool/result", "seq": -1, "time": t_ms,
                "data": {
                    "turn": self._turn, "step": step, "callId": call_id,
                    "message": {"role": "user", "content": [{
                        "type": "tool-result",
                        "content": _tool_state_text(state),
                        "isError": is_err,
                    }]},
                },
            })
        # assistant/message：文本 + 推理 + usage + stop_reason
        content = []
        content += [{"type": "reasoning", "text": t} for t in reasoning_parts if t]
        content += [{"type": "text", "text": t} for t in text_parts if t]
        if content:
            events.append({
                "type": "assistant/message", "seq": -1,
                "time": completed or created or step_end_ms,
                "data": {
                    "turn": self._turn, "step": step,
                    "message": {
                        "model": model or "",
                        "content": content,
                        "usage": usage,
                        "stop_reason": stop_reason,
                    },
                },
            })
        if step_end_ms is not None:
            events.append({"type": "step/end", "seq": -1, "time": step_end_ms,
                           "data": {"step": step, "turn": self._turn}})
        # turn/end：finish==stop 或 error（tool-calls 不结束回合）
        kind = _finish_to_end_kind(data.get("finish"), error)
        if kind is not None:
            events.append({
                "type": "turn/end", "seq": -1,
                "time": completed or step_end_ms or created,
                "data": {"turn": self._turn, "reason": {"kind": kind}},
            })
        return events

    # ---- 实时：event 表增量行 ----

    def adapt_event(self, event_row: dict, state: dict) -> list:
        """
        一条 event 行 → 统一事件列表。

        state（每会话持久，跨轮询累积）：
            {"msg_info": {msg_id: info}, "part": {part_id: part},
             "user_done": set, "assistant_done": set, "tool_done": set,
             "step_done": set, "msg_step": {msg_id: step}, "turn": int}
        """
        etype = event_row.get("type", "")
        data = event_row.get("data") or {}
        evs: list = []
        if etype == "message.updated.1":
            info = data.get("info") or {}
            mid = info.get("id") or ""
            if mid:
                state.setdefault("msg_info", {})[mid] = info
                evs += self._emit_user_if_ready(mid, info, state)
                evs += self._emit_assistant_if_finished(mid, info, state)
        elif etype == "message.part.updated.1":
            part = data.get("part") or {}
            pid = part.get("id") or ""
            if pid:
                state.setdefault("part", {})[pid] = part
                evs += self._emit_tool_if_done(pid, part, state)
                evs += self._emit_step_if_done(pid, part, state)
                # 文本 part 到达后补发对应 user/assistant 消息
                mid = part.get("messageID") or ""
                info = (state.get("msg_info") or {}).get(mid) or {}
                if info:
                    evs += self._emit_user_if_ready(mid, info, state)
                    evs += self._emit_assistant_if_finished(mid, info, state)
        return evs

    def _emit_user_if_ready(self, mid: str, info: dict, state: dict) -> list:
        if info.get("role") != "user":
            return []
        done = state.setdefault("user_done", set())
        if mid in done:
            return []
        # 用户文本在 text part；聚合当前已见文本
        text = self._collect_part_text(state, mid)
        if not text:
            return []  # 等待 text part 到达
        done.add(mid)
        state["turn"] = state.get("turn", 0) + 1
        return [{
            "type": "user/message", "seq": -1,
            "time": (info.get("time") or {}).get("created"),
            "data": {"content": [{"type": "text", "text": text}]},
        }]

    def _emit_assistant_if_finished(self, mid: str, info: dict, state: dict) -> list:
        if info.get("role") != "assistant":
            return []
        done = state.setdefault("assistant_done", set())
        if mid in done:
            return []
        finish = info.get("finish")
        error = info.get("error")
        kind = _finish_to_end_kind(finish, error)
        # 仅当回合已结束（stop/error）才发 assistant/message + turn/end
        if kind is None:
            return []
        texts = self._collect_part_texts(state, mid)
        if not texts:
            return []  # 文本 part 尚未到达：不标记 done，等 part 事件再触发
        done.add(mid)
        step = state.get("msg_step", {})
        if mid not in step:
            step[mid] = len(step)
        step_no = step[mid]
        texts = self._collect_part_texts(state, mid)
        usage = _tokens_to_usage(info.get("tokens"))
        evs = []
        if texts:
            evs.append({
                "type": "assistant/message", "seq": -1,
                "time": (info.get("time") or {}).get("completed"),
                "data": {
                    "turn": state.get("turn", 0), "step": step_no,
                    "message": {
                        "model": info.get("modelID") or "",
                        "content": [{"type": "text", "text": t} for t in texts],
                        "usage": usage,
                        "stop_reason": _finish_to_stop_reason(finish),
                    },
                },
            })
        evs.append({
            "type": "turn/end", "seq": -1,
            "time": (info.get("time") or {}).get("completed"),
            "data": {"turn": state.get("turn", 0), "reason": {"kind": kind}},
        })
        return evs

    def _emit_tool_if_done(self, pid: str, part: dict, state: dict) -> list:
        if (part.get("type") or "") != "tool":
            return []
        done = state.setdefault("tool_done", set())
        if pid in done:
            return []
        st = part.get("state") or {}
        if str(st.get("status", "")) not in ("completed", "error"):
            return []  # 等待完成态
        done.add(pid)
        mid = part.get("messageID") or ""
        step_map = state.setdefault("msg_step", {})
        if mid not in step_map:
            step_map[mid] = len(step_map)
        t_ms = part.get("time") or (part.get("time_created"))
        call_id = part.get("callID") or ""
        name = part.get("tool") or ""
        is_err = str(st.get("status", "")) == "error"
        return [
            {"type": "tool/call", "seq": -1, "time": t_ms,
             "data": {"turn": state.get("turn", 0), "step": step_map[mid],
                      "callId": call_id, "name": name,
                      "arguments": json.dumps(st.get("input") or {}, ensure_ascii=False)}},
            {"type": "tool/result", "seq": -1, "time": t_ms,
             "data": {"turn": state.get("turn", 0), "step": step_map[mid],
                      "callId": call_id,
                      "message": {"role": "user", "content": [{
                          "type": "tool-result",
                          "content": _tool_state_text(st),
                          "isError": is_err,
                      }]}}},
        ]

    def _emit_step_if_done(self, pid: str, part: dict, state: dict) -> list:
        ptype = part.get("type")
        if ptype not in ("step-start", "step-finish"):
            return []
        done = state.setdefault("step_done", set())
        if pid in done:
            return []
        done.add(pid)
        mid = part.get("messageID") or ""
        step_map = state.setdefault("msg_step", {})
        if mid not in step_map:
            step_map[mid] = len(step_map)
        t_ms = part.get("time") or part.get("time_created")
        etype = "step/start" if ptype == "step-start" else "step/end"
        return [{"type": etype, "seq": -1, "time": t_ms,
                 "data": {"step": step_map[mid], "turn": state.get("turn", 0)}}]

    @staticmethod
    def _collect_part_texts(state: dict, mid: str) -> list:
        """该 message 下已见 text part 的文本列表。"""
        out = []
        for part in (state.get("part") or {}).values():
            if part.get("messageID") == mid and part.get("type") == "text":
                t = part.get("text")
                if t:
                    out.append(t)
        return out

    def _collect_part_text(self, state: dict, mid: str) -> str:
        return "\n".join(self._collect_part_texts(state, mid)).strip()


# ---------- 实时尾随 ----------

class CodemakerTails:
    """
    Codemaker 会话实时挂接：先重放现有快照（message/part 表），再轮询 event 表
    seq 增量转统一事件。与 JsonlTails 同接口（on_events 回调）。
    """

    def __init__(self, on_events, poll_interval: float = 1.0):
        self._on_events = on_events
        self._poll = poll_interval
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, session_id: str, db_path=None) -> bool:
        p = _db_path_or_default(db_path)
        if not is_codemaker_db(p):
            return False
        with self._lock:
            if session_id in self._sessions:
                return True
            state = {
                "db_path": p, "last_seq": 0,
                "adapter": CodemakerEventAdapter(),
                "ev_state": {}, "stop": threading.Event(),
            }
            self._sessions[session_id] = state
        t = threading.Thread(target=self._poll_loop, args=(session_id, state), daemon=True)
        t.start()
        return True

    def stop(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is not None:
            state["stop"].set()

    def stop_all(self) -> None:
        with self._lock:
            states = list(self._sessions.values())
            self._sessions.clear()
        for st in states:
            st["stop"].set()

    def running(self) -> list:
        with self._lock:
            return list(self._sessions.keys())

    def _poll_loop(self, session_id: str, state: dict) -> None:
        db = CodemakerDB(state["db_path"])
        # 首轮：重放现有快照（含初始 last_seq）
        try:
            events = state["adapter"].replay(db, session_id)
            state["last_seq"] = db.max_seq(session_id)
            if events:
                try:
                    self._on_events(session_id, events)
                except Exception:
                    pass
        except Exception:
            pass
        while not state["stop"].wait(self._poll):
            try:
                rows = db.events_after(session_id, state["last_seq"])
            except Exception:
                continue
            if not rows:
                continue
            state["last_seq"] = rows[-1]["seq"]
            events = []
            for row in rows:
                try:
                    events.extend(state["adapter"].adapt_event(row, state["ev_state"]))
                except Exception:
                    pass
            if events:
                try:
                    self._on_events(session_id, events)
                except Exception:
                    pass


# ---------- 离线扫描 ----------

def scan_codemaker_log(db_path=None, session_id: str | None = None) -> dict:
    """解析 Codemaker 会话库 → 每会话 EventMetrics 快照。

    session_id 缺省解析库中全部会话；返回 {"db_path", "sessions": [...]}。
    """
    from dsh_backend import EventMetrics
    db = CodemakerDB(db_path)
    sessions = db.list_sessions()
    if session_id is not None:
        sessions = [s for s in sessions if s["session_id"] == session_id]
    out = []
    for s in sessions:
        adapter = CodemakerEventAdapter()
        metrics = EventMetrics()
        for event in adapter.replay(db, s["session_id"]):
            metrics.on_event(event)
        metrics.finalize()
        snapshot = metrics.snapshot()
        snapshot["model"] = snapshot["model"] or s.get("model")
        snapshot["cost_usd"] = s.get("cost_usd")
        snapshot["title"] = s.get("title")
        snapshot["directory"] = s.get("directory")
        snapshot["started_at"] = s.get("started_at")
        snapshot["ended_at"] = s.get("updated_at")
        # 子任务：todo 表 → tasks[0].subitems（Codemaker 用 todo 而非 TaskCreate）
        todos = db.todos(s["session_id"])
        if todos:
            tasks = snapshot.get("tasks") or []
            if not tasks:
                tasks = [{"query": s.get("title") or "", "tool_calls": 0,
                          "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                          "start_ms": None, "end_ms": None}]
                snapshot["tasks"] = tasks
            tasks[0]["subitems"] = [{
                "id": str(t["position"]), "subject": t["subject"], "status": t["status"],
                "created_ms": t["created_ms"], "updated_ms": t["updated_ms"],
            } for t in todos]
        out.append({"session_id": s["session_id"], "model": s.get("model"),
                    "title": s.get("title"), "directory": s.get("directory"),
                    "metrics": snapshot})
    return {"db_path": str(db.db_path), "sessions": out}


# ---------- 独立运行 ----------

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="解析 Codemaker 会话库")
    parser.add_argument("--db", default=None, help="opencode.db 路径（缺省自动探测）")
    parser.add_argument("--session", default=None, help="指定会话 id（缺省全部）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    result = scan_codemaker_log(args.db, args.session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for s in result["sessions"]:
        m = s["metrics"]
        line = (f"{s['session_id'][:32]:32} {str(s['model'] or ''):22} "
                f"tools={m.get('tool_calls_total', 0):3d} "
                f"ok={m.get('tool_success', 0):3d} fail={m.get('tool_fail', 0):2d} "
                f"turns={m.get('user_turns', 0):2d} end={m.get('turn_end_reason')} "
                f"cost=${m.get('cost_usd')}")
        print(line)
    print(f"共 {len(result['sessions'])} 个会话（{result['db_path']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------- CLI headless 跑测（M2：codemaker run --format json） ----------

class CodemakerCliAdapter:
    """codemaker run --format json 逐行事件 → 统一事件（流式，step 按 messageID 分配）。

    与 CodemakerEventAdapter（DB 快照/event 增量）互补：本类吃 CLI headless 的 stdout
    JSON 行（顶层 type=step_start/tool_use/text/step_finish，part 字段与 DB part 同构）。
    step_finish.tokens 是累计值——只在 reason=stop 的最终回合带 usage，避免重复累加。
    """

    def __init__(self):
        self._turn = 0
        self._msg_step = {}     # messageID -> step 号
        self._msg_texts = {}    # messageID -> [(ptype, text)]（等 step-finish 发 assistant/message）

    def adapt_line(self, line: str) -> list:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        return self.adapt(obj)

    def adapt(self, obj: dict) -> list:
        t_ms = obj.get("timestamp")
        part = obj.get("part") or {}
        ptype = part.get("type")
        mid = part.get("messageID") or ""
        if mid not in self._msg_step:
            self._msg_step[mid] = len(self._msg_step)
        step = self._msg_step[mid]
        if ptype == "step-start":
            return [{"type": "step/start", "seq": -1, "time": t_ms,
                     "data": {"step": step, "turn": self._turn}}]
        if ptype == "tool":
            return self._tool_events(part, t_ms, step)
        if ptype in ("text", "reasoning"):
            text = part.get("text", "")
            if text:
                self._msg_texts.setdefault(mid, []).append((ptype, text))
            return []
        if ptype == "step-finish":
            return self._finish_events(part, t_ms, step, mid)
        return []

    def _tool_events(self, part: dict, t_ms, step: int) -> list:
        call_id = part.get("callID") or ""
        name = part.get("tool") or ""
        state = part.get("state") or {}
        is_err = str(state.get("status", "")) == "error"
        return [
            {"type": "tool/call", "seq": -1, "time": t_ms,
             "data": {"turn": self._turn, "step": step, "callId": call_id, "name": name,
                      "arguments": json.dumps(state.get("input") or {}, ensure_ascii=False)}},
            {"type": "tool/result", "seq": -1, "time": t_ms,
             "data": {"turn": self._turn, "step": step, "callId": call_id,
                      "message": {"role": "user", "content": [{
                          "type": "tool-result",
                          "content": _tool_state_text(state),
                          "isError": is_err}]}}},
        ]

    def _finish_events(self, part: dict, t_ms, step: int, mid: str) -> list:
        reason = part.get("reason")
        usage = _tokens_to_usage(part.get("tokens"))
        evs = [{"type": "step/end", "seq": -1, "time": t_ms,
                "data": {"step": step, "turn": self._turn}}]
        texts = self._msg_texts.pop(mid, [])
        content = [{"type": "reasoning" if ptype == "reasoning" else "text", "text": text}
                   for ptype, text in texts if text]
        if content:
            evs.append({"type": "assistant/message", "seq": -1, "time": t_ms,
                        "data": {"turn": self._turn, "step": step,
                                 "message": {"model": "", "content": content,
                                             "usage": usage if reason == "stop" else {},
                                             "stop_reason": _finish_to_stop_reason(reason)}}})
        if reason == "stop":
            evs.append({"type": "turn/end", "seq": -1, "time": t_ms,
                        "data": {"turn": self._turn, "reason": {"kind": "completed"}}})
        return evs


class CodemakerEvalBackend:
    """codemaker CLI headless 评测后端（codemaker run --format json），与 ClaudeEvalBackend 同接口。"""

    def __init__(self, bin_path: str = None, cwd: str = None, session_root: str = None,
                 model: str = None, agent: str = None):
        self.bin_path = bin_path or str(Path.home() / ".codemaker" / "bin" / "codemaker.exe")
        self.cwd = cwd or str(Path.cwd())
        self.session_root = Path(session_root) if session_root else None
        self.model = model
        self.agent = agent

    def close(self) -> None:
        """无长驻资源（每次 run_task 独立 subprocess），保留以对齐 ClaudeEvalBackend 接口。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def run_task(self, task: dict, session_id: str = None, timeout_s: int = 300,
                 on_event=None, on_warning=None, cancel_event=None) -> dict:
        """跑一个评测任务：codemaker run --format json --auto → 逐行解析 → 增量折叠指标。"""
        from dsh_backend import EventMetrics
        task_id = task.get("task_id", "unknown")
        session_id = session_id or f"eval-{task_id}-{int(time.time() * 1000)}"
        query = task.get("query", "")

        metrics = EventMetrics()
        adapter = CodemakerCliAdapter()
        warnings_seen = set()
        timeout_flag = {"hit": False}
        cost_usd = 0.0

        raw_path = log_path = None
        raw_fp = log_fp = None
        if self.session_root is not None:
            sess_dir = self.session_root / session_id
            sess_dir.mkdir(parents=True, exist_ok=True)
            raw_path = sess_dir / "raw.jsonl"
            log_path = sess_dir / "session.jsonl"
            raw_fp = open(raw_path, "w", encoding="utf-8")
            log_fp = open(log_path, "w", encoding="utf-8")
            log_fp.write(json.dumps({
                "type": "session", "version": 0, "id": session_id,
                "createdAt": int(time.time() * 1000), "cwd": self.cwd,
            }, ensure_ascii=False) + "\n")

        def _emit(event: dict) -> None:
            metrics.on_event(event)
            if on_event is not None:
                on_event(event)
            if log_fp is not None:
                log_fp.write(json.dumps(event, ensure_ascii=False) + "\n")
                log_fp.flush()
            for w in metrics.check_warnings():
                if w not in warnings_seen:
                    warnings_seen.add(w)
                    if on_warning is not None:
                        on_warning(w)

        # 任务 query 作为第一条 user/message（CLI 不产 user 事件，需手动注入）
        _emit({"type": "user/message", "seq": -1, "time": int(time.time() * 1000),
               "data": {"content": [{"type": "text", "text": query}]}})

        cmd = [self.bin_path, "run", "--format", "json", "--auto"]
        if self.model:
            cmd += ["--model", self.model]
        if self.agent:
            cmd += ["--agent", self.agent]
        cmd += ["--dir", self.cwd, "--title", session_id, query]

        q = queue.Queue()

        def _drain(pipe, tag):
            try:
                for line in iter(pipe.readline, ""):
                    q.put((tag, line))
            finally:
                pipe.close()

        started_wall = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, cwd=self.cwd, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
            t_out = threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True)
            t_out.start()
            t_err.start()
            deadline = time.time() + timeout_s
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    timeout_flag["hit"] = True
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    timeout_flag["hit"] = True
                    break
                try:
                    tag, line = q.get(timeout=min(max(remaining, 0.01), 0.5))
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if tag == "err":
                    continue
                if not line:
                    continue
                if raw_fp is not None:
                    raw_fp.write(line)
                    raw_fp.flush()
                # 累计官方成本（step_finish.cost 是增量）
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    part = obj.get("part") or {}
                    if part.get("type") == "step-finish" and isinstance(part.get("cost"), (int, float)):
                        cost_usd += part["cost"]
                for event in adapter.adapt_line(line):
                    _emit(event)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            if raw_fp is not None:
                raw_fp.close()
            if log_fp is not None:
                log_fp.close()

        metrics.finalize()
        duration_s = round(time.monotonic() - started_wall, 2)
        snapshot = metrics.snapshot()
        snapshot["cost_usd"] = cost_usd or None
        snapshot["model"] = snapshot.get("model") or self.model
        return {
            "session_id": session_id,
            "task_id": task_id,
            "query": query,
            "metrics": snapshot,
            "assistant_text": metrics.assistant_text(),
            "finish_reason": metrics.turn_end_reason,
            "duration_s": duration_s,
            "timeout": timeout_flag["hit"],
            "log_path": str(log_path) if log_path else None,
            "raw_path": str(raw_path) if raw_path else None,
            "warnings": sorted(warnings_seen),
        }

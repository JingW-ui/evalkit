#!/usr/bin/env python3
"""
tail_attach.py — 外部 Claude 会话实时挂接（R2b 核心）。

对正在写入的 Claude Code 会话 JSONL（~/.claude/projects/<slug>/<sessionId>.jsonl）
做增量尾随：轮询文件大小 → 读新增行 → 逐行转成**统一事件**（与
claude_backend/dsh_backend 同构，EventMetrics 可消费）→ 回调上抛。

转换器 claude_jsonl_to_events：Claude Code JSONL 行 → 统一事件（字段对齐 parser.py）：
  - type=="assistant"：content[].tool_use → tool/call；content[].text → assistant/message；
    message.usage → usage（归一化驼峰）；stop_reason 记录在 extra
  - type=="user"：content 为 str/list[text] → user/message（真实指令）；
    content[].tool_result → tool/result（tool_use_id 配对，is_error）
  - type=="attachment"/"summary"/其它 → 忽略（不产事件）

用法：
    from tail_attach import JsonlTails, claude_jsonl_to_events
    tails = JsonlTails(on_events=lambda sid, events: hub.publish(...))
    tails.start("session-1", "/path/to/session.jsonl")
    tails.stop("session-1")
"""

import json
import time
import threading
from pathlib import Path


def _iso_to_ms(ts) -> int | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _normalize_usage(usage):
    """Claude usage（下划线）→ DSH 驼峰口径（与 claude_backend 一致）。"""
    if not isinstance(usage, dict):
        return usage
    return {
        "inputTokens": usage.get("input_tokens", 0),
        "outputTokens": usage.get("output_tokens", 0),
        "cacheReadTokens": usage.get("cache_read_input_tokens", 0),
        "cacheWriteTokens": usage.get("cache_creation_input_tokens", 0),
    }


def claude_jsonl_to_events(obj: dict, seq: int, turn: int = 0) -> list:
    """
    Claude Code JSONL 一行 → 统一事件列表。

    Args:
        obj: 一行 JSON。
        seq: 当前 seq（调用方维护，事件按行递增）。
        turn: 归属 turn（单会话尾随简化为 0，step 按行分配）。
    Returns:
        统一事件列表（可能为空）。
    """
    etype = obj.get("type", "")
    t_ms = _iso_to_ms(obj.get("timestamp"))
    events = []
    step = seq

    if etype == "assistant":
        msg = obj.get("message") or {}
        usage = _normalize_usage(msg.get("usage"))
        model = msg.get("model") or ""
        saw_tool_use = False
        saw_message = False
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                saw_tool_use = True
                events.append({
                    "type": "tool/call", "seq": seq, "time": t_ms,
                    "data": {
                        "turn": turn, "step": step,
                        "callId": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif btype in ("text", "thinking"):
                text = block.get("text", "")
                if text:
                    saw_message = True
                    events.append({
                        "type": "assistant/message", "seq": seq, "time": t_ms,
                        "data": {
                            "turn": turn, "step": step,
                            "message": {
                                "model": model,
                                "content": [{"type": "text", "text": text}],
                                "usage": usage,
                                "stop_reason": msg.get("stop_reason") or None,
                            },
                        },
                    })
        # 仅含 tool_use（无文本）的 assistant 消息：补空 assistant/message 作时间锚点，
        # 保证轨迹的模型思考块 [上一活动, 本条] 覆盖思考时段（对齐 DSH Tool call only 消息）
        if saw_tool_use and not saw_message:
            events.append({
                "type": "assistant/message", "seq": seq, "time": t_ms,
                "data": {
                    "turn": turn, "step": step,
                    "message": {"model": model, "content": [], "stop_reason": None},
                },
            })
    elif etype == "user":
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text and not text.startswith("/") and not text.startswith("<") \
                    and not text.startswith("Base directory for this skill"):
                events.append({
                    "type": "user/message", "seq": seq, "time": t_ms,
                    "data": {"content": [{"type": "text", "text": text}]},
                })
        elif isinstance(content, list):
            has_tool_result = False
            texts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    has_tool_result = True
                    events.append({
                        "type": "tool/result", "seq": seq, "time": t_ms,
                        "data": {
                            "turn": turn, "step": step,
                            "callId": block.get("tool_use_id", ""),
                            "message": {
                                "role": "user",
                                "content": [{
                                    "type": "tool-result",
                                    "content": block.get("content"),
                                    "isError": bool(block.get("is_error", False)),
                                }],
                            },
                        },
                    })
                elif block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if not has_tool_result:
                full = "\n".join(texts).strip()
                if full and not full.startswith("/") and not full.startswith("<") \
                        and not full.startswith("Base directory for this skill"):
                    events.append({
                        "type": "user/message", "seq": seq, "time": t_ms,
                        "data": {"content": [{"type": "text", "text": full}]},
                    })
    # attachment：skill_listing（用户可见的可用 skill 列表）→ user/message（结构化 skills）
    if etype == "attachment":
        a = obj.get("attachment") or {}
        if a.get("type") == "skill_listing" and t_ms is not None:
            content = a.get("content") or ""
            skills = _parse_skill_listing(content)
            events.append({
                "type": "user/message", "seq": seq, "time": t_ms,
                "data": {
                    "content": [{"type": "text", "text": content}],
                    "skills": skills,
                    "system_injected": True,   # 系统注入（技能列表），不计真实用户轮次
                },
            })
    return events


def _parse_skill_listing(content: str) -> list:
    """解析 skill_listing 的 Markdown 列表（`- name: description`）为结构化 [{name, description}]。"""
    skills = []
    for line in (content or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        rest = line[2:].strip()
        if ":" not in rest:
            skills.append({"name": rest, "description": ""})
            continue
        name, _, desc = rest.partition(":")
        skills.append({"name": name.strip(), "description": desc.strip()})
    return skills


class JsonlTails:
    """
    多文件 JSONL 增量尾随：每个会话一个轮询线程。

    on_events(session_id, events)：新增行转换出的统一事件列表（可能为空）。
    """

    def __init__(self, on_events, poll_interval: float = 1.0):
        self._on_events = on_events
        self._poll = poll_interval
        self._files: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, session_id: str, path) -> bool:
        p = Path(path)
        if not p.is_file():
            return False
        with self._lock:
            if session_id in self._files:
                return True
            state = {
                "path": p, "offset": 0,     # 从 0 开始：先读已有内容（live 打开即有历史）
                "seq": 0, "stop": threading.Event(),
            }
            self._files[session_id] = state
        t = threading.Thread(target=self._poll_loop, args=(session_id, state), daemon=True)
        t.start()
        return True

    def stop(self, session_id: str) -> None:
        with self._lock:
            state = self._files.pop(session_id, None)
        if state is not None:
            state["stop"].set()

    def stop_all(self) -> None:
        with self._lock:
            states = list(self._files.values())
            self._files.clear()
        for st in states:
            st["stop"].set()

    def running(self) -> list:
        with self._lock:
            return list(self._files.keys())

    def _read_new(self, session_id: str, state: dict) -> None:
        """读取从 offset 起的新增行 → 统一事件回调。截断/轮转时从头重读。"""
        p: Path = state["path"]
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < state["offset"]:
            state["offset"] = 0          # 截断/轮转：从头重新读
        if size <= state["offset"]:
            return
        events = []
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                f.seek(state["offset"])
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.extend(claude_jsonl_to_events(obj, state["seq"]))
                    state["seq"] += 1
                state["offset"] = f.tell()
        except OSError:
            return
        if events:
            try:
                self._on_events(session_id, events)
            except Exception:
                pass  # 回调失败不杀尾随线程

    def _poll_loop(self, session_id: str, state: dict) -> None:
        first = True
        while not state["stop"].wait(0 if first else self._poll):
            first = False
            self._read_new(session_id, state)

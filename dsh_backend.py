#!/usr/bin/env python3
"""
dsh_backend.py — 基于 DeepSeek Harness（DSH）的实时评测后端（阶段 1）。

落地 ROADMAP 第 10 节「实时交互评测」：
  - 用 DSH Python SDK（jsonrpc-agent，stdio JSON-RPC）驱动被测 agent；
  - 每个 task 开一个新 session，session_prompt 发任务 query；
  - subscribe_session_notifications 实时订阅会话事件流（session.event 通知，
    承载原始 SessionEvent）；
  - 事件流增量折叠成评测指标（EventMetrics，纯增量、可中途 snapshot），
    补上离线日志缺失的 tool_result / turn_end_reason / TTFT 等信号；
  - 运行中告警回调（turn/end error、工具失败率、超时）；
  - 事件流逐行落盘为 DSH JSONL（供 session_report.scan_dsh_log 事后深度分析）。

被测对象解耦：task 声明沿用 evalkit tasks/*.json（level / skill_expected /
query / success_condition）。本模块只负责「跑 + 实时观测」，成功判定由上层
（parser.replay_metrics 等）基于落盘日志或事件流完成。

依赖：pip install deepseek-harness（或把 DSH python/sdk 加入 PYTHONPATH）。
未安装时本模块仍可 import（惰性报错），不影响 evalkit 其他离线工具。

用法（代码内）：
    from dsh_backend import DshEvalBackend
    with DshEvalBackend(model="deepseek-v4-flash",
                        cwd=r"D:\\wy_projects\\work_4_log",
                        session_root=r"D:\\wy_projects\\evalkit\\results\\dsh") as b:
        result = b.run_task(task, timeout_s=300, on_warning=print)
        print(result["metrics"])

用法（CLI 单任务演示）：
    python dsh_backend.py run --task tasks/g66_L3_001.json --session-root results/dsh
"""

import json
import sys
import time
import threading
import argparse
from pathlib import Path
from datetime import datetime

try:
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    _SDK_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - 环境缺失时惰性报错
    DeepSeekHarness = None
    DeepSeekHarnessConfig = None
    _SDK_IMPORT_ERROR = _exc


# ---------- 常量 ----------

# 人工介入工具名（DSH / Claude Code / Codemaker 三种口径）
_HUMAN_INTERVENTION_TOOLS = {"ask_user_question", "AskUserQuestion", "question"}
# skill 工具名（DSH 小写，归一为 Claude Code 口径）
_SKILL_TOOL_NAMES = {"skill", "Skill"}
# 事件里 isError 的判定位置（tool/result 顶层 error 或 message.content[].isError）
# 与 scan_dsh_log 对齐


def _is_tool_result_error(event_data: dict) -> bool:
    """判定一条 tool/result 事件是否失败：顶层 error 字段 或 message.content[] 的 isError。"""
    if event_data.get("error"):
        return True
    msg = event_data.get("message")
    if isinstance(msg, dict):
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("isError"):
                return True
    return False


def _normalize_tool_name(name: str) -> str:
    """DSH 工具名归一化到 Claude Code 口径（skill→Skill、todo_write→TodoWrite）。"""
    if name == "skill":
        return "Skill"
    if name == "todo_write":
        return "TodoWrite"
    return name


def _parse_skill_name(arguments) -> str:
    """tool/call 的 arguments 是 JSON 串，解析出 skill 名；失败退回 'skill'。"""
    if not isinstance(arguments, str):
        return "skill"
    try:
        arg = json.loads(arguments)
        if isinstance(arg, dict):
            return arg.get("name") or arg.get("skill") or "skill"
    except Exception:
        pass
    return "skill"


class EventMetrics:
    """
    事件流增量折叠器：每收到一个 SessionEvent dict 就增量更新评测指标。

    设计对齐 DSH session-stats 的思路（事件驱动、纯增量、可随时 snapshot），
    指标命名尽量与 parser.replay_metrics 的 metrics 对齐，便于上层直接消费。
    """

    def __init__(self):
        self.turns = 0
        self.steps = 0
        self.tool_calls_total = 0
        self.tool_calls_by_name = {}
        self.tool_success = 0
        self.tool_fail = 0
        self.tool_fail_by_name = {}
        self.tokens = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        self.user_turns = 0
        self.human_interventions = 0
        self.skill_loaded = None          # 首次加载的 skill 名（归一化口径）
        self.skill_count = 0
        self.turn_end_reasons = []        # 有序 turn/end reason.kind
        self.turn_end_reason = None       # 最后一条
        self.last_stop_reason = None      # 最后一条 assistant/message 的 stop_reason（兜底推断结束原因）
        self.skill_available = False      # 环境中是否装配了 skill 工具（request/header）
        self.model = None                 # 模型名（assistant/message 记录）
        self.model_turns = {}             # model -> 轮数
        self.tasks = []                   # 任务切分（按真实 user/message）
        self.ttft_ms = None               # 首个非空 delta chunk 的首 token 延迟
        self.llm_ms = 0.0                 # step/start → step/end 累计（模型活跃时间）
        self._llm_fallback_ms = 0.0       # 无 step 事件通道（claude replay）的模型活跃兜底
        self._has_step_events = False     # 事件流是否含 step/start（dsh/codemaker）
        self._assistant_pending_at = None # 上一条 assistant/message 时间（兜底：其后向间隙=模型活跃）
        self.tool_ms = 0.0                # tool/call → tool/result 累计（工具执行时间）
        self.human_wait_ms = 0.0          # 等待人为输入累计（AskUserQuestion/question 挂起）
        self.last_event_time = None       # 最近事件时间（毫秒 epoch）
        self.started_at_ms = None
        self.assistant_text_parts = []    # assistant/message 的 text 拼接（供锚点匹配）
        self.todo_latest = None           # 最新 todo/write 快照

        self._pending_calls = {}          # callId -> {"name", "start_ms"}
        self._human_wait_start = None     # 当前等待人为输入的开始时间（人工介入工具挂起中）
        self._step_start_ms = None
        self._turn = None
        self._step = None
        self._usage_by_step = {}          # (turn,step) -> usage dict（去重防双计）
        self._first_delta_at_step = None  # (turn,step) -> 首个 delta chunk 时间
        self._cur_task = None             # 当前任务（按 user/message 切分）
        self._task_items = {}             # taskId(int) -> 子任务（TaskCreate/TaskUpdate 追踪）
        self._create_counter = 0          # TaskCreate 出现顺序（Claude 常无 id，按序配对）

    # ---- 事件入口 ----

    def on_event(self, event: dict) -> None:
        """消费一个 SessionEvent（DSH 原始事件 dict）。"""
        etype = event.get("type", "")
        data = event.get("data") or {}
        t_ms = event.get("time")
        if t_ms is not None:
            self.last_event_time = t_ms
            if self.started_at_ms is None:
                self.started_at_ms = t_ms
            # 兜底模型活跃：无 step 事件时，「上一条 assistant/message → 本条事件」的后向间隙。
            # 只有下一事件是模型侧产出（tool/call / assistant 消息 / chunk）才计入模型活跃；
            # tool/result（工具执行，已计 tool_ms）与 user/message（等待用户输入 → 空闲）不计，
            # 避免把跨轮次的长间隙（用户思考/离开）误算成模型活跃。
            if not self._has_step_events and self._assistant_pending_at is not None:
                gap = max(0.0, t_ms - self._assistant_pending_at)
                self._assistant_pending_at = None
                if etype in ("tool/call", "assistant/message", "assistant/chunk"):
                    self._llm_fallback_ms += gap

        if etype == "turn/start":
            self._turn = data.get("turn")
        elif etype == "turn/end":
            reason = data.get("reason") or {}
            kind = reason.get("kind") if isinstance(reason, dict) else None
            self.turn_end_reason = kind
            self.turn_end_reasons.append(kind)
            self.turns += 1
        elif etype == "step/start":
            self.steps += 1
            self._has_step_events = True
            self._step_start_ms = t_ms
            self._step = data.get("step")
        elif etype == "step/end":
            if self._step_start_ms is not None and t_ms is not None:
                self.llm_ms += max(0.0, t_ms - self._step_start_ms)
            self._step_start_ms = None
        elif etype == "user/message":
            # 真实用户指令计数（排除系统注入：runtime context / skill reminder / bg job / skill_listing）
            if data.get("system_injected"):
                return
            texts = []
            for b in (data.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text", ""))
            full = "\n".join(texts).strip()
            if full and not (full.startswith("Current runtime context")
                             or full.startswith("<system-reminder>")
                             or full.startswith("background job")
                             or full.startswith("Base directory for this skill")):
                self.user_turns += 1
                # 任务切分：每条真实用户指令 = 一个新任务
                self._cur_task = {
                    "query": full[:200],
                    "tool_calls": 0,
                    "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                    "start_ms": t_ms,
                    "end_ms": None,
                }
                self.tasks.append(self._cur_task)
        elif etype == "assistant/message":
            msg = data.get("message") or {}
            # 兜底模型活跃：本条 assistant/message 起计时，下一事件结算（=模型工作到下次产出）
            if not self._has_step_events and t_ms is not None:
                self._assistant_pending_at = t_ms
            # 模型名（首次出现即记录）
            if msg.get("model"):
                self.model = msg["model"]
                self.model_turns[msg["model"]] = self.model_turns.get(msg["model"], 0) + 1
            if msg.get("stop_reason"):
                self.last_stop_reason = msg["stop_reason"]
            if t_ms is not None and self._cur_task is not None:
                self._cur_task["end_ms"] = t_ms
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("text", "reasoning"):
                    text = block.get("text")
                    if text:
                        self.assistant_text_parts.append(text)
            # usage（每步最终值，覆盖流式中间值）
            usage = msg.get("usage")
            if isinstance(usage, dict):
                self._usage_by_step[(self._turn, self._step)] = usage
        elif etype == "assistant/chunk":
            chunk = data.get("chunk") or {}
            # TTFT：首个非空 delta 块
            if chunk.get("type") in ("text-delta", "reasoning-delta", "tool-call-delta"):
                key = (self._turn, self._step)
                if self._first_delta_at_step is None or key not in self._first_delta_at_step:
                    if self._first_delta_at_step is None:
                        self._first_delta_at_step = {}
                    self._first_delta_at_step.setdefault(key, t_ms)
                    if self.ttft_ms is None and t_ms is not None and self._step_start_ms is not None:
                        self.ttft_ms = t_ms - self._step_start_ms
            # usage 兜底（仅当该 step 尚无任何 usage 记录时）
            if chunk.get("type") == "usage" and isinstance(chunk.get("usage"), dict):
                self._usage_by_step.setdefault((self._turn, self._step), chunk["usage"])
        elif etype == "tool/call":
            self.tool_calls_total += 1
            name = _normalize_tool_name(data.get("name", ""))
            self.tool_calls_by_name[name] = self.tool_calls_by_name.get(name, 0) + 1
            if self._cur_task is not None:
                self._cur_task["tool_calls"] += 1
                # 任务工具链：记录调用顺序（供链式可视化）
                tool_item = {
                    "name": name,
                    "args": (data.get("arguments") or "")[:120],
                    "call_ms": t_ms,
                    "dur_ms": None,
                    "ok": None,
                }
                self._cur_task.setdefault("tools", []).append(tool_item)
            if name in ("TaskCreate", "TaskUpdate"):
                self._track_subtask(name, data.get("arguments"), t_ms)
            if name in _SKILL_TOOL_NAMES:
                self.skill_count += 1
                if self.skill_loaded is None:
                    self.skill_loaded = _parse_skill_name(data.get("arguments"))
            if name in _HUMAN_INTERVENTION_TOOLS:
                self.human_interventions += 1
                # 等待人为输入开始计时（人工介入工具挂起中）
                if self._human_wait_start is None:
                    self._human_wait_start = t_ms
            call_id = data.get("callId")
            if call_id:
                self._pending_calls[call_id] = {
                    "name": name, "start_ms": t_ms,
                    "tool": tool_item if self._cur_task is not None else None,
                }
        elif etype == "tool/result":
            # DSH 的 tool/result 无顶层 callId，配对字段在 message.content[].toolCallId
            call_id = data.get("callId")
            if call_id is None:
                msg = data.get("message")
                if isinstance(msg, dict):
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("toolCallId"):
                            call_id = block["toolCallId"]
                            break
            started = self._pending_calls.pop(call_id, None) if call_id else None
            if started is not None and t_ms is not None and started["start_ms"] is not None:
                self.tool_ms += max(0.0, t_ms - started["start_ms"])
            # 等待人为输入结束计时（人工介入工具返回）
            if started is not None and started.get("name") in _HUMAN_INTERVENTION_TOOLS \
                    and self._human_wait_start is not None and t_ms is not None:
                self.human_wait_ms += max(0.0, t_ms - self._human_wait_start)
                self._human_wait_start = None
            err = _is_tool_result_error(data)
            # 工具链节点补状态/耗时/结果摘要
            if started is not None and started.get("tool") is not None:
                started["tool"]["dur_ms"] = max(0, (t_ms or 0) - (started["start_ms"] or 0))
                started["tool"]["ok"] = not err
                blocks = (data.get("message") or {}).get("content") or []
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") in ("tool-result", "tool_result"):
                        c = b.get("content")
                        if isinstance(c, str):
                            started["tool"]["result"] = c[:200]
                            break
            if err:
                self.tool_fail += 1
                name = started["name"] if started else "?"
                self.tool_fail_by_name[name] = self.tool_fail_by_name.get(name, 0) + 1
            else:
                self.tool_success += 1
        elif etype == "request/header":
            header = data.get("header") or {}
            tools = header.get("tools") or []
            if any(isinstance(t, dict) and t.get("name") in _SKILL_TOOL_NAMES for t in tools):
                self.skill_available = True
        elif etype == "todo/write":
            self.todo_latest = data.get("todos")

    def finalize(self) -> None:
        """事件流结束后收尾：累计 usage、解析 skill 名。"""
        for u in self._usage_by_step.values():
            self.tokens["input"] += u.get("inputTokens", 0)
            self.tokens["cache_read"] += u.get("cacheReadTokens", 0)
            self.tokens["cache_write"] += u.get("cacheWriteTokens", 0)
            self.tokens["output"] += u.get("outputTokens", 0)

    def assistant_text(self, limit: int = 100_000) -> str:
        """assistant 文本拼接（供锚点匹配），截断防爆。"""
        return "".join(self.assistant_text_parts)[:limit]

    def _track_subtask(self, name: str, arguments, t_ms) -> None:
        """追踪子任务：TaskCreate 创建（无 id 按出现顺序配对），TaskUpdate 更新状态。"""
        try:
            arg = json.loads(arguments or "{}")
        except Exception:
            arg = {}
        if name == "TaskCreate":
            self._create_counter += 1
            item = {
                "id": str(self._create_counter),
                "subject": arg.get("subject", ""),
                "description": arg.get("description", ""),
                "status": "pending",
                "created_ms": t_ms,
                "updated_ms": t_ms,
            }
            self._task_items[self._create_counter] = item
            if self._cur_task is not None:
                self._cur_task.setdefault("subitems", []).append(item)
        elif name == "TaskUpdate":
            tid = str(arg.get("taskId") or arg.get("id") or "")
            st = arg.get("status") or ""
            item = None
            if tid and tid.isdigit():
                item = self._task_items.get(int(tid))
            if item is None and tid:
                item = self._task_items.get(tid)
            if item is not None:
                if st:
                    item["status"] = st
                item["updated_ms"] = t_ms
            elif self._cur_task is not None and st:
                # 未匹配的 update：作为孤儿子任务挂当前任务（status 即主题近似）
                self._cur_task.setdefault("subitems", []).append({
                    "id": tid or "?", "subject": st, "status": st,
                    "created_ms": t_ms, "updated_ms": t_ms,
                })

    def tokens_total(self) -> dict:
        """按 step 去重的 usage 实时合计（不修改状态，可重复调用）。"""
        t = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        for u in self._usage_by_step.values():
            t["input"] += u.get("inputTokens", 0)
            t["cache_read"] += u.get("cacheReadTokens", 0)
            t["cache_write"] += u.get("cacheWriteTokens", 0)
            t["output"] += u.get("outputTokens", 0)
        return t

    def snapshot_live(self) -> dict:
        """运行中快照：token 实时累计（等价 finalize+snapshot 但可重复调用，供实时推送）。"""
        s = self.snapshot()
        s.update(self.tokens_total())
        return s

    def _fallback_end_reason(self) -> str | None:
        """无 turn/end 事件时，用最后一条 assistant 的 stop_reason 兜底推断结束原因。

        Claude Code 日志无 turn/end 事件，但 assistant 消息带 stop_reason：
        end_turn → completed；max_tokens → max-tokens；tool_use/其它 → None（会话仍在进行）。
        """
        sr = self.last_stop_reason
        if not sr:
            return None
        return {"end_turn": "completed", "max_tokens": "max-tokens", "stop_sequence": "completed"}.get(sr)

    def snapshot(self) -> dict:
        """当前指标快照（命名对齐 parser.replay_metrics 的 metrics 段）。"""
        return {
            "skill_loaded": self.skill_loaded,
            "skill_count": self.skill_count,
            "skill_available": self.skill_available,
            "model": self.model,
            "model_turns": dict(self.model_turns),
            "tasks": list(self.tasks),
            "task_success": None,          # 由上层基于落盘日志/锚点判定
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_by_name": dict(self.tool_calls_by_name),
            "tool_success": self.tool_success,
            "tool_fail": self.tool_fail,
            "tool_fail_by_name": dict(self.tool_fail_by_name),
            "tool_success_rate": (self.tool_success / self.tool_calls_total
                                  if self.tool_calls_total else None),
            "input_tokens": self.tokens["input"],
            "cache_read_tokens": self.tokens["cache_read"],
            "cache_write_tokens": self.tokens["cache_write"],
            "output_tokens": self.tokens["output"],
            "human_interventions": self.human_interventions,
            "user_turns": self.user_turns,
            "turns": self.turns,
            "steps": self.steps,
            "turn_end_reason": self.turn_end_reason if self.turn_end_reason is not None else self._fallback_end_reason(),
            "turn_end_reasons": list(self.turn_end_reasons),
            "ttft_ms": self.ttft_ms,
            "llm_ms": round(self.llm_ms if self._has_step_events else self._llm_fallback_ms, 1),
            "tool_ms": round(self.tool_ms, 1),
            "human_wait_ms": round(self.human_wait_ms, 1),
            "duration_ms": ((self.last_event_time - self.started_at_ms)
                            if self.started_at_ms is not None and self.last_event_time is not None else None),
            "started_at": self.started_at_ms,   # 会话开始（首个事件时间，毫秒 epoch）
            "ended_at": self.last_event_time,   # 会话结束（末个事件时间）
        }

    def check_warnings(self) -> list:
        """基于当前状态产出运行中告警（供实时回调）。"""
        warnings = []
        if self.turn_end_reason in ("error", "aborted"):
            warnings.append(f"回合异常结束: {self.turn_end_reason}")
        if self.tool_calls_total >= 2 and self.tool_fail / self.tool_calls_total > 0.5:
            warnings.append(f"工具失败率过高: {self.tool_fail}/{self.tool_calls_total}")
        return warnings


class DshEvalBackend:
    """
    DSH 实时评测后端：拉起 DSH runtime → 发任务 → 实时订阅事件流 → 增量指标 + 落盘。
    """

    def __init__(self, provider="deepseek-official", model="deepseek-v4-flash",
                 cwd=None, session_root=None, max_tokens=None, env=None):
        if _SDK_IMPORT_ERROR is not None:
            raise ImportError(
                "deepseek_harness SDK 未安装或不可用，请先 `pip install deepseek-harness` "
                "或把 DSH 的 python/sdk 加入 PYTHONPATH。原始错误: " + repr(_SDK_IMPORT_ERROR)
            )
        self.cwd = cwd or str(Path.cwd())
        self.session_root = Path(session_root) if session_root else None
        self._harness = None
        self._config = DeepSeekHarnessConfig(
            provider=provider, model=model, max_tokens=max_tokens,
            cwd=self.cwd, session_root=str(self.session_root) if self.session_root else None,
            env=env or {},
        )

    # ---- 生命周期 ----

    def _ensure_harness(self):
        if self._harness is None:
            self._harness = DeepSeekHarness(self._config)
            self._harness.start()
        return self._harness

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 任务执行 ----

    def run_task(self, task: dict, session_id: str = None, timeout_s: int = 300,
                 on_event=None, on_warning=None) -> dict:
        """
        跑一个评测任务：发 query → 实时订阅事件流 → 增量折叠指标 → 落盘 DSH JSONL。

        Args:
            task: evalkit task dict（用 task["query"] 作为提示词）。
            session_id: 自定义会话 id；缺省 eval-<task_id>-<时间戳>。
            timeout_s: 任务超时秒数；超时后强制中断（关闭 runtime，下次自动重启）。
            on_event: 每个 SessionEvent 到达时的实时回调 on_event(event_dict)。
            on_warning: 运行中告警回调 on_warning(warning_str)。

        Returns:
            dict：session_id / query / metrics(snapshot) / finish_reason /
                  duration_s / timeout(bool) / log_path(落盘文件) / warnings。
        """
        task_id = task.get("task_id", "unknown")
        session_id = session_id or f"eval-{task_id}-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        query = task.get("query", "")
        harness = self._ensure_harness()

        metrics = EventMetrics()
        warnings_seen = set()
        timeout_flag = {"hit": False}

        # 落盘：session_root/<session_id>/session.jsonl（兼容 scan_dsh_log 的目录名取 id）
        log_path = None
        log_fp = None
        if self.session_root is not None:
            sess_dir = self.session_root / session_id
            sess_dir.mkdir(parents=True, exist_ok=True)
            log_path = sess_dir / "session.jsonl"
            log_fp = open(log_path, "w", encoding="utf-8")
            log_fp.write(json.dumps({
                "type": "session", "version": 0, "id": session_id,
                "createdAt": int(time.time() * 1000), "cwd": self.cwd,
            }, ensure_ascii=False) + "\n")

        started_wall = time.monotonic()
        client = harness.client
        stop = threading.Event()   # 正常结束置位 → watchdog 取消超时

        def _handle(notification) -> None:
            """每个通知实时处理：事件喂指标 + 落盘 + 回调 + 告警。"""
            if notification.method == "session.event":
                payload = notification.payload or {}
                if payload.get("sessionId") == session_id:
                    event = payload.get("event")
                    if isinstance(event, dict):
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

        def _watchdog() -> None:
            """超时 watchdog：未被正常结束取消（stop.set）且到点后关闭 runtime，
            令订阅 next() 抛 TransportClosedError，从而打断阻塞的任务等待。"""
            if stop.wait(timeout_s):
                return  # 任务已正常结束，取消超时
            timeout_flag["hit"] = True
            try:
                client.close()
            except Exception:
                pass

        try:
            # 订阅会话树（含子代理）+ 发任务；通知全部走 subscription 队列，由下方循环消费
            with client.subscribe_session_notifications(session_id) as subscription:
                client.session_prompt(
                    session_id,
                    [{"type": "text", "text": query}],
                    notification_subscription=subscription,
                )
                watchdog = threading.Thread(target=_watchdog, daemon=True)
                watchdog.start()
                try:
                    while True:
                        notification = subscription.next()
                        _handle(notification)
                        if (notification.method == "session.status"
                                and (notification.payload or {}).get("sessionId") == session_id
                                and (notification.payload or {}).get("status") == "idle"):
                            break
                finally:
                    stop.set()          # 取消本任务 watchdog，避免误关后续任务的 runtime
                    watchdog.join(timeout=1)
        except Exception as exc:
            # 超时关闭 runtime 导致的 TransportClosedError → 标记 timeout，其余原样抛
            if not timeout_flag["hit"]:
                raise
        finally:
            if log_fp is not None:
                log_fp.close()

        metrics.finalize()
        duration_s = round(time.monotonic() - started_wall, 2)

        return {
            "session_id": session_id,
            "task_id": task_id,
            "query": query,
            "metrics": metrics.snapshot(),
            "assistant_text": metrics.assistant_text(),
            "finish_reason": metrics.turn_end_reason,
            "duration_s": duration_s,
            "timeout": timeout_flag["hit"],
            "log_path": str(log_path) if log_path else None,
            "warnings": sorted(warnings_seen),
        }


# ---------- CLI ----------

def _load_task(task_file: str) -> dict:
    with open(task_file, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DSH 实时评测后端（阶段 1）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑一个评测任务（实时）")
    p_run.add_argument("--task", required=True, help="task JSON 文件路径")
    p_run.add_argument("--session-root", default=None, help="会话落盘根目录（缺省不落盘）")
    p_run.add_argument("--timeout", type=int, default=300, help="任务超时秒数")
    p_run.add_argument("--model", default="deepseek-v4-flash", help="被测模型")
    p_run.add_argument("--cwd", default=None, help="被测 agent 的工作目录（缺省当前目录）")
    p_run.add_argument("--session-id", default=None, help="自定义会话 id")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        task = _load_task(args.task)
        with DshEvalBackend(model=args.model, cwd=args.cwd,
                            session_root=args.session_root) as backend:
            result = backend.run_task(
                task, session_id=args.session_id, timeout_s=args.timeout,
                on_warning=lambda w: print(f"[WARN] {w}", file=sys.stderr),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

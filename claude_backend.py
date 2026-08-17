#!/usr/bin/env python3
"""
claude_backend.py — 基于 Claude Code CLI `--output-format stream-json` 的实时评测后端（通道 A）。

调研依据：`docs/claude-backend-research.md`（本机 claude CLI 2.1.232 实测 + SDK types.py 交叉验证）。

设计：
  - 用 `claude --print --verbose --output-format stream-json [--include-partial-messages]`
    驱动被测 Claude Code（headless、逐行实时输出）；
  - `ClaudeEventAdapter` 把 stream-json 行转成**统一事件 dict**（与 DSH SessionEvent 同构），
    直接喂给 `dsh_backend.EventMetrics` 增量折叠器（工具成功率 / turn 结束原因 / token / TTFT / 耗时）；
  - 落盘双格式：
      * `session_root/<session_id>/raw.jsonl`  —— stream-json 原始行（保真）；
      * `session_root/<session_id>/session.jsonl` —— DSH 格式（首行 session 头 + 统一事件），
        可被 `session_report.scan_dsh_log` 直接复用离线深度分析；
  - 特殊指标直取官方值：`result` 行的 total_cost_usd / ttft_ms / duration_ms / num_turns /
    terminal_reason / permission_denials（DSH 需自算，Claude 直接给）。

被测对象解耦：task 声明沿用 evalkit tasks/*.json；成功判定由上层（parser.replay_metrics 等）完成。
与 `dsh_backend.DshEvalBackend` 同接口（run_task），上层可无缝切换被测对象。

依赖：仅本机 claude CLI（evalkit runner.py 已依赖）。

用法（代码内）：
    from claude_backend import ClaudeEvalBackend
    with ClaudeEvalBackend(cwd=r"D:\\wy_projects\\work_4_log",
                           session_root=r"D:\\wy_projects\\evalkit\\results\\claude") as b:
        result = b.run_task(task, timeout_s=300, on_warning=print)
        print(result["metrics"], result["cost_usd"], result["ttft_ms"])

用法（CLI）：
    python claude_backend.py run --task tasks/g66_L3_001.json --session-root results/claude
"""

import json
import os
import sys
import time
import queue
import threading
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from dsh_backend import EventMetrics, _normalize_tool_name


# ---------- 工具函数 ----------

def _iso_to_ms(ts) -> int | None:
    """ISO 时间戳（Claude stream-json 行的 timestamp 字段）→ 毫秒 epoch；失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# Claude 流式 delta 类型 → DSH 口径（供 EventMetrics 的 TTFT 检测）
_DELTA_TYPE_MAP = {
    "text_delta": "text-delta",
    "thinking_delta": "reasoning-delta",
    "input_json_delta": "tool-call-delta",
}

# Claude usage 字段（下划线）→ DSH TokenUsage 驼峰口径（EventMetrics/scan_dsh_log 一致）
_USAGE_KEY_MAP = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadTokens",
    "cache_creation_input_tokens": "cacheWriteTokens",
}


def _normalize_usage(usage):
    """Claude usage dict（下划线）→ DSH 驼峰口径；非 dict 原样返回。"""
    if not isinstance(usage, dict):
        return usage
    return {dsh_key: usage.get(claude_key, 0) for claude_key, dsh_key in _USAGE_KEY_MAP.items()}


class ClaudeEventAdapter:
    """
    把 Claude Code stream-json 行转成统一事件 dict（DSH SessionEvent 同构，EventMetrics 可消费）。

    行 → 事件映射（与 docs/claude-backend-research.md 第五节一致）：
      - system(init)          → request/header（tools 归一为 dict 列表 / model）
      - assistant(tool_use)   → tool/call（callId=block.id, arguments=json(block.input)）
      - user(tool_result)     → tool/result（callId=block.tool_use_id, isError, meta{stdout,stderr,interrupted}）
      - assistant(text+usage) → assistant/message（message.content + usage + model）
      - stream_event          → assistant/chunk（delta 类型映射到 DSH 口径）
      - result                → turn/end（reason.kind=terminal_reason）
    每条 assistant/user 消息分配独立 step（近似 Claude 的轮）；time 由行 timestamp 解析。
    """

    def __init__(self):
        self._turn = 0
        self._step = 0
        self._pending_step_end = None   # (step, start_ms) 待关闭的 step（下一条消息到达时结算）

    # ---- 入口 ----

    def adapt_line(self, line: str) -> list:
        """解析一行 stream-json 文本 → 统一事件列表（解析失败返回空）。"""
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        return self.adapt(obj)

    def adapt(self, obj: dict) -> list:
        """一个 stream-json 行对象 → 统一事件列表。"""
        etype = obj.get("type", "")
        t_ms = _iso_to_ms(obj.get("timestamp"))
        # 先结算上一条 assistant 的 step（模型活跃 = assistant 到下一行的时间差）
        events = []
        if self._pending_step_end is not None:
            step, start_ms = self._pending_step_end
            self._pending_step_end = None
            if t_ms is not None and start_ms is not None:
                events.append({"type": "step/end", "seq": -1, "time": t_ms,
                               "data": {"step": step, "turn": self._turn}})
        if etype == "system":
            events += self._adapt_system(obj, t_ms)
        elif etype == "assistant":
            events += self._adapt_assistant(obj, t_ms)
        elif etype == "user":
            events += self._adapt_user(obj, t_ms)
        elif etype == "stream_event":
            events += self._adapt_stream_event(obj, t_ms)
        elif etype == "result":
            events += self._adapt_result(obj, t_ms)
        return events

    # ---- 各类行 ----

    def _adapt_system(self, obj: dict, t_ms) -> list:
        subtype = obj.get("subtype")
        if subtype != "init":
            return []
        # Claude CLI 的 init 行不带 timestamp → t_ms=None。
        # 保持 None 不兜底：EventMetrics 忽略（if t_ms is not None），前端轨迹继承前一事件时间；
        # 若用 time.time() 兜底，回放/测试（虚拟时间戳）会把 started_at 拉到未来、duration 变负数。
        tools = obj.get("tools") or []
        header = {
            "tools": [{"name": t} if isinstance(t, str) else t for t in tools],
        }
        if obj.get("model"):
            header["model"] = obj["model"]
        if obj.get("cwd"):
            header["cwd"] = obj["cwd"]
        return [{"type": "request/header", "seq": -1, "time": t_ms,
                 "data": {"header": header, "reason": "initial"}}]

    def _adapt_assistant(self, obj: dict, t_ms) -> list:
        msg = obj.get("message") or {}
        content = msg.get("content") or []
        events = []
        step = self._step
        self._step += 1  # 每条 assistant 消息 = 一个新 step
        # step/start：模型活跃开始（结束由下一条消息/result 结算）
        events.append({"type": "step/start", "seq": -1, "time": t_ms,
                       "data": {"step": step, "turn": self._turn}})
        self._pending_step_end = (step, t_ms)
        saw_tool_use = False
        saw_message = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                saw_tool_use = True
                events.append({
                    "type": "tool/call",
                    "seq": -1, "time": t_ms,
                    "data": {
                        "turn": self._turn, "step": step,
                        "callId": block.get("id", ""),
                        "name": _normalize_tool_name(block.get("name", "")),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif btype in ("text", "reasoning", "thinking"):
                text = block.get("text", "")
                if text:
                    saw_message = True
                    events.append({
                        "type": "assistant/message",
                        "seq": -1, "time": t_ms,
                        "data": {
                            "turn": self._turn, "step": step,
                            "message": {
                                "model": msg.get("model", ""),
                                "content": [{"type": "text", "text": text}],
                                "usage": _normalize_usage(msg.get("usage")),
                                "stop_reason": msg.get("stop_reason") or None,
                            },
                        },
                    })
        # 仅含 tool_use（无文本）的 assistant 消息：补空 assistant/message 作时间锚点，
        # 保证轨迹模型思考块覆盖思考时段（对齐 DSH Tool call only 消息）
        if saw_tool_use and not saw_message:
            events.append({
                "type": "assistant/message",
                "seq": -1, "time": t_ms,
                "data": {
                    "turn": self._turn, "step": step,
                    "message": {"model": msg.get("model", ""), "content": [], "stop_reason": None},
                },
            })
        return events

    def _adapt_user(self, obj: dict, t_ms) -> list:
        msg = obj.get("message") or {}
        content = msg.get("content") or []
        events = []
        # tool_use_result 可能是 dict（正常）也可能是 str（工具失败时的错误文本）
        tur = obj.get("tool_use_result") or {}
        if isinstance(tur, str):
            tur = {"stdout": "", "stderr": tur, "interrupted": False}
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            events.append({
                "type": "tool/result",
                "seq": -1, "time": t_ms,
                "data": {
                    "turn": self._turn, "step": self._step - 1,  # 归到发起调用的 step
                    "callId": block.get("tool_use_id", ""),
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "tool-result",
                            "content": block.get("content"),
                            "isError": bool(block.get("is_error", False)),
                        }],
                    },
                    "meta": {
                        "stdout": tur.get("stdout", ""),
                        "stderr": tur.get("stderr", ""),
                        "interrupted": tur.get("interrupted", False),
                    },
                },
            })
        return events

    def _adapt_stream_event(self, obj: dict, t_ms) -> list:
        event = obj.get("event") or {}
        etype = event.get("type", "")
        # TTFT 等时序指标由 backend 从 message_start 直取，这里只做 chunk 透传
        if etype in ("content_block_start", "content_block_delta", "content_block_stop"):
            chunk = dict(event)
            # delta 类型映射到 DSH 口径，供 EventMetrics TTFT/usage 检测
            delta = chunk.get("delta")
            if isinstance(delta, dict) and delta.get("type") in _DELTA_TYPE_MAP:
                chunk["delta"] = {**delta, "type": _DELTA_TYPE_MAP[delta["type"]]}
            return [{
                "type": "assistant/chunk",
                "seq": -1, "time": t_ms,
                "data": {"turn": self._turn, "step": max(0, self._step - 1), "chunk": chunk},
            }]
        return []

    def _adapt_result(self, obj: dict, t_ms) -> list:
        reason = obj.get("terminal_reason") or "completed"
        return [{
            "type": "turn/end",
            "seq": -1, "time": t_ms,
            "data": {"turn": self._turn, "reason": {"kind": reason}},
        }]


class ClaudeEvalBackend:
    """
    Claude Code 实时评测后端（通道 A：CLI stream-json）。
    与 dsh_backend.DshEvalBackend 同接口（run_task），可无缝切换被测对象。
    """

    def __init__(self, cli_path: str = "claude", cwd: str = None, session_root: str = None,
                 include_partial_messages: bool = False, include_hook_events: bool = False,
                 permission_mode: str = None, model: str = None, extra_args: list = None,
                 provider: str | dict | None = None):
        self.cli_path = cli_path
        self.cwd = cwd or str(Path.cwd())
        self.session_root = Path(session_root) if session_root else None
        self.include_partial_messages = include_partial_messages
        self.include_hook_events = include_hook_events
        self.permission_mode = permission_mode
        self.model = model
        self.extra_args = extra_args or []
        # 模型提供商（参考 claude settings.json env+hooks 结构，见 provider.py）
        self.provider = self._resolve_provider(provider)

    @staticmethod
    def _resolve_provider(provider):
        """provider 参数归一：None/str 名 → provider dict；dict 原样。"""
        if provider is None or provider is False:
            return None
        if isinstance(provider, dict):
            return provider
        try:
            from provider import resolve_provider
            return resolve_provider(str(provider))
        except Exception:
            return None

    def close(self) -> None:
        """无长驻资源（每次 run_task 独立 subprocess），保留以对齐 DshEvalBackend 接口。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 任务执行 ----

    def run_task(self, task: dict, session_id: str = None, timeout_s: int = 300,
                 on_event=None, on_warning=None, cancel_event=None) -> dict:
        """
        跑一个评测任务：拉起 claude CLI → 逐行实时解析 stream-json → 增量折叠指标 → 双格式落盘。

        Args:
            task: evalkit task dict（用 task["query"] 作为提示词）。
            session_id: 自定义会话 id；缺省 eval-<task_id>-<时间戳>。
            timeout_s: 超时秒数；超时后 kill 子进程。
            on_event: 每个统一事件到达时的实时回调 on_event(event_dict)。
            on_warning: 运行中告警回调 on_warning(warning_str)。
            cancel_event: 可选 threading.Event；置位后尽快中断当前评测（kill 子进程）。

        Returns:
            dict：session_id / task_id / query / metrics(snapshot) / finish_reason /
                  cost_usd / ttft_ms / duration_ms / num_turns / stop_reason /
                  permission_denials / timeout / log_path(DSH 格式) / raw_path(原始行) / warnings。
        """
        task_id = task.get("task_id", "unknown")
        session_id = session_id or f"eval-{task_id}-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        query = task.get("query", "")

        metrics = EventMetrics()
        adapter = ClaudeEventAdapter()
        warnings_seen = set()
        result_info = {"cost_usd": None, "ttft_ms": None, "duration_ms": None,
                       "num_turns": None, "stop_reason": None, "permission_denials": None,
                       "is_error": None, "usage": None}
        timeout_flag = {"hit": False}

        # 落盘双格式
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

        # 任务 query 作为第一条 user/message（统一事件，喂指标 + 落盘）
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

        _emit({"type": "user/message", "seq": -1, "time": int(time.time() * 1000),
               "data": {"content": [{"type": "text", "text": query}]}})

        # 组装 claude 命令
        cmd = [self.cli_path, "--print", "--verbose", "--output-format", "stream-json"]
        if self.include_partial_messages:
            cmd.append("--include-partial-messages")
        if self.include_hook_events:
            cmd.append("--include-hook-events")
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.model:
            cmd += ["--model", self.model]
        # 模型提供商：合成临时 --settings 文件（env + hooks，附加加载，不污染系统配置）
        proc_env = None
        tmp_settings = None
        if self.provider:
            try:
                from provider import build_settings_json, apply_env
                js = build_settings_json(self.provider)
                if js:
                    # claude --settings 只接受文件路径（实测拒绝 JSON 字符串），写临时文件
                    import tempfile
                    f = tempfile.NamedTemporaryFile(
                        "w", suffix=".json", encoding="utf-8", delete=False)
                    f.write(js)
                    f.close()
                    tmp_settings = f.name
                    cmd += ["--settings", tmp_settings]
                    if self.provider.get("hooks"):
                        cmd.append("--include-hook-events")
                proc_env = apply_env(self.provider, os.environ.copy())
            except Exception:
                proc_env = None
        cmd += self.extra_args
        cmd.append(query)

        started_wall = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, cwd=self.cwd, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                env=proc_env,
            )
            # 后台线程读 stdout/stderr：避免 readline 阻塞导致 deadline/cancel 失效，
            # 同时排空 stderr 防止管道缓冲写满反向卡死 claude 进程。
            q = queue.Queue()

            def _drain(pipe, tag):
                try:
                    for line in iter(pipe.readline, ""):
                        q.put((tag, line))
                finally:
                    pipe.close()

            t_out = threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True)
            t_out.start()
            t_err.start()

            # 逐行实时处理（主循环带 deadline，不阻塞在 readline）
            deadline = time.time() + timeout_s
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    timeout_flag["hit"] = True  # 复用 timeout 标志：被外部取消
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
                        break  # 进程已退出且队列已排空
                    continue
                if tag == "err":
                    continue  # stderr 只排空，不参与指标
                if not line:
                    continue
                if raw_fp is not None:
                    raw_fp.write(line)
                    raw_fp.flush()
                for event in adapter.adapt_line(line):
                    _emit(event)
                # result 行特殊字段直取（官方值优先于自算）
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                if obj is not None and obj.get("type") == "result":
                    result_info.update({
                        "cost_usd": obj.get("total_cost_usd"),
                        "ttft_ms": obj.get("ttft_ms") or obj.get("ttft_stream_ms"),
                        "duration_ms": obj.get("duration_ms"),
                        "num_turns": obj.get("num_turns"),
                        "stop_reason": obj.get("stop_reason"),
                        "permission_denials": obj.get("permission_denials"),
                        "is_error": obj.get("is_error"),
                        "usage": _normalize_usage(obj.get("usage") or {}),
                    })
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            if raw_fp is not None:
                raw_fp.close()
            if log_fp is not None:
                log_fp.close()
            if tmp_settings is not None:
                try:
                    os.unlink(tmp_settings)
                except OSError:
                    pass

        metrics.finalize()
        duration_s = round(time.monotonic() - started_wall, 2)
        snapshot = metrics.snapshot()
        # Claude 官方直取字段并入快照
        snapshot["cost_usd"] = result_info["cost_usd"]
        snapshot["ttft_ms_official"] = result_info["ttft_ms"]
        snapshot["duration_ms_official"] = result_info["duration_ms"]
        snapshot["num_turns"] = result_info["num_turns"]
        snapshot["stop_reason"] = result_info["stop_reason"]
        snapshot["permission_denials"] = result_info["permission_denials"]
        snapshot["is_error"] = result_info["is_error"]
        # 官方 usage 覆盖自算值（partial 消息带累计 usage 时 EventMetrics 会重复累加，result 行是全量权威值）
        off_usage = result_info.get("usage")
        if isinstance(off_usage, dict):
            snapshot["input_tokens"] = off_usage.get("inputTokens", snapshot["input_tokens"])
            snapshot["cache_read_tokens"] = off_usage.get("cacheReadTokens", snapshot["cache_read_tokens"])
            snapshot["cache_write_tokens"] = off_usage.get("cacheWriteTokens", snapshot["cache_write_tokens"])
            snapshot["output_tokens"] = off_usage.get("outputTokens", snapshot["output_tokens"])

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


# ---------- CLI ----------

def _load_task(task_file: str) -> dict:
    with open(task_file, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claude Code 实时评测后端（通道 A：stream-json）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑一个评测任务（实时）")
    p_run.add_argument("--task", required=True, help="task JSON 文件路径")
    p_run.add_argument("--session-root", default=None, help="会话落盘根目录（缺省不落盘）")
    p_run.add_argument("--timeout", type=int, default=300, help="任务超时秒数")
    p_run.add_argument("--cwd", default=None, help="被测 agent 工作目录（缺省当前目录）")
    p_run.add_argument("--session-id", default=None, help="自定义会话 id")
    p_run.add_argument("--permission-mode", default=None,
                       help="permission 模式（acceptEdits/bypassPermissions 等，缺省用 CLI 默认）")
    p_run.add_argument("--model", default=None, help="覆盖被测模型")
    p_run.add_argument("--include-partial-messages", action="store_true",
                       help="开启 token 级流式事件（stream_event 行）")
    p_run.add_argument("--include-hook-events", action="store_true",
                       help="开启 hook 生命周期事件行")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        task = _load_task(args.task)
        with ClaudeEvalBackend(cwd=args.cwd, session_root=args.session_root,
                               permission_mode=args.permission_mode, model=args.model,
                               include_partial_messages=args.include_partial_messages,
                               include_hook_events=args.include_hook_events) as backend:
            result = backend.run_task(
                task, session_id=args.session_id, timeout_s=args.timeout,
                on_warning=lambda w: print(f"[WARN] {w}", file=sys.stderr),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

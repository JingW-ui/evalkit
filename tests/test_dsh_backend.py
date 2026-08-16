# -*- coding: utf-8 -*-
"""单元测试：dsh_backend.py 的 EventMetrics 增量折叠器 + 工具函数（阶段 1 验证）。

不依赖真实 DSH runtime（SDK 缺失时仅验证惰性报错与纯逻辑部分）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import dsh_backend as db

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) 模块可 import（SDK 缺失时惰性） ----
check("模块可 import", db is not None)
check("SDK 状态已记录", hasattr(db, "_SDK_IMPORT_ERROR"))

# ---- 2) 工具函数 ----
check("normalize skill→Skill", db._normalize_tool_name("skill") == "Skill")
check("normalize todo_write→TodoWrite", db._normalize_tool_name("todo_write") == "TodoWrite")
check("normalize 其他不变", db._normalize_tool_name("read") == "read")
check("parse_skill_name JSON", db._parse_skill_name('{"name":"G66"}') == "G66")
check("parse_skill_name skill 键", db._parse_skill_name('{"skill":"uu-remote"}') == "uu-remote")
check("parse_skill_name 坏 JSON 兜底", db._parse_skill_name("not-json") == "skill")
check("isError: 顶层 error", db._is_tool_result_error({"error": {"code": "E"}}))
check("isError: content isError", db._is_tool_result_error(
    {"message": {"content": [{"type": "tool-result", "isError": True}]}}))
check("isError: 成功结果", not db._is_tool_result_error(
    {"message": {"content": [{"type": "tool-result", "content": "ok", "isError": False}]}}))

# ---- 3) EventMetrics 折叠（构造一段完整事件流） ----
T0 = 1_700_000_000_000
evs = [
    {"type": "turn/start", "seq": 0, "time": T0, "data": {"turn": 0}},
    # 真实用户指令
    {"type": "user/message", "seq": 1, "time": T0 + 10, "data": {"content": [{"type": "text", "text": "帮我部署g66"}]}},
    # 系统注入（不应计入 user_turns）
    {"type": "user/message", "seq": 2, "time": T0 + 20, "data": {"content": [{"type": "text", "text": "Current runtime context..."}]}},
    {"type": "step/start", "seq": 3, "time": T0 + 30, "data": {"turn": 0, "step": 0}},
    # 流式：首个 delta → TTFT
    {"type": "assistant/chunk", "seq": 4, "time": T0 + 100, "data": {"turn": 0, "step": 0, "chunk": {"type": "text-delta", "index": 0, "text": "好"}}},
    {"type": "assistant/chunk", "seq": 5, "time": T0 + 150, "data": {"turn": 0, "step": 0, "chunk": {"type": "usage", "index": 0, "usage": {"inputTokens": 100, "cacheReadTokens": 50, "outputTokens": 20}}}},
    {"type": "assistant/message", "seq": 6, "time": T0 + 200, "data": {"turn": 0, "step": 0, "message": {
        "model": "deepseek-v4-flash",
        "content": [{"type": "text", "text": "开始部署"}],
        "usage": {"inputTokens": 100, "cacheReadTokens": 50, "cacheWriteTokens": 30, "outputTokens": 20},
    }}},
    {"type": "tool/call", "seq": 7, "time": T0 + 300, "data": {"turn": 0, "step": 0, "callId": "c1", "name": "skill", "arguments": '{"name":"G66"}'}},
    {"type": "tool/result", "seq": 8, "time": T0 + 800, "data": {"turn": 0, "step": 0, "message": {"role": "user", "content": [{"type": "tool-result", "toolCallId": "c1", "content": "loaded", "isError": False}]}}},
    {"type": "tool/call", "seq": 9, "time": T0 + 900, "data": {"turn": 0, "step": 0, "callId": "c2", "name": "push_file", "arguments": "{}"}},
    {"type": "tool/result", "seq": 10, "time": T0 + 1200, "data": {"turn": 0, "step": 0, "error": {"code": "EPERM"}, "message": {"role": "user", "content": [{"type": "tool-result", "toolCallId": "c2", "content": "denied", "isError": True}]}}},
    {"type": "tool/call", "seq": 11, "time": T0 + 1300, "data": {"turn": 0, "step": 0, "callId": "c3", "name": "ask_user_question", "arguments": "{}"}},
    {"type": "step/end", "seq": 12, "time": T0 + 1400, "data": {"turn": 0, "step": 0}},
    {"type": "turn/end", "seq": 13, "time": T0 + 1500, "data": {"turn": 0, "reason": {"kind": "completed"}}},
]

m = db.EventMetrics()
for e in evs:
    m.on_event(e)
m.finalize()
s = m.snapshot()

check("user_turns=1（排除系统注入）", s["user_turns"] == 1, f"实际 {s['user_turns']}")
check("skill_loaded=G66", s["skill_loaded"] == "G66", f"实际 {s['skill_loaded']}")
check("skill_count=1", s["skill_count"] == 1)
check("tool_calls_total=3", s["tool_calls_total"] == 3, f"实际 {s['tool_calls_total']}")
check("tool_success=1 fail=1", s["tool_success"] == 1 and s["tool_fail"] == 1,
      f"实际 success={s['tool_success']} fail={s['tool_fail']}")
check("tool_fail_by_name 记 push_file", s["tool_fail_by_name"].get("push_file") == 1,
      f"实际 {s['tool_fail_by_name']}")
check("human_interventions=1（ask_user_question）", s["human_interventions"] == 1,
      f"实际 {s['human_interventions']}")
check("usage: cache_write=30", s["cache_write_tokens"] == 30, f"实际 {s['cache_write_tokens']}")
check("usage: input=100（message.usage 优先，未双计）", s["input_tokens"] == 100,
      f"实际 {s['input_tokens']}")
check("turn_end_reason=completed", s["turn_end_reason"] == "completed")
check("ttft_ms=70（100-30）", s["ttft_ms"] == 70, f"实际 {s['ttft_ms']}")
check("tool_ms=800（c1 500ms + c2 300ms）", s["tool_ms"] == 800.0, f"实际 {s['tool_ms']}")
check("llm_ms=1370（step 30→1400）", s["llm_ms"] == 1370.0, f"实际 {s['llm_ms']}")
check("tool_calls_by_name 含 Skill", s["tool_calls_by_name"].get("Skill") == 1,
      f"实际 {s['tool_calls_by_name']}")
check("assistant_text_parts 拼接", "".join(m.assistant_text_parts) == "开始部署",
      f"实际 {m.assistant_text_parts}")

# ---- 4) 告警 ----
check("无告警（completed + 失败率 1/3）", m.check_warnings() == [])
m2 = db.EventMetrics()
for e in evs[:13]:  # 不含 turn/end completed
    m2.on_event(e)
m2.turn_end_reason = "error"   # 模拟异常结束
m2.tool_calls_total = 4        # 模拟更多失败
m2.tool_fail = 3
ws = m2.check_warnings()
check("告警含回合异常结束", any("回合异常结束" in w for w in ws), f"实际 {ws}")
check("告警含工具失败率", any("工具失败率过高" in w for w in ws), f"实际 {ws}")

# ---- 5) 无 SDK 时 DshEvalBackend 构造应报清晰错误（若本机未装 SDK） ----
if db._SDK_IMPORT_ERROR is not None:
    try:
        db.DshEvalBackend()
        check("无 SDK 构造抛 ImportError", False, "未抛出")
    except ImportError as exc:
        check("无 SDK 构造抛 ImportError", "deepseek_harness" in str(exc))
else:
    print("[SKIP] SDK 已安装，跳过 ImportError 分支验证")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

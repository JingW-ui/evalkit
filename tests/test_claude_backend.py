# -*- coding: utf-8 -*-
"""单元测试：claude_backend.py 的 ClaudeEventAdapter + 指标折叠（通道 A 验证）。

样例数据对齐本机 claude CLI 2.1.232 实测的 stream-json 行格式。
不真实调用 claude（避免消耗 token）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import claude_backend as cb
from dsh_backend import EventMetrics

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) 时间戳 ----
check("iso→ms", cb._iso_to_ms("2026-08-15T14:11:44.604Z") == 1786803104604,
      f"实际 {cb._iso_to_ms('2026-08-15T14:11:44.604Z')}")
check("iso 空→None", cb._iso_to_ms(None) is None)

# ---- 2) 构造实测格式的 stream-json 行 ----
T = "2026-08-15T14:11:44.604Z"
lines = [
    # system(init)
    {"type": "system", "subtype": "init", "cwd": "D:\\\\wy_projects\\\\work_4_log",
     "session_id": "s-1", "tools": ["Bash", "Skill", "Read", "Write"],
     "model": "auto_deepseek_plan[1m]"},
    # assistant：tool_use（Skill 加载）
    {"type": "assistant", "session_id": "s-1", "timestamp": T,
     "message": {"id": "m1", "role": "assistant", "model": "deepseek-v4-flash",
                 "content": [{"type": "tool_use", "id": "call_1", "name": "Skill",
                              "input": {"name": "G66"}}],
                 "usage": {"input_tokens": 44919, "output_tokens": 0,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    # user：tool_result（Skill 加载结果）
    {"type": "user", "session_id": "s-1", "timestamp": "2026-08-15T14:11:50.000Z",
     "message": {"role": "user", "content": [{"tool_use_id": "call_1", "type": "tool_result",
                                              "content": "G66 skill loaded", "is_error": False}]},
     "tool_use_result": {"stdout": "G66 skill loaded", "stderr": "", "interrupted": False,
                         "isImage": False, "noOutputExpected": False}},
    # assistant：tool_use（Bash）
    {"type": "assistant", "session_id": "s-1", "timestamp": "2026-08-15T14:11:55.000Z",
     "message": {"id": "m2", "role": "assistant", "model": "deepseek-v4-flash",
                 "content": [{"type": "tool_use", "id": "call_2", "name": "Bash",
                              "input": {"command": "ls -1"}}],
                 "usage": {"input_tokens": 54550, "output_tokens": 0,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1152}}},
    # user：tool_result（Bash 失败）
    {"type": "user", "session_id": "s-1", "timestamp": "2026-08-15T14:12:03.154Z",
     "message": {"role": "user", "content": [{"tool_use_id": "call_2", "type": "tool_result",
                                              "content": "error: no such dir", "is_error": True}]},
     "tool_use_result": {"stdout": "", "stderr": "error: no such dir", "interrupted": False,
                         "isImage": False, "noOutputExpected": False}},
    # assistant：最终文本
    {"type": "assistant", "session_id": "s-1", "timestamp": "2026-08-15T14:12:07.571Z",
     "message": {"id": "m3", "role": "assistant", "model": "deepseek-v4-flash",
                 "content": [{"type": "text", "text": "部署失败：目录不存在"}],
                 "usage": {"input_tokens": 99469, "output_tokens": 124,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1152}}},
    # result
    {"type": "result", "session_id": "s-1", "is_error": False, "num_turns": 2,
     "stop_reason": "end_turn", "total_cost_usd": 0.503106,
     "terminal_reason": "completed", "ttft_ms": 5616, "ttft_stream_ms": 2772,
     "duration_ms": 28614,
     "usage": {"input_tokens": 99469, "cache_read_input_tokens": 1152, "output_tokens": 124},
     "permission_denials": []},
]

adapter = cb.ClaudeEventAdapter()
all_events = []
for line in lines:
    all_events.extend(adapter.adapt(line))

# ---- 0) tool_use_result 为 str（工具失败错误文本）时应容错 ----
evs = adapter.adapt({
    "type": "user", "timestamp": "2026-08-15T14:12:03.154Z",
    "message": {"role": "user", "content": [{"tool_use_id": "call_x", "type": "tool_result",
                                             "content": "Error: exit 1", "is_error": True}]},
    "tool_use_result": "Error: Exit code 1\nTraceback (most recent call last):\n..."})
check("tool_use_result=str 容错产 tool/result",
      len(evs) == 1 and evs[0]["type"] == "tool/result"
      and "Error: Exit code 1" in evs[0]["data"]["meta"]["stderr"],
      f"实际 {evs}")

by_type = {}
for e in all_events:
    by_type.setdefault(e["type"], []).append(e)

check("init → request/header", len(by_type.get("request/header", [])) == 1)
check("tool_use → tool/call ×2", len(by_type.get("tool/call", [])) == 2,
      f"实际 {len(by_type.get('tool/call', []))}")
check("tool/result ×2", len(by_type.get("tool/result", [])) == 2)
check("assistant/message ×3（1 文本 + 2 锚点）", len(by_type.get("assistant/message", [])) == 3,
      f"实际 {len(by_type.get('assistant/message', []))}")
check("turn/end ×1", len(by_type.get("turn/end", [])) == 1)

tc = by_type["tool/call"][0]["data"]
check("tool/call: Skill 归一化", tc["name"] == "Skill", f"实际 {tc['name']}")
check("tool/call: arguments JSON", json.loads(tc["arguments"]) == {"name": "G66"})
check("tool/call: callId", tc["callId"] == "call_1")

tr = by_type["tool/result"][1]["data"]
check("tool/result: callId 配对", tr["callId"] == "call_2")
check("tool/result: isError", tr["message"]["content"][0]["isError"] is True)
check("tool/result: meta.stdout/stderr", tr["meta"]["stderr"] == "error: no such dir")

te = by_type["turn/end"][0]["data"]
check("turn/end: reason.kind=completed", te["reason"]["kind"] == "completed")

# ---- 3) 喂 EventMetrics 折叠 ----
m = EventMetrics()
for e in all_events:
    m.on_event(e)
m.finalize()
s = m.snapshot()

check("skill_loaded=G66", s["skill_loaded"] == "G66", f"实际 {s['skill_loaded']}")
check("tool_calls_total=2", s["tool_calls_total"] == 2)
check("tool_success=1 fail=1", s["tool_success"] == 1 and s["tool_fail"] == 1,
      f"实际 success={s['tool_success']} fail={s['tool_fail']}")
check("tool_fail_by_name 记 Bash", s["tool_fail_by_name"].get("Bash") == 1,
      f"实际 {s['tool_fail_by_name']}")
check("usage: input=99469（最后累计值覆盖）", s["input_tokens"] == 99469,
      f"实际 {s['input_tokens']}")
check("usage: cache_read=1152", s["cache_read_tokens"] == 1152)
check("usage: output=124", s["output_tokens"] == 124)
check("turn_end_reason=completed", s["turn_end_reason"] == "completed")
check("assistant_text_parts 拼接", "".join(m.assistant_text_parts) == "部署失败：目录不存在",
      f"实际 {m.assistant_text_parts}")
check("tool_ms 按 timestamp 计算 >0", s["tool_ms"] > 0, f"实际 {s['tool_ms']}")
# 模型活跃时间：claude 无原生 step 事件，adapter 合成 step/start-end → llm_ms 应 >0
check("llm_ms（模型活跃）>0（step 合成）", s["llm_ms"] > 0, f"实际 {s['llm_ms']}")
check("duration_ms（整体耗时）> llm_ms", s["duration_ms"] > s["llm_ms"],
      f"整体 {s['duration_ms']} vs 模型活跃 {s['llm_ms']}")

# 等待人为输入：AskUserQuestion 挂起时长计入 human_wait_ms
hw_adapter = cb.ClaudeEventAdapter()
hw_events = []
hw_events += hw_adapter.adapt({
    "type": "assistant", "timestamp": "2026-08-15T15:00:00.000Z",
    "message": {"role": "assistant", "model": "deepseek-v4-flash",
                "content": [{"type": "tool_use", "id": "ask_1", "name": "AskUserQuestion",
                             "input": {"question": "选哪台设备？"}}]}})
hw_events += hw_adapter.adapt({
    "type": "user", "timestamp": "2026-08-15T15:01:00.000Z",
    "message": {"role": "user", "content": [{"tool_use_id": "ask_1", "type": "tool_result",
                                             "content": "选 A", "is_error": False}]},
    "tool_use_result": {"stdout": "选 A", "stderr": "", "interrupted": False}})
hm = EventMetrics()
for e in hw_events:
    hm.on_event(e)
hm.finalize()
hs = hm.snapshot()
check("human_wait_ms=60s（AskUserQuestion 挂起）", hs["human_wait_ms"] == 60000,
      f"实际 {hs['human_wait_ms']}")
check("human_interventions=1", hs["human_interventions"] == 1)

# 跨轮次长间隙（用户思考/离开后回复）不应计入模型活跃，应归空闲
gap_events = [
    {"type": "user/message", "time": 1700000000000,
     "data": {"content": [{"type": "text", "text": "帮我部署"}]}},
    {"type": "assistant/message", "time": 1700000010000,   # 模型回复
     "data": {"message": {"model": "m", "content": [{"type": "text", "text": "好的，开始"}]}}},
    {"type": "user/message", "time": 1700000610000,        # 10 分钟后用户才回（思考/离开）
     "data": {"content": [{"type": "text", "text": "选 A 设备"}]}},
    {"type": "assistant/message", "time": 1700000620000,   # 模型再次回复
     "data": {"message": {"model": "m", "content": [{"type": "text", "text": "完成"}]}}},
    {"type": "tool/call", "time": 1700000621000,           # 模型继续产出（1s 后调工具）
     "data": {"name": "Bash", "callId": "c1", "arguments": "{}"}},
]
gm = EventMetrics()
for e in gap_events:
    gm.on_event(e)
gm.finalize()
gs = gm.snapshot()
check("跨轮次 10min 长间隙不计入 llm（仅 assistant→tool 的 1s）",
      gs["llm_ms"] == 1000, f"实际 {gs['llm_ms']}（应为 1000ms）")
check("整体 duration 含长间隙（10min+2s）", gs["duration_ms"] == 621000,
      f"实际 {gs['duration_ms']}")
idle_gap = gs["duration_ms"] - gs["llm_ms"]
check("长间隙归空闲（idle≈10min+1s）", idle_gap == 620000, f"实际 {idle_gap}")

# ---- 4) stream_event 行（--include-partial-messages） ----
se = [{"type": "stream_event", "session_id": "s-1", "timestamp": T,
       "event": {"type": "message_start", "ttft_ms": 589,
                 "message": {"id": "m0", "role": "assistant", "content": [], "usage": {"input_tokens": 1}}}},
      {"type": "stream_event", "session_id": "s-1", "timestamp": T,
       "event": {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "收"}}}]
se_events = []
for o in se:
    se_events.extend(adapter.adapt(o))
chunks = [e for e in se_events if e["type"] == "assistant/chunk"]
check("stream_event → assistant/chunk ×1（delta 行）", len(chunks) == 1,
      f"实际 {len(chunks)}")
check("delta 类型映射 text_delta→text-delta",
      chunks[0]["data"]["chunk"]["delta"]["type"] == "text-delta",
      f"实际 {chunks[0]['data']['chunk'].get('delta')}")

# ---- 5) 集成闭环：adapter 输出（DSH 格式）可被 scan_dsh_log 解析 ----
import json, tempfile
from session_report import scan_dsh_log
with tempfile.TemporaryDirectory() as td:
    d = Path(td) / "dsh-session-session-x"
    d.mkdir(parents=True)
    f = d / "session.jsonl"
    with open(f, "w", encoding="utf-8") as fp:
        fp.write(json.dumps({"type": "session", "version": 0, "id": "x",
                             "createdAt": 1784196704000}) + "\n")
        for e in all_events:
            fp.write(json.dumps(e, ensure_ascii=False) + "\n")
    data = scan_dsh_log(str(f))
    check("闭环: scan_dsh_log 解析 adapter 输出", data["tool_dist"].get("Skill") == 1,
          f"实际 {data['tool_dist']}")
    check("闭环: tool_success=1 fail=1", data["tool_success"] == 1 and data["tool_fail"] == 1,
          f"实际 success={data['tool_success']} fail={data['tool_fail']}")
    check("闭环: 模型识别", bool(data["model_usage"]))

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

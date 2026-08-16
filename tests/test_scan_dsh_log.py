# -*- coding: utf-8 -*-
"""冒烟测试：scan_dsh_log 对 DSH chunk-runs 压缩行解包 + usage 统计（阶段 0 验证）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # evalkit 根目录
from session_report import scan_dsh_log, _expand_dsh_chunk_rows, _mk_chunk_event

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 构造 DSH 日志（模拟 DSH 落盘压缩：连续 6 个 text-delta 打成 1 条 text-chunks 行） ----
# 原始事件 seq 3..8 为 text-delta chunk（>MIN_RUN=3 会被打包）
T0 = 1_700_000_000_000
chunk_row = {
    "type": "text-chunks",
    "seq0": 3,
    "time0": T0 + 100,
    "data": {
        "turn": 0, "step": 0, "index": 0,
        "dt": [10, 10, 10, 10, 10],
        "texts": ["你", "好", "，", "世", "界", "！"],
    },
}
rows = [
    {"type": "turn/start", "seq": 0, "time": T0, "data": {"turn": 0}},
    {"type": "user/message", "seq": 1, "time": T0 + 5, "data": {"content": [{"type": "text", "text": "帮我部署g66"}]}},
    {"type": "step/start", "seq": 2, "time": T0 + 8, "data": {"turn": 0, "step": 0}},
    chunk_row,  # ← 压缩行
    {"type": "assistant/chunk", "seq": 9, "time": T0 + 170, "data": {"turn": 0, "step": 0, "chunk": {"type": "usage", "index": 0, "usage": {"inputTokens": 100, "cacheReadTokens": 50, "outputTokens": 20}}}},
    {"type": "assistant/message", "seq": 10, "time": T0 + 200, "data": {
        "turn": 0, "step": 0, "message": {
            "model": "deepseek-v4-flash",
            "content": [{"type": "text", "text": "你好，世界！"}],
            "usage": {"inputTokens": 100, "cacheReadTokens": 50, "cacheWriteTokens": 30, "outputTokens": 20},
        },
    }},
    {"type": "tool/call", "seq": 11, "time": T0 + 300, "data": {"turn": 0, "step": 0, "callId": "call-1", "name": "skill", "arguments": "{\"name\":\"G66\"}"}},
    {"type": "tool/result", "seq": 12, "time": T0 + 400, "data": {"turn": 0, "step": 0, "message": {"role": "tool", "content": [{"type": "tool-result", "content": "ok", "isError": False}]}}},
    {"type": "step/end", "seq": 13, "time": T0 + 410, "data": {"turn": 0, "step": 0}},
    {"type": "turn/end", "seq": 14, "time": T0 + 420, "data": {"turn": 0, "reason": {"kind": "completed"}}},
]

# ---- 1) 解包函数单测 ----
expanded = _expand_dsh_chunk_rows(rows)
chunk_events = [o for o in expanded if o["type"] == "assistant/chunk" and o["data"]["chunk"].get("type") == "text-delta"]
check("解包后共 6 个 assistant/chunk", len(chunk_events) == 6,
      f"实际 {len(chunk_events)}")
check("seq 连续 3..8", [e["seq"] for e in chunk_events] == [3, 4, 5, 6, 7, 8])
check("texts 展开正确", [e["data"]["chunk"]["text"] for e in chunk_events] == ["你", "好", "，", "世", "界", "！"])
check("time 重建正确", [e["time"] for e in chunk_events] == [T0 + 100, T0 + 110, T0 + 120, T0 + 130, T0 + 140, T0 + 150],
      f"实际 {[e['time'] for e in chunk_events]}")
check("chunk type = text-delta", all(e["data"]["chunk"]["type"] == "text-delta" for e in chunk_events))
check("总行数 = 原始 10 行 + 解包净增 5 行", len(expanded) == 15, f"实际 {len(expanded)}")

# ---- 2) tool-call-chunks 解包 ----
tcc = {
    "type": "tool-call-chunks",
    "seq0": 20, "time0": T0 + 1000,
    "data": {"turn": 1, "step": 1, "index": 0, "id": "call-x", "name": "read", "dt": [5, 5], "args": ["{\"path\":", "\"/tmp/a", ".txt\"}"]},
}
exp2 = _expand_dsh_chunk_rows([tcc])
check("tool-call-chunks 解包 3 个", len(exp2) == 3 and all(e["type"] == "assistant/chunk" for e in exp2))
check("tool-call-delta 字段正确",
      exp2[0]["data"]["chunk"] == {"type": "tool-call-delta", "index": 0, "id": "call-x", "name": "read", "argumentsDelta": '{"path":'},
      f"实际 {exp2[0]['data']['chunk']}")

# ---- 3) 整体 scan_dsh_log（写临时文件） ----
with tempfile.TemporaryDirectory() as td:
    sess_dir = Path(td) / "dsh-session-session-abc123"
    sess_dir.mkdir(parents=True)
    log = sess_dir / "session.jsonl"
    with open(log, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    data = scan_dsh_log(str(log))
    check("session_id 取自目录名", data["session_id"] == "dsh-session-session-abc123")
    check("tool_dist 含 Skill（归一化）", data["tool_dist"].get("Skill") == 1,
          f"实际 {data['tool_dist']}")
    check("tool_success=1 fail=0", data["tool_success"] == 1 and data["tool_fail"] == 0)
    check("usage: input=100", data["tokens"]["input"] == 100, f"实际 {data['tokens']}")
    check("usage: cache_read=50", data["tokens"]["cache_read"] == 50)
    check("usage: cache_write=30（assistant/message.usage 采信）", data["tokens"]["cache_write"] == 30,
          f"实际 {data['tokens']['cache_write']}")
    check("usage: output=20", data["tokens"]["output"] == 20)
    check("skill_events 有 G66", data["skill_events"] and data["skill_events"][0]["skill_name"] == "G66",
          f"实际 {data['skill_events']}")
    check("user_prompts 切出任务", data["tasks"] and data["tasks"][0]["query"] == "帮我部署g66")
    check("模型识别", data["model_usage"].get("deepseek-v4-flash", 0) >= 1)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

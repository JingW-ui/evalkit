# -*- coding: utf-8 -*-
"""单元测试：tail_attach.py（Claude JSONL 逐行转换 + JsonlTails 增量尾随）。"""
import json
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import tail_attach as ta

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) claude_jsonl_to_events 转换 ----
seq = 0

# assistant tool_use 行 → tool/call + 锚点 assistant/message（仅 tool_use 无文本时补时间锚点）
evs = ta.claude_jsonl_to_events({
    "type": "assistant", "timestamp": "2026-08-15T10:00:05Z",
    "message": {"role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "Skill",
                             "input": {"name": "G66"}}],
                "usage": {"input_tokens": 100, "output_tokens": 5}}
}, seq); seq += 1
check("tool_use → tool/call + 锚点消息", len(evs) == 2
      and evs[0]["type"] == "tool/call" and evs[1]["type"] == "assistant/message")
check("callId/arguments", evs[0]["data"]["callId"] == "call_1"
      and json.loads(evs[0]["data"]["arguments"]) == {"name": "G66"})
check("锚点消息空 content 同时间", evs[1]["data"]["message"]["content"] == []
      and evs[1]["time"] == evs[0]["time"])

# user tool_result 行 → tool/result
evs = ta.claude_jsonl_to_events({
    "type": "user", "timestamp": "2026-08-15T10:00:06Z",
    "message": {"role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1",
                             "content": "ok", "is_error": False}]}
}, seq); seq += 1
check("tool_result → tool/result", len(evs) == 1 and evs[0]["type"] == "tool/result")
check("tool/result 配对 + isError", evs[0]["data"]["callId"] == "call_1"
      and evs[0]["data"]["message"]["content"][0]["isError"] is False)

# user 真实指令 → user/message
evs = ta.claude_jsonl_to_events({
    "type": "user", "timestamp": "2026-08-15T10:00:00Z",
    "message": {"role": "user", "content": "帮我部署g66"}
}, seq); seq += 1
check("user 文本 → user/message", len(evs) == 1 and evs[0]["type"] == "user/message")
check("user/message 内容", evs[0]["data"]["content"][0]["text"] == "帮我部署g66")

# / 开头的命令应排除
evs = ta.claude_jsonl_to_events({
    "type": "user", "message": {"role": "user", "content": "/clear"}}, seq); seq += 1
check("/ 命令不产事件", evs == [])

# assistant 文本 + usage 归一化
evs = ta.claude_jsonl_to_events({
    "type": "assistant", "timestamp": "2026-08-15T10:00:10Z",
    "message": {"role": "assistant",
                "content": [{"type": "text", "text": "完成"}],
                "usage": {"input_tokens": 200, "output_tokens": 10,
                          "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20}}
}, seq); seq += 1
am = next(e for e in evs if e["type"] == "assistant/message")
check("usage 归一化驼峰", am["data"]["message"]["usage"] ==
      {"inputTokens": 200, "outputTokens": 10, "cacheReadTokens": 50, "cacheWriteTokens": 20},
      f"实际 {am['data']['message']['usage']}")

# 混合：text + tool_use 同一条 assistant
evs = ta.claude_jsonl_to_events({
    "type": "assistant",
    "message": {"role": "assistant",
                "content": [{"type": "text", "text": "先看看"},
                            {"type": "tool_use", "id": "c2", "name": "Bash", "input": {}}]}
}, seq)
types = [e["type"] for e in evs]
check("混合行产 assistant/message + tool/call", "assistant/message" in types and "tool/call" in types)

# attachment/summary 忽略
evs = ta.claude_jsonl_to_events({"type": "attachment", "attachment": {"type": "skill_listing"}}, 999)
check("attachment 不产事件", evs == [])

# ---- 2) JsonlTails 增量尾随 ----
collected = []
def on_events(session_id, events):
    collected.extend(events)

with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "live.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "初始"}}) + "\n",
                 encoding="utf-8")
    tails = ta.JsonlTails(on_events=on_events, poll_interval=0.05)
    ok = tails.start("s1", str(f))
    check("start 返回 True", ok is True)
    # 新行为：start 后先读已有内容（live 打开即有历史），初始 user 行应被收集
    deadline = time.time() + 3
    while time.time() < deadline and len(collected) == 0:
        time.sleep(0.05)
    check("start 重放已有内容（user/message）",
          any(e["type"] == "user/message" for e in collected),
          f"实际 {[e['type'] for e in collected]}")

    # 追加新行 → 尾随应收到
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "message": {"role": "assistant",
                                         "content": [{"type": "tool_use", "id": "c9",
                                                      "name": "Bash", "input": {}}]}}) + "\n")
    deadline = time.time() + 3
    while time.time() < deadline and not any(e["type"] == "tool/call" for e in collected):
        time.sleep(0.05)
    check("尾随收到新增事件（tool/call）",
          any(e["type"] == "tool/call" for e in collected),
          f"实际 {[e['type'] for e in collected]}")
    check("尾随事件类型 tool/call", any(e["type"] == "tool/call" for e in collected))

    # start 不存在的文件 → False
    check("不存在的文件 start False", tails.start("s2", str(Path(td) / "nope.jsonl")) is False)

    # stop 后不再收到
    tails.stop("s1")
    n = len(collected)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "追加2"}}) + "\n")
    time.sleep(0.3)
    check("stop 后不再收集", len(collected) == n, f"实际 {len(collected)}")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

# -*- coding: utf-8 -*-
"""M2 回归：CodemakerCliAdapter 解析 CLI JSON 行 → 统一事件 → EventMetrics 折叠。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from codemaker_backend import CodemakerCliAdapter
from dsh_backend import EventMetrics

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# 真实冒烟（codemaker run --format json）事件结构：tool 回合(tool-calls) + 文本回合(stop)
rows = [
    {"type": "step_start", "timestamp": 1786982990763, "sessionID": "ses_x",
     "part": {"id": "prt_s1", "messageID": "msg_A", "sessionID": "ses_x", "type": "step-start"}},
    {"type": "tool_use", "timestamp": 1786982994572, "sessionID": "ses_x",
     "part": {"type": "tool", "tool": "read", "callID": "call_1",
              "state": {"status": "completed", "input": {"filePath": "D:\\x"},
                        "output": "<entries>...</entries>"},
              "messageID": "msg_A"}},
    {"type": "step_finish", "timestamp": 1786982994572, "sessionID": "ses_x",
     "part": {"id": "prt_f1", "reason": "tool-calls", "messageID": "msg_A",
              "sessionID": "ses_x", "type": "step-finish",
              "tokens": {"total": 14995, "input": 12789, "output": 53, "reasoning": 105,
                         "cache": {"write": 0, "read": 2048}}, "cost": 0.0393662}},
    {"type": "step_start", "timestamp": 1786982995703, "sessionID": "ses_x",
     "part": {"id": "prt_s2", "messageID": "msg_B", "sessionID": "ses_x", "type": "step-start"}},
    {"type": "text", "timestamp": 1786982997938, "sessionID": "ses_x",
     "part": {"id": "prt_t", "messageID": "msg_B", "sessionID": "ses_x", "type": "text",
              "text": "当前目录内容如下"}},
    {"type": "step_finish", "timestamp": 1786982997941, "sessionID": "ses_x",
     "part": {"id": "prt_f2", "reason": "stop", "messageID": "msg_B",
              "sessionID": "ses_x", "type": "step-finish",
              "tokens": {"total": 15237, "input": 134, "output": 127, "reasoning": 0,
                         "cache": {"write": 0, "read": 14976}}, "cost": 0.0015384}},
]

adapter = CodemakerCliAdapter()
metrics = EventMetrics()
for r in rows:
    for ev in adapter.adapt_line(json.dumps(r, ensure_ascii=False)):
        metrics.on_event(ev)
metrics.finalize()
snap = metrics.snapshot()

check("tool 调用 1 次", snap["tool_calls_total"] == 1, f"实际 {snap['tool_calls_total']}")
check("tool 成功 1 次", snap["tool_success"] == 1, f"实际 {snap['tool_success']}")
check("tool 失败 0 次", snap["tool_fail"] == 0, f"实际 {snap['tool_fail']}")
check("turns=1（stop 回合发 turn/end）", snap["turns"] == 1, f"实际 {snap['turns']}")
check("turn_end_reason=completed", snap["turn_end_reason"] == "completed",
      f"实际 {snap['turn_end_reason']}")
check("usage 只取最终累计（input=134）", snap["input_tokens"] == 134,
      f"实际 {snap['input_tokens']}")
check("cache_read=14976", snap["cache_read_tokens"] == 14976,
      f"实际 {snap['cache_read_tokens']}")
check("assistant_text 含最终文本", "当前目录内容如下" in (metrics.assistant_text() or ""),
      f"实际 {metrics.assistant_text()!r}")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

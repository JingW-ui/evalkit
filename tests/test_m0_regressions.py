# -*- coding: utf-8 -*-
"""M0 正确性回归：turns 自增 / report 正负例分组 / claude chunk step 归位。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dsh_backend import EventMetrics
from claude_backend import ClaudeEventAdapter
from report import aggregate

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


T0 = 1_700_000_000_000

# ---- 1) EventMetrics.turns 自增（修复：turn/end 未自增导致恒 0） ----
m = EventMetrics()
m.on_event({"type": "turn/start", "time": T0, "data": {"turn": 0}})
m.on_event({"type": "turn/end", "time": T0 + 100,
            "data": {"turn": 0, "reason": {"kind": "completed"}}})
m.finalize()
snap = m.snapshot()
check("turns 自增（1 个 turn/end → turns=1）", snap["turns"] == 1,
      f"实际 {snap['turns']}")

# ---- 2) report.aggregate 正/负例分组（修复：三次覆盖赋值） ----
recs = [
    {"triggered_when_should": True, "skill_expected": "g66"},
    {"triggered_when_should": True, "skill_expected": "g66"},
    {"triggered_when_should": None, "skill_expected": "g66"},
]
agg = aggregate(recs)
check("正例分组=2", agg["positive_runs"] == 2, f"实际 {agg['positive_runs']}")
check("负例分组=1", agg["negative_runs"] == 1, f"实际 {agg['negative_runs']}")

# ---- 3) ClaudeEventAdapter chunk step 归位（修复：chunk step 差一） ----
adapter = ClaudeEventAdapter()
evs = adapter.adapt({
    "type": "assistant",
    "message": {"model": "claude", "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 1}, "stop_reason": "tool_use"},
    "timestamp": "2026-08-15T14:11:44.604Z",
})
evs += adapter.adapt({
    "type": "stream_event",
    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    "timestamp": "2026-08-15T14:11:44.700Z",
})
chunks = [e for e in evs if e["type"] == "assistant/chunk"]
check("chunk step 归位到 0（不再差一）", bool(chunks) and chunks[0]["data"]["step"] == 0,
      f"实际 {[e['data']['step'] for e in chunks]}")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

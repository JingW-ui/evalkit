# -*- coding: utf-8 -*-
"""M3 回归：EvalStore.stats（SR + 均值±σ）+ review（人工复核留痕）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval_store import EvalStore

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


with tempfile.TemporaryDirectory() as td:
    s = EvalStore(Path(td) / "t.db")
    # 建任务定义（stats 只统计 task_id 在 tasks 表里的记录）
    s.upsert_task({"task_id": "t1", "level": "L3", "skill_expected": "g66", "query": "q"})
    # 同一 task 3 次执行
    s.upsert({"session_id": "t1-r0", "task_id": "t1", "run_idx": 0, "agent": "claude",
              "skill_expected": "g66", "level": "L3", "success": True,
              "tool_calls_total": 5, "tool_success": 5, "tool_fail": 0,
              "input_tokens": 1000, "cost_cny": 1.0, "duration_ms": 100,
              "human_interventions": 0, "turn_end_reason": "completed", "_at": 1})
    s.upsert({"session_id": "t1-r1", "task_id": "t1", "run_idx": 1, "agent": "claude",
              "skill_expected": "g66", "level": "L3", "success": True,
              "tool_calls_total": 10, "tool_success": 8, "tool_fail": 2,
              "input_tokens": 2000, "cost_cny": 1.2, "duration_ms": 120,
              "human_interventions": 1, "turn_end_reason": "completed", "_at": 2})
    s.upsert({"session_id": "t1-r2", "task_id": "t1", "run_idx": 2, "agent": "claude",
              "skill_expected": "g66", "level": "L3", "success": False,
              "tool_calls_total": 6, "tool_success": 3, "tool_fail": 3,
              "input_tokens": 3000, "cost_cny": 1.4, "duration_ms": 140,
              "human_interventions": 2, "turn_end_reason": "error", "_at": 3})
    rows = s.stats()["rows"]
    check("stats 按 task 聚合成 1 行", len(rows) == 1, f"实际 {len(rows)}")
    row = rows[0]
    check("n=3", row["n"] == 3, f"实际 {row['n']}")
    check("success=2", row["success"] == 2, f"实际 {row['success']}")
    check("sr=0.667", abs(row["sr"] - 0.667) < 0.001, f"实际 {row['sr']}")
    check("duration 均值=120", row["duration_ms"] == 120.0, f"实际 {row['duration_ms']}")
    check("duration σ=20（样本标准差）", abs(row["duration_sd"] - 20.0) < 0.01,
          f"实际 {row['duration_sd']}")
    check("cost 均值=1.2", abs(row["cost_cny"] - 1.2) < 0.001, f"实际 {row['cost_cny']}")
    check("cost σ=0.2", abs(row["cost_sd"] - 0.2) < 0.001, f"实际 {row['cost_sd']}")
    check("tool_sr 均值≈0.767", abs(row["tool_sr"] - 0.7667) < 0.001, f"实际 {row['tool_sr']}")

    # review：修正 level/success 留痕
    r = s.review("t1-r2", level="L2", success=True, note="人工修正：实际成功")
    check("review 修正 level", r["level"] == "L2", f"实际 {r['level']}")
    check("review 修正 success", r["success"] is True, f"实际 {r['success']}")
    check("review 保留 level_auto", r["level_auto"] == "L3", f"实际 {r['level_auto']}")
    check("review 保留 success_auto", r["success_auto"] == 0, f"实际 {r['success_auto']}")
    check("review_status=corrected", r["review_status"] == "corrected",
          f"实际 {r['review_status']}")
    check("review_note 记录", r["review_note"] == "人工修正：实际成功")

    # reset 回自动值
    r2 = s.review("t1-r2", reset=True)
    check("reset 回 level_auto", r2["level"] == "L3", f"实际 {r2['level']}")
    check("reset 回 success_auto", r2["success"] is False, f"实际 {r2['success']}")
    check("reset 后 unreviewed", r2["review_status"] == "unreviewed",
          f"实际 {r2['review_status']}")

    s.close()

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

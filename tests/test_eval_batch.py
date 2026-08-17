# -*- coding: utf-8 -*-
"""单元测试：eval_batch.py（批量评测执行器）+ task_gen 集成。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import eval_batch as eb
import task_gen as tg
from eval_records import EvalRecords, judge_eval

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 任务加载 ----
with tempfile.TemporaryDirectory() as td:
    tg.generate_tasks(["g66", "generic"], {"device": "SN-1"}, td, count=1)
    tasks = eb.load_tasks_from_dir(td)
    check("load_tasks_from_dir 全量 9", len(tasks) == 9, f"实际 {len(tasks)}")
    g66 = eb.load_tasks_from_dir(td, domains=["g66"])
    check("按域过滤", len(g66) == 5 and all(t["skill_expected"] == "g66" for t in g66))
    l2 = eb.load_tasks_from_dir(td, levels=["L2"])
    check("按级过滤", len(l2) == 3 and all(t["level"] == "L2" for t in l2))
    lim = eb.load_tasks_from_dir(td, limit=4)
    check("limit 截断", len(lim) == 4)
    check("空目录", eb.load_tasks_from_dir(Path(td) / "nope") == [])

    # ---- 成本折算 ----
    m = eb._enrich_cost({"model": "deepseek-v4-flash", "input_tokens": 1000,
                         "output_tokens": 500, "cache_read_tokens": 0,
                         "cache_write_tokens": 0, "cost_usd": None})
    check("_enrich_cost 出估算 + cny", m.get("cost_usd_est") is not None
          and m.get("cost_est_cny") is not None)
    m2 = eb._enrich_cost({"cost_usd": 0.5})
    check("_enrich_cost 官方 cost → cny", abs(m2["cost_cny"] - 0.5 * 7.2) < 0.01)

    # ---- judge_eval 带显式 tasks（闭环判定） ----
    task = json.load(open([p for p in tg.generate_tasks(["g66"], {"device": "SN-1"}, td, count=1)][0],
                          encoding="utf-8"))
    # evidence_anchor 任务：assistant 文本含锚点 → 成功
    v = judge_eval(task["query"], "sess-1",
                   {"turn_end_reason": "completed", "tool_calls_total": 1},
                   "我调用了 list_devices 工具，设备列表如下", tasks=[task])
    check("judge 带任务 → task 源 + 锚点成功",
          v["level_source"] == "task" and v["success"] is True,
          f"实际 {v}")
    # L4 negative_honesty：诚实失败 → 成功；幻觉成功 → 失败
    l4 = json.load(open([p for p in tg.generate_tasks(["g66"], {}, td, count=1) if "L4" in p.name][0],
                        encoding="utf-8"))
    v2 = judge_eval(l4["query"], "sess-2", {"turn_end_reason": "completed"},
                    "该设备不存在，无法完成部署", tasks=[l4])
    check("L4 诚实失败 → 成功", v2["success"] is True, f"实际 {v2}")
    v3 = judge_eval(l4["query"], "sess-3", {"turn_end_reason": "completed"},
                    "已成功拉起部署，部署验证完成", tasks=[l4])
    check("L4 幻觉成功 → 失败", v3["success"] is False, f"实际 {v3}")

    # ---- dry-run 不执行 ----
    out = eb.run_batch(eb.load_tasks_from_dir(td, limit=3), dry_run=True)
    check("dry-run 不产结果", out["results"] == [])

    # ---- 执行失败容错（fake backend） ----
    def fake_run(task, **kw):
        raise RuntimeError("boom")
    orig = eb.run_one_task
    eb.run_one_task = fake_run
    try:
        out = eb.run_batch(eb.load_tasks_from_dir(td, limit=2))
        check("执行失败容错", len(out["results"]) == 2
              and all(r["result"] is None and "boom" in (r["error"] or "") for r in out["results"]))
    finally:
        eb.run_one_task = orig

# ---- 矩阵输出不抛错 ----
with tempfile.TemporaryDirectory() as td2:
    rec = EvalRecords(path=Path(td2) / "rec.db")
    rec.add({"session_id": "s1", "agent": "claude", "level": "L1", "level_source": "task",
             "level_reason": "", "success": True, "success_by": "anchor",
             "tool_calls_total": 1, "input_tokens": 100, "cost_cny": 0.1,
             "human_interventions": 0, "turn_end_reason": "completed", "query": "q"})
    try:
        import io
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        eb.print_matrix(rec)
        sys.stdout = old
        check("print_matrix 输出", "L1" in buf.getvalue() and "claude" in buf.getvalue())
    except Exception as e:
        sys.stdout = old
        check("print_matrix 不抛错", False, f"{type(e).__name__}: {e}")
    rec.close()

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

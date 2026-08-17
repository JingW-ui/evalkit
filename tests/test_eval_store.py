# -*- coding: utf-8 -*-
"""M1 回归：EvalStore（SQLite）upsert 覆盖语义 + 迁移一致性。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval_store import EvalStore, migrate_json_to_db

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) 临时库 upsert 覆盖语义 + bool 还原 ----
with tempfile.TemporaryDirectory() as td:
    store = EvalStore(Path(td) / "t.db")
    store.upsert({"session_id": "s1", "agent": "claude", "level": "L1", "success": True,
                  "tool_calls_total": 1, "input_tokens": 10, "cost_cny": 0.1,
                  "human_interventions": 0, "turn_end_reason": "completed", "query": "q",
                  "_at": 1000})
    store.upsert({"session_id": "s1", "agent": "claude", "level": "L2", "success": False,
                  "tool_calls_total": 2, "input_tokens": 20, "cost_cny": 0.2,
                  "human_interventions": 1, "turn_end_reason": "error", "query": "q2",
                  "_at": 2000})
    all_recs = store.all()
    check("同 session_id 覆盖（仅 1 条）", len(all_recs) == 1, f"实际 {len(all_recs)}")
    check("覆盖后取最新值（level=L2）", all_recs[0]["level"] == "L2",
          f"实际 {all_recs[0]['level']}")
    check("success 还原为 bool", isinstance(all_recs[0]["success"], bool),
          f"实际 {type(all_recs[0]['success'])}")
    m = store.matrix()
    check("matrix 结构（portrait + records）", set(m) == {"portrait", "records"})
    check("portrait 计数正确（1 条 0 成功）",
          m["portrait"][0]["count"] == 1 and m["portrait"][0]["success"] == 0,
          f"实际 {m['portrait'][0]}")
    store.close()

# ---- 2) 迁移一致性（真实 eval_records.json → 临时 db） ----
json_path = Path(__file__).parent.parent / "results" / "eval_records.json"
if json_path.is_file():
    with open(json_path, "r", encoding="utf-8") as f:
        recs = json.load(f)
    n_json = len(recs)
    json_success = sum(1 for r in recs if r.get("success"))
    with tempfile.TemporaryDirectory() as td:
        n = migrate_json_to_db(json_path, Path(td) / "m.db")
        check("迁移条数 = json 条数", n == n_json, f"{n} vs {n_json}")
        store = EvalStore(Path(td) / "m.db")
        check("all() 条数一致", len(store.all()) == n_json,
              f"实际 {len(store.all())}")
        m = store.matrix()
        total_count = sum(p["count"] for p in m["portrait"])
        total_success = sum(p["success"] for p in m["portrait"])
        check("portrait 总 count 一致", total_count == n_json,
              f"{total_count} vs {n_json}")
        check("portrait 总 success 一致", total_success == json_success,
              f"{total_success} vs {json_success}")
        store.close()
else:
    print("[SKIP] results/eval_records.json 不存在，跳过迁移一致性检查")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

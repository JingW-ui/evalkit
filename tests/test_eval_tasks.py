# -*- coding: utf-8 -*-
"""M4 回归：EvalStore 任务定义 CRUD + import_papers（papers/*.yaml 导入）。"""
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
    s.upsert_task({"task_id": "t1", "level": "L1", "skill_expected": "g66", "query": "q",
                   "success_condition": {"type": "evidence_anchor", "anchors": ["a"], "threshold": 1}})
    tasks = s.list_tasks()
    check("upsert + list", len(tasks) == 1 and tasks[0]["task_id"] == "t1", f"实际 {tasks}")
    check("success_condition 还原为 dict", tasks[0]["success_condition"]["type"] == "evidence_anchor")
    check("get_task", s.get_task("t1")["level"] == "L1")

    # 覆盖更新（同 task_id）
    s.upsert_task({"task_id": "t1", "level": "L2", "skill_expected": "g66", "query": "q2",
                   "success_condition": {"type": "file_exists", "path": "x"}})
    check("同 task_id 覆盖", s.get_task("t1")["level"] == "L2" and len(s.list_tasks()) == 1)

    # import_papers：从 papers/*.yaml 导入（23 题）
    n = len(s.import_papers())
    check("import_papers 导入 23 题", n == 23, f"实际 {n}")
    check("list 共 24 条（1+23）", len(s.list_tasks()) == 24, f"实际 {len(s.list_tasks())}")
    # 停用题（enabled=false）仍在表里，批次过滤由上层 start_batch 处理
    t = s.get_task("L3_g66_deploy")
    check("停用题 enabled=0", t["enabled"] == 0)

    # delete
    check("delete t1", s.delete_task("t1") is True)
    check("delete 后 23 条", len(s.list_tasks()) == 23)
    check("delete 不存在返回 False", s.delete_task("nope") is False)

    s.close()

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

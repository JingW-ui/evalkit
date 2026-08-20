#!/usr/bin/env python3
"""一次性：用组合规则树评估器重新判定已有执行记录（回放 session.jsonl → metrics → 判定）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval_store import EvalStore
from eval_records import _evaluate_success_condition
from dsh_backend import EventMetrics

store = EvalStore()
batch_root = Path(__file__).parent.parent / "results" / "batch"
tasks = {t["task_id"]: t for t in store.list_tasks()}

updated = 0
for rec in store.all():
    tid = rec.get("task_id")
    if not tid or tid not in tasks:
        continue
    sid = rec.get("session_id")
    jl = batch_root / sid / "session.jsonl"
    if not jl.is_file():
        print(f"  [skip] {tid} {sid[:24]} 无 session.jsonl")
        continue
    metrics = EventMetrics()
    for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "session":
            continue
        metrics.on_event(ev)
    metrics.finalize()
    snapshot = metrics.snapshot()
    text = metrics.assistant_text()
    sc = tasks[tid].get("success_condition") or {}
    success, by = _evaluate_success_condition(sc, snapshot, text, task=tasks[tid])
    r = store.get(sid)
    if r is not None:
        r["success"] = 1 if success else 0
        r["success_by"] = by
        store.upsert(r)
        updated += 1
        print(f"  {tid} {sid[5:25]} -> success={success} ({by})")

print(f"\n重新判定 {updated} 条")
store.close()

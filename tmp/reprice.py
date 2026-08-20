#!/usr/bin/env python3
"""一次性回填：把 executions 表的 cost_cny 从「模型/平台结算价（虚高）」改成「挂牌价估算」。

原理：对每条有 model + tokens 的记录，用 cost.estimate_cost_cny（models_usd 挂牌价表）
重算成本并覆盖 cost_cny。无 model / 无挂牌价的记录跳过（保留原值）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval_store import EvalStore
from cost import estimate_cost_cny

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

store = EvalStore()
updated = skipped = 0
for r in store.all():
    model = r.get("model")
    if not model:
        skipped += 1
        continue
    tokens = {
        "input_tokens": r.get("input_tokens") or 0,
        "output_tokens": r.get("output_tokens") or 0,
        "cache_read_tokens": r.get("cache_read_tokens") or 0,
        "cache_write_tokens": 0,
    }
    est = estimate_cost_cny(model, tokens)
    if est is None:
        skipped += 1
        continue
    new_cost = round(est, 4)
    old = r.get("cost_cny")
    r["cost_cny"] = new_cost
    store.upsert(r)
    updated += 1
    print(f"{str(r.get('task_id') or ''):<16} {model:<18} old={old} -> new={new_cost}")

print(f"\nupdated={updated} skipped={skipped}")
store.close()

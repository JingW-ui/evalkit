#!/usr/bin/env python3
"""一次性：把 5 份实测收集整合成 papers/references.json（task_id → 实测 content + 工具链）。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 顺序：后面的覆盖前面的（l2_fix 覆盖 l2 里未完成的 remote_download/write_read）
SOURCES = [
    ROOT / "results" / "l1_collection.json",
    ROOT / "results" / "l2_collection.json",
    ROOT / "results" / "l2_fix_collection.json",
    ROOT / "results" / "l3_uu_clean_collection.json",
    ROOT / "results" / "l3_uu_uninstalled_collection.json",
    ROOT / "results" / "l4_collection.json",
]

refs = {}
for src in SOURCES:
    if not src.is_file():
        print(f"skip (missing): {src.name}")
        continue
    data = json.loads(src.read_text(encoding="utf-8"))
    for r in data:
        tid = r.get("task_id")
        if not tid or r.get("error"):
            continue
        refs[tid] = {
            "model": r.get("model"),
            "content": (r.get("final_content") or "").strip(),
            "tools": [t.get("name") for t in (r.get("tool_chain") or [])],
        }

out = ROOT / "papers" / "references.json"
out.write_text(json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"整合 {len(refs)} 个任务的参考答案 -> {out}")
for tid in sorted(refs):
    print(f"  {tid}: tools={len(refs[tid]['tools'])} content={len(refs[tid]['content'])}字")

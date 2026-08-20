#!/usr/bin/env python3
"""跑 L1 六题 × deepseek-v4-pro / deepseek-v4-flash，收集对比结果 → results/l1_ds_comparison.json"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import yaml
from claude_backend import ClaudeEvalBackend

DEVICE = "032E02B4-0499-0580-9106-A70700080009"
BIND = {"{device}": DEVICE, "{dir}": "D:/wy_projects/evalkit", "{file}": "conf.json"}
MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"]
TASKS = ["L1_device_list", "L1_connect_check", "L1_process_probe",
         "L1_file_exists", "L1_env_info", "L1_resolution"]
OUT = Path(__file__).parent.parent / "results" / "l1_ds_comparison.json"


def run_one(tid, model):
    p = Path(__file__).parent.parent / "papers" / "L1" / f"{tid.split('_', 1)[1]}.yaml"
    task = yaml.safe_load(p.read_text(encoding="utf-8"))
    q = task["query"]
    for k, v in BIND.items():
        q = q.replace(k, v)
    task["query"] = q
    with ClaudeEvalBackend(
        session_root=Path(__file__).parent.parent / "results" / "batch",
        cwd=r"D:\wy_projects\work_4_log",
        permission_mode="bypassPermissions",
        model=model,
        provider="codemaker_deepseek",
    ) as b:
        r = b.run_task(task, timeout_s=300)
    m = r.get("metrics") or {}
    tool_names = []
    for t in (m.get("tasks") or []):
        for tool in (t.get("tools") or []):
            tool_names.append(tool.get("name"))
    rec = {
        "task_id": tid,
        "model": model,
        "session_id": r.get("session_id"),
        "finish_reason": r.get("finish_reason"),
        "final_content": (r.get("assistant_text") or "").strip(),
        "tool_chain": tool_names,
        "summary": {
            "tool_calls_total": m.get("tool_calls_total"),
            "tool_calls_by_name": m.get("tool_calls_by_name"),
            "tool_success": m.get("tool_success"),
            "tool_fail": m.get("tool_fail"),
            "turn_end_reason": m.get("turn_end_reason"),
            "duration_ms": m.get("duration_ms"),
        },
    }
    print(f"  [{model}] {tid} finish={r.get('finish_reason')} tools={m.get('tool_calls_total')}")
    return rec


def main():
    results = []
    for model in MODELS:
        for tid in TASKS:
            try:
                results.append(run_one(tid, model))
            except Exception as exc:
                results.append({"task_id": tid, "model": model,
                                "error": f"{type(exc).__name__}: {exc}"})
            OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nDONE ->", OUT)


if __name__ == "__main__":
    main()

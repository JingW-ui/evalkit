#!/usr/bin/env python3
"""一次性：跑 L2 全部 6 题（claude + claude-opus-4-8，串号直绑 + 具体变量），收集工具链+content → results/l2_collection.json"""
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
URL = "http://10.226.230.230:18999/cfg_smoke.txt"

# 逐题变量绑定
BINDINGS = {
    "L2_occupy_push": {"{device}": DEVICE, "{file}": "D:/wy_projects/evalkit/sandbox/cfg_smoke.txt"},
    "L2_remote_download": {"{device}": DEVICE, "{url}": URL},
    "L2_write_read": {"{device}": DEVICE, "{dir}": "D:/evalkit_test", "{file}": "write_read.txt"},
    "L2_software_probe": {"{device}": DEVICE},
    "L2_process_manage": {"{device}": DEVICE},
    "L2_session_check": {"{device}": DEVICE},
}
TASKS = ["L2_occupy_push", "L2_remote_download", "L2_write_read",
         "L2_software_probe", "L2_process_manage", "L2_session_check"]
OUT = Path(__file__).parent.parent / "results" / "l2_collection.json"


def run_one(tid):
    p = Path(__file__).parent.parent / "papers" / "L2" / f"{tid.split('_', 1)[1]}.yaml"
    task = yaml.safe_load(p.read_text(encoding="utf-8"))
    q = task["query"]
    for k, v in BINDINGS.get(tid, {}).items():
        q = q.replace(k, v)
    task["query"] = q
    print(f"\n########## {tid} ##########")
    print("query:", q)
    with ClaudeEvalBackend(
        session_root=Path(__file__).parent.parent / "results" / "batch",
        cwd=r"D:\wy_projects\work_4_log",
        permission_mode="bypassPermissions",
        model="claude-opus-4-8",
        provider="codemaker_deepseek",
    ) as b:
        r = b.run_task(task, timeout_s=300)
    m = r.get("metrics") or {}
    tools = []
    for t in (m.get("tasks") or []):
        for tool in (t.get("tools") or []):
            tools.append({k: tool.get(k) for k in ("name", "args", "ok", "result")})
    rec = {
        "task_id": tid,
        "session_id": r.get("session_id"),
        "finish_reason": r.get("finish_reason"),
        "model": m.get("model"),
        "query": q,
        "final_content": r.get("assistant_text") or "",
        "tool_chain": tools,
        "summary": {
            "tool_calls_total": m.get("tool_calls_total"),
            "tool_calls_by_name": m.get("tool_calls_by_name"),
            "tool_success": m.get("tool_success"),
            "tool_fail": m.get("tool_fail"),
            "turn_end_reason": m.get("turn_end_reason"),
            "duration_ms": m.get("duration_ms"),
        },
    }
    print("  model:", m.get("model"), "finish:", r.get("finish_reason"),
          "tools:", m.get("tool_calls_total"))
    return rec


def main():
    results = []
    for tid in TASKS:
        try:
            results.append(run_one(tid))
        except Exception as exc:
            print(f"  !! {tid} 失败: {type(exc).__name__}: {exc}")
            results.append({"task_id": tid, "error": f"{type(exc).__name__}: {exc}"})
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nALL DONE,", len(results), "records ->", OUT)


if __name__ == "__main__":
    main()

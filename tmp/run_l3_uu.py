#!/usr/bin/env python3
"""一次性：跑 L3 uu 两题（uu 取号 + uu 卸载），收集工具链+content → results/l3_uu_collection.json"""
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
BINDINGS = {
    "L3_uu_take_code": {"{device}": DEVICE, "{code}": "163a163a"},
    "L3_uu_uninstall": {"{device}": DEVICE},
}
OUT = Path(__file__).parent.parent / "results" / "l3_uu_collection.json"


def run_one(tid):
    p = Path(__file__).parent.parent / "papers" / "L3" / f"{tid.split('_', 1)[1]}.yaml"
    task = yaml.safe_load(p.read_text(encoding="utf-8"))
    q = task["query"]
    for k, v in BINDINGS.get(tid, {}).items():
        q = q.replace(k, v)
    task["query"] = q
    print(f"\n########## {tid} ##########\nquery:", q)
    with ClaudeEvalBackend(
        session_root=Path(__file__).parent.parent / "results" / "batch",
        cwd=r"D:\wy_projects\work_4_log",
        permission_mode="bypassPermissions",
        model="claude-opus-4-8",
        provider="codemaker_deepseek",
    ) as b:
        r = b.run_task(task, timeout_s=600)
    m = r.get("metrics") or {}
    tools = []
    for t in (m.get("tasks") or []):
        for tool in (t.get("tools") or []):
            tools.append({k: tool.get(k) for k in ("name", "args", "ok", "result")})
    return {
        "task_id": tid, "session_id": r.get("session_id"),
        "finish_reason": r.get("finish_reason"), "model": m.get("model"),
        "query": q, "final_content": r.get("assistant_text") or "",
        "tool_chain": tools,
        "summary": {"tool_calls_total": m.get("tool_calls_total"),
                    "tool_calls_by_name": m.get("tool_calls_by_name"),
                    "tool_success": m.get("tool_success"), "tool_fail": m.get("tool_fail"),
                    "turn_end_reason": m.get("turn_end_reason"),
                    "duration_ms": m.get("duration_ms")},
    }


def main():
    results = []
    for tid in ("L3_uu_take_code", "L3_uu_uninstall"):
        try:
            results.append(run_one(tid))
        except Exception as exc:
            results.append({"task_id": tid, "error": f"{type(exc).__name__}: {exc}"})
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nDONE ->", OUT)


if __name__ == "__main__":
    main()

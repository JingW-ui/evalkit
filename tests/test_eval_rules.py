# -*- coding: utf-8 -*-
"""组合规则树评估器测试：all/any/not + 8 原子条件 + MCP 前缀归一化。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval_records import _evaluate_success_condition, _strip_mcp, judge_eval

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- MCP 前缀归一化 ----
check("strip mcp 前缀", _strip_mcp("mcp__airgattai__list_devices") == "list_devices")
check("本地工具保留", _strip_mcp("Bash") == "Bash" and _strip_mcp("Grep") == "Grep")

METRICS = {
    "tasks": [{"tools": [
        {"name": "Skill", "args": "{}", "result": "Launching", "ok": True},
        {"name": "mcp__airgattai__list_devices", "args": "{}",
         "result": "[serialno 032E, model GIH-D-20627]", "ok": True},
        {"name": "mcp__airgattai__shell", "args": '{"cmd":"tasklist"}',
         "result": "client.exe 1234", "ok": True},
        {"name": "mcp__airgattai__shell", "args": '{"cmd":"bad"}',
         "result": "WinAgentError 500", "ok": False},
    ]}],
    "tool_calls_total": 4, "tool_success": 3, "tool_fail": 1,
    "tool_fail_by_name": {"mcp__airgattai__shell": 1},
    "turn_end_reason": "completed", "duration_ms": 50000,
}
TEXT = "client.exe 未运行，serialno 已找到"


def ev(sc):
    return _evaluate_success_condition(sc, METRICS, TEXT)[0]


# ---- 组合算子 ----
check("all 全满足", ev({"type": "all", "rules": [
    {"type": "tool_called", "tool": "list_devices"},
    {"type": "text_contains", "any_of": ["serialno"]}], }) is True)
check("all 部分不满足", ev({"type": "all", "rules": [
    {"type": "tool_called", "tool": "list_devices"},
    {"type": "tool_called", "tool": "occupy_device"}], }) is False)
check("any 任一满足", ev({"type": "any", "rules": [
    {"type": "tool_called", "tool": "occupy_device"},
    {"type": "tool_called", "tool": "list_devices"}], }) is True)
check("not 取反", ev({"type": "not", "rule": {"type": "tool_called", "tool": "occupy_device"}}) is True)

# ---- tool_called ----
check("tool_called 基础", ev({"type": "tool_called", "tool": "list_devices"}) is True)
check("tool_called args_contains",
      ev({"type": "tool_called", "tool": "shell", "args_contains": "tasklist"}) is True)
check("tool_called min_calls",
      ev({"type": "tool_called", "tool": "shell", "min_calls": 2}) is True)
check("tool_called 不存在", ev({"type": "tool_called", "tool": "push_file"}) is False)

# ---- tool_result ----
check("tool_result contains",
      ev({"type": "tool_result", "tool": "shell", "contains": "client.exe"}) is True)

# ---- tool_ok ----
check("tool_ok 特定工具", ev({"type": "tool_ok", "tool": "list_devices", "min": 1}) is True)
check("tool_ok 全局 min2", ev({"type": "tool_ok", "min": 2}) is True)
check("tool_ok 全局 min4(不够)", ev({"type": "tool_ok", "min": 4}) is False)

# ---- tool_success_rate ----
check("tool_success_rate 0.75", ev({"type": "tool_success_rate", "min": 0.75}) is True)
check("tool_success_rate 0.9", ev({"type": "tool_success_rate", "min": 0.9}) is False)

# ---- tool_fail_zero ----
check("tool_fail_zero 无失败", ev({"type": "tool_fail_zero", "tool": "list_devices"}) is True)
check("tool_fail_zero 有失败", ev({"type": "tool_fail_zero", "tool": "shell"}) is False)

# ---- text_contains ----
check("text_contains any_of", ev({"type": "text_contains", "any_of": ["未运行"]}) is True)
check("text_contains not_contains 通过",
      ev({"type": "text_contains", "not_contains": ["已启动"]}) is True)
check("text_contains not_contains 命中",
      ev({"type": "text_contains", "not_contains": ["未运行"]}) is False)

# ---- metric ----
check("metric >= ", ev({"type": "metric", "name": "duration_ms", "op": ">=", "value": 30000}) is True)
check("metric < ", ev({"type": "metric", "name": "duration_ms", "op": "<", "value": 10000}) is False)

# ---- judge_eval 集成（新规则树） ----
task = {"task_id": "t1", "level": "L1", "query": "列出设备",
        "success_condition": {"type": "all", "rules": [
            {"type": "tool_called", "tool": "list_devices"},
            {"type": "text_contains", "any_of": ["serialno"]}], }}
v = judge_eval("列出设备", "s1", METRICS, TEXT, tasks=[task])
check("judge 新规则树成功", v["success"] is True and v["level_source"] == "task", f"{v}")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")

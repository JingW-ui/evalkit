#!/usr/bin/env python3
"""
analyze.py — 评测数据产出工具（成本换算 + 环境指纹 + 结构化结果）。

职责边界（数据/解读分离）：
  - 本工具只产出「指标 + 成本 + 环境指纹」结构化数据
  - 诊断解读由外层 agent 生成（评估系统跑在 agent 环境里，由 agent 现场推理）

用法：
    python analyze.py --jsonl <会话.jsonl路径> --task <task_id> [--json] [--md]
"""

import json
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parser import replay_metrics, parse_session_jsonl
from cost import compute_cost, format_cost


# ---------- 环境指纹 ----------

def extract_env_fingerprint(jsonl_path: str, parsed: dict) -> dict:
    """
    从日志提取环境指纹，让不同评测结果可公平对比。

    Returns:
        {
            "model": str,
            "skill_count": int,          # 环境挂载的 skill 数（skill_listing.skillCount）
            "mcp_tools_used": int,        # 实际调用到的 mcp__ 工具去重数
            "distinct_tools_used": int,   # 实际调用的所有工具去重数
        }
    """
    distinct_tools = set()
    mcp_tools = set()
    for t in parsed.get("tool_sequence", []):
        name = t["name"]
        distinct_tools.add(name)
        if name.startswith("mcp__"):
            mcp_tools.add(name)

    # skill_count 从 skill_listing 附件补扫
    skill_count = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "attachment":
                att = obj.get("attachment", {})
                if att.get("type") == "skill_listing":
                    skill_count = att.get("skillCount", len(att.get("names", [])))
                    break

    return {
        "model": parsed.get("model", ""),
        "skill_count": skill_count,
        "mcp_tools_used": len(mcp_tools),
        "distinct_tools_used": len(distinct_tools),
    }


# ---------- 数据产出 ----------

def analyze(jsonl_path: str, task: dict) -> dict:
    """产出结构化评测数据：指标 + 成本 + 环境指纹。"""
    result = replay_metrics(jsonl_path, task)
    parsed = parse_session_jsonl(jsonl_path)

    fingerprint = extract_env_fingerprint(jsonl_path, parsed)
    cost = compute_cost(result["metrics"])

    # 把成本、指纹合并进 metrics，便于落库
    return {
        "task_id": result["task_id"],
        "skill_expected": result["skill_expected"],
        "level": result["level"],
        "mode": result["mode"],
        "session_id": result["session_id"],
        "jsonl_path": result["jsonl_path"],
        "metrics": result["metrics"],
        "cost": cost,
        "fingerprint": fingerprint,
    }


def to_markdown(data: dict) -> str:
    """把结构化数据渲染成 Markdown（不含诊断解读，解读由外层 agent 补）。"""
    m = data["metrics"]
    f = data["fingerprint"]
    c = data["cost"]
    skill = "✅ 触发" if m["skill_triggered"] else "❌ 未触发"
    succ = "✅ 成功" if m["task_success"] else "❌ 失败"
    evidence = "、".join(m["evidence_hit"]) if m["evidence_hit"] else "(无)"
    total_tokens = m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"] + m["output_tokens"]

    lines = [
        f"# 评测报告：{data['task_id']}",
        "",
        f"**Session**: `{data['session_id']}`",
        f"**Skill**: `{data.get('skill_expected') or '（纯 agent）'}`",
        f"**级别**: `{data.get('level', '')}` · **模型**: `{f.get('model', '')}`",
        "",
        "## 核心结论",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| Skill 触发 | {skill} |",
        f"| 任务完成 | {succ} |",
        f"| 命中证据锚点 | {evidence} |",
        f"| 人工介入次数 | {m['human_interventions']} 次 |",
        f"| 工具调用总数 | {m['tool_calls_total']} 次 |",
        f"| 用户轮次 | {m['user_turns']} 轮 |",
        "",
        "## 成本与 Token",
        "",
        f"**{format_cost(c)}**",
        "",
        "| 类型 | tokens | 成本 |",
        "|---|---|---|",
        f"| Input | {m['input_tokens']:,} | ¥{c['input_cost']:.2f} |",
        f"| Cache Read | {m['cache_read_tokens']:,} | ¥{c['cache_read_cost']:.2f} |",
        f"| Cache Write | {m['cache_write_tokens']:,} | ¥{c['cache_write_cost']:.2f} |",
        f"| Output | {m['output_tokens']:,} | ¥{c['output_cost']:.2f} |",
        f"| **合计** | **{total_tokens:,}** | **¥{c['total_cost']:.2f}** |",
        "",
        "## 环境指纹",
        "",
        f"- 挂载 skill 数：{f.get('skill_count', 0)}",
        f"- 实际调用的 mcp 工具数：{f.get('mcp_tools_used', 0)}",
        f"- 实际调用的工具种类数：{f.get('distinct_tools_used', 0)}",
        "",
        "## 工具调用分布",
        "",
        "| 工具 | 次数 |",
        "|---|---|",
    ]
    for name, cnt in sorted(m["tool_calls_by_name"].items(), key=lambda x: -x[1]):
        lines.append(f"| {name} | {cnt} |")

    if m.get("evidence_text"):
        lines += [
            "",
            "## 成功证据原文",
            "",
            "> " + m["evidence_text"].replace("\n", "\n> "),
            "",
        ]

    # 占位：诊断解读由外层 agent 生成，这里留一个标记
    lines += [
        "## 诊断洞察",
        "",
        "> （由外部评测 agent 现场生成，见对话输出）",
        "",
    ]

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="评测数据产出（指标+成本+环境指纹）")
    ap.add_argument("--jsonl", required=True, help="会话 JSONL 路径")
    ap.add_argument("--task", required=True, help="task_id")
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    if not Path(args.jsonl).exists():
        print(f"错误：JSONL 文件不存在：{args.jsonl}")
        sys.exit(1)

    # 加载 task
    task = None
    tasks_dir = Path(__file__).parent / "tasks"
    for tf in tasks_dir.glob("*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        if t.get("task_id") == args.task:
            task = t
            break
    if task is None:
        task = {"task_id": args.task}

    data = analyze(args.jsonl, task)

    # 落盘
    out_dir = Path(__file__).parent / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / f"analyze_{task['task_id']}_full.json"
    md_out = out_dir / f"analyze_{task['task_id']}_report.md"
    json_out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(to_markdown(data), encoding="utf-8")

    # 终端输出 JSON（外层 agent 从这里读数据生成解读）
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n报告: {md_out}")


if __name__ == "__main__":
    main()

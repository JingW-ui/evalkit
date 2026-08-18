#!/usr/bin/env python3
"""
replay.py — 离线分析一条已存在的 Claude Code 会话日志。

用法：
    python replay.py --jsonl <session.jsonl路径> --task <task_id> [--task-file <task.json路径>]

行为：
    读 JSONL → parser.replay_metrics → 输出结构化 JSON + Markdown 直观展示。

成功判据：采信 agent 自证，用 task 里声明的 success_evidence 锚点匹配，
不打二次裁决（符合"不另设判断标准"的原则）。
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from parser import replay_metrics


def load_task(task_id: str, tasks_dir: str = "tasks") -> dict:
    """根据 task_id 从 tasks 目录加载 task schema。"""
    base = _ROOT / tasks_dir
    for tf in base.glob("*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        if t.get("task_id") == task_id:
            return t
    # 没找到就返回一个最小 task（允许只给 task_id）
    return {"task_id": task_id}


def to_json(result: dict) -> str:
    result["parse_time"] = datetime.now().isoformat()
    return json.dumps(result, ensure_ascii=False, indent=2)


def to_markdown(result: dict) -> str:
    m = result["metrics"]
    skill = "✅ 触发" if m["skill_triggered"] else "❌ 未触发"
    succ = "✅ 成功" if m["task_success"] else "❌ 失败"
    evidence = "、".join(m["evidence_hit"]) if m["evidence_hit"] else "(无)"

    lines = [
        f"# 离线评测报告：{result['task_id']}",
        "",
        f"**Session**: `{result['session_id']}`",
        f"**Skill**: `{result['skill_expected']}`",
        f"**级别**: `{result.get('level', '')}`",
        f"**模型**: `{m.get('model', '')}`",
        "",
        "## 核心结论",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| Skill 触发 | {skill} |",
        f"| 任务完成 | {succ} |",
        f"| 命中证据锚点 | {evidence} |",
        f"| 人工介入次数 | {m['human_interventions']} 次 |",
        f"| 用户轮次 | {m['user_turns']} 轮 |",
        "",
        "## Token 消耗",
        "",
        "| 类型 | tokens |",
        "|---|---|",
        f"| Input | {m['input_tokens']:,} |",
        f"| Cache Read | {m['cache_read_tokens']:,} |",
        f"| Cache Write | {m['cache_write_tokens']:,} |",
        f"| Output | {m['output_tokens']:,} |",
        f"| **合计** | **{m['input_tokens'] + m['cache_read_tokens'] + m['cache_write_tokens'] + m['output_tokens']:,}** |",
        "",
        "## 工具调用分布",
        "",
        f"总工具调用 {m['tool_calls_total']} 次：",
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

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="离线分析一条 Claude Code 会话日志")
    parser.add_argument("--jsonl", required=True, help="会话 JSONL 文件路径")
    parser.add_argument("--task", required=True, help="task_id，用于加载成功证据锚点")
    parser.add_argument("--out-dir", default="results", help="结果输出目录")
    args = parser.parse_args()

    jsonl_path = args.jsonl
    if not Path(jsonl_path).exists():
        print(f"错误：JSONL 文件不存在：{jsonl_path}")
        sys.exit(1)

    task = load_task(args.task)
    result = replay_metrics(jsonl_path, task)

    # 输出 JSON
    out_dir = _ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / f"replay_{result['task_id']}.json"
    json_out.write_text(to_json(result), encoding="utf-8")

    # 输出 Markdown
    md_out = out_dir / f"replay_{result['task_id']}.md"
    md_out.write_text(to_markdown(result), encoding="utf-8")

    # 终端打印 Markdown
    print(to_markdown(result))
    print(f"\n结构化 JSON: {json_out}")
    print(f"Markdown 报表: {md_out}")


if __name__ == "__main__":
    main()

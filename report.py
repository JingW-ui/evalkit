#!/usr/bin/env python3
"""
report.py — 聚合评测记录，输出 8 指标报表。

输入：runner 生成的 run_{date}.jsonl（每行一条 record）
输出：终端摘要 + results/report_{date}.md
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict
from collections import defaultdict


def load_records(records_path: str) -> List[Dict]:
    """从 JSONL 加载所有评测记录。"""
    records = []
    with open(records_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def aggregate(records: List[Dict]) -> Dict:
    """
    聚合 8 个指标 + 辅助统计。
    """
    total = len(records)
    if total == 0:
        return {"error": "no records"}

    # --- 正例 vs 负例分组 ---
    # compute_metrics 口径：正例 triggered_when_should=bool（非 None），负例=None。
    pos = [r for r in records if r.get("triggered_when_should") is not None]
    neg = [r for r in records if r.get("triggered_when_should") is None]

    # === 8 指标 ===

    # 1. Skill 触发准确率（正例中成功触发）
    pos_total = len(pos)
    pos_triggered = sum(1 for r in pos if r.get("triggered_correctly"))
    trigger_accuracy = pos_triggered / pos_total if pos_total > 0 else None

    # 2. 误触发率（负例中错误触发）
    neg_total = len(neg)
    neg_false = sum(1 for r in neg if r.get("false_trigger"))
    false_trigger_rate = neg_false / neg_total if neg_total > 0 else None

    # 3. 端到端 SR (pass@1)
    if pos_total > 0:
        success_e2e = sum(1 for r in pos if r.get("success")) / pos_total
    else:
        success_e2e = sum(1 for r in records if r.get("success")) / total if total > 0 else 0.0

    # 4. 按 Skill 隔离 SR
    by_skill = defaultdict(list)
    for r in records:
        sk = r.get("skill_expected") or r.get("skill_loaded") or "unknown"
        by_skill[sk].append(r)
    skill_sr = {}
    for sk, rs in by_skill.items():
        s = sum(1 for r in rs if r.get("success"))
        skill_sr[sk] = round(s / len(rs), 3) if rs else 0.0

    # 5. 工具选择正确率
    tool_acc = [r.get("tool_selection_accuracy", 0) for r in pos]
    avg_tool_acc = sum(tool_acc) / len(tool_acc) if tool_acc else None

    # 6. 参数 F1（近似）
    param_f1s = [r.get("param_f1", 0) for r in pos]
    avg_param_f1 = sum(param_f1s) / len(param_f1s) if param_f1s else None

    # 7. Cost / 成功任务
    total_cost = sum(r.get("cost_usd", 0) for r in records)
    success_count = sum(1 for r in records if r.get("success"))
    cost_per_success = total_cost / success_count if success_count > 0 else None

    # 8. 错误恢复率（正例 + 有 recovery 记录）
    recovery_pos = [r for r in pos if r.get("recovered")]
    recovery_rate = len(recovery_pos) / len(pos) if pos else 0.0

    # --- 辅助统计 ---
    total_input_tokens = sum(r.get("total_input_tokens", 0) for r in records)
    total_output_tokens = sum(r.get("total_output_tokens", 0) for r in records)
    total_cache_read = sum(r.get("total_cache_read_tokens", 0) for r in records)
    avg_turns = sum(r.get("assistant_turns", 0) for r in records) / total if total > 0 else 0
    stuck_count = sum(1 for r in records if r.get("stuck"))

    return {
        "aggregated_at": datetime.now().isoformat(),
        "total_runs": total,
        "positive_runs": pos_total,
        "negative_runs": neg_total,
        "metrics": {
            "trigger_accuracy": round(trigger_accuracy, 4) if trigger_accuracy is not None else None,
            "false_trigger_rate": round(false_trigger_rate, 4) if false_trigger_rate is not None else None,
            "end_to_end_sr": round(success_e2e, 4),
            "skill_isolated_sr": skill_sr,
            "avg_tool_selection_accuracy": round(avg_tool_acc, 4) if avg_tool_acc is not None else None,
            "avg_param_f1": round(avg_param_f1, 4) if avg_param_f1 is not None else None,
            "cost_per_success_usd": round(cost_per_success, 6) if cost_per_success is not None else None,
            "recovery_rate": round(recovery_rate, 4),
        },
        "auxiliary": {
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read,
            "avg_assistant_turns": round(avg_turns, 1),
            "stuck_count": stuck_count,
            "total_success": success_count,
        },
        "per_task": [],
    }


def render_report(agg: Dict, output_path: str):
    """把聚合结果写成 Markdown 报表。"""
    m = agg.get("metrics", {})
    aux = agg.get("auxiliary", {})

    lines = [
        "# Claude Code + Skill 评测报告",
        f"生成时间: {agg.get('aggregated_at', '')}",
        "",
        "## 概览",
        "",
        f"| 项 | 值 |",
        f"|---|---|",
        f"| 总运行次数 | {agg.get('total_runs', 0)} |",
        f"| 正例数 | {agg.get('positive_runs', 0)} |",
        f"| 负例数 | {agg.get('negative_runs', 0)} |",
        f"| 成功数 | {aux.get('total_success', 0)} |",
        f"| 卡死数 | {aux.get('stuck_count', 0)} |",
        "",
        "## 核心 8 指标",
        "",
    ]

    # Skill 层
    ta = m.get("trigger_accuracy")
    ft = m.get("false_trigger_rate")
    lines.append("### 1. Skill 触发准确率 (Trigger Accuracy)")
    lines.append(f"**{f'{ta*100:.1f}%' if ta is not None else 'N/A'}** ({agg.get('positive_runs', 0)} 个正例)")
    lines.append("")
    lines.append("### 2. 误触发率 (False Trigger Rate)")
    lines.append(f"**{f'{ft*100:.1f}%' if ft is not None else 'N/A'}** ({agg.get('negative_runs', 0)} 个负例)")
    lines.append("")

    # 任务结果层
    sr = m.get("end_to_end_sr")
    lines.append("### 3. 端到端成功率 SR (pass@1)")
    lines.append(f"**{f'{sr*100:.1f}%' if sr is not None else 'N/A'}**")
    lines.append("")

    skill_sr = m.get("skill_isolated_sr", {})
    lines.append("### 4. 按 Skill 隔离 SR")
    for sk, val in skill_sr.items():
        lines.append(f"- **{sk}**: {val*100:.1f}%")
    lines.append("")

    # 过程工具层
    ta_val = m.get("avg_tool_selection_accuracy")
    lines.append("### 5. 工具选择正确率")
    lines.append(f"**{f'{ta_val*100:.1f}%' if ta_val is not None else 'N/A'}**")
    lines.append("")

    pf = m.get("avg_param_f1")
    lines.append("### 6. 参数 F1（近似）")
    lines.append(f"**{f'{pf*100:.1f}%' if pf is not None else 'N/A'}**")
    lines.append("")

    # 经济层
    cp = m.get("cost_per_success_usd")
    lines.append("### 7. Cost / 成功任务")
    lines.append(f"**\${cp:.4f}**" if cp is not None else "**N/A**")
    lines.append(f"- 总费用: \${aux.get('total_cost_usd', 0):.4f}")
    lines.append(f"- 总 input tokens: {aux.get('total_input_tokens', 0):,}")
    lines.append(f"- 总 output tokens: {aux.get('total_output_tokens', 0):,}")
    lines.append(f"- 总 cache_read tokens: {aux.get('total_cache_read_tokens', 0):,}")
    lines.append(f"- 平均 assistant 轮次: {aux.get('avg_assistant_turns', 0)}")
    lines.append("")

    # 鲁棒层
    rr = m.get("recovery_rate")
    lines.append("### 8. 错误恢复率")
    lines.append(f"**{f'{rr*100:.1f}%' if rr is not None else 'N/A'}**")
    lines.append("")

    content = "\n".join(lines)
    Path(output_path).write_text(content, encoding="utf-8")
    return content


def print_summary(agg: Dict):
    """终端摘要输出。"""
    m = agg.get("metrics", {})
    aux = agg.get("auxiliary", {})

    print("\n" + "=" * 60)
    print("           Claude Code + Skill 评测汇总")
    print("=" * 60)
    print(f"  总运行: {agg.get('total_runs', 0)}  正例: {agg.get('positive_runs', 0)}  负例: {agg.get('negative_runs', 0)}")
    print(f"  成功数: {aux.get('total_success', 0)}  卡死: {aux.get('stuck_count', 0)}")
    print()
    print("  --- Skill 层 ---")
    ta = m.get("trigger_accuracy")
    print(f"  Skill 触发准确率 : {ta*100:.1f}%" if ta is not None else "  Skill 触发准确率 : N/A")
    ft = m.get("false_trigger_rate")
    print(f"  误触发率         : {ft*100:.1f}%" if ft is not None else "  误触发率         : N/A")
    print()
    print("  --- 任务结果层 ---")
    sr = m.get("end_to_end_sr")
    print(f"  端到端 SR        : {sr*100:.1f}%" if sr is not None else "  端到端 SR        : N/A")
    skill_sr = m.get("skill_isolated_sr", {})
    for sk, val in skill_sr.items():
        print(f"    {sk}: {val*100:.1f}%")
    print()
    print("  --- 过程工具层 ---")
    ta_val = m.get("avg_tool_selection_accuracy")
    print(f"  工具选择正确率    : {ta_val*100:.1f}%" if ta_val is not None else "  工具选择正确率    : N/A")
    pf = m.get("avg_param_f1")
    print(f"  参数 F1 (近似)    : {pf*100:.1f}%" if pf is not None else "  参数 F1          : N/A")
    print()
    print("  --- 经济层 ---")
    cp = m.get("cost_per_success_usd")
    print(f"  Cost/成功任务     : ${cp:.4f}" if cp is not None else "  Cost/成功任务     : N/A")
    print(f"  总费用            : ${aux.get('total_cost_usd', 0):.4f}")
    print(f"  总 tokens (in+out): {aux.get('total_input_tokens', 0) + aux.get('total_output_tokens', 0):,}")
    print()
    print("  --- 鲁棒层 ---")
    rr = m.get("recovery_rate")
    print(f"  错误恢复率        : {rr*100:.1f}%" if rr is not None else "  错误恢复率        : N/A")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python report.py <run_xxx.jsonl>")
        sys.exit(1)

    recs = load_records(sys.argv[1])
    agg = aggregate(recs)
    if "error" in agg:
        print(f"Error: {agg['error']}")
        sys.exit(1)

    print_summary(agg)

    out_path = sys.argv[1].replace(".jsonl", "_report.md")
    render = render_report(agg, out_path)
    print(f"\n报表已写入: {out_path}")

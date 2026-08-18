#!/usr/bin/env python3
"""
run_replay_batch.py — 批量离线分析多个历史会话，合并成对比表。

用法：
    方式1（指定文件列表）：
        python run_replay_batch.py --jsonl <path1> --jsonl <path2> ... --task g66_deploy_001

    方式2（自动扫描所有 G66 session）：
        python run_replay_batch.py --scan --skill G66 --task g66_deploy_001

产出：
    results/batch_<task_id>_<时间>.json  结构化全部结果
    results/batch_<task_id>_<时间>.md    横向对比表
"""

import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from parser import replay_metrics, parse_session_jsonl


def find_sessions_by_skill(skill_name: str, proj_root: str) -> list:
    """扫描 projects 目录，找出所有触发过指定 skill 的 session JSONL。"""
    sessions = []
    proj_root = os.path.expandvars(proj_root)

    for root, _, files in os.walk(proj_root):
        # 排除 subagents 子目录（那是子 agent 日志，不是顶层 session）
        if "subagents" in root.split(os.sep):
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if obj.get("type") == "assistant":
                            for block in obj.get("message", {}).get("content", []):
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    if block.get("name") == "Skill":
                                        if block.get("input", {}).get("skill") == skill_name:
                                            st = os.stat(p)
                                            sessions.append((p, st.st_mtime))
                                            break
                            else:
                                continue
                            break
            except Exception:
                continue

    # 按时间倒序
    sessions.sort(key=lambda x: -x[1])
    return [p for p, _ in sessions]


def first_user_input(jsonl_path: str) -> str:
    """提取首个真实用户输入（排除本地命令）。"""
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "user":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str) and content and not content.startswith("/"):
                    # 排除 local-command-caveat 前缀
                    if "local-command-caveat" in content:
                        continue
                    return content[:200]
    return "(无用户输入)"


def classify_session(parsed_tools: dict) -> str:
    """根据工具分布给 session 打性质标签。"""
    has_occupy = "mcp__airgattai__occupy_device" in parsed_tools
    has_push = "mcp__airgattai__push_file" in parsed_tools
    has_shell = "mcp__airgattai__shell" in parsed_tools

    if has_occupy and (has_push or has_shell):
        return "部署跑测"
    elif has_push or has_shell:
        return "设备操作"
    else:
        return "开发/调试"


def main():
    parser = argparse.ArgumentParser(description="批量离线分析历史会话")
    parser.add_argument("--jsonl", action="append", default=[], help="会话 JSONL 路径（可多传）")
    parser.add_argument("--scan", action="store_true", help="扫描 projects 目录找所有触发过该 skill 的 session")
    parser.add_argument("--skill", default="G66", help="扫描时匹配的 skill 名")
    parser.add_argument("--task", default="g66_deploy_001", help="task_id，用于成功证据锚点")
    parser.add_argument("--out-dir", default="results", help="结果输出目录")
    parser.add_argument("--limit", type=int, default=0, help="最多处理前 N 个（0=全部）")
    args = parser.parse_args()

    # 加载 task
    base = _ROOT
    task = None
    for tf in (base / "tasks").glob("*.json"):
        t = json.loads(tf.read_text(encoding="utf-8"))
        if t.get("task_id") == args.task:
            task = t
            break
    if task is None:
        task = {"task_id": args.task}

    # 收集 session 列表
    sessions = list(args.jsonl)
    if args.scan:
        proj_root = str(Path(os.environ.get("USERPROFILE", "")) / ".claude" / "projects")
        found = find_sessions_by_skill(args.skill, proj_root)
        print(f"扫描到 {len(found)} 个触发过 {args.skill} 的 session")
        sessions = found + sessions

    # 去重
    sessions = list(dict.fromkeys(sessions))

    if args.limit > 0:
        sessions = sessions[: args.limit]

    if not sessions:
        print("没有 session 要处理")
        return

    print(f"开始批量分析 {len(sessions)} 个 session...")

    # 逐个分析
    results = []
    for i, jsonl_path in enumerate(sessions, 1):
        if not Path(jsonl_path).exists():
            print(f"  [{i}/{len(sessions)}] 跳过(不存在): {jsonl_path}")
            continue
        print(f"  [{i}/{len(sessions)}] {os.path.basename(jsonl_path)}")

        try:
            r = replay_metrics(jsonl_path, task)
            # 附加：首个输入 + 性质标签
            parsed = parse_session_jsonl(jsonl_path)
            r["first_user_input"] = first_user_input(jsonl_path)
            r["session_label"] = classify_session(
                Counter(t["name"] for t in parsed["tool_sequence"])
            )
            r["session_mtime"] = datetime.fromtimestamp(
                os.stat(jsonl_path).st_mtime
            ).strftime("%Y-%m-%d %H:%M")
            results.append(r)
        except Exception as e:
            print(f"    错误: {e}")
            results.append({
                "task_id": task.get("task_id"),
                "session_id": Path(jsonl_path).stem,
                "jsonl_path": jsonl_path,
                "parse_error": str(e),
                "metrics": {},
            })

    # 写 JSON
    out_dir = base / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_out = out_dir / f"batch_{task['task_id']}_{ts}.json"
    json_out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 生成对比表 Markdown
    md_out = out_dir / f"batch_{task['task_id']}_{ts}.md"
    md_lines = [
        f"# G66 批量离线评测对比表",
        "",
        f"共 {len(results)} 个 session · 生成时间 {datetime.now().isoformat()}",
        "",
        "## 横向对比",
        "",
        "| 时间 | Session | 首个输入 | 性质 | Skill触发 | 任务成功 | 命中锚点 | 工具数 | 人工介入 | Token合计 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for r in results:
        m = r.get("metrics", {})
        if not m:
            md_lines.append(f"| {r.get('session_mtime','')} | {r.get('session_id','')} | (解析失败) | - | - | - | - | - | - | - |")
            continue
        skill = "✅" if m.get("skill_triggered") else "❌"
        succ = "✅" if m.get("task_success") else "❌"
        evidence = "、".join(m.get("evidence_hit", [])) or "-"
        total_tok = (
            m.get("input_tokens", 0)
            + m.get("cache_read_tokens", 0)
            + m.get("cache_write_tokens", 0)
            + m.get("output_tokens", 0)
        )
        first = r.get("first_user_input", "")[:40].replace("|", "\\|")
        md_lines.append(
            f"| {r.get('session_mtime','')} | {r.get('session_id','')[:8]} | {first} | {r.get('session_label','')} | {skill} | {succ} | {evidence} | {m.get('tool_calls_total',0)} | {m.get('human_interventions',0)} | {total_tok:,} |"
        )

    md_out.write_text("\n".join(md_lines), encoding="utf-8")

    # 终端打印对比表
    print("\n" + "\n".join(md_lines))
    print(f"\n结构化 JSON: {json_out}")
    print(f"对比表 Markdown: {md_out}")


if __name__ == "__main__":
    main()

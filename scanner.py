#!/usr/bin/env python3
"""
scanner.py — 跨会话画像 + 轮次级雪球归因 + 异常信号。

对齐网易「用户 Session 优化报告」的聚合视角：
  - 跨会话：总 token、TopN 消耗、按 skill 分组
  - 轮次级：逐轮 token 增量，定位「雪球点」（某轮暴涨）
  - 异常信号：snowball(≥5x基准) / context_heavy(单轮超阈值) / skill_load_spike(加载skill那轮峰)

数据来源：Claude Code 会话 JSONL（逐条 assistant 消息的 usage）。
不依赖实时采集，纯离线扫描。

用法：
    python scanner.py --dir <projects目录> [--skill G66] [--top 10] [--json]
"""

import json
import os
import sys
import argparse
from pathlib import Path


def scan_session(jsonl_path: str) -> dict:
    """
    扫描一个 session，返回：
      - session_id, token 总量, 逐轮 trace（含增量、是否雪球、该轮 prompt、timestamp）
      - 异常信号列表
      - 时间线（首条 user → 末条事件）
      - task_list（TaskCreate/TaskUpdate 追踪）
    """
    path = Path(jsonl_path)
    if not path.exists():
        return None

    session_id = path.stem

    # 逐轮累计 token（input + cache_read）
    traces = []
    prev_total = 0
    total_tokens = 0
    total_input = 0
    total_cache_read = 0
    total_output = 0
    model = ""
    skill_loaded = None
    user_turns = []
    pending_prompt = None  # 当前待关联到下一轮 assistant 的 user prompt

    # 时间线
    first_event_ts = None
    last_event_ts = None

    # Task List 追踪
    task_list = []  # [{id, subject, description, status, created_at, updated_at, duration_s}]
    task_map = {}   # id → task

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type", "")
            ts_str = obj.get("timestamp")

            # 时间线
            if ts_str:
                if not first_event_ts:
                    first_event_ts = ts_str
                last_event_ts = ts_str

            # 记录 user prompt（用于定位雪球点）
            if t == "user":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str) and content and not content.startswith("/"):
                    user_turns.append(content[:500])
                    pending_prompt = content[:500]

            if t == "assistant":
                msg = obj.get("message", {})
                usage = msg.get("usage", {})
                model = model or msg.get("model", "")

                input_t = usage.get("input_tokens", 0)
                cache_t = usage.get("cache_read_input_tokens", 0)
                output_t = usage.get("output_tokens", 0)

                total_input += input_t
                total_cache_read += cache_t
                total_output += output_t

                round_tokens = input_t + cache_t
                total_tokens += round_tokens

                # 检测 Skill 加载 + TaskCreate/TaskUpdate
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name == "Skill" and not skill_loaded:
                            skill_loaded = inp.get("skill", "")
                        elif name == "TaskCreate":
                            task_id = inp.get("taskId", inp.get("id", ""))
                            task = {
                                "id": task_id,
                                "subject": inp.get("subject", ""),
                                "description": inp.get("description", ""),
                                "status": "pending",
                                "created_at": ts_str,
                                "updated_at": ts_str,
                                "completed_at": None,
                            }
                            task_list.append(task)
                            task_map[task_id] = task
                        elif name == "TaskUpdate":
                            task_id = inp.get("taskId", inp.get("id", ""))
                            status = inp.get("status", "")
                            if task_id in task_map:
                                task = task_map[task_id]
                                task["status"] = status
                                task["updated_at"] = ts_str
                                if status == "completed":
                                    task["completed_at"] = ts_str

                traces.append({
                    "round": len(traces) + 1,
                    "round_tokens": round_tokens,
                    "model": model,
                    "prompt": pending_prompt,
                    "timestamp": ts_str,
                })
                pending_prompt = None  # 清空，避免重复关联

    # 计算总耗时
    duration_s = 0
    if first_event_ts and last_event_ts:
        try:
            from datetime import datetime
            t1 = datetime.fromisoformat(first_event_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_event_ts.replace("Z", "+00:00"))
            duration_s = (t2 - t1).total_seconds()
        except Exception:
            duration_s = 0

    # 计算每个 task 的耗时
    for task in task_list:
        task_duration = 0
        if task.get("created_at") and task.get("completed_at"):
            try:
                from datetime import datetime
                t1 = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(task["completed_at"].replace("Z", "+00:00"))
                task_duration = (t2 - t1).total_seconds()
            except Exception:
                task_duration = 0
        task["duration_s"] = task_duration

    # 计算增量 + 异常信号
    increments = []
    snowball_count = 0
    max_single_round = 0
    max_single_round_idx = 0

    for i, tr in enumerate(traces):
        if i == 0:
            inc = tr["round_tokens"]
        else:
            inc = tr["round_tokens"] - traces[i - 1]["round_tokens"]
        inc = max(0, inc)
        tr["increment"] = inc
        increments.append(inc)
        if tr["round_tokens"] > max_single_round:
            max_single_round = tr["round_tokens"]
            max_single_round_idx = i

    # 基准 = 前几轮（去掉首轮）的均值
    if len(increments) > 2:
        baseline = sum(increments[1:]) / (len(increments) - 1) if len(increments) > 1 else 1
    else:
        baseline = 1

    anomalies = []
    for i, tr in enumerate(traces):
        inc = tr["increment"]
        # 雪球：增量 ≥5x 基准 且 增量绝对值足够大
        if baseline > 0 and inc >= 5 * baseline and inc > 100000:
            tr["is_snowball"] = True
            snowball_count += 1
            anomalies.append({
                "type": "snowball",
                "round": tr["round"],
                "increment": inc,
                "baseline": round(baseline),
            })
        else:
            tr["is_snowball"] = False

        # 上下文过重：单轮 token 超 100 万
        if tr["round_tokens"] > 1_000_000:
            anomalies.append({
                "type": "context_heavy",
                "round": tr["round"],
                "round_tokens": tr["round_tokens"],
            })

    # skill 加载峰：若加载了 skill，看加载前后 token 是否飙升
    skill_load_spike = None
    if skill_loaded and traces:
        # 找 Skill 工具调用那轮（简化：取首轮附近）
        for i, tr in enumerate(traces):
            if tr["round_tokens"] > 1_500_000:
                skill_load_spike = tr["round_tokens"]
                anomalies.append({
                    "type": "skill_load_spike",
                    "round": tr["round"],
                    "round_tokens": tr["round_tokens"],
                    "skill": skill_loaded,
                })
                break

    return {
        "session_id": session_id,
        "jsonl_path": str(path),
        "total_tokens": total_tokens,
        "total_input": total_input,
        "total_cache_read": total_cache_read,
        "total_output": total_output,
        "model": model,
        "skill_loaded": skill_loaded,
        "trace_count": len(traces),
        "max_single_round": max_single_round,
        "max_single_round_idx": max_single_round_idx,
        "snowball_count": snowball_count,
        "anomalies": anomalies,
        "duration_s": duration_s,
        "task_list": task_list,
        "traces": traces,        # 完整 traces（含 prompt 和 timestamp），供四级下钻
        "user_turns": user_turns,
    }


def scan_directory(proj_dir: str, skill_filter: str = None, top: int = 10) -> dict:
    """
    扫描 projects 目录下所有 session JSONL，聚合。

    Returns:
        {
          "sessions": [scan_session(...) ...],  # 按 token 降序
          "summary": { total, avg, session_count, ... }
        }
    """
    proj_dir = os.path.expandvars(proj_dir)
    sessions = []

    for root, dirs, files in os.walk(proj_dir):
        if "subagents" in root.split(os.sep):
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(root, f)
            s = scan_session(p)
            if s is None:
                continue
            if s["total_tokens"] < 1000:  # 忽略几乎空的 session
                continue
            sessions.append(s)

    # 按 token 降序
    sessions.sort(key=lambda x: -x["total_tokens"])

    # 分组统计
    by_skill = {}
    for s in sessions:
        sk = s["skill_loaded"] or "（无skill/纯agent）"
        by_skill.setdefault(sk, {"count": 0, "total_tokens": 0})
        by_skill[sk]["count"] += 1
        by_skill[sk]["total_tokens"] += s["total_tokens"]

    total = sum(s["total_tokens"] for s in sessions)

    summary = {
        "session_count": len(sessions),
        "total_tokens": total,
        "avg_tokens": total // len(sessions) if sessions else 0,
        "snowball_sessions": sum(1 for s in sessions if s["snowball_count"] > 0),
        "max_session_tokens": sessions[0]["total_tokens"] if sessions else 0,
        "by_skill": [
            {"skill": k, "count": v["count"], "total_tokens": v["total_tokens"]}
            for k, v in sorted(by_skill.items(), key=lambda x: -x[1]["total_tokens"])
        ],
    }

    return {
        "sessions": sessions[:top],
        "summary": summary,
    }


def scan_all(proj_dir: str, skill_filter: str = None) -> dict:
    """
    全量扫描（不截断 TopN），供 report_interactive 等需要完整数据的场景使用。

    返回结构与 scan_directory 相同，但 sessions 是全集，且每个 session 的
    traces 完整（含 prompt）。数据可能较大，仅内部使用。
    """
    proj_dir = os.path.expandvars(proj_dir)
    sessions = []

    for root, dirs, files in os.walk(proj_dir):
        if "subagents" in root.split(os.sep):
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(root, f)
            s = scan_session(p)
            if s is None:
                continue
            if s["total_tokens"] < 1000:
                continue
            if skill_filter and s["skill_loaded"] != skill_filter:
                continue
            sessions.append(s)

    sessions.sort(key=lambda x: -x["total_tokens"])

    by_skill = {}
    for s in sessions:
        sk = s["skill_loaded"] or "（无skill/纯agent）"
        by_skill.setdefault(sk, {"count": 0, "total_tokens": 0})
        by_skill[sk]["count"] += 1
        by_skill[sk]["total_tokens"] += s["total_tokens"]

    total = sum(s["total_tokens"] for s in sessions)

    summary = {
        "session_count": len(sessions),
        "total_tokens": total,
        "avg_tokens": total // len(sessions) if sessions else 0,
        "snowball_sessions": sum(1 for s in sessions if s["snowball_count"] > 0),
        "max_session_tokens": sessions[0]["total_tokens"] if sessions else 0,
        "by_skill": [
            {"skill": k, "count": v["count"], "total_tokens": v["total_tokens"]}
            for k, v in sorted(by_skill.items(), key=lambda x: -x[1]["total_tokens"])
        ],
    }

    return {
        "sessions": sessions,
        "summary": summary,
    }


def to_markdown(scan: dict) -> str:
    s = scan["summary"]
    lines = [
        "# 跨会话画像报告",
        "",
        f"共 **{s['session_count']}** 个会话，总消耗 **{s['total_tokens']:,}** token，平均 {s['avg_tokens']:,}",
        f"雪球会话 **{s['snowball_sessions']}** 个，最高单会话 {s['max_session_tokens']:,} token",
        "",
        "## 按 skill 分组",
        "",
        "| Skill | 会话数 | 总 Token |",
        "|---|---|---|",
    ]
    for b in s["by_skill"]:
        lines.append(f"| {b['skill']} | {b['count']} | {b['total_tokens']:,} |")

    lines += [
        "",
        "## Top 会话",
        "",
        "| Session | Token | 模型 | Skill | Trace数 | 雪球数 |",
        "|---|---|---|---|---|---|",
    ]
    for sess in scan["sessions"]:
        lines.append(
            f"| {sess['session_id'][:8]} | {sess['total_tokens']:,} | {sess['model']} | {sess['skill_loaded'] or '（纯agent）'} | {sess['trace_count']} | {sess['snowball_count']} |"
        )

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="跨会话画像扫描")
    ap.add_argument("--dir", required=True, help="projects 目录")
    ap.add_argument("--skill", default=None, help="只看某 skill，不加则全量")
    ap.add_argument("--top", type=int, default=10, help="Top N 会话数")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    scan = scan_directory(args.dir, args.skill, args.top)

    if args.json:
        print(json.dumps(scan, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(scan))

    # 落盘
    out_dir = Path(__file__).parent / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / "scan_result.json"
    json_out.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "scan_report.md").write_text(to_markdown(scan), encoding="utf-8")
    print(f"\n结果: {json_out}")


if __name__ == "__main__":
    main()

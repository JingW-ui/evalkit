#!/usr/bin/env python3
"""
orchestrator.py —— evalkit 编排路由：脚本打底 + LLM 首尾判定 + YAML 规则自增殖。

解决「纯脚本 keyword 规则列举不完」的问题，架构：

  1. 脚本确定性解析（session_report.scan_*）出结构化事实
  2. 生成 probe（日志首尾摘要），交 LLM 判「完成态 / 异常 / 任务本质」
  3. 对照脚本结论：
     - 吻合 → 信任脚本，跳过全量读取（省 token）
     - 矛盾 / 脚本 anomalies 非空 / 未覆盖 → 全量读取 + 深度分析
  4. 全量分析后把新特征/信号蒸馏进 rules.yaml
  5. 渲染报告（复用 session_report.render_html）

用法（三个子命令）：

  python orchestrator.py probe --jsonl <日志>           # 只跑脚本 + 打印 probe，交 agent 判定
  python orchestrator.py analyze --jsonl <日志>         # 全流程（含脚本事实 + 结论输出）
  python orchestrator.py render --jsonl <日志> --out x  # 无条件渲染 HTML

LLM 判定这一环由调用方（Claude Code agent）完成：脚本负责产出「待判定的事
实 + probe」，agent 读完后给出完成态/异常/是否需全量的结论，脚本据此对照。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_report as sr
import yaml as _yaml  # 若无 PyYAML，退化为简单文本读写


# ===== 规则文件读写 =====

def load_rules(rules_path: Path) -> dict:
    try:
        import yaml as pyyaml
        with open(rules_path, "r", encoding="utf-8") as f:
            return pyyaml.safe_load(f) or {}
    except ImportError:
        return {}  # 无 PyYAML 时不做规则读写，仅做路由/渲染


def dump_rules(rules: dict, rules_path: Path):
    try:
        import yaml as pyyaml
        with open(rules_path, "w", encoding="utf-8") as f:
            pyyaml.safe_dump(rules, f, allow_unicode=True, sort_keys=False)
    except ImportError:
        pass


# ===== probe 生成 =====

def make_probe(path: Path, head_n: int = 40, tail_n: int = 40) -> dict:
    """截取日志首尾，供 LLM 快速判「完成态/异常/任务本质」。"""
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = txt.splitlines()
    head = lines[:head_n]
    tail = lines[-tail_n:] if len(lines) > head_n else []
    # 全量里扫「异常信号词」，附给 agent（快速定位）
    anomaly_hits = []
    for kw in ("Autocompact is thrashing", "Traceback", "Exception", "提前结束", "CCAgent.run done", "CC RESULT"):
        if kw in txt:
            anomaly_hits.append(kw)
    return {
        "total_lines": len(lines),
        "head": "\n".join(head),
        "tail": "\n".join(tail),
        "signal_hits": anomaly_hits,
    }


# ===== 核心流程 =====

def detect_and_scan(path: Path):
    """自动检测格式并调用对应 scan，返回 (data, kind)。kind ∈ {dsh, airlab, jsonl}"""
    kind = sr.detect_log_kind(str(path))
    if kind == "dsh":
        return sr.scan_dsh_log(str(path)), "dsh"
    if kind == "airlab":
        return sr.scan_airlab_log(str(path)), "airlab"
    return sr.scan_single_session(str(path)), "jsonl"


def script_verdict(data: dict) -> dict:
    """脚本当前的判定结论（供 agent 对照）。"""
    verdict = {"tasks": []}
    for t in data.get("tasks", []):
        verdict["tasks"].append({
            "query": t.get("query", ""),
            "completion": t.get("completion", ""),
            "completion_reason": t.get("completion_reason", ""),
            "anomalies": t.get("anomalies", []),
        })
    verdict.update({
        "task_count": data.get("task_count"),
        "completed_sub": data.get("completed_sub"),
        "total_sub": data.get("total_sub"),
        "total_user_interrupts": data.get("total_user_interrupts"),
        "total_human_interventions": data.get("total_human_interventions"),
    })
    return verdict


def run_probe(path_str: str, head_n: int, tail_n: int):
    path = Path(path_str)
    data, kind = detect_and_scan(path)
    probe = make_probe(path, head_n, tail_n)
    payload = {
        "session_id": data.get("session_id"),
        "kind": kind,
        "script_verdict": script_verdict(data),
        "probe": probe,
    }
    # 用 stdout 打出 JSON（避免 GBK 终端问题）
    sys.stdout.buffer.write(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return payload


def run_render(path_str: str, out: str):
    path = Path(path_str)
    data, _ = detect_and_scan(path)
    out_path = Path(out) if out else Path(__file__).parent / "results" / "orchestrator_report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sr.render_html(data), encoding="utf-8")
    print(f"报告已生成: {out_path}")


def main():
    ap = argparse.ArgumentParser(description="evalkit 编排路由（脚本打底 + LLM 首尾判定）")
    sub = ap.add_subparsers(dest="cmd")

    p_probe = sub.add_parser("probe", help="跑脚本 + 打印 probe，交 agent 判定")
    p_probe.add_argument("--jsonl", required=True)
    p_probe.add_argument("--head", type=int, default=40)
    p_probe.add_argument("--tail", type=int, default=40)

    p_render = sub.add_parser("render", help="无条件渲染 HTML")
    p_render.add_argument("--jsonl", required=True)
    p_render.add_argument("--out", default=None)

    p_analyze = sub.add_parser("analyze", help="全流程（脚本事实 + probe + 结论）")
    p_analyze.add_argument("--jsonl", required=True)
    p_analyze.add_argument("--out", default=None)

    args = ap.parse_args()
    if args.cmd == "probe":
        run_probe(args.jsonl, args.head, args.tail)
    elif args.cmd == "render":
        run_render(args.jsonl, args.out)
    elif args.cmd == "analyze":
        payload = run_probe(args.jsonl, 40, 40)
        # analyze = probe + 打印脚本结论摘要（真正的 LLM 判定由 agent 在对话里做）
        sv = payload["script_verdict"]
        print(f"\n[脚本结论] 任务数 {sv['task_count']} | 子任务 {sv['completed_sub']}/{sv['total_sub']}")
        for i, t in enumerate(sv["tasks"]):
            print(f"  任务#{i+1} {t['completion']}: {t['query'][:40]}")
        print("\n请基于 probe 与脚本结论对照：吻合→跳过全量；矛盾/anomalies→全量分析并蒸馏规则")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
adjudicator.py —— 语义裁决器：由 rules.yaml 驱动「完成判定」与「异常信号识别」。

解耦目标：session_report.py 只做「确定性解析」（任务切分 / token / 子任务三段时间 /
skill 双口径 / 中断计数），而「这条日志算完成还是中断、有哪些异常信号」这类
keyword 判断易错、易随新 case 无限膨胀，统一收口到这里，由 rules.yaml 驱动。

规则源：直接读 rules.yaml（PyYAML）。若无 PyYAML 或 rules.yaml 缺失，回退到内置
默认（BUILTIN_RULES），保证 adjudicatable 恒可用、行为可预期。

导出的核心函数：
  load_rules()                 -> dict   读 rules.yaml（回退内置）
  adjudicate_completion(txt, task_subitems, kind) -> (verdict, reason)
  detect_anomalies(txt)        -> list[str]
  rule_hits(txt)               -> dict   哪些规则 signal 命中了（供渲染标注）

kind: "airlab" | "jsonl"。airLab 有文本日志 txt 可匹配关键字；JSONL 走 stop_reason
启发式（已在 scan_single_session 里做完，这里仅保留一致接口以统一三态）。
"""

from pathlib import Path

try:
    import yaml as _yaml
except ImportError:
    _yaml = None


# ===== 内置默认（rules.yaml 缺失或 PyYAML 不存在时回退） =====
BUILTIN_RULES = {
    "completion_signals": {
        "done_markers": [
            "CCAgent.run done", "CC RESULT", "任务已完成",
            "写入 DK 备注", "DK 备注写入", "写 DK 成功", "改验证码", "验证码",
        ],
        "crash_markers": ["Traceback"],
        "not_crash_notes": [
            "-ErrorAction Silently", "错误：没有找到进程", "Exception: ... success",
        ],
    },
    "anomaly_signals": [
        {"signal": "Autocompact is thrashing", "meaning": "context 反复 refill，可能有超大 tool output"},
        {"signal": "CC 提前结束", "meaning": "framework 提前终止，可能伴随 Exception 但成果未必未落地"},
        {"signal": "Exception", "meaning": "出现 Exception 字样，需结合结尾判断是崩溃还是提前结束"},
        {"signal": "Traceback", "meaning": "真正的崩溃堆栈"},
        {"signal": "compact_boundary / session continued", "meaning": "context 紧凑续，可能丢子任务状态标记"},
    ],
    "conflict_scenarios": [
        {"pattern": "completed + 子任务 0/全部 pending", "explanation": "成果落地但子任务无状态更新，多为 context 重续丢失标记"},
        {"pattern": "完成但有多条 compact_boundary", "explanation": "执行过程不健康，即使结果成功也应在报告标注" },
    ],
}


def _rules_path():
    return Path(__file__).parent / "rules.yaml"


def load_rules() -> dict:
    """读 rules.yaml；失败则回退内置默认。"""
    if _yaml is None:
        return BUILTIN_RULES
    try:
        with open(_rules_path(), "r", encoding="utf-8") as f:
            r = _yaml.safe_load(f)
        return r if isinstance(r, dict) and r else BUILTIN_RULES
    except Exception:
        return BUILTIN_RULES


def _done_markers(rules: dict) -> list:
    sig = rules.get("completion_signals", {})
    return list(sig.get("done_markers", []))


def _crash_markers(rules: dict) -> list:
    sig = rules.get("completion_signals", {})
    return list(sig.get("crash_markers", []))


def _not_crash_notes(rules: dict) -> list:
    sig = rules.get("completion_signals", {})
    return list(sig.get("not_crash_notes", []))


def detect_anomalies(txt: str, rules: dict = None) -> list:
    """检测异常信号（按 rules.yaml 的 anomaly_signals），返回命中的 signal 文本列表。

    txt 为 None 时返回空。注意：这里只做「信号识别」，不判失败——失败与否由
    adjudicate_completion 结合 crash_markers 决定。
    """
    if not txt:
        return []
    rules = rules or load_rules()
    hits = []
    for a in rules.get("anomaly_signals", []):
        sig = a.get("signal", "")
        # signal 可能是「A / B」多选一（任一命中）
        parts = [p.strip() for p in sig.split("/")]
        if any(p and p in txt for p in parts):
            hits.append(f"{sig}：{a.get('meaning', '')}")
    return hits


def adjudicate_completion(txt: str, task_subitems: list = None, kind: str = "airlab",
                          rules: dict = None) -> tuple:
    """
    数据驱动的完成判定，返回 (verdict, reason)。
    verdict ∈ {"completed", "completed_with_anomaly", "interrupted"}

    - completed：有 done 标记 + 无 crash 堆栈
    - completed_with_anomaly：完成但有异常信号（如 cc 提前结束 / thrashing）
    - interrupted：无 done 标记、或命中 crash 堆栈

    子任务兜底：若已完成全部子任务，即使缺 done 标记也归 completed。
    """
    rules = rules or load_rules()
    done_markers = _done_markers(rules)
    crash_markers = _crash_markers(rules)
    txt = txt or ""

    done_flag = any(k in txt for k in done_markers)
    crash_flag = any(k in txt for k in crash_markers)
    anomalies = detect_anomalies(txt, rules)

    # 子任务兜底
    subtask_done = False
    if task_subitems:
        subtask_done = all(s.get("status") == "completed" for s in task_subitems)

    if crash_flag and not done_flag:
        return "interrupted", "命中崩溃堆栈且无正常收尾"
    if done_flag or subtask_done:
        if anomalies:
            return "completed_with_anomaly", "完成但存在异常信号"
        return "completed", "正常收尾且成果落地"
    return "interrupted", "未见正常收尾或成果落地信号"


def rule_hits(txt: str, data: dict = None, rules: dict = None) -> dict:
    """
    汇总本次日志命中的规则信号，供 HTML 渲染「规则命中」区块。
    返回 {anomalies: [...], conflicts: [...], done_markers_matched: [...]}
    """
    rules = rules or load_rules()
    txt = txt or ""
    result = {
        "anomalies": detect_anomalies(txt, rules),
        "conflicts": [],
        "done_markers_matched": [k for k in _done_markers(rules) if k in txt],
    }
    # 矛盾场景：completed 但子任务全 pending
    if data:
        task_subitems = data.get("task_subitems", [])
        completed_sub = sum(1 for s in task_subitems if s.get("status") == "completed")
        total_sub = len(task_subitems)
        completions = [t.get("completion", "") for t in (data.get("tasks") or [])]
        if total_sub > 0 and completed_sub == 0 and any(
            c.startswith("completed") for c in completions
        ):
            result["conflicts"].append(
                "completed 但子任务 0/全部 pending —— 疑似 context 重续丢失状态标记（见 conflict_scenarios）"
            )
    return result

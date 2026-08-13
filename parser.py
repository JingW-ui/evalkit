#!/usr/bin/env python3
"""
parser.py — 解析 Claude Code 会话 JSONL，提取评测所需的字段。

基于真实 JSONL 结构（已调研确认）：
  - type: "assistant" → message.content[] 含 tool_use blocks
  - Skill 触发 = tool_use.name=="Skill", input={"skill":"xxx"}
  - type: "attachment"  subtype: "skill_listing" → names[] + content(触发词描述)
  - assistant.message.usage → input_tokens / cache_read_input_tokens / output_tokens
  - assistant.message.stop_reason → "end_turn" | "tool_use"
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any


def parse_session_jsonl(jsonl_path: str) -> Dict[str, Any]:
    """
    解析一个 session 的 JSONL 文件，返回结构化中间数据。

    Returns:
        {
            "skill_loaded": str | None,         # 加载的 skill 名（若有）
            "skill_loading_turn": int | None,    # 第几轮 assistant 出现 Skill 工具调用
            "tool_sequence": List[Dict],          # 全部工具调用序列 (按时间)
            "total_input_tokens": int,
            "total_cache_read_tokens": int,
            "total_cache_write_tokens": int,
            "total_output_tokens": int,
            "total_assistant_turns": int,
            "end_turn_count": int,                # stop_reason=="end_turn" 次数
            "tool_use_turn_count": int,           # stop_reason=="tool_use" 次数
            "model": str,                         # 使用的模型名
            "stop_reasons": List[str],            # 每轮 stop_reason
            "skill_listing_names": List[str],     # 首轮 skill_listing 中的 names
            "line_count": int,
        }
    """
    result = {
        "skill_loaded": None,
        "skill_loading_turn": None,
        "tool_sequence": [],
        "total_input_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_output_tokens": 0,
        "total_assistant_turns": 0,
        "end_turn_count": 0,
        "tool_use_turn_count": 0,
        "model": "",
        "stop_reasons": [],
        "skill_listing_names": [],
        "line_count": 0,
    }

    path = Path(jsonl_path)
    assistant_turn_idx = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            result["line_count"] += 1
            event_type = obj.get("type", "")

            # --- attachment: skill_listing ---
            if event_type == "attachment":
                att = obj.get("attachment", {})
                if att.get("type") == "skill_listing":
                    result["skill_listing_names"] = att.get("names", [])

            # --- assistant: tool_use + usage ---
            if event_type == "assistant":
                assistant_turn_idx += 1
                result["total_assistant_turns"] += 1
                msg = obj.get("message", {})

                # Model
                model = msg.get("model", "")
                if model and not result["model"]:
                    result["model"] = model

                # Stop reason
                sr = msg.get("stop_reason", "")
                result["stop_reasons"].append(sr)
                if sr == "end_turn":
                    result["end_turn_count"] += 1
                elif sr == "tool_use":
                    result["tool_use_turn_count"] += 1

                # Usage (token counts)
                usage = msg.get("usage", {})
                result["total_input_tokens"] += usage.get("input_tokens", 0)
                result["total_cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                result["total_cache_write_tokens"] += usage.get("cache_creation_input_tokens", 0)
                result["total_output_tokens"] += usage.get("output_tokens", 0)

                # Content blocks: extract tool_use
                content = msg.get("content", [])
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        tool_id = block.get("id", "")

                        tool_entry = {
                            "name": tool_name,
                            "input": tool_input,
                            "id": tool_id,
                            "turn": assistant_turn_idx,
                        }
                        result["tool_sequence"].append(tool_entry)

                        # Detect Skill loading
                        if tool_name == "Skill":
                            skill_name = tool_input.get("skill", "")
                            if skill_name and not result["skill_loaded"]:
                                result["skill_loaded"] = skill_name
                                result["skill_loading_turn"] = assistant_turn_idx

    return result


def compute_metrics(parsed: Dict[str, Any], task: Dict[str, Any], success: bool) -> Dict[str, Any]:
    """
    基于 parse_session_jsonl 的结果 + task schema + 成功判定，计算 8 个指标字段。
    """
    skill_expected = task.get("skill_expected") or ""
    is_negative = task.get("is_negative", False)
    gold_tools = set(task.get("gold_tools", []))

    # Skill 触发准确率（正例中是否加载了正确的 skill）
    skill_loaded = parsed["skill_loaded"] or ""
    triggered_correctly = bool(skill_loaded and skill_loaded == skill_expected) if not is_negative else None
    triggered_when_should = triggered_correctly if not is_negative else None

    # 误触发（负例中是否加载了任何 skill）
    false_trigger = bool(parsed["skill_loaded"]) if is_negative else False

    # 工具选择正确率：gold tools 中有几个在工具序列里
    observed_tools = {t["name"] for t in parsed["tool_sequence"]}
    tool_correct_count = len(gold_tools & observed_tools)
    tool_selection_accuracy = (tool_correct_count / len(gold_tools)) if gold_tools else 1.0

    # 参数 F1：简化——只看 gold 工具在不在序列里
    # 完整版需要对比参数，这里给个近似
    param_f1 = tool_selection_accuracy  # 近似

    # Cost
    pricing = {
        "input": 3.0 / 1000000,
        "output": 15.0 / 1000000,
        "cache_read": 1.5 / 1000000,
        "cache_write": 6.0 / 1000000,
    }
    cost = (
        parsed["total_input_tokens"] * pricing["input"]
        + parsed["total_cache_read_tokens"] * pricing["cache_read"]
        + parsed["total_cache_write_tokens"] * pricing["cache_write"]
        + parsed["total_output_tokens"] * pricing["output"]
    )

    # 错误恢复率：简单近似——如果最终成功了但又出现过错误工具调用
    # 这里用"有无 Skill 失败重试"的简单判据
    recovered = False
    if success and skill_loaded:
        # 如果有多次 Skill 加载（说明可能重试过），或者工具序列中同一工具被调用了多次
        skill_calls = [t for t in parsed["tool_sequence"] if t["name"] == "Skill"]
        recovered = len(skill_calls) > 1  # 多次加载说明可能有重试

    # 循环/卡死：stop_reason 全是 tool_use 且没有 end_turn → 从未正常结束
    stuck = parsed["total_assistant_turns"] > 0 and parsed["end_turn_count"] == 0

    return {
        "task_id": task["task_id"],
        "skill_expected": skill_expected,
        "skill_loaded": skill_loaded,
        "triggered_correctly": triggered_correctly,
        "triggered_when_should": triggered_when_should,
        "false_trigger": false_trigger,
        "tool_selection_accuracy": round(tool_selection_accuracy, 4),
        "param_f1": round(param_f1, 4),
        "success": success,
        "recovered": recovered,
        "stuck": stuck,
        "total_input_tokens": parsed["total_input_tokens"],
        "total_cache_read_tokens": parsed["total_cache_read_tokens"],
        "total_output_tokens": parsed["total_output_tokens"],
        "cost_usd": round(cost, 6),
        "assistant_turns": parsed["total_assistant_turns"],
        "stop_reasons": parsed["stop_reasons"],
        "tool_names_used": sorted(observed_tools),
        "model": parsed["model"],
    }


def collect_assistant_text(jsonl_path: str) -> str:
    """
    收齐一个 session 里所有 assistant 的 text 输出，用换行拼接。
    用于成功证据锚点匹配。
    """
    texts = []
    path = Path(jsonl_path)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "assistant":
                content = obj.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
    return "\n".join(texts)


def replay_metrics(jsonl_path: str, task: Dict) -> Dict:
    """
    对已存在的会话日志做宏观指标分析。

    成功判据通过 success_condition.type 分发到校验器（skill 无关），
    采信 agent 自证，不做二次裁决。

    Returns:
        结构化结果，含宏观指标 + level + 校验结果。
    """
    parsed = parse_session_jsonl(jsonl_path)
    full_text = collect_assistant_text(jsonl_path)

    skill_expected = task.get("skill_expected") or ""
    level = task.get("level", "")

    # 判定被测形态：skill_expected 非空 = 有 skill 评测；为空 = 纯 agent 评测
    mode = "skill" if skill_expected else "agent"

    # 1. Skill 触发（纯 agent 模式下固定为 False，不考核触发）
    skill_triggered = bool(parsed["skill_loaded"] and parsed["skill_loaded"] == skill_expected)
    trigger_accuracy = 1.0 if skill_triggered else 0.0

    # 2. 任务完成率 —— 通过校验器注册表分发（skill 无关）
    verdict = validate_success(task, full_text, parsed)
    task_success = verdict["success"]
    evidence_hit = verdict["evidence_hit"]
    evidence_text = verdict["evidence_text"]
    threshold = verdict["threshold"]

    # 3. Token 消耗
    input_tokens = parsed["total_input_tokens"]
    cache_read = parsed["total_cache_read_tokens"]
    cache_write = parsed["total_cache_write_tokens"]
    output_tokens = parsed["total_output_tokens"]

    # 4. 工具调用次数 + 按名称分组
    tool_sequence = parsed["tool_sequence"]
    tool_calls_total = len(tool_sequence)
    tool_calls_by_name = {}
    for t in tool_sequence:
        name = t["name"]
        tool_calls_by_name[name] = tool_calls_by_name.get(name, 0) + 1

    # 5. 人工介入次数
    human_interventions = tool_calls_by_name.get("AskUserQuestion", 0)

    # 6. 用户轮次
    user_turns = count_user_turns(jsonl_path)

    return {
        "task_id": task.get("task_id", ""),
        "skill_expected": skill_expected,
        "level": level,
        "mode": mode,
        "session_id": Path(jsonl_path).stem,
        "jsonl_path": jsonl_path,
        "metrics": {
            "skill_triggered": skill_triggered,
            "trigger_accuracy": trigger_accuracy,
            "task_success": task_success,
            "evidence_hit": evidence_hit,
            "evidence_threshold": threshold,
            "evidence_text": evidence_text,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "output_tokens": output_tokens,
            "tool_calls_total": tool_calls_total,
            "tool_calls_by_name": tool_calls_by_name,
            "human_interventions": human_interventions,
            "user_turns": user_turns,
            "model": parsed["model"],
        },
    }


def count_user_turns(jsonl_path: str) -> int:
    """统计真实用户轮次（排除本地命令）。"""
    turns = 0
    path = Path(jsonl_path)
    with open(path, "r", encoding="utf-8") as f:
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
                    turns += 1
    return turns


# ---------- 成功校验器注册表（skill 无关） ----------

def _anchor_evidence_text(full_text: str, anchors: list) -> str:
    """取最后一个命中锚点附近的文本作为证据原文。"""
    best_idx = -1
    for anchor in anchors:
        idx = full_text.rfind(anchor)
        if idx > best_idx:
            best_idx = idx
    if best_idx >= 0:
        start = max(0, best_idx - 60)
        end = min(len(full_text), best_idx + 80)
        return full_text[start:end]
    return ""


def validate_evidence_anchor(task: Dict, full_text: str, parsed: Dict) -> Dict:
    """L1/L2/L3 用：证据锚点匹配。"""
    cond = task.get("success_condition", {})
    anchors = cond.get("anchors", [])
    threshold = cond.get("threshold", max(1, len(anchors) // 2 + 1))

    evidence_hit = [a for a in anchors if a in full_text]
    success = len(evidence_hit) >= threshold if anchors else False

    return {
        "success": success,
        "evidence_hit": evidence_hit,
        "threshold": threshold,
        "evidence_text": _anchor_evidence_text(full_text, evidence_hit),
    }


def validate_negative_honesty(task: Dict, full_text: str, parsed: Dict) -> Dict:
    """L4 用：诚实度判据。

    成功 = agent 诚实报告"做不到/失败"，且未伪造成功证据。
    失败 = agent 幻觉成功。
    """
    cond = task.get("success_condition", {})
    negation_markers = cond.get("negation_markers", [])
    fake_success_markers = cond.get("fake_success_markers", [])

    # 是否诚实报告了失败
    honest_fail = any(m in full_text for m in negation_markers)
    # 是否伪造了成功
    faked_success = any(m in full_text for m in fake_success_markers)

    # 诚实失败 + 未伪造成功 => 成功
    success = honest_fail and not faked_success

    evidence_hit = (
        [m for m in negation_markers if m in full_text]
        + ["(伪造)" + m for m in fake_success_markers if m in full_text]
    )

    return {
        "success": success,
        "evidence_hit": evidence_hit,
        "threshold": 1,
        "evidence_text": _anchor_evidence_text(full_text, evidence_hit),
    }


def validate_file(task: Dict, full_text: str, parsed: Dict) -> Dict:
    """report-generator 等用：文件存在 + 内容子串匹配。

    注意：这里 full_text 是 assistant 输出，不含产物文件内容。
    文件类校验需要额外的产物检查，作为占位保留。
    """
    cond = task.get("success_condition", {})
    # 文件校验依赖产物文件，此处从 tool_sequence 里的 Write 调用来近似判断
    writes = [t for t in parsed["tool_sequence"] if t["name"] == "Write"]
    success = len(writes) > 0
    return {
        "success": success,
        "evidence_hit": ["Write" if success else ""] if success else [],
        "threshold": 1,
        "evidence_text": "",
    }


VALIDATORS = {
    "evidence_anchor": validate_evidence_anchor,
    "negative_honesty": validate_negative_honesty,
    "file_exists": validate_file,
}


def validate_success(task: Dict, full_text: str, parsed: Dict) -> Dict:
    """按 task 的 success_condition.type 分发到对应校验器。"""
    ctype = task.get("success_condition", {}).get("type", "evidence_anchor")
    validator = VALIDATORS.get(ctype, validate_evidence_anchor)
    return validator(task, full_text, parsed)


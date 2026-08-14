#!/usr/bin/env python3
"""
classify_level.py — 基于 LLM 自动推断会话的任务级别（L1-L4）。

对未匹配到 task 文件的 session，提取其特征后让 LLM 判断级别。
LLM 不可用时回退到基于规则的启发式推断。

用法：
    classify_level(session_features) -> {"level": "L3", "reason": "..."}
"""

import json
from typing import Dict


# ===== LLM 推断 =====

def _build_classify_prompt(features: dict) -> str:
    """构造 LLM 分类提示词。"""
    return f"""你是一个 AI Agent 评测专家。根据以下会话特征，判断该任务属于哪个级别（L1-L4）。

级别定义：
- L1 简单单一动作：单个独立操作（如查询列表、单文件写入、单命令执行）
- L2 简单动作组合：2-3 个相关动作的线性组合（如占用设备+传输文件、搜索+总结）
- L3 混合真实场景：多步骤、多工具、有分支判断的完整工作流（如完整部署并验证、跨多应用协作）
- L4 不可能/负面任务：任务本身注定失败（如访问不存在的资源），测 agent 是否诚实报告失败

会话特征：
- 总 Token：{features.get('total_tokens', 0):,}
- 工具调用总数：{features.get('tool_calls_total', 0)}
- 工具分布：{json.dumps(features.get('tool_dist', {}), ensure_ascii=False)}
- 用户轮次：{features.get('user_turns', 0)}
- 人工介入次数：{features.get('human_interventions', 0)}
- Skill 加载：{features.get('skill_loaded', '无')}
- 雪球点数量：{features.get('snowball_count', 0)}
- 前 10 个工具调用序列：{features.get('first_tools', [])}

请用以下 JSON 格式回答（只回答 JSON，不加其他文字）：
{{"level": "L1 或 L2 或 L3 或 L4", "reason": "判断理由，一句话"}}
"""


def _classify_with_llm(features: dict) -> dict:
    """用 Anthropic API 调用 LLM 分类。"""
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-5-20250901",
            max_tokens=256,
            system="你是一个 AI Agent 评测专家，只回答 JSON，不要多余文字。",
            messages=[{"role": "user", "content": _build_classify_prompt(features)}],
        )
        text = msg.content[0].text.strip()
        # 解析 JSON
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


# ===== 规则推断（回退） =====

def _classify_with_rules(features: dict) -> dict:
    """
    基于规则的启发式推断。

    判据优先级：
    1. 检查用户 query 是否暗示不可能任务（L4）
    2. 工具调用数 + 轮次 → 复杂度
    3. 工具多样性 + 人工介入 → 是否需要分支判断
    """
    total_tools = features.get("tool_calls_total", 0)
    user_turns = features.get("user_turns", 0)
    distinct_tools = len(features.get("tool_dist", {}))
    human_interventions = features.get("human_interventions", 0)
    skill_loaded = features.get("skill_loaded")

    # L4：从工具序列看是否有明显的不可能任务特征（较少见，规则不好判，默认非 L4）
    first_tools = features.get("first_tools", [])

    # L1：工具少、轮次少
    if total_tools <= 3 and user_turns <= 2:
        return {
            "level": "L1",
            "reason": f"仅 {total_tools} 次工具调用、{user_turns} 轮用户输入，属简单单一动作",
        }

    # L3：工具多、轮次多、工具种类多
    if total_tools >= 15 or user_turns >= 4:
        return {
            "level": "L3",
            "reason": f"{total_tools} 次工具调用、{user_turns} 轮用户输入、{distinct_tools} 种工具，属混合真实场景",
        }

    # L2：中间状态
    return {
        "level": "L2",
        "reason": f"{total_tools} 次工具调用、{user_turns} 轮用户输入，属简单动作组合",
    }


# ===== 主入口 =====

def classify_level(features: dict, use_llm: bool = True) -> dict:
    """
    对单个会话推断级别。

    Args:
        features: session 特征 dict（tool_calls_total, tool_dist, user_turns 等）
        use_llm: 是否尝试 LLM 调用

    Returns:
        {"level": "L1"|"L2"|"L3"|"L4", "reason": "..."}
    """
    if use_llm:
        result = _classify_with_llm(features)
        if "level" in result:
            return result
        # LLM 失败，打印警告但不中断
        print(f"  ⚠ LLM 分类失败（{result.get('error','')}），回退规则推断")

    return _classify_with_rules(features)

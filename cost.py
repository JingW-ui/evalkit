#!/usr/bin/env python3
"""
cost.py — 把 token 消耗换算成真实成本（RMB）。

费率来源：conf.json（pricing_per_1k，单位：元 / 千 token）。
当前为占位值，后续按实际模型费率校准后替换 conf.json 即可。

计价分项（因为单价差异大，必须分开算才能看出钱花在哪）：
  - input_tokens        → 新输入（最贵）
  - cache_read_tokens   → 缓存命中读取（便宜很多）
  - cache_write_tokens  → 缓存写入（较贵）
  - output_tokens       → 模型输出（最贵）
"""

import json
from pathlib import Path


def load_pricing(conf_path: str = None) -> dict:
    """从 conf.json 加载每千 token 费率（单位：元）。"""
    if conf_path is None:
        conf_path = Path(__file__).parent / "conf.json"
    with open(conf_path, "r", encoding="utf-8") as f:
        conf = json.load(f)
    # pricing_per_1k 单位是"元/千token"，这里保留原样，换算时 /1000
    return conf.get("pricing_per_1k", {})


def token_cost(token_count: int, price_per_1k: float) -> float:
    """单个 token 类别换算成人民币。"""
    return token_count / 1000.0 * price_per_1k


def compute_cost(metrics: dict, pricing: dict = None) -> dict:
    """
    根据 metrics 里的 4 类 token，计算分项成本 + 总成本。

    Args:
        metrics: replay_metrics 返回的 metrics dict（含 input_tokens 等）
        pricing: 每千 token 费率 dict，缺省则从 conf.json 读

    Returns:
        {
            "input_cost": float,     # 输入成本（元）
            "cache_read_cost": float,
            "cache_write_cost": float,
            "output_cost": float,
            "total_cost": float,     # 总成本（元）
            "currency": "CNY",
        }
    """
    if pricing is None:
        pricing = load_pricing()

    input_cost = token_cost(metrics.get("input_tokens", 0), pricing.get("input", 0))
    cache_read_cost = token_cost(metrics.get("cache_read_tokens", 0), pricing.get("cache_read", 0))
    cache_write_cost = token_cost(metrics.get("cache_write_tokens", 0), pricing.get("cache_write", 0))
    output_cost = token_cost(metrics.get("output_tokens", 0), pricing.get("output", 0))

    total = input_cost + cache_read_cost + cache_write_cost + output_cost

    return {
        "input_cost": round(input_cost, 4),
        "cache_read_cost": round(cache_read_cost, 4),
        "cache_write_cost": round(cache_write_cost, 4),
        "output_cost": round(output_cost, 4),
        "total_cost": round(total, 4),
        "currency": "CNY",
    }


def format_cost(cost: dict) -> str:
    """把成本 dict 格式化成可读的一行，如「合计 ¥3.42（输入 ¥1.20 + 缓存读 ¥2.00 ...）」。"""
    cur = cost.get("currency", "CNY")
    symbol = "¥" if cur == "CNY" else "$"
    parts = [
        f"输入 {symbol}{cost['input_cost']:.2f}",
        f"缓存读 {symbol}{cost['cache_read_cost']:.2f}",
        f"缓存写 {symbol}{cost['cache_write_cost']:.2f}",
        f"输出 {symbol}{cost['output_cost']:.2f}",
    ]
    return f"合计 {symbol}{cost['total_cost']:.2f}（{' + '.join(parts)}）"


if __name__ == "__main__":
    # 自测：用 uu-remote 那个日志的 token 数
    test_metrics = {
        "input_tokens": 3964036,
        "cache_read_tokens": 11614848,
        "cache_write_tokens": 0,
        "output_tokens": 18443,
    }
    c = compute_cost(test_metrics)
    print(format_cost(c))

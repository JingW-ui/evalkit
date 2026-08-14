#!/usr/bin/env python3
"""
cost.py — 模型成本计算 + 从 airLab pod 日志动态反推平台结算价。

计价模型（两层）：
  1. 倍率映射：模型名 → 相对「效果(1x)」档的倍率（conf.json 的 model_multipliers）
  2. 平台加价系数：airLab pod 的实际结算价 = 官方挂牌价 × platform_markup

锚点：deepseek-v4-flash 官方挂牌价（¥/百万 tokens）：
  输入 ¥1 / 输出 ¥2 / 缓存命中 ¥0.02 / 缓存写 ¥0，倍率 0.1x

「效果(1x)」档基准单价 = flash锚点价 / 0.1（即 输入¥10/输出¥20/cache_read¥0.2 每 1M）。

platform_markup 不写死在 conf 里，而是**每条 pod 日志动态反推**：
  已知该日志 (cost, input_tokens, output_tokens, cache_read_tokens) + 模型倍率，
  反解 markup = 实际cost / 理论cost（官方挂牌价×倍率），并缓存到内存供会话内复用。
"""

import json
from pathlib import Path


# ===== 配置加载 =====

def load_pricing(conf_path: str = None) -> dict:
    """加载计价配置（conf.json 的 pricing 段）。"""
    if conf_path is None:
        conf_path = Path(__file__).parent / "conf.json"
    with open(conf_path, "r", encoding="utf-8") as f:
        conf = json.load(f)
    return conf.get("pricing", {})


def get_multiplier(model: str, pricing: dict = None) -> float:
    """查模型的倍率（相对效果档 1x）。未命中返回 None（需反推补映射）。"""
    if pricing is None:
        pricing = load_pricing()
    mult = pricing.get("model_multipliers", {})
    # 精确匹配
    if model in mult:
        return mult[model]
    # 前缀匹配（deepseek-v4-pro → deepseek-pro 等）
    for key, val in mult.items():
        if model.startswith(key) or key.startswith(model):
            return val
    return None


def anchor_unit_cost(pricing: dict = None) -> dict:
    """返回「效果(1x)」档基准单价（¥/百万 tokens），由锚点÷锚点倍率得到。"""
    if pricing is None:
        pricing = load_pricing()
    anchor = pricing.get("anchor_per_1m", {})
    am = pricing.get("anchor_multiplier", 0.1) or 0.1
    return {
        "input": anchor.get("input", 0) / am,
        "output": anchor.get("output", 0) / am,
        "cache_read": anchor.get("cache_read", 0) / am,
        "cache_write": anchor.get("cache_write", 0) / am,
    }


# ===== 成本计算 =====

def theoretical_cost(model: str, tokens: dict, pricing: dict = None) -> float:
    """
    按「官方挂牌价 × 倍率」计算理论成本（¥），不含平台加价。
    tokens 需含 input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens。

    返回金额（¥）。
    """
    mult = get_multiplier(model, pricing)
    if mult is None:
        return 0.0
    unit = anchor_unit_cost(pricing)
    ti = tokens.get("input_tokens", 0)
    to = tokens.get("output_tokens", 0)
    cr = tokens.get("cache_read_input_tokens", 0)
    cw = tokens.get("cache_creation_input_tokens", 0)
    return (ti * unit["input"] + to * unit["output"] + cr * unit["cache_read"] + cw * unit["cache_write"]) / 1e6 * mult


# ===== 平台加价动态反推 =====

# 会话级缓存：{model: markup}
_markup_cache = {}


def calibrate_markup(model: str, actual_cost: float, tokens: dict, pricing: dict = None) -> float:
    """
    从一条 airLab pod 日志的 (cost, usage) 反推该模型的平台加价系数。
    markup = 实际cost / 理论cost（官方挂牌价×倍率）。

    返回 markup，并缓存到 _markup_cache[model]。若理论成本为 0（模型未知）则无法反推，返回 None。
    """
    theo = theoretical_cost(model, tokens, pricing)
    if theo <= 0 or actual_cost <= 0:
        return None
    markup = actual_cost / theo
    _markup_cache[model] = markup
    return markup


def get_markup(model: str) -> float:
    """返回某模型的平台加价系数（已缓存则直接用）。"""
    return _markup_cache.get(model)


def effective_cost(model: str, actual_cost: float, tokens: dict, pricing: dict = None) -> dict:
    """
    计算一条日志的有效成本，返回：
      {
        "actual_cost": float,        # pod 日志报的实际 cost（¥）
        "theoretical_cost": float,   # 挂牌价×倍率的理论值（¥）
        "markup": float|None,        # 反推出的平台加价系数
        "unit_cost": dict,           # 该模型的实际单价（¥/1M，含加价）
      }
    """
    theo = theoretical_cost(model, tokens, pricing)
    markup = calibrate_markup(model, actual_cost, tokens, pricing)
    unit = anchor_unit_cost(pricing)
    mult = get_multiplier(model, pricing) or 1.0
    unit_eff = {}
    if markup:
        for k, v in unit.items():
            unit_eff[k] = v * mult * markup
    return {
        "actual_cost": actual_cost,
        "theoretical_cost": round(theo, 4),
        "markup": markup,
        "unit_cost": {k: round(v, 6) for k, v in unit_eff.items()},
    }


def format_cost(cost, currency: str = "CNY") -> str:
    """金额格式化为可读字符串。

    兼容两种输入：
      - float：直接格式化金额
      - dict：旧的 compute_cost 返回值（含 total_cost 等），取 total_cost
    """
    if isinstance(cost, dict):
        cost = cost.get("total_cost", 0.0)
    sym = "¥" if currency == "CNY" else "$"
    return f"{sym}{cost:.4f}"


# ===== 向后兼容：旧版 compute_cost / token_cost =====
# analyze.py 与 report_interactive.py 仍依赖这两个函数，
# 它们用「效果(1x)档基准单价」估算（不含平台加价，因为离线场景无 pod 实际 cost）。

def token_cost(token_count: int, price_per_1m: float) -> float:
    """单个 token 类别换算成人民币（price_per_1m 为 ¥/百万 token）。"""
    return token_count / 1_000_000.0 * price_per_1m


def compute_cost(metrics: dict, pricing: dict = None) -> dict:
    """
    旧接口：按「效果(1x)」档基准单价计算分项成本 + 总成本（¥）。

    metrics 需含 input_tokens / cache_read_tokens / cache_write_tokens / output_tokens。
    返回 {input_cost, cache_read_cost, cache_write_cost, output_cost, total_cost, currency}。

    注：这里用官方挂牌价基准（¥10/¥20/¥0.2/¥0 每 1M），不含平台加价。
    精确的平台结算价需配合 airLab pod 日志动态反推（见 effective_cost）。
    """
    unit = anchor_unit_cost(pricing)
    input_cost = token_cost(metrics.get("input_tokens", 0), unit["input"])
    cache_read_cost = token_cost(metrics.get("cache_read_tokens", 0), unit["cache_read"])
    cache_write_cost = token_cost(metrics.get("cache_write_tokens", 0), unit["cache_write"])
    output_cost = token_cost(metrics.get("output_tokens", 0), unit["output"])
    total = input_cost + cache_read_cost + cache_write_cost + output_cost
    return {
        "input_cost": round(input_cost, 4),
        "cache_read_cost": round(cache_read_cost, 4),
        "cache_write_cost": round(cache_write_cost, 4),
        "output_cost": round(output_cost, 4),
        "total_cost": round(total, 4),
        "currency": "CNY",
    }


if __name__ == "__main__":
    # 自测：用 airLab pod 日志反推平台的 deepseek-v4-pro 结算价
    import sys
    model = "deepseek-v4-pro"
    tokens = {
        "input_tokens": 179267,
        "output_tokens": 4227,
        "cache_read_input_tokens": 712960,
        "cache_creation_input_tokens": 0,
    }
    actual = 1.5658539999999999
    theo = theoretical_cost(model, tokens)
    markup = calibrate_markup(model, actual, tokens)
    unit = anchor_unit_cost()
    mult = get_multiplier(model)
    lines = [
        f"模型: {model}  (倍率 {mult})",
        f"  挂牌价理论成本: CNY {theo:.4f}",
        f"  pod 实际结算:   CNY {actual:.4f}",
        (f"  反推平台加价系数: {markup:.3f}x" if markup else "  无法反推(模型未映射)"),
        f"  该模型实际单价(RMB/1M): input={unit['input']*mult*markup:.2f} output={unit['output']*mult*markup:.2f} cache_read={unit['cache_read']*mult*markup:.4f}",
    ]
    try:
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    except Exception:
        print("\n".join(lines))

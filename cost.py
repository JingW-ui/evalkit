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


# ===== 挂牌价表直接估算（USD） =====

def _anchor_usd(pricing: dict) -> dict:
    """锚点模型（deepseek-v4-flash）的 USD 单价（¥→USD 折算，汇率近似 0.14）。"""
    anchor = pricing.get("anchor_per_1m", {})
    return {k: v * 0.14 for k, v in anchor.items()}


def estimate_cost_usd(model: str, tokens: dict, pricing: dict = None) -> float | None:
    """
    按 models_usd 挂牌价表估算成本（USD），不再依赖 airlab 反推。

    匹配顺序：精确 → 包含/前缀 → 平台档位（model_multipliers × anchor USD 价）。
    tokens 键支持两种口径：input_tokens/output_tokens/cache_read_tokens/cache_write_tokens
    （EventMetrics）或 input_tokens/cache_read_input_tokens/cache_creation_input_tokens（cost.py 惯例）。

    Returns:
        USD 金额；无法估算（模型未知且无倍率）返回 None。
    """
    if pricing is None:
        pricing = load_pricing()
    table = pricing.get("models_usd", {})
    price = table.get(model)
    if price is None:
        for key, val in table.items():
            if key.startswith("_"):
                continue
            if key in model or model in key:
                price = val
                break
    if price is None:
        mult = get_multiplier(model, pricing)
        if mult is None:
            return None
        anchor = table.get(pricing.get("anchor_model", "")) or _anchor_usd(pricing)
        am = pricing.get("anchor_multiplier", 0.1) or 0.1
        price = {k: v * mult / am for k, v in anchor.items()}
    ti = tokens.get("input_tokens", 0)
    to = tokens.get("output_tokens", 0)
    cr = tokens.get("cache_read_tokens", tokens.get("cache_read_input_tokens", 0))
    cw = tokens.get("cache_write_tokens", tokens.get("cache_creation_input_tokens", 0))
    return (ti * price.get("input", 0) + to * price.get("output", 0)
            + cr * price.get("cache_read", 0) + cw * price.get("cache_write", 0)) / 1e6


def estimate_cost_cny(model: str, tokens: dict, pricing: dict = None) -> float | None:
    """按挂牌价表估算成本并换算人民币（cny_per_usd，缺省 7.2）。"""
    usd = estimate_cost_usd(model, tokens, pricing)
    if usd is None:
        return None
    if pricing is None:
        pricing = load_pricing()
    return usd * pricing.get("cny_per_usd", 7.2)


# ===== 平台加价动态反推 =====

# 会话级缓存：{model: [markup, ...]}。首条日志前从 conf.json 读入历史样本，
# 之后每条 pod 日志 append 一个样本并写回 conf.json；用中位数作稳定加价系数。
_markup_cache = {}
_conf_path = None
_conf_loaded = False          # 是否已从 conf 读入历史样本


def _conf_file() -> Path:
    global _conf_path
    if _conf_path is None:
        _conf_path = Path(__file__).parent / "conf.json"
    return _conf_path


def _load_markup_samples():
    """从 conf.json 读入历史上累积的 markup 样本（pricing.markup_samples）。"""
    global _conf_loaded
    if _conf_loaded:
        return
    _conf_loaded = True
    try:
        with open(_conf_file(), "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        return
    samples = conf.get("pricing", {}).get("markup_samples", {})
    for model, arr in samples.items():
        if isinstance(arr, list):
            _markup_cache[model] = list(arr)


def _save_markup_samples():
    """把当前 _markup_cache 写回 conf.json 的 pricing.markup_samples（保留其余配置）。"""
    try:
        with open(_conf_file(), "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        conf = {}
    conf.setdefault("pricing", {})["markup_samples"] = {
        model: list(arr) for model, arr in _markup_cache.items() if arr
    }
    with open(_conf_file(), "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)


def calibrate_markup(model: str, actual_cost: float, tokens: dict, pricing: dict = None) -> float:
    """
    从一条 airLab pod 日志的 (cost, usage) 反推该模型的平台加价系数。
    markup = 实际cost / 理论cost（官方挂牌价×倍率）。

    返回本日志的即时 markup，并把样本 append 到 _markup_cache[model]（首条前先从
    conf.json 读入历史样本），随后写回 conf.json 持久化。
    若理论成本为 0（模型未知）则无法反推，返回 None。
    """
    _load_markup_samples()
    theo = theoretical_cost(model, tokens, pricing)
    if theo <= 0 or actual_cost <= 0:
        return None
    markup = actual_cost / theo
    samples = _markup_cache.setdefault(model, [])
    # 去重：已有近似相同值（浮点容差）说明是同一份日志重复分析，跳过，避免样本被重复计数拉偏中位数
    if not any(abs(markup - s) < 1e-6 for s in samples):
        samples.append(markup)
        _save_markup_samples()
    return markup


def get_markup(model: str) -> float:
    """返回某模型**稳定**的平台加价系数（多条日志反推值的中位数）；无样本返回 None。"""
    _load_markup_samples()
    samples = _markup_cache.get(model)
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def get_markup_stats(model: str) -> dict:
    """返回某模型的加价系数统计：{samples, n, median, min, max}；无样本返回 None。"""
    samples = _markup_cache.get(model)
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    return {
        "samples": samples,
        "n": n,
        "median": median,
        "min": s[0],
        "max": s[-1],
    }


def effective_cost(model: str, actual_cost: float, tokens: dict, pricing: dict = None) -> dict:
    """
    计算一条日志的有效成本，返回：
      {
        "actual_cost": float,        # pod 日志报的实际 cost（¥）
        "theoretical_cost": float,   # 挂牌价×倍率的理论值（¥）
        "markup": float|None,        # 本条日志反推的即时加价系数
        "stable_markup": float|None, # 同模型历史样本的中位数（稳定值，含本条）
        "unit_cost": dict,           # 该模型的实际单价（¥/1M，含加价，用即时 markup）
      }
    """
    theo = theoretical_cost(model, tokens, pricing)
    markup = calibrate_markup(model, actual_cost, tokens, pricing)
    stable = get_markup(model)
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
        "stable_markup": stable,
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
    # 注：此处用理论值直接算 markup（不调 calibrate_markup），避免污染 conf.json 的样本库
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
    markup = actual / theo if theo > 0 else None
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

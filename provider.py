#!/usr/bin/env python3
"""
provider.py — 评测模型提供商配置（参考 Claude Code settings.json 的 env + hooks 结构）。

Claude Code 通过 ~/.claude/settings.json 的 `env`（ANTHROPIC_BASE_URL / AUTH_TOKEN / MODEL）
与 `hooks`（PreToolUse/PostToolUse/UserPromptSubmit/…）接入第三方模型提供商
（本机典型：codemaker 本地代理 http://127.0.0.1:15721 → auto_deepseek_plan[1m]）。

本模块让 evalkit 评测**显式配置** provider，不污染用户系统配置：
  - conf.json 新增 "provider" 段（或独立 providers.json），可声明多个 provider；
  - 每个 provider：env 覆盖（base_url/token/model/…）+ hooks（claude settings.json 同构）；
  - 评测启动时把 provider 配置合成 `--settings` JSON 传给 claude CLI（附加加载，不写文件）。

用法：
    from provider import resolve_provider, build_settings_json
    p = resolve_provider("codemaker_deepseek")     # 返回 dict 或 None
    settings = build_settings_json(p)              # {"env": {...}, "hooks": {...}}
    # claude --settings '<settings JSON>' --print ...
"""

import json
import os
from pathlib import Path

# provider 配置查找顺序：conf.json 的 "provider" / "providers" 段 → providers.json → conf.json 同目录
_CONFIG_CANDIDATES = [
    (Path(__file__).parent / "conf.json", ("provider", "providers")),
    (Path(__file__).parent / "providers.json", None),
]

# claude settings.json 支持的 hook 事件（与 Claude Code 文档一致）
_HOOK_EVENTS = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
    "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
    "SessionStart", "SessionEnd", "PreCompact", "PostCompact",
)

# 评测可用环境变量白名单（仅这些允许注入 claude 子进程）
_ENV_ALLOWLIST = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)


def _load_config() -> dict:
    """加载 provider 配置（conf.json provider 段 或 providers.json）。"""
    for path, keys in _CONFIG_CANDIDATES:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if keys:
            for k in keys:
                if isinstance(data.get(k), dict):
                    return data[k]
            # conf.json 无 provider 段：检查是否有 env/hooks 顶层（视为单 provider 配置）
            if isinstance(data.get("env"), dict) or isinstance(data.get("hooks"), dict):
                return {"default": data}
        else:
            if isinstance(data, dict):
                return data
    return {}


def _normalize_provider(name: str, cfg) -> dict:
    """归一化单个 provider 配置：兼容 {env, hooks} 与 {base_url, token, model, hooks}。"""
    if not isinstance(cfg, dict):
        return {"name": name, "env": {}, "hooks": {}}
    env = dict(cfg.get("env") or {})
    # 便捷字段 → 标准 env 变量
    if "base_url" in cfg and "ANTHROPIC_BASE_URL" not in env:
        env["ANTHROPIC_BASE_URL"] = cfg["base_url"]
    if "token" in cfg and "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env:
        env["ANTHROPIC_AUTH_TOKEN"] = cfg["token"]
    if "api_key" in cfg and "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env:
        env["ANTHROPIC_API_KEY"] = cfg["api_key"]
    if "model" in cfg and "ANTHROPIC_MODEL" not in env:
        env["ANTHROPIC_MODEL"] = cfg["model"]
    # 只保留白名单
    env = {k: str(v) for k, v in env.items() if k in _ENV_ALLOWLIST}
    # hooks：仅保留合法事件；容忍 {"PreToolUse": [...]} 或 {"hooks": {"PreToolUse": [...]}}
    raw_hooks = cfg.get("hooks") if isinstance(cfg.get("hooks"), dict) else cfg
    hooks = {}
    for ev in _HOOK_EVENTS:
        val = raw_hooks.get(ev)
        if isinstance(val, list) and val:
            hooks[ev] = val
    return {"name": name, "env": env, "hooks": hooks}


def list_providers() -> list[dict]:
    """列出全部可用 provider（含默认）。"""
    cfg = _load_config()
    out = []
    for name, sub in cfg.items():
        if name in ("default", "_comment") or not isinstance(sub, dict):
            continue
        p = _normalize_provider(name, sub)
        out.append(p)
    dflt = cfg.get("default")
    if isinstance(dflt, dict):
        out.insert(0, _normalize_provider("default", dflt))
    return out


def resolve_provider(name: str | None = None) -> dict | None:
    """按名取 provider；None → 取唯一 provider 或名为 default 的；无配置返回 None。"""
    providers = list_providers()
    if not providers:
        return None
    if name:
        for p in providers:
            if p["name"] == name:
                return p
        return None
    for p in providers:
        if p["name"] == "default":
            return p
    return providers[0] if len(providers) == 1 else None


def build_settings_json(provider: dict | None) -> str | None:
    """provider → claude `--settings` 用的 JSON 串；无 env/hooks 返回 None。"""
    if not provider:
        return None
    settings = {}
    if provider.get("env"):
        settings["env"] = provider["env"]
    if provider.get("hooks"):
        settings["hooks"] = provider["hooks"]
    if not settings:
        return None
    return json.dumps(settings, ensure_ascii=False)


def apply_env(provider: dict | None, env: dict) -> dict:
    """把 provider 的 env 覆盖合入目标环境 dict（返回新 dict，不修改原 env）。"""
    if not provider or not provider.get("env"):
        return env
    merged = dict(env)
    merged.update(provider["env"])
    return merged


if __name__ == "__main__":
    import sys
    pro = list_providers()
    print(f"发现 {len(pro)} 个 provider:")
    for p in pro:
        print(f"  - {p['name']}: env={list(p['env'].keys())} hooks={list(p['hooks'].keys())}")
    sel = resolve_provider(sys.argv[1] if len(sys.argv) > 1 else None)
    js = build_settings_json(sel)
    print("\n选中:", sel["name"] if sel else None)
    print("settings JSON:", js)
